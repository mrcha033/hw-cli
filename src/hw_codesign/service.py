from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import zipfile
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .artifacts import deterministic_zip, sha256, simple_pdf, write_manifest
from .backends.atopile import AtopileBackend
from .backends.command import resolve_tool
from .backends.electronics import CONTRACT_STAGES
from .backends.freerouting import FreeroutingBackend
from .backends.kicad import KiCadBackend
from .backends.mechanical import OpenCascadeMechanicalBackend
from .backends.python_netlist import PythonNetlistBackend
from .backends.tscircuit import TSCircuitBackend
from .backends.zephyr import ZephyrBackend
from .generators import firmware_profile, generate_bom, generate_electronics, generate_firmware, generate_mechanical
from .io import atomic_write_text, read_yaml, write_json, write_yaml
from .models import Failure, FailureCategory, GateReport, RepairPatch, Status
from .placement import check_layout_signal_integrity, check_layout_thermal_integrity, check_placement, propose_placement
from .policy import ChangePolicy
from .provenance import artifact_provenance
from .reference_backend import build_firmware_reference, export_fabrication, internal_drc, internal_erc
from .resolver import SUPPLIER_EVIDENCE_MAX_AGE_DAYS, _evidence_is_stale, role_for_component
from .resources import resource_root
from .supplier_adapters import supplier_adapter
from .validation import Validator, persist_report
from .workspace import Workspace

_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_POSIX_ABSOLUTE_PATH = re.compile(r"^/(?:[^/\s]+/)*[^/\s]*$")
_RELEASE_ELIGIBLE_BACKENDS = frozenset({"tscircuit", "kicad", "python_netlist", "atopile"})
_FABRICATION_RELEASE_BACKENDS = frozenset({"tscircuit", "kicad"})
_RELEASE_TIER_BY_BACKEND: dict[str, str] = {
    "reference": "candidate",
    "python_netlist": "netlist",
    "atopile": "hdl_source",
    "tscircuit": "fabrication",
    "kicad": "fabrication",
}
# Gates that require native toolchain (ERC/DRC/autoroute/Zephyr). When include_external=False
# these are BLOCKED-by-design; the benchmark excludes them from software_gate_pass_rate so CI
# can track software-gate convergence without native tools.
_EXTERNAL_GATES = frozenset({
    "autoroute", "native_erc", "native_drc",
    "kicad_library_crosscheck", "native_mechanical_validation", "native_zephyr_build",
})
# Extended set of structurally-BLOCKED gates that do not indicate a design defect:
# external tool gates + physical-evidence gate (requires bench testing, not software-fixable) +
# compiled_electronics_backend for the reference backend (candidate-only by design).
# design_until_release emits "software_gates_ready" when only these are non-passing.
_STRUCTURAL_BLOCKED_GATES = _EXTERNAL_GATES | frozenset({
    "physical_qualification",
    "compiled_electronics_backend",
})

# Held-out specs for the design benchmark (one-line intent → design_until_release).
# Each entry must have a matching template in SUPPORTED_TEMPLATES.
_BENCHMARK_SPECS: list[dict[str, str]] = [
    {
        "id": "sensor_logger_usb",
        "template": "sensor_data_logger",
        "intent": "ESP32-S3 data logger with USB-C power and ICM-42688-P 6-axis IMU",
    },
    {
        "id": "ble_lipo_sensor",
        "template": "ble_sensor_node",
        "intent": "nRF52840 BLE sensor node with LiPo charging SHT31 temperature humidity 50x35 mm",
    },
    {
        "id": "robotics_controller",
        "template": "robotics_controller_full",
        "intent": "STM32H7 motor controller 12 channels 24V CAN bus emergency stop 4-layer",
    },
    {
        "id": "rp2040_usb_hid",
        "template": "rp2040_usb_device",
        "intent": "RP2040 USB HID device with 2 MB QSPI flash and USB-C connector",
    },
]


def _module_summary(m: dict[str, Any]) -> str:
    behavior = m.get("behavior", "unknown")
    if behavior == "timeout_shutdown":
        trigger = m.get("trigger", {})
        return f"timeout_shutdown: signal={trigger.get('signal','?')}, timeout={trigger.get('timeout_ms','?')}ms"
    if behavior == "periodic_transmit":
        frame = m.get("frame", {})
        transport = m.get("transport", "can").upper()
        return f"periodic_transmit: {transport} id={frame.get('id','?')} dlc={frame.get('dlc','?')} every {m.get('interval_ms','?')}ms"
    if behavior == "state_machine":
        states = m.get("states", [])
        return f"state_machine: {len(states)} states, {len(m.get('transitions', []))} transitions"
    if behavior == "sensor_poll":
        return f"sensor_poll: {m.get('sensor','?')} on {m.get('bus','i2c').upper()} every {m.get('poll_interval_ms', 100)}ms"
    if behavior == "interface_stack":
        return f"interface_stack: {m.get('stack','?')} via {m.get('library','project')}"
    return behavior


def _derive_agent_actions(actions: list[dict[str, Any]], spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Map repair-plan actions to specific agent tool calls."""
    agent_actions: list[dict[str, Any]] = []
    for action in actions:
        code = action["failure_code"]
        _gate = action.get("gate", "")
        if code == "missing_required_block":
            msg = action["action"]
            # Extract category from message like "Missing schematic block: can"
            category = msg.split(":")[-1].strip() if ":" in msg else ""
            if category:
                agent_actions.append({
                    "tool": "hw_propose_circuit_block",
                    "args": {"category": category},
                    "reason": f"'{category}' block is missing from schematic",
                })
        elif code == "single_pin_net":
            agent_actions.append({
                "tool": "hw_add_circuit_block",
                "args": {},
                "reason": "A net has only one connection — add the missing component or correct the net name",
            })
        elif code in {"placement_ref_not_in_bom", "placement_target_not_in_bom"}:
            agent_actions.append({
                "tool": "hw_set_placement_constraint",
                "args": {},
                "reason": action["action"],
            })
        elif code in {"unresolved_signal_reference", "firmware_signal_not_in_pinmap"}:
            agent_actions.append({
                "tool": "hw_design_firmware_module",
                "args": {},
                "reason": action["action"],
            })
        elif code == "missing_estop_shutdown_behavior":
            suggested = action.get("details", {}).get("suggested_module") or {
                "id": "motor_estop_watchdog",
                "behavior": "timeout_shutdown",
                "trigger": {"signal": "ESTOP_IN", "timeout_ms": 100},
                "action": {"disable_all": "motor_enables", "assert": "FAULT_LED"},
            }
            agent_actions.append({
                "tool": "hw_design_firmware_module",
                "args": suggested,
                "reason": "Add the required e-stop motor shutdown behavior",
            })
        elif action.get("patches"):
            agent_actions.append({
                "tool": "hw_apply_repair_plan",
                "args": {"patches": action["patches"]},
                "reason": action["action"],
            })
    return agent_actions


def _assumptions_as_dict(assumptions: Any) -> dict[str, Any]:
    """Return assumptions as a dict, handling both dict and list formats.

    Some templates (e.g. rp2040_usb_device) store assumptions as a list of
    {id, text, rationale} items rather than the dict format used elsewhere.
    List-format assumptions never carry critical/requires_user_review flags,
    so returning an empty dict is always safe.
    """
    return assumptions if isinstance(assumptions, dict) else {}


def _cadquery_available() -> bool:
    try:
        import cadquery  # noqa: F401
        return True
    except ImportError:
        return False


def _portable_review_value(value: Any, workspace_root: Path) -> Any:
    """Return review-bundle data without machine-local absolute paths."""
    if isinstance(value, dict):
        return {key: _portable_review_value(item, workspace_root) for key, item in value.items()}
    if isinstance(value, list):
        return [_portable_review_value(item, workspace_root) for item in value]
    if isinstance(value, str):
        return _portable_review_string(value, workspace_root)
    return value


def _portable_review_string(value: str, workspace_root: Path) -> str:
    root = workspace_root.resolve()
    root_text = root.as_posix()
    norm = value.replace("\\", "/")
    if norm == root_text:
        return "."
    if norm.startswith(f"{root_text}/"):
        return norm[len(root_text) + 1:]
    if root_text in norm:
        return norm.replace(root_text, ".")
    if _WINDOWS_ABSOLUTE_PATH.match(value):
        return f"<host-path>/{norm.split('/')[-1]}"
    if _POSIX_ABSOLUTE_PATH.match(value):
        return f"<host-path>/{Path(value).name}"
    return value


class HardwareService:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        resources = resource_root(self.root)
        toolchain_root = Path(os.environ.get("HW_TOOLCHAIN_ROOT", resources)).resolve()
        self.parts_root = resources / "parts"
        self.workspace = Workspace(self.root)
        self.validator = Validator(resources / "schemas")
        self.kicad = KiCadBackend(resources)
        self.freerouting = FreeroutingBackend(toolchain_root)
        self.mechanical = OpenCascadeMechanicalBackend()
        self.zephyr = ZephyrBackend()
        self.tscircuit = TSCircuitBackend(toolchain_root, parts_root=self.parts_root)
        self.python_netlist = PythonNetlistBackend(resources)
        self.atopile = AtopileBackend(resources)

    def create_project(self, name: str, template: str = "robotics_controller_full") -> dict[str, Any]:
        result = self.workspace.create_project(name, template)
        result["status"] = "generated"
        return result

    def read_spec(self, project: str) -> dict[str, Any]:
        return self.workspace.read_spec(project)

    def update_spec(self, project: str, section: str, value: dict[str, Any], user_approved: bool = False) -> dict[str, Any]:
        if section == "safety" and not user_approved:
            ChangePolicy().check_spec_paths(["safety.requirements"])
        if section == "manufacturing" and "limits" in value and not user_approved:
            ChangePolicy().check_spec_paths(["manufacturing.limits.change"])
        changed = self.workspace.update_spec(project, section, value)
        return {"status": "generated", "changed_files": [changed], "user_approved": user_approved}

    def update_requirements(self, project: str, requirements_text: str) -> dict[str, Any]:
        """Deterministically lower common natural-language requirements into the typed spec."""
        system_path = self.workspace.require_project(project) / "spec" / "system.yaml"
        firmware_path = self.workspace.require_project(project) / "spec" / "firmware.yaml"
        manufacturing_path = self.workspace.require_project(project) / "spec" / "manufacturing.yaml"
        req_path = self.workspace.require_project(project) / "spec" / "requirements.yaml"
        system_file = read_yaml(system_path)
        firmware_file = read_yaml(firmware_path)
        manufacturing_file = read_yaml(manufacturing_path)
        changed: list[str] = []
        lowered_fields: list[dict[str, Any]] = []

        def _source_range(match: re.Match[str]) -> dict[str, int]:
            return {"start": match.start(), "end": match.end()}

        def _literal_range(token: str) -> dict[str, int] | None:
            start = requirements_text.lower().find(token.lower())
            return {"start": start, "end": start + len(token)} if start >= 0 else None

        _PATTERNS: list[tuple[str, list[str], Any, str]] = [
            (r"(\d+)\s*(?:채널|channel)", ["actuation", "motor_channels"], int, "integer"),
            (r"(?:각\s*채널\s*)?(?:피크\s*)?(\d+(?:\.\d+)?)\s*A", ["actuation", "motor_channel_peak_current_a"], float, "number"),
            (r"(\d+(?:\.\d+)?)\s*V\s*(?:배터리|battery)", ["system", "supply", "battery", "pack_voltage_nominal"], float, "number"),
            (r"(STM32H7\w*|ESP32S3|RP2040)", ["compute", "mcu", "family"], str.upper, "string"),
            (r"(\d+)\s*[- ]?layer", ["manufacturing", "pcb", "layers"], int, "integer"),
        ]
        _ROOTS = {
            "actuation": system_file, "system": system_file, "compute": system_file,
            "manufacturing": manufacturing_file,
        }
        _AFFECTED_GATES_BY_ROOT = {
            "actuation": ["electrical_semantics", "power_budget", "firmware_pinmap", "thermal_integrity"],
            "system": ["electrical_semantics", "power_tree_integrity", "mechanical_fit"],
            "compute": ["pin_symbol_footprint", "firmware_pinmap", "native_zephyr_build"],
            "manufacturing": ["layout_signal_integrity", "native_drc", "manufacturing_export"],
            "firmware": ["firmware_interface_contract", "firmware_pinmap", "native_zephyr_build"],
            "sensing": ["electrical_semantics", "firmware_pinmap", "firmware_interface_contract"],
        }
        for pattern, keys, convert, field_type in _PATTERNS:
            match = re.search(pattern, requirements_text, flags=re.IGNORECASE)
            if match:
                target = _ROOTS[keys[0]]
                for key in keys[:-1]:
                    target = target[key]
                value = convert(match.group(1))
                target[keys[-1]] = value
                spec_path = ".".join(keys)
                changed.append(spec_path)
                lowered_fields.append({
                    "spec_path": spec_path,
                    "value": value,
                    "field_type": field_type,
                    "source_span": match.group(0),
                    "source_range": _source_range(match),
                    "affected_gates": _AFFECTED_GATES_BY_ROOT.get(keys[0], []),
                    "status": "lowered",
                })

        lowered_text = requirements_text.lower()
        if "zephyr" in lowered_text:
            firmware_file["firmware"]["framework"] = "zephyr"
            changed.append("firmware.framework")
            lowered_fields.append({
                "spec_path": "firmware.framework",
                "value": "zephyr",
                "field_type": "string",
                "source_span": "zephyr",
                "source_range": _literal_range("zephyr"),
                "affected_gates": _AFFECTED_GATES_BY_ROOT["firmware"],
                "status": "lowered",
            })
        features = {"imu": "imu", "emergency stop": "e_stop", "e-stop": "e_stop", "비상 정지": "e_stop"}
        for token, key in features.items():
            if token in lowered_text:
                system_file["sensing"][key] = "required"
                spec_path = f"sensing.{key}"
                changed.append(spec_path)
                lowered_fields.append({
                    "spec_path": spec_path,
                    "value": "required",
                    "field_type": "enum",
                    "source_span": token,
                    "source_range": _literal_range(token),
                    "affected_gates": _AFFECTED_GATES_BY_ROOT["sensing"],
                    "status": "lowered",
                })
        write_yaml(system_path, system_file)
        write_yaml(firmware_path, firmware_file)
        write_yaml(manufacturing_path, manufacturing_file)
        unresolved_ambiguous = []
        if not re.search(r"(?:external|onboard|외장|온보드).*(?:driver|드라이버)", requirements_text, re.IGNORECASE):
            unresolved_ambiguous.append("motor driver topology retained from documented assumption")
        if not re.search(r"(?:forced|passive|팬|자연 냉각)", requirements_text, re.IGNORECASE):
            unresolved_ambiguous.append("cooling condition retained from documented assumption")
        _unsupported: list[tuple[str, str, str]] = [
            (r"\bIP\s*6[0-9]\b", "ip_protection", "IP ingress protection rating (e.g. IP67) — not lowered into spec"),
            (r"\bCAN-?FD\b", "bus_protocol", "CAN-FD bus variant — not lowered into spec"),
            (r"\bASIL\b", "functional_safety", "ASIL functional-safety level — not lowered into spec"),
            (r"\b\d+(?:\.\d+)?\s*A\s+continuous\b", "current_rating", "continuous current rating — not lowered into spec"),
            (r"\bJLCPCB\b", "manufacturing_service", "JLCPCB assembly service — not lowered into spec"),
            (r"\bimpedance[\s-]controlled\b", "pcb_stackup", "impedance-controlled PCB stackup — not lowered into spec"),
        ]
        _reasons: dict[str, str] = {
            "ip_protection": "IP ingress protection not modeled as a typed spec field",
            "bus_protocol": "CAN-FD requested but firmware/electrical constraints only model classical CAN",
            "functional_safety": "ASIL functional-safety level not modeled in typed spec",
            "current_rating": "Continuous current not modeled separately from peak current in typed spec",
            "manufacturing_service": "JLCPCB assembly service selection not modeled in typed spec",
            "pcb_stackup": "Impedance-controlled stackup not modeled in typed spec",
        }
        _affected_gates_by_category: dict[str, list[str]] = {
            "ip_protection": ["mechanical_fit", "physical_qualification"],
            "bus_protocol": ["firmware_interface_contract", "firmware_pinmap", "native_zephyr_build"],
            "functional_safety": ["safety_requirements", "firmware_interface_contract", "physical_qualification"],
            "current_rating": ["electrical_semantics", "power_budget", "thermal_integrity", "physical_qualification"],
            "manufacturing_service": ["component_provenance", "manufacturing_export", "artifact_integrity"],
            "pcb_stackup": ["layout_signal_integrity", "native_drc", "manufacturing_export"],
        }
        matched = [
            {"category": cat, "label": label, "match": match}
            for pat, cat, label in _unsupported
            if (match := re.search(pat, requirements_text, re.IGNORECASE))
        ]
        unsupported_constraints = [item["label"] for item in matched]
        unresolved_items = [
            {
                "source": item["label"],
                "source_span": item["match"].group(0),
                "source_range": _source_range(item["match"]),
                "category": item["category"],
                "field_type": "unsupported_constraint",
                "status": "unresolved",
                "release_blocking": True,
                "reason": _reasons[item["category"]],
                "required_human_approvals": [f"approve_or_lower_{item['category']}"],
                "affected_gates": _affected_gates_by_category.get(item["category"], []),
            }
            for item in matched
        ]
        req_file = read_yaml(req_path)
        if "requirements" not in req_file:
            req_file["requirements"] = {"raw_inputs": [], "active_lowered": [], "active_unresolved": []}
        existing_inputs = req_file["requirements"].get("raw_inputs", [])
        input_id = f"req_input_{len(existing_inputs) + 1:04d}"
        req_file["requirements"]["raw_inputs"] = [*existing_inputs, {"id": input_id, "text": requirements_text, "created_by": "user"}]
        lowered_by_path = {item["spec_path"]: item for item in lowered_fields}
        req_file["requirements"]["active_lowered"] = [
            {"id": f"req_{i:04d}", "source": sp, **lowered_by_path.get(sp, {"spec_path": sp, "status": "lowered"})}
            for i, sp in enumerate(sorted(set(changed)), start=1)
        ]
        req_file["requirements"]["active_unresolved"] = [
            {"id": f"req_unresolved_{i:04d}", **item}
            for i, item in enumerate(unresolved_items, start=1)
        ]
        affected_gates = sorted({
            gate
            for item in [*lowered_fields, *unresolved_items]
            for gate in item.get("affected_gates", [])
        })
        required_human_approvals = sorted({
            approval
            for item in unresolved_items
            for approval in item.get("required_human_approvals", [])
        })
        lowered_tokens = [
            {
                "id": f"req_token_lowered_{i:04d}",
                "kind": "lowered_field",
                "status": item["status"],
                "spec_path": item["spec_path"],
                "field_type": item["field_type"],
                "value": item["value"],
                "source_span": item.get("source_span"),
                "source_range": item.get("source_range"),
                "affected_gates": item.get("affected_gates", []),
                "required_human_approvals": [],
            }
            for i, item in enumerate(sorted(lowered_fields, key=lambda field: field["spec_path"]), start=1)
        ]
        unresolved_tokens = [
            {
                "id": f"req_token_unresolved_{i:04d}",
                "kind": "unsupported_constraint",
                "status": item["status"],
                "category": item["category"],
                "field_type": item["field_type"],
                "source": item["source"],
                "source_span": item.get("source_span"),
                "source_range": item.get("source_range"),
                "release_blocking": item["release_blocking"],
                "reason": item["reason"],
                "affected_gates": item.get("affected_gates", []),
                "required_human_approvals": item.get("required_human_approvals", []),
            }
            for i, item in enumerate(unresolved_items, start=1)
        ]
        assumption_tokens = [
            {
                "id": f"req_token_assumption_{i:04d}",
                "kind": "retained_assumption",
                "status": "retained_from_spec_assumption",
                "source": item,
                "source_span": None,
                "source_range": None,
                "release_blocking": False,
                "affected_gates": ["release_preparation"],
                "required_human_approvals": [],
            }
            for i, item in enumerate(unresolved_ambiguous, start=1)
        ]
        compiler_ir = {
            "version": "requirements_ir_v1",
            "input_id": input_id,
            "lowered_fields": sorted(lowered_fields, key=lambda item: item["spec_path"]),
            "unresolved_assumptions": [
                {
                    "source": item,
                    "status": "retained_from_spec_assumption",
                    "release_blocking": False,
                    "required_human_approvals": [],
                    "affected_gates": ["release_preparation"],
                }
                for item in unresolved_ambiguous
            ],
            "unsupported_constraints": unresolved_items,
            "tokens": [*lowered_tokens, *unresolved_tokens, *assumption_tokens],
            "required_human_approvals": required_human_approvals,
            "affected_gates": affected_gates,
        }
        req_file["requirements"]["compiler_ir"] = compiler_ir
        write_yaml(req_path, req_file)
        return {
            "status": "generated",
            "has_unresolved_constraints": bool(unsupported_constraints),
            "mode": "replace_active_requirements",
            "changed_paths": sorted(set(changed)),
            "changed_files": [str(system_path), str(firmware_path), str(manufacturing_path), str(req_path)],
            "unresolved_requirements": unresolved_ambiguous,
            "unsupported_constraints": unsupported_constraints,
            "compiler_ir": compiler_ir,
            "required_human_approvals": required_human_approvals,
            "affected_gates": affected_gates,
        }

    def validate_spec(self, project: str) -> dict[str, Any]:
        path = self.workspace.require_project(project)
        report = self.validator.validate_spec(self.read_spec(project))
        persist_report(path, report)
        return report.to_dict()

    def generate_all(self, project: str) -> dict[str, Any]:
        path = self.workspace.require_project(project)
        spec = self.read_spec(project)
        backend = spec.get("electronics", {}).get("backend", "reference")
        electronics, resolution, resolution_report = generate_electronics(path, spec, self.parts_root, backend)
        graph = json.loads((path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
        adapters = {"tscircuit": self.tscircuit, "kicad": self.kicad, "python_netlist": self.python_netlist, "atopile": self.atopile}
        if backend in adapters:
            electronics.extend(adapters[backend].generate_source(path, spec, graph))
        files = {
            "electronics": electronics,
            "mechanical": generate_mechanical(path, spec, graph),
            "firmware": generate_firmware(path, spec, graph),
        }
        provenance = artifact_provenance(spec, self.parts_root, backend, compiler_version=self.tscircuit.VERSION if backend == "tscircuit" else None, release_eligible=False)
        write_json(path / "mechanical" / "generated" / "provenance.json", provenance)
        write_json(path / "firmware" / "generated" / "provenance.json", provenance)
        files["bom"] = [generate_bom(path)]
        return {"status": "candidate" if backend == "reference" else "generated", "backend": backend, "files": files, "component_resolution": resolution, "resolution_report": resolution_report}

    def generate_reference_intent(self, project: str) -> dict[str, Any]:
        spec = self.read_spec(project)
        if spec.get("electronics", {}).get("backend", "reference") != "reference":
            return {"status": "blocked", "code": "reference_backend_not_selected"}
        return self.generate_electronics_only(project)

    def generate_electronics_source(self, project: str) -> dict[str, Any]:
        return self.generate_electronics_only(project)

    def generate_mechanical_source(self, project: str) -> dict[str, Any]:
        return self.generate_mechanical_only(project)

    def generate_firmware_source(self, project: str) -> dict[str, Any]:
        return self.generate_firmware_only(project)

    def generate_electronics_only(self, project: str) -> dict[str, Any]:
        path = self.workspace.require_project(project)
        spec = self.read_spec(project)
        backend = spec.get("electronics", {}).get("backend", "reference")
        files, resolution, report = generate_electronics(path, spec, self.parts_root, backend)
        graph = json.loads((path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
        adapters = {"tscircuit": self.tscircuit, "kicad": self.kicad, "python_netlist": self.python_netlist, "atopile": self.atopile}
        if backend in adapters:
            files.extend(adapters[backend].generate_source(path, spec, graph))
        return {
            "status": "candidate" if backend == "reference" else "generated",
            "files": files,
            "component_resolution": resolution,
            "resolution_report": report,
            "supplier_availability_report": graph.get("supplier_availability_report"),
            "datasheet_evidence_report": graph.get("datasheet_evidence_report"),
        }

    def generate_mechanical_only(self, project: str) -> dict[str, Any]:
        path = self.workspace.require_project(project)
        spec = self.read_spec(project)
        graph_path = path / "electronics" / "generated" / "electrical_graph.json"
        try:
            graph = json.loads(graph_path.read_text(encoding="utf-8")) if graph_path.is_file() else {"components": []}
        except json.JSONDecodeError:
            graph = {"components": []}
        files = generate_mechanical(path, spec, graph)
        return {"status": "generated", "files": files}

    def generate_firmware_only(self, project: str) -> dict[str, Any]:
        path = self.workspace.require_project(project)
        spec = self.read_spec(project)
        graph_path = path / "electronics" / "generated" / "electrical_graph.json"
        if not graph_path.is_file():
            return {"status": "blocked", "files": [], "code": "resolved_graph_missing"}
        files = generate_firmware(path, spec, json.loads(graph_path.read_text(encoding="utf-8")))
        return {"status": "generated", "files": files}

    def run_all_checks(self, project: str, include_external: bool = True) -> dict[str, Any]:
        path = self.workspace.require_project(project)
        spec = self.read_spec(project)
        backend = spec.get("electronics", {}).get("backend", "reference")
        reports_dir = path / "validation" / "reports"
        contract_report_names = tuple(f"{name}_{stage}.json" for name in ("tscircuit", "kicad", "python_netlist", "atopile") for stage in CONTRACT_STAGES)
        stale_reference = ("compiled_electronics_backend.json",)
        active_contract = {f"{backend}_{stage}.json" for stage in CONTRACT_STAGES}
        stale_contracts = contract_report_names if backend == "reference" else tuple(item for item in contract_report_names if item not in active_contract)
        for name in stale_contracts + (stale_reference if backend != "reference" else ()):
            (reports_dir / name).unlink(missing_ok=True)
        reports = [
            self.validator.validate_spec(spec),
            self.validator.check_requirements_lowering(spec),
            self.validator.check_electrical_semantics(spec),
            self.validator.check_mechanical(spec),
        ]
        pinmap_path = path / "firmware" / "generated" / "pinmap.json"
        pinmap = json.loads(pinmap_path.read_text(encoding="utf-8")) if pinmap_path.exists() else []
        if pinmap:
            reports.append(self.validator.check_pinmap(pinmap))
        else:
            reports.append(GateReport("firmware_pinmap", Status.FAIL, [Failure(FailureCategory.FIRMWARE_ERROR, "missing_pinmap", "Generate firmware before checking the pinmap")]))
        graph_path = path / "electronics" / "generated" / "electrical_graph.json"
        graph = json.loads(graph_path.read_text(encoding="utf-8")) if graph_path.exists() else {"components": [], "nets": []}
        reports.append(self.validator.check_mechanical_connector_retention(spec, graph))
        reports.append(self.validator.check_mechanical_connector_cutouts(spec, graph))
        reports.append(self.validator.check_mechanical_mounting_integrity(spec, graph))
        fw_modules = spec.get("firmware", {}).get("modules", [])
        reports.append(self.validator.check_firmware_modules(fw_modules, pinmap, spec=spec, graph=graph))
        if graph_path.exists():
            if graph.get("component_resolution_report"):
                reports.append(self._report_from_dict(graph["component_resolution_report"]))
            if graph.get("supplier_availability_report"):
                reports.append(self._report_from_dict(graph["supplier_availability_report"]))
            else:
                reports.append(GateReport("supplier_availability", Status.BLOCKED, [Failure(FailureCategory.BOM_ERROR, "supplier_availability_not_resolved", "Regenerate electronics with a configured supplier adapter")]))
            if graph.get("datasheet_evidence_report"):
                reports.append(self._report_from_dict(graph["datasheet_evidence_report"]))
            else:
                reports.append(GateReport("datasheet_evidence", Status.BLOCKED, [Failure(FailureCategory.BOM_ERROR, "datasheet_evidence_not_resolved", "Regenerate electronics with the curated evidence catalog")]))
            reports.append(self.validator.check_bom(graph["components"]))
            reports.append(self.validator.check_sourcing(graph["components"]))
            reports.append(self._sourcing_resilience_report(spec, graph))
            reports.append(self.validator.check_component_metadata(graph["components"]))
            reports.append(self.validator.check_graph_pin_resolution(graph))
            reports.append(self.validator.check_power_tree(graph, spec))
            reports.append(self.validator.check_power_integrity_estimate(graph, spec))
            reports.append(self.validator.check_interface_integrity(graph))
            reports.append(self._semantic_schematic_roundtrip_report(path, graph))
            reports.append(self._support_circuit_completeness_report(spec, graph))
            reports.append(self.validator.check_hw_sw_parity(graph, pinmap))
            reports.append(self._firmware_interface_contract_report(path, spec, graph, pinmap))
        else:
            reports.append(GateReport("bom", Status.FAIL, [Failure(FailureCategory.BOM_ERROR, "missing_bom_source", "Generate electronics before checking the BOM")]))
            reports.append(self._semantic_schematic_roundtrip_report(path, graph))
        reports.extend([internal_erc(graph), internal_drc(path, spec, graph), build_firmware_reference(path)])
        if graph.get("components"):
            placement_proposal = propose_placement(spec, graph)
            reports.append(check_placement(placement_proposal, graph))
            reports.append(check_layout_thermal_integrity(placement_proposal, graph, spec))
            reports.append(check_layout_signal_integrity(placement_proposal, graph, spec))
        if graph.get("components") and graph_path.exists():
            try:
                ref_fab_out = path / "exports" / "candidates" / "reference-fabrication"
                ref_fab_artifacts = export_fabrication(path, spec, graph, ref_fab_out)
                reports.append(GateReport("reference_fabrication", Status.PASS, [], metrics={"artifact_count": len(ref_fab_artifacts), "candidate_only": True}, artifacts=ref_fab_artifacts, backend={"name": "reference-fabrication", "release_eligible": False, "candidate_only": True}))
            except Exception as exc:
                reports.append(GateReport("reference_fabrication", Status.FAIL, [Failure(FailureCategory.EDA_ERROR, "reference_fabrication_error", str(exc))], backend={"name": "reference-fabrication", "release_eligible": False}))
        if backend == "reference":
            reports.append(GateReport("compiled_electronics_backend", Status.BLOCKED, [Failure(FailureCategory.EDA_ERROR, "reference_backend_candidate_only", "Reference electronics backend produces candidate artifacts only")], backend={"name": "reference", "release_eligible": False}))
        elif backend == "atopile":
            reports.extend(self.atopile.evaluate(path, graph))
        elif backend == "tscircuit":
            reports.extend(self.tscircuit.evaluate(path, graph))
        elif backend == "kicad":
            reports.extend(self.kicad.evaluate(path, graph))
        elif backend == "python_netlist":
            reports.extend(self.python_netlist.evaluate(path, graph))
        else:
            reports.append(GateReport("compiled_electronics_backend", Status.BLOCKED, [Failure(FailureCategory.EDA_ERROR, "unknown_backend", f"Unknown electronics backend: {backend}")]))
        if include_external:
            autoroute = self.freerouting.route(path)
            erc = self.kicad.run_erc(path); erc.gate = "native_erc"
            drc = self.kicad.run_drc(path); drc.gate = "native_drc"
            library_failures = [failure for failure in [*erc.failures, *drc.failures] if failure.details.get("type") in {"lib_footprint_issues", "lib_footprint_mismatch", "footprint_link_issues"}]
            library_status = Status.BLOCKED if Status.BLOCKED in {erc.status, drc.status} else (Status.FAIL if library_failures else Status.PASS)
            library_crosscheck = GateReport("kicad_library_crosscheck", library_status, library_failures, metrics={"method": "native_erc_drc_library_resolution", "issues": len(library_failures)}, backend={"name": "kicad-cli"})
            if backend == "tscircuit":
                reports = [report for report in reports if report.gate != "tscircuit_manufacturing_export"]
                manufacturing = self.kicad.export_manufacturing(path, path / "exports" / "candidates" / "backend-validation" / "tscircuit")
                manufacturing.gate = "tscircuit_manufacturing_export"
                manufacturing.backend = {**manufacturing.backend, "electronics_backend": "tscircuit", "export_bridge": "compiled_graph_to_kicad"}
                reports.append(manufacturing)
            board_step = self._latest_board_step(path)
            mechanical = self.mechanical.generate(spec, path / "exports" / "candidates" / "native-check", graph=graph, board_step=board_step)
            mechanical.gate = "native_mechanical_validation"
            reports.extend([autoroute, erc, drc, library_crosscheck, mechanical, self.zephyr.build(path, spec.get("firmware", {}).get("target", "unknown"))])
        else:
            for gate, message in (("autoroute", "Freerouting was not requested"), ("native_erc", "KiCad ERC was not requested"), ("native_drc", "KiCad DRC was not requested"), ("kicad_library_crosscheck", "KiCad library cross-check was not requested"), ("native_mechanical_validation", "Native CAD validation was not requested"), ("native_zephyr_build", "Native Zephyr build was not requested")):
                reports.append(GateReport(gate, Status.BLOCKED, [Failure(FailureCategory.TOOL_ERROR, "external_gate_not_run", message)]))
        try:
            self.generate_physical_qualification_plan(project)
        except Exception:
            pass  # Non-fatal: qualification plan generation can fail on transient filesystem state
        reports.append(self._physical_qualification_report(path))
        reports.append(self._candidate_critic_report(path, spec, graph, reports, include_external=include_external))
        reports.append(self._design_dependency_graph_report(reports))
        for report in reports:
            persist_report(path, report)
        return self._report_set(reports)

    def prepare_release(self, project: str, checks: dict[str, Any], native_checks_confirmed: bool = False) -> dict[str, Any]:
        path = self.workspace.require_project(project)
        spec = self.read_spec(project)
        backend = spec.get("electronics", {}).get("backend", "reference")
        release_tier = self._release_tier_for_backend(backend)
        if backend not in _RELEASE_ELIGIBLE_BACKENDS:
            return {"status": "blocked", "code": "compiled_electronics_backend_required", "reports": [GateReport("release_preparation", Status.BLOCKED, [Failure(FailureCategory.RELEASE_ERROR, "compiled_electronics_backend_required", "Release preparation requires a release-eligible electronics backend")]).to_dict()]}
        if not native_checks_confirmed:
            return {"status": "blocked", "code": "native_release_checks_required", "reports": checks["reports"]}
        if any(item["status"] != "pass" for item in checks["reports"]):
            return {"status": "blocked", "code": "release_gates_not_passed", "reports": checks["reports"]}
        unresolved_assumptions = [name for name, a in _assumptions_as_dict(spec.get("assumptions", {})).items() if a.get("critical") and a.get("requires_user_review")]
        if unresolved_assumptions:
            return {"status": "blocked", "code": "unresolved_critical_assumptions", "unresolved": unresolved_assumptions, "reports": checks["reports"]}
        graph = json.loads((path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
        profile = firmware_profile(spec, graph)
        release = path / "exports" / "releases" / spec["project"]["revision"]
        if release.exists():
            return {"status": "blocked", "code": "release_revision_exists", "release_path": str(release)}
        staging = path / "exports" / ".staging" / spec["project"]["revision"]
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        files: list[str] = []
        if backend == "python_netlist":
            tier_report = self._python_netlist_release_artifacts(path, staging)
        elif backend == "atopile":
            tier_report = self._atopile_release_source_artifacts(path, staging)
        else:
            tier_report = self.kicad.export_manufacturing(path, staging)
        if tier_report.status == Status.PASS:
            files.extend(tier_report.artifacts)
        persist_report(path, tier_report)
        mechanical_reports: list[GateReport] = []
        if release_tier == "fabrication":
            mechanical_report = self.mechanical.generate(spec, staging, graph=graph, board_step=staging / "mechanical" / "board.step", release_eligible=True)
            if mechanical_report.status == Status.PASS:
                files.extend(mechanical_report.artifacts)
            persist_report(path, mechanical_report)
            mechanical_reports.append(mechanical_report)
        if tier_report.status != Status.PASS or any(report.status != Status.PASS for report in mechanical_reports):
            shutil.rmtree(staging, ignore_errors=True)
            statuses = {tier_report.status, *(report.status for report in mechanical_reports)}
            return {"status": "blocked" if Status.BLOCKED in statuses else "fail", "release_path": str(release), "reports": [tier_report.to_dict(), *(report.to_dict() for report in mechanical_reports)]}
        firmware = staging / "firmware"; firmware.mkdir(parents=True, exist_ok=True)
        firmware_sources = [(item, item.relative_to(path / "firmware").as_posix()) for item in sorted((path / "firmware").rglob("*")) if item.is_file() and "build" not in item.parts]
        deterministic_zip(firmware / "source.zip", firmware_sources)
        for name in ("pinmap.h", "devicetree.overlay"):
            source = path / "firmware" / "generated" / name
            (firmware / name).write_bytes(source.read_bytes())
        atomic_write_text(firmware / "build_instructions.md", self._release_firmware_build_instructions(profile))
        docs = staging / "docs"; docs.mkdir(parents=True, exist_ok=True)
        statuses = [f"{item['gate']}: {item['status']}" for item in checks["reports"]]
        atomic_write_text(docs / "design_report.md", "# Design Report\n\n" + "\n".join(f"- {line}" for line in statuses) + "\n")
        write_json(docs / "validation_report.json", checks)
        atomic_write_text(docs / "bringup_guide.md", self._release_bringup_guide(profile))
        atomic_write_text(docs / "known_risks.md", self._release_known_risks(profile))
        qualification = self.generate_physical_qualification_plan(project)
        qualification_plan = Path(qualification["artifact"])
        qualification_markdown = Path(qualification["markdown"])
        if qualification_plan.is_file():
            (docs / "physical_qualification_plan.json").write_bytes(qualification_plan.read_bytes())
        if qualification_markdown.is_file():
            (docs / "physical_qualification_plan.md").write_bytes(qualification_markdown.read_bytes())
        simple_pdf(f"{profile['model']} Schematic", [f"Components: {len(graph['components'])}", f"Nets: {len(graph['nets'])}", "See KiCad project for editable source."], docs / "schematic.pdf")
        assembly_lines = (
            ["Top-side placement is provided in fabrication/pick_and_place.csv.", "Verify connector orientation before assembly."]
            if release_tier == "fabrication"
            else ["No PCB placement or fabrication package is included in this release tier.", "Use netlist evidence for electrical review; promote through a fabrication backend for Gerbers."]
        )
        simple_pdf("Assembly Drawing", assembly_lines, docs / "assembly_drawing.pdf")
        simple_pdf("Validation Report", statuses, docs / "validation_report.pdf")
        simple_pdf("Design Report", statuses + ["Physical qualification risks are listed in known_risks.md."], docs / "design_report.pdf")
        simple_pdf("Layout Preview", [f"Board envelope: {spec['mechanical']['envelope']['board_width_mm']} x {spec['mechanical']['envelope']['board_height_mm']} mm", f"Placement entries: {len(graph['components'])}"], docs / "layout_preview.pdf")
        simple_pdf("Bring-up Guide", self._release_bringup_pdf_lines(profile), docs / "bringup_guide.pdf")
        if release_tier == "fabrication":
            fabrication = staging / "fabrication"
            fabrication.mkdir(parents=True, exist_ok=True)
            (fabrication / "assembly_drawing.pdf").write_bytes((docs / "assembly_drawing.pdf").read_bytes())
        provenance = graph.get("provenance", {}) | {"release_eligible": True, "candidate_only": False, "release_tier": release_tier}
        write_json(staging / "provenance.json", provenance)
        write_manifest(staging, staging / "manifest.json", provenance=provenance, candidate_only=False)
        release.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(release)
        files = [item.replace(str(staging), str(release), 1) for item in files]
        manifest = str(release / "manifest.json")
        reports = [tier_report.to_dict(), *(report.to_dict() for report in mechanical_reports)]
        for report in reports:
            report["artifacts"] = [item.replace(str(staging), str(release), 1) for item in report.get("artifacts", [])]
        return {"status": "released", "release_path": str(release), "files": files + [str(release / "firmware" / "source.zip"), manifest], "reports": reports}

    @staticmethod
    def _release_tier_for_backend(backend: str) -> str:
        return _RELEASE_TIER_BY_BACKEND.get(backend, "candidate")

    def _python_netlist_release_artifacts(self, project_path: Path, staging: Path) -> GateReport:
        compiled = project_path / "electronics" / "source" / "python_netlist" / "compiled_netlist.json"
        if not compiled.is_file():
            return GateReport(
                "python_netlist_release_artifacts",
                Status.BLOCKED,
                [Failure(FailureCategory.EDA_ERROR, "compiled_netlist_missing", "Run evaluate with python_netlist backend first to produce compiled_netlist.json")],
                backend={"name": "python_netlist", "release_tier": "netlist"},
            )
        dest = staging / "netlist"
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / "compiled_netlist.json"
        target.write_bytes(compiled.read_bytes())
        return GateReport(
            "python_netlist_release_artifacts",
            Status.PASS,
            [],
            artifacts=[str(target)],
            metrics={"release_tier": "netlist", "artifact": "compiled_netlist.json"},
            backend={"name": "python_netlist", "release_tier": "netlist"},
        )

    def _atopile_release_source_artifacts(self, project_path: Path, staging: Path) -> GateReport:
        source_dir = project_path / "electronics" / "source" / "atopile"
        ato_file = source_dir / "design.ato"
        ato_yaml = source_dir / "ato.yaml"
        if not ato_file.is_file():
            return GateReport(
                "atopile_source_release_artifacts",
                Status.BLOCKED,
                [Failure(FailureCategory.EDA_ERROR, "source_not_generated", "Run generate_electronics_only first to produce atopile source")],
                backend={"name": "atopile", "release_tier": "hdl_source"},
            )
        dest = staging / "source" / "atopile"
        dest.mkdir(parents=True, exist_ok=True)
        target_ato = dest / "design.ato"
        target_yaml = dest / "ato.yaml"
        target_ato.write_bytes(ato_file.read_bytes())
        artifacts = [str(target_ato)]
        if ato_yaml.is_file():
            target_yaml.write_bytes(ato_yaml.read_bytes())
            artifacts.append(str(target_yaml))
        return GateReport(
            "atopile_source_release_artifacts",
            Status.PASS,
            [],
            artifacts=artifacts,
            metrics={"release_tier": "hdl_source", "artifact": "design.ato"},
            backend={"name": "atopile", "release_tier": "hdl_source"},
        )

    @staticmethod
    def _release_firmware_build_instructions(profile: dict[str, Any]) -> str:
        return (
            "# Firmware Build\n\n"
            "Reference verification: `cmake -S firmware/reference -B build -G Ninja && cmake --build build && ctest --test-dir build`.\n\n"
            f"Zephyr target: `west build -b {profile['board_name']} firmware/zephyr/app`.\n"
        )

    @staticmethod
    def _release_bringup_guide(profile: dict[str, Any]) -> str:
        if profile["board_name"] == "ble_sensor_node":
            return (
                "# Bring-up Guide\n\n"
                "1. Inspect assembly and verify no shorts with battery and USB disconnected.\n"
                "2. Connect USB-C — verify VBAT rises to 3.7–4.2 V on BT1 pad, CHG_STAT toggles low.\n"
                "3. Verify V3V3 rail (U4 LDO output) is stable at 3.3 V with USB connected.\n"
                "4. Flash the Zephyr image over SWD with nRF Command Line Tools.\n"
                "5. Verify BLE advertisement packet visible via a BLE scanner app.\n"
                "6. Verify I2C bus: SHT31 identity register (0x08) and BQ27441 device type (0x0421).\n"
                "7. Verify fuel gauge state-of-charge readout matches actual LiPo charge level.\n"
                "8. Increase advertising interval and sensor rate only under thermal monitoring.\n"
            )
        if profile["board_name"] == "sensor_data_logger":
            return (
                "# Bring-up Guide\n\n"
                "1. Inspect assembly and verify no shorts with power removed.\n"
                "2. Current-limit the USB supply below 0.5 A and apply 5 V through USB-C VBUS.\n"
                "3. Verify the 3.3 V rail before enabling the ESP32-S3 module.\n"
                "4. Flash the Zephyr image over the ESP32-S3 USB boot path.\n"
                "5. Verify USB console enumeration, I2C pullups, IMU identity, and interrupt activity.\n"
                "6. Increase logging duration only under instrumented thermal and current monitoring.\n"
            )
        return (
            "# Bring-up Guide\n\n"
            "1. Inspect assembly and verify no shorts with power removed.\n"
            "2. Current-limit the bench supply below 0.5 A and apply 24 V through the protected input.\n"
            "3. Verify 5 V and 3.3 V rails before fitting the MCU.\n"
            "4. Keep motor enable disabled; flash the Zephyr image over SWD.\n"
            "5. Verify console, IMU identity, CAN loopback, E-stop latch, then each PWM/encoder/current channel.\n"
            "6. Increase load only under instrumented thermal and transient monitoring.\n"
        )

    @staticmethod
    def _release_known_risks(profile: dict[str, Any]) -> str:
        if profile["board_name"] == "ble_sensor_node":
            return (
                "# Known Risks\n\n"
                "- BLE RF performance, antenna keepout effectiveness, and coexistence with nearby 2.4 GHz sources require anechoic-chamber qualification.\n"
                "- LiPo cell abuse tolerance (overcharge, over-discharge, thermal runaway) requires cell-level testing separate from BQ24079 protection characterisation.\n"
                "- EMI/EMC, I2C bus integrity, and ESD robustness on exposed USB-C connector require physical qualification.\n"
            )
        if profile["board_name"] == "sensor_data_logger":
            return (
                "# Known Risks\n\n"
                "- EMI/EMC, USB signal integrity, antenna keepout effectiveness, enclosure detuning, ESD robustness, connector fatigue, and logging endurance require physical qualification.\n"
                "- The secondary reference design is USB-powered and does not validate battery, motor, CAN, or high-current power behavior.\n"
            )
        return (
            "# Known Risks\n\n"
            "- EMI/EMC, full-load thermal behavior, vibration life, battery abuse, motor transients, ingress protection, and connector fatigue require physical qualification.\n"
            "- The reference design controls external motor drivers; it does not route 8 A motor phase current through the controller PCB.\n"
        )

    @staticmethod
    def _release_bringup_pdf_lines(profile: dict[str, Any]) -> list[str]:
        if profile["board_name"] == "ble_sensor_node":
            return [
                "Connect USB-C and verify VBAT charging.",
                "Verify V3V3 LDO output stable at 3.3 V.",
                "Flash via SWD with nRF Command Line Tools.",
                "Verify BLE advertising packet visible.",
                "Verify I2C: SHT31 and BQ27441 identity.",
            ]
        if profile["board_name"] == "sensor_data_logger":
            return [
                "Current-limit initial USB-C power-up.",
                "Verify the 3.3 V rail before ESP32-S3 operation.",
                "Verify USB console enumeration.",
                "Verify I2C IMU identity and interrupt activity.",
            ]
        return [
            "Current-limit initial power-up.",
            "Verify rails before MCU operation.",
            "Test E-stop before motor enable.",
            "Run each channel with motor power isolated.",
        ]

    def check_release_gate(self, project: str, reports: list[GateReport] | None = None, include_external: bool = False) -> dict[str, Any]:
        path = self.workspace.require_project(project)
        spec = self.read_spec(project)
        if reports is None:
            report_data = self.run_all_checks(project, include_external=include_external)
            reports = [self._report_from_dict(item) for item in report_data["reports"]]
        revision = spec["project"]["revision"]
        release = path / "exports" / "releases" / revision
        backend = spec.get("electronics", {}).get("backend", "reference")
        release_tier = self._release_tier_for_backend(backend)
        if backend == "atopile":
            tier_required = [
                release / "source" / "atopile" / "design.ato",
            ]
        elif backend == "python_netlist":
            tier_required = [
                release / "netlist" / "compiled_netlist.json",
            ]
        else:
            tier_required = [
                release / "fabrication" / "gerbers.zip", release / "fabrication" / "drill.zip",
                release / "fabrication" / "bom.csv", release / "fabrication" / "pick_and_place.csv",
                release / "fabrication" / "assembly_drawing.pdf",
            ]
        mechanical_required: list[Path] = []
        if release_tier == "fabrication":
            mechanical_required = [
                release / "mechanical" / "board.step", release / "mechanical" / "enclosure.step", release / "mechanical" / "enclosure.stl", release / "mechanical" / "assembly.step",
                release / "mechanical" / "mechanical_manifest.json",
            ]
        required = [
            *tier_required,
            *mechanical_required,
            release / "firmware" / "source.zip", release / "docs" / "design_report.md",
            release / "firmware" / "pinmap.h", release / "firmware" / "devicetree.overlay", release / "firmware" / "build_instructions.md",
            release / "docs" / "validation_report.json", release / "docs" / "bringup_guide.md", release / "docs" / "known_risks.md",
            release / "docs" / "physical_qualification_plan.json", release / "docs" / "physical_qualification_plan.md",
            release / "docs" / "schematic.pdf", release / "docs" / "layout_preview.pdf", release / "docs" / "design_report.pdf",
            release / "docs" / "validation_report.pdf", release / "docs" / "bringup_guide.pdf", release / "manifest.json",
        ]
        if release_tier == "fabrication":
            required.extend(release / "mechanical" / "variants" / f"enclosure_{variant['name']}.step" for variant in spec["mechanical"]["variants"])
            fixtures = spec["mechanical"].get("fixtures", {})
            if fixtures.get("mounting_plate", {}).get("enabled"):
                required.append(release / "mechanical" / "mounting_plate.step")
            if fixtures.get("frame_brackets", {}).get("enabled"):
                required.extend([release / "mechanical" / "frame_bracket_left.step", release / "mechanical" / "frame_bracket_right.step"])
        if backend not in _RELEASE_ELIGIBLE_BACKENDS:
            reports = [*reports, GateReport("backend_release_policy", Status.BLOCKED, [Failure(FailureCategory.RELEASE_ERROR, "compiled_electronics_backend_required", f"Backend {backend} is not release eligible")])]
        elif backend == "atopile":
            # HDL-source release: compile/source parity gates are required; footprint,
            # layout, and manufacturing export are KiCad-plugin fabrication evidence.
            _ato_exempt = {"atopile_footprint_parity", "atopile_layout_completeness", "atopile_manufacturing_export"}
            reports = [r for r in reports if r.gate not in _ato_exempt]
            required_ato_gates = {f"atopile_{s}" for s in ("compile", "netlist_extract", "graph_parity")}
            present_gates = {r.gate for r in reports}
            for gate_name in sorted(required_ato_gates - present_gates):
                reports = [*reports, GateReport(gate_name, Status.BLOCKED, [Failure(FailureCategory.RELEASE_ERROR, "gate_not_run", f"Required gate was not executed: {gate_name}")])]
        elif backend == "python_netlist":
            # layout_completeness and manufacturing_export are intentionally BLOCKED for python_netlist
            # (it produces a compiled netlist, not PCB layout or gerbers). Strip them so they don't
            # cause the release gate to fail — the backend's release_blocking_gates excludes them.
            _pn_exempt = {"python_netlist_layout_completeness", "python_netlist_manufacturing_export"}
            reports = [r for r in reports if r.gate not in _pn_exempt]
            required_pn_gates = {f"python_netlist_{s}" for s in ("compile", "netlist_extract", "graph_parity", "footprint_parity")}
            present_gates = {r.gate for r in reports}
            for gate_name in sorted(required_pn_gates - present_gates):
                reports = [*reports, GateReport(gate_name, Status.BLOCKED, [Failure(FailureCategory.RELEASE_ERROR, "gate_not_run", f"Required gate was not executed: {gate_name}")])]
        else:
            required_tsc_gates = {f"{backend}_{stage}" for stage in CONTRACT_STAGES}
            present_gates = {r.gate for r in reports}
            for gate_name in sorted(required_tsc_gates - present_gates):
                reports = [*reports, GateReport(gate_name, Status.BLOCKED, [Failure(FailureCategory.RELEASE_ERROR, "gate_not_run", f"Required gate was not executed: {gate_name}")])]
        present_gates = {r.gate for r in reports}
        if "physical_qualification" not in present_gates:
            reports = [*reports, GateReport("physical_qualification", Status.BLOCKED, [Failure(FailureCategory.RELEASE_ERROR, "gate_not_run", "Required gate was not executed: physical_qualification")])]
        present_gates = {r.gate for r in reports}
        for gate_name in ("semantic_schematic_roundtrip", "design_dependency_graph"):
            if gate_name not in present_gates:
                reports = [*reports, GateReport(gate_name, Status.BLOCKED, [Failure(FailureCategory.RELEASE_ERROR, "gate_not_run", f"Required gate was not executed: {gate_name}")])]
        reports = [*reports, self._artifact_integrity_report(release, required=required)]
        report = self.validator.release_gate(reports, spec.get("assumptions", {}), required)
        persist_report(path, report)
        return report.to_dict()

    def generate_repair_plan(self, project: str, check_result: dict[str, Any] | None = None) -> dict[str, Any]:
        check_result = check_result or self.run_all_checks(project)
        spec = self.read_spec(project)
        actions: list[dict[str, Any]] = []
        requires_user_decision = False
        _text_advice = {
            "current_budget_exceeded": "Add power-domain concurrency limits or increase an explicitly approved battery/current-path rating.",
            "tool_unavailable": "Run the deterministic toolchain in the pinned Docker image or install the missing backend.",
            "missing_mpn": "Resolve an approved MPN and substitute before release.",
            "insufficient_clearance": "Increase enclosure dimensions or reduce the constrained envelope.",
            "pin_conflict": "Reassign the conflicting MCU peripheral pin in the firmware/electrical source.",
            "unlowered_requirement": "Waive or lower this requirement into a typed spec field before release.",
            "missing_estop_shutdown_behavior": "Add a timeout_shutdown firmware module triggered by ESTOP_IN that disables motor outputs.",
            "unsafe_estop_shutdown_action": "Change the ESTOP_IN timeout_shutdown module so it disables motor outputs or motor enables.",
        }
        per_channel = spec.get("actuation", {}).get("motor_channel_peak_current_a", 0.0)
        battery_peak = spec.get("system", {}).get("supply", {}).get("battery", {}).get("pack_current_peak_a", 0.0)
        enclosure = list(spec.get("mechanical", {}).get("enclosure_internal_mm", []))
        for report in check_result["reports"]:
            for failure in report["failures"]:
                code = failure["code"]
                details = failure.get("details", {})
                action = _text_advice.get(code, f"Resolve {code}: {failure['message']}")
                requires = failure.get("requires_user_decision", False) or code in {"current_budget_exceeded", "unsafe_assumption", "unlowered_requirement"}
                requires_user_decision |= requires
                patches: list[dict[str, Any]] = []
                if code == "current_budget_exceeded" and per_channel > 0 and battery_peak > 0:
                    safe_channels = math.floor(battery_peak / per_channel)
                    if safe_channels >= 1:
                        patches.append(RepairPatch("system", "actuation.max_simultaneous_peak_channels", safe_channels, requires_approval=True, safety_class="review_required", source_gate=report["gate"], source_failure=code).to_dict())
                elif code == "insufficient_clearance":
                    axis = details.get("axis")
                    minimum = details.get("minimum_mm")
                    _axis_idx = {"width": 0, "height": 1, "depth": 2}
                    idx = _axis_idx.get(axis) if axis else None
                    if idx is not None and minimum is not None and enclosure:
                        new_enclosure = list(enclosure)
                        new_enclosure[idx] = math.ceil(minimum)
                        patches.append(RepairPatch("mechanical", "mechanical.enclosure_internal_mm", new_enclosure, source_gate=report["gate"], source_failure=code).to_dict())
                elif code == "unlowered_requirement":
                    patches.append(RepairPatch("requirements", f"requirements.active_unresolved.{details.get('requirement_id', 'unknown')}.status", "waived", requires_approval=True, safety_class="review_required", source_gate=report["gate"], source_failure=code).to_dict())
                actions.append({"gate": report["gate"], "failure_code": code, "action": action, "patches": patches, "requires_user_decision": requires})
        agent_actions = _derive_agent_actions(actions, spec)
        return {"status": "generated", "project": project, "requires_user_decision": requires_user_decision, "actions": actions, "agent_actions": agent_actions}

    def resolve_assumption(self, project: str, name: str, resolution: Any, approved: bool) -> dict[str, Any]:
        path = self.workspace.require_project(project) / "spec" / "system.yaml"
        value = self.workspace.read_spec(project)
        assumptions = value.get("assumptions", {})
        if name not in assumptions:
            raise ValueError(f"Unknown assumption: {name}")
        if not approved:
            return {"status": "blocked", "assumption": name, "message": "Explicit approval is required to resolve an assumption"}
        assumptions[name]["resolved_value"] = resolution
        assumptions[name]["requires_user_review"] = False
        system_file = read_yaml(path)
        system_file["assumptions"] = assumptions
        write_yaml(path, system_file)
        return {"status": "pass", "assumption": name, "resolution": resolution}

    def export_release_bundle(self, project: str, gate_result: dict[str, Any] | None = None) -> dict[str, Any]:
        gate = gate_result or self.check_release_gate(project, include_external=True)
        if gate["status"] != "pass":
            return {"status": "blocked", "release_gate": gate, "message": "Release bundle cannot be exported until all required gates pass"}
        path = self.workspace.require_project(project)
        revision = self.read_spec(project)["project"]["revision"]
        release = path / "exports" / "releases" / revision
        bundle = path / "exports" / "releases" / f"{project}-{revision}.zip"
        deterministic_zip(bundle, [(artifact, artifact.relative_to(release).as_posix()) for artifact in release.rglob("*") if artifact.is_file()])
        with zipfile.ZipFile(bundle) as archive:
            bad = archive.testzip()
        if bad:
            return {"status": "fail", "bundle": str(bundle), "corrupt_entry": bad}
        return {"status": "released", "bundle": str(bundle), "sha256": sha256(bundle), "bytes": bundle.stat().st_size}

    def run_design_iteration(self, project: str, include_external: bool = True) -> dict[str, Any]:
        generated = self.generate_all(project)
        checks = self.run_all_checks(project, include_external=include_external)
        repair_plan = self.generate_repair_plan(project, checks)
        # Release artifacts are written only when all checks pass (in design_until_release),
        # not speculatively on every iteration.
        iteration_id = self.workspace.snapshot(project, {"goal": "improve this hardware candidate toward release promotion"})
        candidate = self._write_candidate_bundle(project, iteration_id, generated, checks)
        all_pass = all(item["status"] == "pass" for item in checks["reports"])
        blocked = any(item["status"] == "blocked" for item in checks["reports"])
        result = {
            "status": "pass" if all_pass else ("blocked" if blocked or repair_plan["requires_user_decision"] else "fail"),
            "iteration_id": iteration_id,
            "generated": generated,
            "passed_gates": [item["gate"] for item in checks["reports"] if item["status"] == "pass"],
            "failed_gates": [item["gate"] for item in checks["reports"] if item["status"] != "pass"],
            "blocked_gates": [item["gate"] for item in checks["reports"] if item["status"] == "blocked"],
            "repair_plan": repair_plan,
            "candidate": candidate,
            "release_gate": {"status": "pass" if all_pass else ("blocked" if blocked else "fail")},
        }
        write_json(self.workspace.require_project(project) / "history" / "iterations" / iteration_id / "result.json", result)
        self._append_failures(project, iteration_id, checks)
        return result

    def design_candidate(
        self,
        project: str,
        include_external: bool = False,
        with_review_bundle: bool = True,
        requirements_text: str | None = None,
    ) -> dict[str, Any]:
        """Lower an optional brief, then generate a cross-domain hardware candidate."""
        requirements_update = self.update_requirements(project, requirements_text) if requirements_text else None
        project_path = self.workspace.require_project(project)
        generated = self.generate_all(project)
        checks = self.run_all_checks(project, include_external=include_external)
        spec = self.read_spec(project)
        backend = spec.get("electronics", {}).get("backend", "reference")
        backend_release_eligible = backend in _RELEASE_ELIGIBLE_BACKENDS
        iteration_id = self.workspace.snapshot(project, {"goal": "design evidence-backed hardware candidate"})
        candidate = self._write_candidate_bundle(project, iteration_id, generated, checks)

        reports = checks.get("reports", [])
        gate_summary = {
            "total": len(reports),
            "pass": sum(1 for item in reports if item.get("status") == "pass"),
            "fail": sum(1 for item in reports if item.get("status") == "fail"),
            "blocked": sum(1 for item in reports if item.get("status") == "blocked"),
        }
        blocking_gates = [
            {"gate": item.get("gate"), "status": item.get("status"), "failure_count": len(item.get("failures", []))}
            for item in reports
            if item.get("status") != "pass"
        ]
        release_blocking_failures = [item["gate"] for item in blocking_gates if item.get("gate")]
        if not backend_release_eligible:
            release_blocking_failures.insert(0, f"backend '{backend}' is candidate-only and not release-eligible")

        files = generated.get("files", {})
        design_domains = {
            "electronics": files.get("electronics", []),
            "mechanical": files.get("mechanical", []),
            "firmware": files.get("firmware", []),
            "sourcing": ["component_resolution", "component_provenance", "sourcing_resilience"],
            "manufacturing": files.get("bom", []),
        }

        review_bundle = self.export_review(project) if with_review_bundle else None
        reviewable_artifacts = {
            "candidate_bundle": candidate.get("bundle"),
            "candidate_manifest": (Path(candidate["path"]) / "manifest.json").as_posix(),
            "review_bundle": review_bundle.get("file") if review_bundle else None,
            "review_html": review_bundle.get("html") if review_bundle else None,
            "physical_qualification_plan": (self.workspace.require_project(project) / "validation" / "physical" / "qualification_plan.json").as_posix(),
            "physical_qualification_plan_markdown": (self.workspace.require_project(project) / "validation" / "physical" / "qualification_plan.md").as_posix(),
            "electronics": files.get("electronics", []),
            "mechanical": files.get("mechanical", []),
            "firmware": files.get("firmware", []),
            "manufacturing": files.get("bom", []),
        }
        result = {
            "status": "candidate",
            "project": project,
            "iteration_id": iteration_id,
            "design_goal": "cross-domain hardware candidate for evidence-backed promotion",
            "backend": backend,
            "backend_release_eligible": backend_release_eligible,
            "release_eligible": False,
            "candidate_only": True,
            "release_blocking_failures": release_blocking_failures,
            "requirements_update": requirements_update,
            "generated": generated,
            "design_domains": design_domains,
            "semantic_representation": self._candidate_semantic_representation(project_path, files),
            "sourcing_choices": self._candidate_sourcing_choices(generated.get("component_resolution") or []),
            "reviewable_artifacts": reviewable_artifacts,
            "dependency_graph": next((item for item in checks.get("reports", []) if item.get("gate") == "design_dependency_graph"), None),
            "grounding_summary": self._candidate_grounding_summary(generated.get("component_resolution") or [], checks),
            "gate_summary": gate_summary,
            "blocking_gates": blocking_gates,
            "candidate": candidate,
            "review_bundle": review_bundle,
            "promotion": {
                "status": "not_promoted",
                "next_gate": "hw_check_release_gate",
                "rule": "Only candidates with passing required gates, resolved critical assumptions, and complete artifact integrity can be promoted.",
            },
        }
        write_json(project_path / "history" / "iterations" / iteration_id / "result.json", result)
        self._append_failures(project, iteration_id, checks)
        return result

    def explore_design_space(self, project: str, max_candidates: int = 8) -> dict[str, Any]:
        """Generate and rank deterministic design alternatives from current project evidence."""
        project_path = self.workspace.require_project(project)
        max_candidates = max(1, int(max_candidates))
        generated = self.generate_all(project)
        checks = self.run_all_checks(project, include_external=False)
        spec = self.read_spec(project)
        graph_path = project_path / "electronics" / "generated" / "electrical_graph.json"
        graph = json.loads(graph_path.read_text(encoding="utf-8")) if graph_path.is_file() else {"components": [], "nets": []}
        contract_path = project_path / "mechanical" / "source" / "mechanical_contract.json"
        mechanical_contract = json.loads(contract_path.read_text(encoding="utf-8")) if contract_path.is_file() else {}
        reports = checks.get("reports", [])
        gate_summary = {
            "total": len(reports),
            "pass": sum(1 for item in reports if item.get("status") == "pass"),
            "fail": sum(1 for item in reports if item.get("status") == "fail"),
            "blocked": sum(1 for item in reports if item.get("status") == "blocked"),
        }
        capabilities = self.get_capabilities()
        candidates = [
            self._design_space_baseline_candidate(spec, graph, gate_summary),
            *self._design_space_backend_candidates(spec, capabilities),
            *self._design_space_component_candidates(spec, graph),
            *self._design_space_mechanical_candidates(spec, mechanical_contract),
            *self._design_space_supplier_candidates(spec, graph),
        ]
        ranked = sorted(candidates, key=lambda item: (-item["score"], item["id"]))
        for rank, candidate in enumerate(ranked, start=1):
            candidate["rank"] = rank
        returned = ranked[:max_candidates]
        exploration_dir = project_path / "history" / "design_space"
        exploration_dir.mkdir(parents=True, exist_ok=True)
        artifact = exploration_dir / "exploration.json"
        result = {
            "status": "generated",
            "project": project,
            "candidate_only": True,
            "release_eligible": False,
            "exploration_model": "deterministic_multi_axis_tradeoff_v1",
            "axes": ["baseline_gate_state", "electronics_backend", "component_alternative", "mechanical_enclosure_variant", "supplier_provider"],
            "max_candidates": max_candidates,
            "generated_summary": {
                "status": generated.get("status"),
                "backend": generated.get("backend"),
                "file_counts": {key: len(value) for key, value in (generated.get("files") or {}).items()},
                "component_resolution_status": (generated.get("resolution_report") or {}).get("status"),
                "component_resolution_count": len(generated.get("component_resolution") or []),
            },
            "gate_summary": gate_summary,
            "selected_candidate": returned[0] if returned else None,
            "candidates": returned,
            "all_candidate_count": len(ranked),
            "recommendation": (
                "Use the top-ranked option as the next candidate input only after reviewing blockers; "
                "release still requires hw_check_release_gate and physical qualification evidence."
            ),
            "artifact": str(artifact),
        }
        write_json(artifact, result)
        return result

    @staticmethod
    def _design_space_baseline_candidate(spec: dict[str, Any], graph: dict[str, Any], gate_summary: dict[str, int]) -> dict[str, Any]:
        backend = spec.get("electronics", {}).get("backend", "reference")
        provider = spec.get("sourcing", {}).get("provider", "curated")
        selected_variant = spec.get("mechanical", {}).get("selected_variant")
        resolved = [item for item in graph.get("component_resolution", []) if item.get("resolution") == "curated"]
        score = 50 + gate_summary.get("pass", 0) * 2 - gate_summary.get("fail", 0) * 8 - gate_summary.get("blocked", 0) * 3
        score += min(10, len(resolved) // 4)
        blockers = []
        if gate_summary.get("fail", 0):
            blockers.append({"code": "failing_gates_present", "count": gate_summary["fail"]})
        if gate_summary.get("blocked", 0):
            blockers.append({"code": "blocked_gates_present", "count": gate_summary["blocked"]})
        if backend not in _RELEASE_ELIGIBLE_BACKENDS:
            blockers.append({"code": "backend_candidate_only", "backend": backend})
        return {
            "id": "baseline:current_generated_candidate",
            "axis": "baseline_gate_state",
            "title": "Current generated candidate",
            "score": score,
            "score_breakdown": {
                "passed_gates_bonus": gate_summary.get("pass", 0) * 2,
                "failing_gates_penalty": gate_summary.get("fail", 0) * -8,
                "blocked_gates_penalty": gate_summary.get("blocked", 0) * -3,
                "curated_component_bonus": min(10, len(resolved) // 4),
            },
            "patch": None,
            "metrics": {
                "backend": backend,
                "sourcing_provider": provider,
                "selected_mechanical_variant": selected_variant,
                "components": len(graph.get("components", [])),
                "nets": len(graph.get("nets", [])),
                "gate_summary": gate_summary,
            },
            "tradeoffs": ["Keeps the current generated cross-domain artifact set unchanged."],
            "blockers": blockers,
            "evidence": [
                "electronics/generated/electrical_graph.json",
                "electronics/generated/semantic/semantic_schematic.py",
                "validation/reports",
            ],
        }

    @staticmethod
    def _design_space_backend_candidates(spec: dict[str, Any], capabilities: dict[str, Any]) -> list[dict[str, Any]]:
        current = spec.get("electronics", {}).get("backend", "reference")
        backends = capabilities.get("backends", {})
        tools = capabilities.get("external_tools", {})
        tier_bonus = {
            "candidate": 0,
            "netlist": 8,
            "hdl_source": 10,
            "fabrication": 16,
        }
        candidates: list[dict[str, Any]] = []
        for name in ("reference", "python_netlist", "atopile", "tscircuit", "kicad"):
            info = backends.get(name, {"release_eligible": name in _RELEASE_ELIGIBLE_BACKENDS, "candidate_only": name not in _RELEASE_ELIGIBLE_BACKENDS})
            required_tool = info.get("requires_tool")
            tool_info = tools.get(required_tool, {}) if required_tool else {}
            tool_available = bool(tool_info.get("available")) if required_tool else True
            release_tier = capabilities.get("release_tiers", {}).get(name, _RELEASE_TIER_BY_BACKEND.get(name, "unknown"))
            score = 40 + tier_bonus.get(release_tier, 0)
            breakdown = {"release_tier_bonus": tier_bonus.get(release_tier, 0)}
            blockers: list[dict[str, Any]] = []
            tradeoffs: list[str] = []
            if name == current:
                score += 4
                breakdown["currently_selected_bonus"] = 4
            if info.get("release_eligible"):
                score += 12
                breakdown["release_eligible_bonus"] = 12
            else:
                score -= 14
                breakdown["candidate_only_penalty"] = -14
                blockers.append({"code": "backend_candidate_only", "backend": name})
            if required_tool:
                if tool_available:
                    score += 8
                    breakdown["required_tool_available_bonus"] = 8
                else:
                    score -= 16
                    breakdown["required_tool_missing_penalty"] = -16
                    blockers.append({"code": "required_tool_missing", "tool": required_tool, "backend": name})
            if name == "python_netlist":
                tradeoffs.append("Produces deterministic compiled netlist evidence; PCB layout/manufacturing remains a separate bridge.")
            elif name == "atopile":
                tradeoffs.append("Produces HDL-source release evidence; fabrication output still depends on a KiCad bridge.")
            elif name in {"tscircuit", "kicad"}:
                tradeoffs.append("Targets fabrication release evidence when native backend gates and physical qualification pass.")
            else:
                tradeoffs.append("Fastest candidate-only path for inspection; cannot be promoted without switching backend.")
            candidates.append({
                "id": f"backend:{name}",
                "axis": "electronics_backend",
                "title": f"Use {name} electronics backend",
                "score": score,
                "score_breakdown": breakdown,
                "patch": {"section": "system", "spec_path": "electronics.backend", "value": name, "operation": "replace"},
                "metrics": {
                    "backend": name,
                    "selected": name == current,
                    "release_eligible": bool(info.get("release_eligible")),
                    "release_tier": release_tier,
                    "required_tool": required_tool,
                    "required_tool_available": tool_available,
                    "contract_stages": list(CONTRACT_STAGES) if name != "reference" else [],
                },
                "tradeoffs": tradeoffs,
                "blockers": blockers,
                "evidence": ["hw_get_capabilities", "spec/system.yaml"],
            })
        return candidates

    def _design_space_component_candidates(self, spec: dict[str, Any], graph: dict[str, Any]) -> list[dict[str, Any]]:
        role_set = spec.get("electronics", {}).get("role_set", "robotics_controller")
        role_path = self.parts_root / "role_sets" / f"{role_set}.yaml"
        if not role_path.is_file():
            return []
        role_data = read_yaml(role_path)
        alternatives = role_data.get("alternatives") or {}
        if not alternatives:
            return []
        database = self._design_space_component_database()
        evidence = self._design_space_evidence_by_component()
        provider = spec.get("sourcing", {}).get("provider", "curated")
        supplier_records = self._design_space_supplier_records(provider)
        try:
            adapter = supplier_adapter(provider)
        except ValueError:
            adapter = None
        affected_refs: dict[str, list[str]] = {}
        for component in graph.get("components", []):
            affected_refs.setdefault(role_for_component(component), []).append(component.get("ref", ""))
        candidates: list[dict[str, Any]] = []
        for role, alternate_items in alternatives.items():
            selected_id = (role_data.get("roles", {}).get(role) or {}).get("component_id")
            selected = database.get(selected_id)
            if not selected:
                continue
            for alternate in alternate_items:
                alt_id = alternate.get("component_id")
                if not alt_id or alt_id == selected_id:
                    continue
                alt = database.get(alt_id)
                blockers: list[dict[str, Any]] = []
                tradeoffs: list[str] = []
                breakdown: dict[str, int] = {}
                score = 42
                if not alt:
                    blockers.append({"code": "alternate_component_missing", "component_id": alt_id})
                    candidates.append(self._design_space_component_candidate(role, selected, {"id": alt_id}, alternate, affected_refs.get(role, []), score - 30, breakdown, tradeoffs, blockers, provider))
                    continue

                footprint_match = selected.get("footprint", {}).get("library_id") == alt.get("footprint", {}).get("library_id")
                pads_match = sorted(selected.get("footprint", {}).get("expected_pads", [])) == sorted(alt.get("footprint", {}).get("expected_pads", []))
                pin_numbers_match = sorted(str(pin.get("number")) for pin in selected.get("pins", [])) == sorted(str(pin.get("number")) for pin in alt.get("pins", []))
                pin_names_match = sorted(pin.get("name") for pin in selected.get("pins", [])) == sorted(pin.get("name") for pin in alt.get("pins", []))
                if footprint_match and pads_match:
                    score += 14
                    breakdown["footprint_contract_bonus"] = 14
                else:
                    score -= 20
                    breakdown["footprint_contract_penalty"] = -20
                    blockers.append({"code": "footprint_contract_mismatch", "selected": selected_id, "alternate": alt_id})
                if pin_numbers_match:
                    score += 10
                    breakdown["pin_number_contract_bonus"] = 10
                else:
                    score -= 20
                    breakdown["pin_number_contract_penalty"] = -20
                    blockers.append({"code": "pin_number_contract_mismatch", "selected": selected_id, "alternate": alt_id})
                if not pin_names_match:
                    score -= 4
                    breakdown["pin_name_review_penalty"] = -4
                    blockers.append({"code": "pin_name_review_required", "selected": selected_id, "alternate": alt_id})
                approved_evidence = [
                    item for item in evidence.get(alt_id, [])
                    if item.get("review_status") == "approved" and {"pins", "package", "footprint"}.issubset(set(item.get("supports", [])))
                ]
                if approved_evidence:
                    score += 8
                    breakdown["datasheet_evidence_bonus"] = 8
                else:
                    score -= 12
                    breakdown["datasheet_evidence_penalty"] = -12
                    blockers.append({"code": "datasheet_evidence_missing", "alternate": alt_id})
                if alt.get("lifecycle") == "active":
                    score += 5
                    breakdown["active_lifecycle_bonus"] = 5
                else:
                    score -= 10
                    breakdown["lifecycle_penalty"] = -10
                    blockers.append({"code": "alternate_lifecycle_not_active", "alternate": alt_id, "lifecycle": alt.get("lifecycle")})
                record = supplier_records.get(alt_id)
                offer = adapter.normalize(record).to_dict() if adapter and record else None
                if not offer:
                    blockers.append({"code": "supplier_record_missing", "provider": provider, "alternate": alt_id})
                    breakdown["supplier_record_penalty"] = -8
                    score -= 8
                elif offer.get("availability") == "available" and offer.get("observed_at") and not self._design_space_supplier_observation_stale(offer["observed_at"]):
                    breakdown["supplier_available_bonus"] = 8
                    score += 8
                else:
                    breakdown["supplier_freshness_penalty"] = -4
                    score -= 4
                    blockers.append({"code": "supplier_freshness_required", "provider": provider, "availability": offer.get("availability"), "observed_at": offer.get("observed_at")})
                required_reviews = list(alternate.get("required_reviews") or [])
                if required_reviews:
                    score -= 3 * len(required_reviews)
                    breakdown["required_review_penalty"] = -3 * len(required_reviews)
                    blockers.append({"code": "functional_review_required", "reviews": required_reviews})
                tradeoffs.append(alternate.get("rationale", "Curated role alternative requires review before use."))
                candidates.append(self._design_space_component_candidate(role, selected, alt, alternate, affected_refs.get(role, []), score, breakdown, tradeoffs, blockers, provider))
        return candidates

    @staticmethod
    def _design_space_component_candidate(
        role: str,
        selected: dict[str, Any],
        alternate: dict[str, Any],
        alternate_rule: dict[str, Any],
        affected_refs: list[str],
        score: int,
        score_breakdown: dict[str, int],
        tradeoffs: list[str],
        blockers: list[dict[str, Any]],
        provider: str,
    ) -> dict[str, Any]:
        selected_id = selected.get("id")
        alt_id = alternate.get("id") or alternate_rule.get("component_id")
        return {
            "id": f"component:{role}:{alt_id}",
            "axis": "component_alternative",
            "title": f"Use {alternate.get('mpn', alt_id)} for role {role}",
            "score": score,
            "score_breakdown": score_breakdown,
            "patch": {
                "section": "system",
                "spec_path": f"electronics.role_overrides.{role}.component_id",
                "value": alt_id,
                "operation": "replace",
                "role_override": {
                    "role": role,
                    "component_id": alt_id,
                    "resolution": alternate_rule.get("resolution", "curated"),
                    "reason": alternate_rule.get("rationale"),
                },
            },
            "metrics": {
                "role": role,
                "selected_component_id": selected_id,
                "alternate_component_id": alt_id,
                "selected_mpn": selected.get("mpn"),
                "alternate_mpn": alternate.get("mpn"),
                "affected_refs": sorted(ref for ref in affected_refs if ref),
                "provider": provider,
                "compatibility": alternate_rule.get("compatibility", {}),
            },
            "tradeoffs": tradeoffs,
            "blockers": blockers,
            "evidence": [
                "parts/role_sets",
                "parts/components",
                "spec/system.yaml",
                "parts/evidence/datasheets.yaml",
                f"parts/suppliers/{provider}.yaml",
            ],
        }

    def _design_space_component_database(self) -> dict[str, dict[str, Any]]:
        database: dict[str, dict[str, Any]] = {}
        for path in sorted((self.parts_root / "components").glob("*.yaml")):
            payload = read_yaml(path)
            for item in payload.get("components", []):
                database[item["id"]] = item
        return database

    def _design_space_evidence_by_component(self) -> dict[str, list[dict[str, Any]]]:
        path = self.parts_root / "evidence" / "datasheets.yaml"
        if not path.is_file():
            return {}
        payload = read_yaml(path)
        by_component: dict[str, list[dict[str, Any]]] = {}
        for item in payload.get("evidence", []):
            by_component.setdefault(item.get("component_id", ""), []).append(item)
        return by_component

    def _design_space_supplier_records(self, provider: str) -> dict[str, dict[str, Any]]:
        path = self.parts_root / "suppliers" / f"{provider}.yaml"
        if not path.is_file():
            return {}
        payload = read_yaml(path)
        return {item.get("component_id"): item for item in payload.get("records", []) if item.get("component_id")}

    @staticmethod
    def _design_space_mechanical_candidates(spec: dict[str, Any], mechanical_contract: dict[str, Any]) -> list[dict[str, Any]]:
        mechanical = spec.get("mechanical", {})
        actuation = spec.get("actuation", {})
        selected = mechanical.get("selected_variant")
        high_vibration = mechanical.get("vibration_environment") == "high"
        connectors_exposed = bool(mechanical.get("connectors_exposed", True))
        motor_load = int(actuation.get("motor_channels") or 0) > 0
        contract_variants = {item.get("name"): item for item in mechanical_contract.get("variants", []) if item.get("name")}
        candidates: list[dict[str, Any]] = []
        for variant in mechanical.get("variants", []):
            name = variant.get("name")
            if not name:
                continue
            score = 55
            tradeoffs: list[str] = []
            blockers: list[dict[str, Any]] = []
            breakdown: dict[str, int] = {}
            if name == selected:
                breakdown["currently_selected_bonus"] = 4
                score += 4
            if variant.get("gasket"):
                breakdown["ingress_gasket_bonus"] = 12
                score += 12
                tradeoffs.append("Gasketed lid improves ingress and contamination margin.")
            elif connectors_exposed:
                breakdown["exposed_connector_ingress_penalty"] = -6
                score -= 6
                blockers.append({"code": "ingress_review_required", "reason": "Variant has no gasket while connectors are exposed."})
            ventilation = variant.get("ventilation", "none")
            if ventilation != "none":
                if motor_load:
                    breakdown["passive_cooling_bonus"] = 8
                    score += 8
                tradeoffs.append(f"Ventilation pattern {ventilation} may improve thermal margin but needs EMI/ingress review.")
                blockers.append({"code": "emi_ingress_review_required", "ventilation": ventilation})
            elif motor_load:
                breakdown["sealed_thermal_uncertainty_penalty"] = -4
                score -= 4
                blockers.append({"code": "thermal_airflow_unknown", "reason": "Sealed motor-controller enclosure needs thermal load evidence."})
            if high_vibration and variant.get("lid_style") == "snap":
                breakdown["snap_lid_vibration_penalty"] = -8
                score -= 8
                blockers.append({"code": "vibration_retention_review_required", "lid_style": "snap"})
            contract_variant = contract_variants.get(name, {})
            candidates.append({
                "id": f"mechanical:{name}",
                "axis": "mechanical_enclosure_variant",
                "title": f"Use {name} enclosure variant",
                "score": score,
                "score_breakdown": breakdown,
                "patch": {"section": "system", "spec_path": "mechanical.selected_variant", "value": name, "operation": "replace"},
                "metrics": {
                    "selected": name == selected,
                    "lid_style": variant.get("lid_style"),
                    "ventilation": ventilation,
                    "gasket": bool(variant.get("gasket")),
                    "contract_available": bool(contract_variant),
                },
                "tradeoffs": tradeoffs or ["No special mechanical tradeoff identified from the typed variant fields."],
                "blockers": blockers,
                "evidence": ["spec/system.yaml", "mechanical/source/mechanical_contract.json"],
            })
        return candidates

    def _design_space_supplier_candidates(self, spec: dict[str, Any], graph: dict[str, Any]) -> list[dict[str, Any]]:
        current_provider = spec.get("sourcing", {}).get("provider", "curated")
        component_ids = sorted({item.get("component_id") for item in graph.get("components", []) if item.get("component_id")})
        supplier_dir = self.parts_root / "suppliers"
        candidates: list[dict[str, Any]] = []
        for supplier_path in sorted(supplier_dir.glob("*.yaml")):
            catalog = read_yaml(supplier_path)
            provider = catalog.get("provider", supplier_path.stem)
            records = {item.get("component_id"): item for item in catalog.get("records", []) if item.get("component_id")}
            try:
                adapter = supplier_adapter(provider)
            except ValueError as exc:
                candidates.append({
                    "id": f"sourcing:{provider}",
                    "axis": "supplier_provider",
                    "title": f"Resolve sourcing through {provider}",
                    "score": 0,
                    "score_breakdown": {"unsupported_provider_penalty": -50},
                    "patch": {"section": "system", "spec_path": "sourcing.provider", "value": provider, "operation": "replace"},
                    "metrics": {"provider": provider, "components": len(component_ids)},
                    "tradeoffs": ["Supplier adapter is not available."],
                    "blockers": [{"code": "supplier_adapter_unavailable", "message": str(exc)}],
                    "evidence": [str(supplier_path.relative_to(self.parts_root.parent))],
                })
                continue
            available = unknown = missing = unavailable = stale = fresh = 0
            blockers: list[dict[str, Any]] = []
            for component_id in component_ids:
                record = records.get(component_id)
                if not record:
                    missing += 1
                    continue
                offer = adapter.normalize(record).to_dict()
                availability = offer.get("availability")
                if availability == "available":
                    available += 1
                    observed_at = offer.get("observed_at")
                    if observed_at and not self._design_space_supplier_observation_stale(observed_at):
                        fresh += 1
                    else:
                        stale += 1
                elif availability in {"out_of_stock", "discontinued"}:
                    unavailable += 1
                else:
                    unknown += 1
            if missing:
                blockers.append({"code": "supplier_records_missing", "count": missing})
            if unknown:
                blockers.append({"code": "supplier_availability_unknown", "count": unknown})
            if stale:
                blockers.append({"code": "supplier_evidence_not_current", "count": stale})
            if unavailable:
                blockers.append({"code": "supplier_unavailable", "count": unavailable})
            score = 45 + available * 3 + fresh * 2 - missing * 2 - unknown - stale * 2 - unavailable * 8
            if provider == current_provider:
                score += 3
            candidates.append({
                "id": f"sourcing:{provider}",
                "axis": "supplier_provider",
                "title": f"Resolve sourcing through {provider}",
                "score": score,
                "score_breakdown": {
                    "available_bonus": available * 3,
                    "fresh_observation_bonus": fresh * 2,
                    "missing_record_penalty": missing * -2,
                    "unknown_availability_penalty": -unknown,
                    "stale_or_missing_observation_penalty": stale * -2,
                    "unavailable_penalty": unavailable * -8,
                    "current_provider_bonus": 3 if provider == current_provider else 0,
                },
                "patch": {"section": "system", "spec_path": "sourcing.provider", "value": provider, "operation": "replace"},
                "metrics": {
                    "provider": provider,
                    "components_checked": len(component_ids),
                    "records_present": len(component_ids) - missing,
                    "available": available,
                    "fresh_observations": fresh,
                    "unknown": unknown,
                    "stale_or_missing_observation": stale,
                    "unavailable": unavailable,
                    "missing": missing,
                },
                "tradeoffs": [
                    "Supplier-provider switch changes availability evidence source, not the resolved electrical component IDs.",
                    "Fresh availability evidence is required before release promotion can rely on this sourcing path.",
                ],
                "blockers": blockers,
                "evidence": [str(supplier_path.relative_to(self.parts_root.parent))],
            })
        return candidates

    @staticmethod
    def _design_space_supplier_observation_stale(observed_at: str) -> bool:
        try:
            timestamp = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
        except ValueError:
            return True
        return datetime.now(UTC) - timestamp > timedelta(days=30)

    @staticmethod
    def _candidate_semantic_representation(project_path: Path, files: dict[str, list[str]]) -> dict[str, Any]:
        electrical_graph = project_path / "electronics" / "generated" / "electrical_graph.json"
        semantic_schematic = project_path / "electronics" / "generated" / "semantic" / "semantic_schematic.json"
        semantic_code = project_path / "electronics" / "generated" / "semantic" / "semantic_schematic.py"
        mechanical_contract = project_path / "mechanical" / "source" / "mechanical_contract.json"
        firmware_pinmap = project_path / "firmware" / "generated" / "pinmap.json"
        return {
            "authoring_model": "semantic-first",
            "purpose": (
                "LLM-facing semantic representation. Native EDA, CAD, firmware, "
                "and manufacturing outputs are generated from typed artifacts rather "
                "than treated as the primary prompt surface."
            ),
            "layers": {
                "requirements": [item.as_posix() for item in sorted((project_path / "spec").glob("*.yaml"))],
                "electronics_graph": electrical_graph.as_posix() if electrical_graph.is_file() else None,
                "semantic_schematic": semantic_schematic.as_posix() if semantic_schematic.is_file() else None,
                "semantic_schematic_code": semantic_code.as_posix() if semantic_code.is_file() else None,
                "electronics_intent": files.get("electronics", []),
                "relative_placement": {
                    "source": semantic_schematic.as_posix() if semantic_schematic.is_file() else (electrical_graph.as_posix() if electrical_graph.is_file() else None),
                    "style": "constraint-derived component positions with provenance, not raw native PCB geometry",
                },
                "mechanical_contract": mechanical_contract.as_posix() if mechanical_contract.is_file() else None,
                "firmware_pinmap": firmware_pinmap.as_posix() if firmware_pinmap.is_file() else None,
            },
            "native_outputs": {
                "kicad_and_fabrication": "generated evidence artifacts; not the primary authoring representation",
                "cad_exports": "generated from the mechanical contract and board STEP evidence",
                "firmware_build_inputs": "generated from the same pinmap used by electrical and firmware parity checks",
            },
            "representation_contract": {
                "pin_wiring": "nets enumerate component refs plus pin names, pin numbers, roles, and MCU pins; semantic_schematic.py round-trips to the JSON model",
                "placement": "placements and constraints preserve provenance for relative layout reasoning",
                "grounding": "component records carry curated component IDs, MPNs, packages, and footprints",
            },
        }

    @staticmethod
    def _candidate_sourcing_choices(component_resolution: list[dict[str, Any]]) -> list[dict[str, Any]]:
        choices: list[dict[str, Any]] = []
        for item in component_resolution:
            data = item.get("data") or {}
            offer = data.get("supplier_offer") or item.get("provenance", {}).get("supplier_record") or {}
            evidence = data.get("datasheet_evidence") or []
            choices.append({
                "ref": item.get("ref"),
                "role": item.get("role"),
                "component_id": item.get("component_id"),
                "resolution": item.get("resolution"),
                "manufacturer": data.get("manufacturer"),
                "mpn": data.get("mpn"),
                "package": data.get("package"),
                "lifecycle": data.get("lifecycle"),
                "sourcing_status": (data.get("sourcing") or {}).get("status"),
                "supplier": {
                    "provider": offer.get("provider"),
                    "sku": offer.get("sku"),
                    "availability": offer.get("availability"),
                    "stock": offer.get("stock"),
                    "observed_at": offer.get("observed_at"),
                },
                "datasheet_evidence_ids": [entry.get("id") for entry in evidence if entry.get("id")],
            })
        return choices

    @staticmethod
    def _semantic_schematic_roundtrip_report(project_path: Path, graph: dict[str, Any]) -> GateReport:
        graph_path = project_path / "electronics" / "generated" / "electrical_graph.json"
        semantic_path = project_path / "electronics" / "generated" / "semantic" / "semantic_schematic.json"
        code_path = project_path / "electronics" / "generated" / "semantic" / "semantic_schematic.py"
        artifacts = [str(path) for path in (semantic_path, code_path) if path.is_file()]
        missing = [str(path.relative_to(project_path)) for path in (graph_path, semantic_path, code_path) if not path.is_file()]
        if missing:
            return GateReport(
                "semantic_schematic_roundtrip",
                Status.BLOCKED,
                [Failure(
                    FailureCategory.EDA_ERROR,
                    "semantic_schematic_artifact_missing",
                    "Generate electronics before checking semantic schematic round-trip integrity",
                    path="electronics.generated.semantic",
                    details={"missing": missing},
                )],
                metrics={"missing_artifacts": missing},
                artifacts=artifacts,
                backend={"name": "semantic_schematic_dsl", "deterministic": True},
            )

        failures: list[Failure] = []
        semantic_json: dict[str, Any] | None = None
        semantic_code: dict[str, Any] | None = None
        try:
            loaded = json.loads(semantic_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                semantic_json = loaded
            else:
                failures.append(Failure(FailureCategory.EDA_ERROR, "semantic_schematic_json_invalid", "semantic_schematic.json must contain an object", path="electronics.generated.semantic_schematic"))
        except Exception as exc:
            failures.append(Failure(FailureCategory.EDA_ERROR, "semantic_schematic_json_invalid", f"Could not read semantic_schematic.json: {exc}", path="electronics.generated.semantic_schematic"))

        try:
            # Run in a subprocess for process isolation: prevents modified/malicious
            # file content from affecting the server's own memory or state.
            wrapper = (
                "import json, sys\n"
                f"exec(open({str(code_path)!r}, encoding='utf-8').read())\n"
                "sys.stdout.write(json.dumps(semantic_schematic))\n"
            )
            proc = subprocess.run(
                [sys.executable, "-c", wrapper],
                capture_output=True, text=True, timeout=15,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or f"exited with status {proc.returncode}")
            loaded = json.loads(proc.stdout)
            if isinstance(loaded, dict):
                semantic_code = loaded
            else:
                failures.append(Failure(FailureCategory.EDA_ERROR, "semantic_schematic_code_invalid", "semantic_schematic.py must define semantic_schematic as an object", path="electronics.generated.semantic_schematic_code"))
        except subprocess.TimeoutExpired:
            failures.append(Failure(FailureCategory.EDA_ERROR, "semantic_schematic_code_invalid", "semantic_schematic.py timed out after 15s", path="electronics.generated.semantic_schematic_code"))
        except Exception as exc:
            failures.append(Failure(FailureCategory.EDA_ERROR, "semantic_schematic_code_invalid", f"Could not execute semantic_schematic.py: {exc}", path="electronics.generated.semantic_schematic_code"))

        if semantic_json is not None and semantic_code is not None and semantic_json != semantic_code:
            changed = sorted({*semantic_json.keys(), *semantic_code.keys()} - {key for key in semantic_json.keys() & semantic_code.keys() if semantic_json.get(key) == semantic_code.get(key)})
            failures.append(Failure(
                FailureCategory.EDA_ERROR,
                "semantic_schematic_roundtrip_mismatch",
                "semantic_schematic.py does not round-trip to semantic_schematic.json",
                path="electronics.generated.semantic",
                details={"differing_top_level_keys": changed[:10]},
            ))

        if semantic_json is not None:
            graph_components = {item.get("ref"): item for item in graph.get("components", []) if item.get("ref")}
            semantic_components = {item.get("ref"): item for item in semantic_json.get("components", []) if item.get("ref")}
            graph_refs = set(graph_components)
            semantic_refs = set(semantic_components)
            if graph_refs != semantic_refs:
                failures.append(Failure(
                    FailureCategory.EDA_ERROR,
                    "semantic_component_set_mismatch",
                    "Semantic schematic component refs do not match the electrical graph",
                    path="electronics.generated.semantic.components",
                    details={"missing_refs": sorted(graph_refs - semantic_refs), "extra_refs": sorted(semantic_refs - graph_refs)},
                ))
            footprint_mismatches = [
                {"ref": ref, "graph": graph_components[ref].get("footprint"), "semantic": semantic_components[ref].get("footprint")}
                for ref in sorted(graph_refs & semantic_refs)
                if graph_components[ref].get("footprint") != semantic_components[ref].get("footprint")
            ]
            if footprint_mismatches:
                failures.append(Failure(
                    FailureCategory.EDA_ERROR,
                    "semantic_component_footprint_mismatch",
                    "Semantic schematic component footprints do not match the electrical graph",
                    path="electronics.generated.semantic.components",
                    details={"mismatches": footprint_mismatches[:10]},
                ))

            graph_nets = {item.get("name"): item for item in graph.get("nets", []) if item.get("name")}
            semantic_nets = {item.get("name"): item for item in semantic_json.get("nets", []) if item.get("name")}
            graph_net_names = set(graph_nets)
            semantic_net_names = set(semantic_nets)
            if graph_net_names != semantic_net_names:
                failures.append(Failure(
                    FailureCategory.EDA_ERROR,
                    "semantic_net_set_mismatch",
                    "Semantic schematic net names do not match the electrical graph",
                    path="electronics.generated.semantic.nets",
                    details={"missing_nets": sorted(graph_net_names - semantic_net_names), "extra_nets": sorted(semantic_net_names - graph_net_names)},
                ))

            connection_mismatches: list[dict[str, Any]] = []
            missing_pin_names: list[dict[str, Any]] = []
            for name in sorted(graph_net_names & semantic_net_names):
                graph_connections = set()
                for pin_ref in graph_nets[name].get("connected_pins", []):
                    ref, sep, number = str(pin_ref).partition(".")
                    graph_connections.add((ref, number if sep else ""))
                semantic_connections = {
                    (str(item.get("component_ref")), str(item.get("pin_number") or ""))
                    for item in semantic_nets[name].get("pin_name_connections", [])
                }
                if graph_connections != semantic_connections:
                    connection_mismatches.append({
                        "net": name,
                        "missing": sorted(graph_connections - semantic_connections),
                        "extra": sorted(semantic_connections - graph_connections),
                    })
                for item in semantic_nets[name].get("pin_name_connections", []):
                    if not item.get("pin_name"):
                        missing_pin_names.append({"net": name, "component_ref": item.get("component_ref"), "pin_number": item.get("pin_number")})
            if connection_mismatches:
                failures.append(Failure(
                    FailureCategory.EDA_ERROR,
                    "semantic_net_connection_mismatch",
                    "Semantic schematic pin-name connections do not match graph connected_pins",
                    path="electronics.generated.semantic.nets",
                    details={"mismatches": connection_mismatches[:10]},
                ))
            if missing_pin_names:
                failures.append(Failure(
                    FailureCategory.EDA_ERROR,
                    "semantic_pin_name_missing",
                    "Semantic schematic connections must preserve pin names",
                    path="electronics.generated.semantic.nets",
                    details={"connections": missing_pin_names[:10]},
                ))

        total_connections = 0
        if semantic_json is not None:
            total_connections = sum(len(net.get("pin_name_connections", [])) for net in semantic_json.get("nets", []))
        return GateReport(
            "semantic_schematic_roundtrip",
            Status.FAIL if failures else Status.PASS,
            failures,
            metrics={
                "components": len((semantic_json or {}).get("components", [])),
                "nets": len((semantic_json or {}).get("nets", [])),
                "pin_name_connections": total_connections,
                "code_roundtrip_exact": semantic_json is not None and semantic_code is not None and semantic_json == semantic_code,
            },
            artifacts=artifacts,
            backend={"name": "semantic_schematic_dsl", "deterministic": True},
        )

    @staticmethod
    def _candidate_grounding_summary(component_resolution: list[dict[str, Any]], checks: dict[str, Any]) -> dict[str, Any]:
        reports = {item.get("gate"): item for item in checks.get("reports", [])}

        def gate(name: str) -> dict[str, Any]:
            report = reports.get(name)
            if not report:
                return {"gate": name, "status": "missing", "failure_count": 0}
            return {
                "gate": name,
                "status": report.get("status", "missing"),
                "failure_count": len(report.get("failures", [])),
            }

        def area(label: str, gates: list[str]) -> dict[str, Any]:
            entries = [gate(name) for name in gates]
            statuses = [entry["status"] for entry in entries]
            if any(status == "fail" for status in statuses):
                status = "fail"
            elif any(status in {"blocked", "missing"} for status in statuses):
                status = "blocked"
            elif statuses and all(status == "pass" for status in statuses):
                status = "pass"
            else:
                status = "blocked"
            return {"area": label, "status": status, "gates": entries}

        component_total = len(component_resolution)
        curated_count = sum(1 for item in component_resolution if item.get("resolution") == "curated")
        datasheet_count = sum(1 for item in component_resolution if (item.get("data") or {}).get("datasheet_evidence"))
        supplier_known_count = 0
        supplier_unknown_refs: list[str] = []
        for item in component_resolution:
            data = item.get("data") or {}
            offer = data.get("supplier_offer") or item.get("provenance", {}).get("supplier_record") or {}
            if offer.get("availability") == "available":
                supplier_known_count += 1
            elif item.get("ref"):
                supplier_unknown_refs.append(item["ref"])

        return {
            "component_grounding": {
                "total": component_total,
                "curated": curated_count,
                "with_datasheet_evidence": datasheet_count,
                "with_available_supplier_offer": supplier_known_count,
                "unresolved_refs": [item.get("ref") for item in component_resolution if item.get("resolution") != "curated" and item.get("ref")],
                "supplier_unknown_refs": supplier_unknown_refs,
            },
            "risk_areas": [
                area("pinout_package_footprint", ["datasheet_evidence", "component_provenance", "pin_symbol_footprint"]),
                area("semantic_representation_integrity", ["semantic_schematic_roundtrip"]),
                area("component_availability_lifecycle", ["component_resolution", "supplier_availability", "sourcing", "sourcing_resilience", "component_provenance"]),
                area("support_circuit_and_power_assumptions", ["requirements_lowering", "semantic_electrical", "power_tree_integrity", "power_integrity_estimate", "interface_integrity", "support_circuit_completeness", "ir_erc", "bom"]),
                area("long_horizon_dependency_integrity", ["design_dependency_graph"]),
                area("layout_routing_manufacturability", ["placement_constraints", "layout_thermal_integrity", "layout_signal_integrity", "ir_pcb_sanity", "reference_fabrication", "compiled_electronics_backend"]),
                area("mechanical_integration", ["mechanical_fit", "mechanical_connector_retention", "mechanical_connector_cutouts", "mechanical_mounting_integrity"]),
                area("firmware_interface", ["firmware_pinmap", "firmware_modules", "hw_sw_parity", "firmware_interface_contract", "reference_firmware_build"]),
                area("physical_qualification_evidence", ["physical_qualification"]),
            ],
            "physical_oracle_gaps": [
                "SI/PI, EMI/EMC, thermal load behavior, vibration, ingress, connector fatigue, and board bring-up still require simulation or physical evidence outside this digital candidate.",
            ],
        }

    def _candidate_critic_report(
        self,
        project_path: Path,
        spec: dict[str, Any],
        graph: dict[str, Any],
        reports: list[GateReport],
        *,
        include_external: bool,
    ) -> GateReport:
        """Second-pass critic over the candidate as a whole, not a single domain gate."""
        backend = spec.get("electronics", {}).get("backend", "reference")
        by_gate = {report.gate: report for report in reports}
        failures: list[Failure] = []

        graph_provenance = graph.get("provenance") or {}
        if graph_provenance.get("release_eligible"):
            failures.append(Failure(
                FailureCategory.RELEASE_ERROR,
                "candidate_graph_claims_release_eligible",
                "Generated electrical graph claims release eligibility before promotion gates ran",
                path="electronics.generated.electrical_graph.provenance.release_eligible",
                details={"backend": backend},
            ))

        if backend not in _RELEASE_ELIGIBLE_BACKENDS:
            backend_gate = by_gate.get("compiled_electronics_backend")
            if backend_gate is None or backend_gate.status == Status.PASS:
                failures.append(Failure(
                    FailureCategory.RELEASE_ERROR,
                    "candidate_only_backend_not_blocked",
                    f"Candidate-only backend {backend} is not blocked by compiled_electronics_backend",
                    path="electronics.backend",
                    details={"backend": backend, "gate_status": backend_gate.status.value if backend_gate else "missing"},
                ))

        ref_fab = by_gate.get("reference_fabrication")
        if ref_fab and ref_fab.status == Status.PASS and ref_fab.backend.get("release_eligible"):
            failures.append(Failure(
                FailureCategory.RELEASE_ERROR,
                "reference_fabrication_claims_release_eligible",
                "Reference fabrication artifacts are candidate previews and must not claim release eligibility",
                path="exports.candidates.reference-fabrication",
                details={"backend": ref_fab.backend},
            ))

        physical = by_gate.get("physical_qualification")
        physical_gap_categories: list[str] = []
        if physical and physical.status != Status.PASS:
            physical_gap_categories = sorted({
                str(failure.details.get("category"))
                for failure in physical.failures
                if failure.code == "physical_evidence_missing" and failure.details.get("category")
            })
            failures.append(Failure(
                FailureCategory.RELEASE_ERROR,
                "physical_oracle_gap_open",
                "Candidate still requires external physical qualification evidence; this is not a software gate failure",
                severity="warning",
                path="validation.physical",
                details={
                    "physical_qualification_status": physical.status.value,
                    "gap_categories": physical_gap_categories,
                    "required_tests": physical.metrics.get("required_tests") if physical.metrics else None,
                },
            ))

        native_not_run = sorted(
            gate for gate, report in by_gate.items()
            if gate in _EXTERNAL_GATES
            and any(failure.code == "external_gate_not_run" for failure in report.failures)
        )
        if native_not_run:
            failures.append(Failure(
                FailureCategory.TOOL_ERROR,
                "native_toolchain_gates_not_run",
                "Native toolchain gates were not run; candidate cannot be release-reviewed as fabrication-authoritative",
                severity="warning",
                path="validation.reports",
                details={"include_external": include_external, "gates": native_not_run},
            ))

        error_count = sum(1 for failure in failures if failure.severity == "error")
        warning_count = sum(1 for failure in failures if failure.severity == "warning")
        return GateReport(
            "candidate_critic",
            Status.FAIL if error_count else Status.PASS,
            failures,
            metrics={
                "critic_version": "candidate_critic_v0",
                "errors": error_count,
                "warnings": warning_count,
                "backend": backend,
                "include_external": include_external,
                "physical_gap_categories": physical_gap_categories,
                "native_toolchain_gates_not_run": native_not_run,
            },
            backend={"name": "candidate-critic", "deterministic": True},
        )

    @staticmethod
    def _design_dependency_graph_report(reports: list[GateReport]) -> GateReport:
        dependencies: dict[str, list[str]] = {
            "requirements_lowering": ["spec_schema"],
            "semantic_electrical": ["spec_schema", "requirements_lowering"],
            "component_resolution": ["spec_schema"],
            "semantic_schematic_roundtrip": ["semantic_electrical", "component_resolution"],
            "datasheet_evidence": ["component_resolution"],
            "supplier_availability": ["component_resolution"],
            "bom": ["component_resolution"],
            "sourcing": ["component_resolution"],
            "sourcing_resilience": ["component_resolution", "sourcing", "component_provenance"],
            "component_provenance": ["component_resolution", "datasheet_evidence"],
            "pin_symbol_footprint": ["component_resolution", "component_provenance"],
            "power_tree_integrity": ["pin_symbol_footprint", "component_provenance"],
            "power_integrity_estimate": ["power_tree_integrity", "pin_symbol_footprint"],
            "interface_integrity": ["pin_symbol_footprint", "power_tree_integrity"],
            "firmware_pinmap": ["pin_symbol_footprint", "semantic_schematic_roundtrip"],
            "firmware_modules": ["firmware_pinmap", "hw_sw_parity"],
            "support_circuit_completeness": ["component_resolution", "semantic_electrical", "semantic_schematic_roundtrip", "power_tree_integrity", "interface_integrity"],
            "hw_sw_parity": ["firmware_pinmap", "pin_symbol_footprint", "semantic_schematic_roundtrip", "interface_integrity"],
            "firmware_interface_contract": ["firmware_pinmap", "hw_sw_parity", "interface_integrity"],
            "reference_firmware_build": ["firmware_pinmap", "firmware_modules", "firmware_interface_contract"],
            "placement_constraints": ["pin_symbol_footprint", "semantic_schematic_roundtrip"],
            "layout_thermal_integrity": ["placement_constraints", "power_tree_integrity", "interface_integrity"],
            "layout_signal_integrity": ["placement_constraints", "interface_integrity"],
            "ir_pcb_sanity": ["pin_symbol_footprint", "placement_constraints", "layout_thermal_integrity", "layout_signal_integrity", "semantic_schematic_roundtrip"],
            "reference_fabrication": ["ir_pcb_sanity", "placement_constraints", "layout_thermal_integrity", "layout_signal_integrity"],
            "compiled_electronics_backend": ["pin_symbol_footprint", "semantic_schematic_roundtrip"],
            "autoroute": ["ir_pcb_sanity"],
            "native_erc": ["pin_symbol_footprint"],
            "native_drc": ["ir_pcb_sanity"],
            "kicad_library_crosscheck": ["native_erc", "native_drc"],
            "mechanical_connector_cutouts": ["mechanical_fit", "placement_constraints"],
            "mechanical_mounting_integrity": ["mechanical_fit", "placement_constraints"],
            "native_mechanical_validation": ["mechanical_fit", "mechanical_connector_retention", "mechanical_connector_cutouts", "mechanical_mounting_integrity", "reference_fabrication"],
            "native_zephyr_build": ["firmware_pinmap", "firmware_modules", "firmware_interface_contract", "reference_firmware_build"],
            "physical_qualification": ["reference_fabrication", "reference_firmware_build", "firmware_modules", "firmware_interface_contract", "mechanical_fit", "mechanical_connector_retention", "mechanical_connector_cutouts", "mechanical_mounting_integrity", "layout_thermal_integrity", "layout_signal_integrity"],
        }
        by_gate = {report.gate: report for report in reports}
        failures: list[Failure] = []
        evaluated_edges = 0
        blocked_nodes: list[str] = []
        for gate, prereqs in dependencies.items():
            report = by_gate.get(gate)
            if not report:
                continue
            if report.status != Status.PASS:
                blocked_nodes.append(gate)
                continue
            for prereq in prereqs:
                evaluated_edges += 1
                prereq_report = by_gate.get(prereq)
                if prereq_report is None or prereq_report.status != Status.PASS:
                    failures.append(Failure(
                        FailureCategory.RELEASE_ERROR,
                        "dependency_prerequisite_not_passed",
                        f"{gate} passed before prerequisite {prereq} passed",
                        path=f"gates.{gate}",
                        details={
                            "gate": gate,
                            "prerequisite": prereq,
                            "prerequisite_status": prereq_report.status.value if prereq_report else "missing",
                        },
                    ))
        return GateReport(
            "design_dependency_graph",
            Status.FAIL if failures else Status.PASS,
            failures,
            metrics={
                "nodes": len(by_gate),
                "declared_edges": sum(len(value) for value in dependencies.values()),
                "evaluated_edges": evaluated_edges,
                "blocked_or_unpassed_nodes": sorted(set(blocked_nodes)),
            },
            backend={"name": "structural_dependency_graph", "deterministic": True},
        )

    @staticmethod
    def _support_net_contracts(role_set_name: str) -> dict[str, dict[str, Any]]:
        contracts = {
            "robotics_controller": {
                "power_input": {"required_nets": {"VBAT_RAW", "GND"}},
                "fuse": {"required_nets": {"VBAT_RAW", "VBAT_FUSED"}},
                "reverse_polarity": {"required_nets": {"VBAT_FUSED", "VBAT", "GND"}},
                "tvs": {"required_nets": {"VBAT", "GND"}},
                "efuse": {"required_nets": {"VBAT", "VSYS", "GND", "ESTOP_GATE"}},
                "regulator_5v": {"required_nets": {"VSYS", "V5", "GND"}},
                "regulator_3v3": {"required_nets": {"V5", "V3V3", "GND"}},
                "mcu": {"required_nets": {"V3V3", "GND"}},
                "imu": {"required_nets": {"V3V3", "GND", "I2C_IMU_SCL", "I2C_IMU_SDA", "IMU_INT"}},
                "can": {"required_nets": {"V5", "GND", "CAN_RX", "CAN_TX", "CANH", "CANL"}},
                "estop": {"required_nets": {"V3V3", "GND", "ESTOP_IN"}},
                "safety_gate": {"required_nets": {"V3V3", "GND", "ESTOP_IN", "ESTOP_GATE"}},
                "can_connector": {"required_nets": {"CANH", "CANL", "GND"}},
                "motor_io": {
                    "required_nets": {"V5", "GND", "ESTOP_GATE"},
                    "required_motor_suffixes": {"_PWM", "_CURRENT", "_ENC"},
                },
            },
            "sensor_data_logger": {
                "power_input": {"required_nets": {"USB_VBUS", "GND", "USB_DP_RAW", "USB_DM_RAW"}},
                "fuse": {"required_nets": {"USB_VBUS", "USB_FUSED"}},
                "reverse_polarity": {"required_nets": {"USB_FUSED", "USB_PROT", "GND"}},
                "tvs": {"required_nets": {"USB_DP_RAW", "USB_DM_RAW", "USB_DP", "USB_DM", "GND"}},
                "regulator_3v3": {"required_nets": {"USB_PROT", "V3V3", "GND"}},
                "mcu": {"required_nets": {"V3V3", "GND", "USB_DP", "USB_DM", "I2C_IMU_SCL", "I2C_IMU_SDA"}},
                "imu": {"required_nets": {"V3V3", "GND", "I2C_IMU_SCL", "I2C_IMU_SDA", "IMU_INT"}},
            },
            "ble_sensor_node": {
                "power_input": {"required_nets": {"USB_VBUS", "GND", "USB_DP_RAW", "USB_DM_RAW"}},
                "fuse": {"required_nets": {"USB_VBUS", "USB_FUSED"}},
                "reverse_polarity": {"required_nets": {"USB_FUSED", "USB_PROT", "GND"}},
                "tvs": {"required_nets": {"USB_DP_RAW", "USB_DM_RAW", "USB_DP", "USB_DM", "GND"}},
                "charger": {"required_nets": {"USB_PROT", "VBAT", "GND", "CHG_STAT", "CHG_ISET"}},
                "regulator_3v3": {"required_nets": {"VBAT", "V3V3", "GND"}},
                "mcu": {"required_nets": {"V3V3", "GND", "I2C_SCL", "I2C_SDA", "USB_DP", "USB_DM"}},
                "env_sensor": {"required_nets": {"V3V3", "GND", "I2C_SCL", "I2C_SDA", "TEMP_INT"}},
                "fuel_gauge": {"required_nets": {"VBAT", "V3V3", "GND", "I2C_SCL", "I2C_SDA", "FUEL_ALRT"}},
            },
        }
        return contracts.get(role_set_name, {})

    @staticmethod
    def _support_net_contract_failures(role_set_name: str, role: str, component: dict[str, Any], contract: dict[str, Any]) -> list[Failure]:
        nets = {pin.get("net") for pin in component.get("pins", []) if pin.get("net")}
        missing = sorted(set(contract.get("required_nets", set())) - nets)
        failures: list[Failure] = []
        if missing:
            failures.append(Failure(
                FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                "support_role_net_contract_failed",
                f"{component.get('ref', '?')} does not satisfy the {role} support-circuit net contract",
                path="electronics.components",
                details={
                    "role_set": role_set_name,
                    "role": role,
                    "ref": component.get("ref"),
                    "missing_nets": missing,
                    "present_nets": sorted(nets),
                },
            ))
        required_motor_suffixes = set(contract.get("required_motor_suffixes", set()))
        if required_motor_suffixes:
            missing_suffixes = sorted(
                suffix
                for suffix in required_motor_suffixes
                if not any(str(net).startswith("MOTOR") and str(net).endswith(suffix) for net in nets)
            )
            if missing_suffixes:
                failures.append(Failure(
                    FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                    "support_role_net_contract_failed",
                    f"{component.get('ref', '?')} is missing motor-interface nets required by {role}",
                    path="electronics.components",
                    details={
                        "role_set": role_set_name,
                        "role": role,
                        "ref": component.get("ref"),
                        "missing_motor_suffixes": missing_suffixes,
                        "present_nets": sorted(nets),
                    },
                ))
        return failures

    def _sourcing_resilience_report(
        self,
        spec: dict[str, Any],
        graph: dict[str, Any],
        role_data_override: dict[str, Any] | None = None,
    ) -> GateReport:
        role_set_name = spec.get("electronics", {}).get("role_set", "robotics_controller")
        role_path = self.parts_root / "role_sets" / f"{role_set_name}.yaml"
        if role_data_override is None and not role_path.is_file():
            return GateReport(
                "sourcing_resilience",
                Status.BLOCKED,
                [Failure(FailureCategory.BOM_ERROR, "role_set_missing", f"Role set is missing: {role_set_name}")],
                backend={"name": "sourcing-resilience-contract", "role_set": role_set_name},
            )

        role_data = role_data_override if role_data_override is not None else read_yaml(role_path)
        critical_roles = set(role_data.get("critical_roles", []))
        selected_roles = role_data.get("roles", {})
        alternatives = role_data.get("alternatives") or {}
        single_source = role_data.get("single_source_justifications") or {}
        provider = spec.get("sourcing", {}).get("provider", "curated")
        component_database = self._design_space_component_database()
        supplier_records = self._design_space_supplier_records(provider)
        resolved_roles = {
            item.get("role"): item.get("component_id")
            for item in graph.get("component_resolution", [])
            if item.get("resolution") == "curated" and item.get("role")
        }
        failures: list[Failure] = []
        alternate_roles: list[str] = []
        justified_roles: list[str] = []
        checked_alternates = 0

        for role in sorted(critical_roles):
            selected_id = (selected_roles.get(role) or {}).get("component_id") or resolved_roles.get(role)
            if not selected_id:
                failures.append(Failure(
                    FailureCategory.BOM_ERROR,
                    "critical_role_sourcing_strategy_missing",
                    f"Critical role lacks a selected sourcing strategy: {role}",
                    path=f"electronics.role_set.{role}",
                    details={"role_set": role_set_name, "role": role},
                ))
                continue
            curated_alternates = [
                item for item in alternatives.get(role, [])
                if item.get("component_id")
                and item.get("component_id") != selected_id
                and item.get("resolution") == "curated"
            ]
            if curated_alternates:
                alternate_roles.append(role)
                for alternate in curated_alternates:
                    checked_alternates += 1
                    compatibility = alternate.get("compatibility") or {}
                    if compatibility.get("pin_numbers") != "exact" or compatibility.get("footprint") != "exact":
                        failures.append(Failure(
                            FailureCategory.BOM_ERROR,
                            "critical_alternate_contract_incomplete",
                            f"Critical role {role} alternate {alternate.get('component_id')} is not a drop-in pin/footprint contract",
                            path=f"electronics.role_set.alternatives.{role}",
                            details={
                                "role": role,
                                "alternate_component_id": alternate.get("component_id"),
                                "compatibility": compatibility,
                            },
                        ))
                    alternate_id = alternate.get("component_id")
                    alternate_part = component_database.get(alternate_id)
                    if not alternate_part:
                        failures.append(Failure(
                            FailureCategory.BOM_ERROR,
                            "critical_alternate_component_missing",
                            f"Critical role {role} alternate {alternate_id} is absent from the curated component database",
                            path=f"electronics.role_set.alternatives.{role}",
                            details={"role": role, "alternate_component_id": alternate_id},
                        ))
                        continue
                    if alternate_part.get("lifecycle") != "active":
                        failures.append(Failure(
                            FailureCategory.BOM_ERROR,
                            "critical_alternate_lifecycle_not_active",
                            f"Critical role {role} alternate {alternate_id} is not active",
                            path=f"electronics.role_set.alternatives.{role}",
                            details={
                                "role": role,
                                "alternate_component_id": alternate_id,
                                "lifecycle": alternate_part.get("lifecycle"),
                            },
                        ))
                    supplier_record = supplier_records.get(alternate_id)
                    if not supplier_record:
                        failures.append(Failure(
                            FailureCategory.BOM_ERROR,
                            "critical_alternate_supplier_record_missing",
                            f"Critical role {role} alternate {alternate_id} lacks a supplier evidence record for {provider}",
                            path=f"electronics.role_set.alternatives.{role}",
                            details={"role": role, "alternate_component_id": alternate_id, "provider": provider},
                        ))
                    else:
                        availability = supplier_record.get("availability")
                        observed_at = supplier_record.get("observed_at")
                        if availability in {"out_of_stock", "discontinued"}:
                            failures.append(Failure(
                                FailureCategory.BOM_ERROR,
                                "critical_alternate_supplier_unavailable",
                                f"Critical role {role} alternate {alternate_id} is {availability} at {provider}",
                                path=f"electronics.role_set.alternatives.{role}",
                                details={
                                    "role": role,
                                    "alternate_component_id": alternate_id,
                                    "provider": provider,
                                    "availability": availability,
                                },
                            ))
                        elif availability == "available" and (not observed_at or _evidence_is_stale(observed_at)):
                            failures.append(Failure(
                                FailureCategory.BOM_ERROR,
                                "critical_alternate_supplier_evidence_stale",
                                f"Critical role {role} alternate {alternate_id} availability evidence is not current",
                                path=f"electronics.role_set.alternatives.{role}",
                                details={
                                    "role": role,
                                    "alternate_component_id": alternate_id,
                                    "provider": provider,
                                    "observed_at": observed_at,
                                    "max_age_days": SUPPLIER_EVIDENCE_MAX_AGE_DAYS,
                                },
                            ))
                continue

            justification = single_source.get(role) or {}
            if justification.get("reason") and justification.get("mitigation"):
                justified_roles.append(role)
            else:
                failures.append(Failure(
                    FailureCategory.BOM_ERROR,
                    "critical_role_resilience_missing",
                    f"Critical role {role} has neither a curated alternate nor a single-source mitigation",
                    path=f"electronics.role_set.{role}",
                    details={"role_set": role_set_name, "role": role, "selected_component_id": selected_id},
                ))

        return GateReport(
            "sourcing_resilience",
            Status.FAIL if failures else Status.PASS,
            failures,
            metrics={
                "role_set": role_set_name,
                "critical_roles": len(critical_roles),
                "alternate_roles": len(alternate_roles),
                "single_source_justified_roles": len(justified_roles),
                "checked_alternates": checked_alternates,
                "supplier_provider": provider,
            },
            artifacts=[str(role_path)] if role_path.is_file() and role_data_override is None else [],
            backend={"name": "sourcing-resilience-contract", "deterministic": True},
        )

    def _support_circuit_completeness_report(self, spec: dict[str, Any], graph: dict[str, Any]) -> GateReport:
        role_set_name = spec.get("electronics", {}).get("role_set", "robotics_controller")
        role_path = self.parts_root / "role_sets" / f"{role_set_name}.yaml"
        if not role_path.is_file():
            return GateReport(
                "support_circuit_completeness",
                Status.BLOCKED,
                [Failure(FailureCategory.BOM_ERROR, "role_set_missing", f"Role set is missing: {role_set_name}")],
                backend={"name": "support-circuit-contract", "role_set": role_set_name},
            )

        role_data = read_yaml(role_path)
        critical_roles = set(role_data.get("critical_roles", []))
        resolved_roles = {item.get("role") for item in graph.get("component_resolution", []) if item.get("resolution") == "curated"}
        present_categories = {item.get("category") for item in graph.get("components", [])}
        components_by_role: dict[str, list[dict[str, Any]]] = {}
        for component in graph.get("components", []):
            components_by_role.setdefault(role_for_component(component), []).append(component)
        failures: list[Failure] = []
        for role in sorted(critical_roles - resolved_roles):
            failures.append(Failure(
                FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                "missing_critical_support_role",
                f"Critical support role is absent or unresolved: {role}",
                path="electronics.role_set",
                details={"role_set": role_set_name, "role": role},
            ))

        protection_roles: dict[str, tuple[str, ...]] = {
            "reverse_polarity": ("reverse_polarity",),
            "fuse_or_efuse": ("fuse", "efuse"),
            "tvs": ("tvs",),
            "motor_enable_gate": ("safety_gate",),
        }
        for protection in spec.get("safety", {}).get("required_protections", []):
            alternatives = protection_roles.get(protection)
            if alternatives and not (resolved_roles & set(alternatives)):
                failures.append(Failure(
                    FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                    "missing_required_protection_role",
                    f"Required protection lacks a resolved support block: {protection}",
                    path="safety.required_protections",
                    details={"protection": protection, "acceptable_roles": list(alternatives)},
                ))

        # Catch plausible-but-wrong graph edits where the role resolves, but no
        # component category remains in the actual schematic graph.
        for role in sorted(critical_roles & resolved_roles):
            if role not in present_categories and not (role.startswith("regulator_") and "regulator" in present_categories):
                failures.append(Failure(
                    FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                    "support_role_not_in_graph",
                    f"Resolved support role has no matching schematic block: {role}",
                    path="electronics.components",
                    details={"role": role},
                ))

        net_contracts = self._support_net_contracts(role_set_name)
        net_contract_checks = 0
        for role in sorted(critical_roles & resolved_roles):
            contract = net_contracts.get(role)
            if not contract:
                continue
            for component in components_by_role.get(role, []):
                net_contract_checks += 1
                failures.extend(self._support_net_contract_failures(role_set_name, role, component, contract))

        return GateReport(
            "support_circuit_completeness",
            Status.FAIL if failures else Status.PASS,
            failures,
            metrics={
                "role_set": role_set_name,
                "critical_roles": len(critical_roles),
                "resolved_critical_roles": len(critical_roles & resolved_roles),
                "components": len(graph.get("components", [])),
                "net_contract_checks": net_contract_checks,
            },
            artifacts=[str(role_path)],
            backend={"name": "support-circuit-contract", "deterministic": True},
        )

    def _firmware_interface_contract_report(
        self,
        project_path: Path,
        spec: dict[str, Any],
        graph: dict[str, Any],
        pinmap: list[dict[str, Any]],
        prj_conf_text: str | None = None,
        test_sources: dict[str, str] | None = None,
    ) -> GateReport:
        prj_conf_path = project_path / "firmware" / "zephyr" / "app" / "prj.conf"
        failures: list[Failure] = []
        if prj_conf_text is None:
            if prj_conf_path.is_file():
                prj_conf_text = prj_conf_path.read_text(encoding="utf-8")
            else:
                failures.append(Failure(
                    FailureCategory.FIRMWARE_ERROR,
                    "firmware_config_missing",
                    "Generated firmware config is missing",
                    path="firmware/zephyr/app/prj.conf",
                ))
                prj_conf_text = ""

        if test_sources is None:
            test_sources = self._firmware_test_sources(project_path)
        config_entries = {
            line.strip()
            for line in prj_conf_text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        test_text = "\n".join([*test_sources.keys(), *test_sources.values()]).upper()
        net_names = {str(net.get("name")) for net in graph.get("nets", []) if net.get("name")}
        signal_classes = {str(net.get("signal_class")) for net in graph.get("nets", []) if net.get("signal_class")}
        categories = {str(component.get("category")) for component in graph.get("components", []) if component.get("category")}
        pinmap_signals = {str(item.get("signal")) for item in pinmap if item.get("signal")}
        architecture = str(graph.get("design_basis", {}).get("architecture") or "")
        role_set_name = str(spec.get("electronics", {}).get("role_set") or "")
        try:
            motor_channels = int(spec.get("actuation", {}).get("motor_channels", 0) or 0)
        except (TypeError, ValueError):
            motor_channels = 0

        required_interfaces: list[str] = []

        def add_failure(code: str, message: str, path: str, details: dict[str, Any]) -> None:
            failures.append(Failure(FailureCategory.FIRMWARE_ERROR, code, message, path=path, details=details))

        def has_config(options: tuple[str, ...]) -> bool:
            for option in options:
                if option.endswith("=y") and option in config_entries:
                    return True
                if not option.endswith("=y") and any(entry == option or entry.startswith(f"{option}=") for entry in config_entries):
                    return True
            return False

        def require_config(interface: str, options: tuple[str, ...], code: str) -> None:
            if not has_config(options):
                add_failure(
                    code,
                    f"Firmware config does not enable required {interface} support",
                    "firmware/zephyr/app/prj.conf",
                    {"interface": interface, "required_config": list(options)},
                )

        def require_test(interface: str, terms: tuple[str, ...], code: str) -> None:
            if not any(term.upper() in test_text for term in terms):
                add_failure(
                    code,
                    f"Generated firmware tests do not cover required {interface} bring-up",
                    "firmware/zephyr/tests",
                    {
                        "interface": interface,
                        "required_terms": list(terms),
                        "test_files": sorted(test_sources),
                    },
                )

        has_i2c = "i2c" in signal_classes or any(name.upper().startswith("I2C") for name in net_names | pinmap_signals)
        if has_i2c:
            required_interfaces.append("i2c")
            require_config("I2C", ("CONFIG_I2C=y",), "firmware_i2c_config_missing")
            require_test("I2C", ("I2C",), "firmware_i2c_bringup_missing")

        has_can = {"CANH", "CANL"} <= net_names or {"CAN_RX", "CAN_TX"} <= pinmap_signals or "can" in categories
        if has_can:
            required_interfaces.append("can")
            require_config("CAN", ("CONFIG_CAN=y",), "firmware_can_config_missing")
            require_test("CAN", ("CAN",), "firmware_can_bringup_missing")

        has_motor_pwm = motor_channels > 0 or any(
            signal.startswith("MOTOR") and (signal.endswith("_PWM") or "_PWM" in signal)
            for signal in pinmap_signals
        )
        if has_motor_pwm:
            required_interfaces.append("motor_pwm")
            motor_pwm_channels = sorted({
                int(match.group(1))
                for signal in pinmap_signals
                if (match := re.fullmatch(r"MOTOR(\d+)_PWM", signal))
            })
            if motor_channels > 0:
                observed = set(motor_pwm_channels)
                expected = set(range(1, motor_channels + 1))
                missing_channels = sorted(expected - observed)
                extra_channels = sorted(observed - expected)
                if missing_channels:
                    add_failure(
                        "firmware_motor_pwm_channel_missing",
                        "Firmware pinmap does not cover every motor PWM channel required by the actuation spec",
                        "firmware/generated/pinmap.json",
                        {
                            "expected_motor_channels": motor_channels,
                            "observed_pwm_channels": motor_pwm_channels,
                            "missing_channels": missing_channels,
                        },
                    )
                if extra_channels:
                    add_failure(
                        "firmware_motor_pwm_channel_extra",
                        "Firmware pinmap exposes motor PWM channels outside the actuation spec",
                        "firmware/generated/pinmap.json",
                        {
                            "expected_motor_channels": motor_channels,
                            "observed_pwm_channels": motor_pwm_channels,
                            "extra_channels": extra_channels,
                        },
                    )
            require_config("motor PWM", ("CONFIG_PWM=y",), "firmware_motor_pwm_config_missing")
            require_test("motor PWM", ("MOTOR", "PWM"), "firmware_motor_pwm_bringup_missing")

        estop_required = (
            bool(spec.get("safety", {}).get("emergency_stop", {}).get("required"))
            or "ESTOP_IN" in pinmap_signals
            or "estop" in categories
        )
        if estop_required:
            required_interfaces.append("estop")
            if "ESTOP_IN" not in pinmap_signals:
                add_failure(
                    "firmware_estop_pinmap_missing",
                    "Emergency-stop support is required but no ESTOP_IN firmware signal is mapped",
                    "firmware/generated/pinmap.json",
                    {"interface": "estop", "required_signal": "ESTOP_IN"},
                )
            require_test("e-stop fail-safe", ("ESTOP", "E-STOP", "FAIL-SAFE", "FAIL_SAFE"), "firmware_estop_bringup_missing")

        has_usb = bool({"USB_DP", "USB_DM"} & pinmap_signals)
        if has_usb:
            required_interfaces.append("usb")
            require_config(
                "USB",
                ("CONFIG_USB_DEVICE_STACK=y", "CONFIG_USB_CDC_ACM=y", "CONFIG_USB_DEVICE_STACK"),
                "firmware_usb_config_missing",
            )
            require_test("USB", ("USB",), "firmware_usb_bringup_missing")

        has_ble = architecture == "nrf52840_ble_sensor" or role_set_name == "ble_sensor_node"
        if has_ble:
            required_interfaces.append("ble")
            require_config("BLE", ("CONFIG_BT=y",), "firmware_ble_config_missing")
            require_test("BLE", ("BLE", "BT"), "firmware_ble_bringup_missing")

        artifacts = [str(prj_conf_path)] if prj_conf_path.is_file() else []
        artifacts.extend(str(project_path / "firmware" / "zephyr" / "tests" / name) for name in sorted(test_sources))
        return GateReport(
            "firmware_interface_contract",
            Status.FAIL if failures else Status.PASS,
            failures,
            metrics={
                "required_interfaces": required_interfaces,
                "test_files": sorted(test_sources),
                "pinmap_signals": sorted(pinmap_signals),
                "config_options": sorted(config_entries),
            },
            artifacts=artifacts,
            backend={"name": "firmware-interface-contract", "deterministic": True},
        )

    @staticmethod
    def _firmware_test_sources(project_path: Path) -> dict[str, str]:
        tests_dir = project_path / "firmware" / "zephyr" / "tests"
        if not tests_dir.is_dir():
            return {}
        return {
            item.name: item.read_text(encoding="utf-8")
            for item in sorted(tests_dir.glob("test_*.c"))
            if item.is_file()
        }

    def run_grounding_benchmark(self, project: str) -> dict[str, Any]:
        """Probe whether current validators catch common plausible-but-wrong hardware candidates."""
        path = self.workspace.require_project(project)
        spec = self.read_spec(project)
        graph_path = path / "electronics" / "generated" / "electrical_graph.json"
        pinmap_path = path / "firmware" / "generated" / "pinmap.json"
        benchmark_path = path / "validation" / "benchmarks" / "hardware_grounding_v0.json"
        missing = [str(item) for item in (graph_path, pinmap_path) if not item.is_file()]
        if missing:
            result = {
                "status": "blocked",
                "project": project,
                "benchmark": "hardware_grounding_v0",
                "code": "generated_artifacts_required",
                "missing_artifacts": missing,
                "message": "Run hw_design_candidate or hw_generate_all before the grounding benchmark.",
            }
            write_json(benchmark_path, result)
            return result

        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        pinmap = json.loads(pinmap_path.read_text(encoding="utf-8"))
        if not graph.get("components") or not graph.get("nets") or not pinmap:
            result = {
                "status": "blocked",
                "project": project,
                "benchmark": "hardware_grounding_v0",
                "code": "benchmark_fixtures_missing",
                "message": "The benchmark requires generated components, nets, and firmware pinmap assignments.",
            }
            write_json(benchmark_path, result)
            return result

        cases: list[dict[str, Any]] = []

        def record(case_id: str, risk: str, mutation: str, report: GateReport, expected_codes: list[str]) -> None:
            observed_codes = sorted({failure.code for failure in report.failures})
            expected = set(expected_codes)
            detected = report.status != Status.PASS and expected.issubset(set(observed_codes))
            cases.append({
                "id": case_id,
                "risk": risk,
                "mutation": mutation,
                "expected_gate": report.gate,
                "expected_codes": expected_codes,
                "observed_status": report.status.value,
                "observed_codes": observed_codes,
                "detected": detected,
                "report": report.to_dict(),
            })

        component = max((item for item in graph["components"] if item.get("pins")), key=lambda item: len(item["pins"]))
        component_pin = str(component["pins"][-1]["number"])
        components_bad_symbol = deepcopy(graph["components"])
        for item in components_bad_symbol:
            if item.get("ref") == component["ref"]:
                item["symbol"]["expected_pins"] = [
                    pin for pin in item.get("symbol", {}).get("expected_pins", [])
                    if str(pin) != component_pin
                ]
                break
        record(
            "wrong_pinout_contract",
            "pinout_package_grounding",
            f"Removed {component['ref']}.{component_pin} from the curated symbol contract",
            self.validator.check_component_metadata(components_bad_symbol),
            ["symbol_pin_missing"],
        )

        components_bad_footprint = deepcopy(graph["components"])
        for item in components_bad_footprint:
            if item.get("ref") == component["ref"]:
                item["footprint_metadata"]["expected_pads"] = [
                    pad for pad in item.get("footprint_metadata", {}).get("expected_pads", [])
                    if str(pad) != component_pin
                ]
                break
        record(
            "wrong_footprint_contract",
            "pinout_package_grounding",
            f"Removed {component['ref']}.{component_pin} from the curated footprint pad contract",
            self.validator.check_component_metadata(components_bad_footprint),
            ["footprint_pad_missing"],
        )

        components_bad_role = deepcopy(graph["components"])
        role_mutation = None
        for item in components_bad_role:
            if item.get("category") != "usb":
                continue
            for pin in item.get("pins", []):
                if pin.get("name") == "VBUS":
                    role_mutation = f"{item['ref']}.{pin['number']}"
                    pin["role"] = "bidirectional"
                    break
            if role_mutation:
                break
        if role_mutation:
            record(
                "wrong_pin_role_contract",
                "pinout_package_grounding",
                f"Changed USB VBUS pin {role_mutation} from power input to bidirectional",
                self.validator.check_component_metadata(components_bad_role),
                ["component_pin_role_mismatch"],
            )
        else:
            record(
                "wrong_pin_role_contract",
                "pinout_package_grounding",
                "No USB VBUS pin was available to mutate",
                GateReport(
                    "component_provenance",
                    Status.BLOCKED,
                    [Failure(FailureCategory.BOM_ERROR, "benchmark_fixture_unavailable", "No USB VBUS pin was available to mutate")],
                ),
                ["component_pin_role_mismatch"],
            )

        role_set_name = spec.get("electronics", {}).get("role_set", "robotics_controller")
        role_path = self.parts_root / "role_sets" / f"{role_set_name}.yaml"
        role_data = read_yaml(role_path) if role_path.is_file() else {}
        critical_roles = set(role_data.get("critical_roles", []))
        resolved_roles = {
            item.get("role")
            for item in graph.get("component_resolution", [])
            if item.get("resolution") == "curated"
        }
        removable_roles = sorted(role for role in critical_roles & resolved_roles if role)
        if removable_roles:
            missing_role = "tvs" if "tvs" in removable_roles else removable_roles[0]
            graph_missing_support = deepcopy(graph)
            graph_missing_support["component_resolution"] = [
                item for item in graph_missing_support.get("component_resolution", [])
                if item.get("role") != missing_role
            ]
            record(
                "missing_support_circuit",
                "support_circuit_completeness",
                f"Removed resolved critical role {missing_role!r} from component resolution",
                self._support_circuit_completeness_report(spec, graph_missing_support),
                ["missing_critical_support_role"],
            )
        else:
            record(
                "missing_support_circuit",
                "support_circuit_completeness",
                "No resolved critical role was available to remove",
                GateReport(
                    "support_circuit_completeness",
                    Status.BLOCKED,
                    [Failure(FailureCategory.BOM_ERROR, "benchmark_fixture_unavailable", "No resolved critical role was available to remove")],
                ),
                ["missing_critical_support_role"],
            )

        graph_miswired_support = deepcopy(graph)
        support_component = next(
            (
                component
                for component in graph_miswired_support.get("components", [])
                if role_for_component(component) == "tvs"
            ),
            None,
        )
        if support_component:
            for pin in support_component.get("pins", []):
                if pin.get("net") == "GND":
                    pin["net"] = "V3V3"
                    break
            record(
                "miswired_support_circuit",
                "support_circuit_completeness",
                f"Disconnected {support_component.get('ref')} TVS/protection role from GND",
                self._support_circuit_completeness_report(spec, graph_miswired_support),
                ["support_role_net_contract_failed"],
            )
        else:
            record(
                "miswired_support_circuit",
                "support_circuit_completeness",
                "No TVS/protection support component was available to miswire",
                GateReport(
                    "support_circuit_completeness",
                    Status.BLOCKED,
                    [Failure(FailureCategory.BOM_ERROR, "benchmark_fixture_unavailable", "No TVS/protection support component was available to miswire")],
                ),
                ["support_role_net_contract_failed"],
            )

        spec_bad_power = deepcopy(spec)
        spec_bad_power.setdefault("system", {}).setdefault("supply", {}).setdefault("battery", {})["pack_current_peak_a"] = 5
        spec_bad_power.setdefault("actuation", {})["motor_channels"] = 4
        spec_bad_power["actuation"]["motor_channel_peak_current_a"] = 10
        spec_bad_power["actuation"]["max_simultaneous_peak_channels"] = 4
        record(
            "bad_power_assumption",
            "power_budget_grounding",
            "Raised concurrent motor demand above available battery peak current",
            self.validator.check_electrical_semantics(spec_bad_power),
            ["current_budget_exceeded"],
        )

        graph_unreachable_power = deepcopy(graph)
        regulator = next((item for item in graph_unreachable_power.get("components", []) if item.get("category") == "regulator"), None)
        if regulator:
            input_pin = next((pin for pin in regulator.get("pins", []) if pin.get("role") == "power_in"), None)
            if input_pin:
                input_pin["net"] = "__UNPOWERED_RAIL__"
                input_pin["voltage_domain"] = None
                graph_unreachable_power.setdefault("nets", []).append({
                    "name": "__UNPOWERED_RAIL__",
                    "signal_class": "power",
                    "voltage_domain": None,
                    "connected_pins": [f"{regulator['ref']}.{input_pin['number']}"],
                })
            record(
                "unreachable_power_rail",
                "power_tree_grounding",
                f"Moved {regulator.get('ref')} regulator input to an unpowered rail",
                self.validator.check_power_tree(graph_unreachable_power, spec),
                ["power_net_unreachable"],
            )

            graph_bad_regulator_order = deepcopy(graph)
            regulator_order = next((item for item in graph_bad_regulator_order.get("components", []) if item.get("ref") == regulator.get("ref")), None)
            if regulator_order:
                input_pin = next((pin for pin in regulator_order.get("pins", []) if pin.get("role") == "power_in"), None)
                output_pin = next((pin for pin in regulator_order.get("pins", []) if pin.get("role") == "power_out"), None)
                if input_pin and output_pin:
                    input_pin["net"], output_pin["net"] = output_pin["net"], input_pin["net"]
                    input_pin["voltage_domain"], output_pin["voltage_domain"] = output_pin.get("voltage_domain"), input_pin.get("voltage_domain")
            record(
                "regulator_voltage_order_violation",
                "power_tree_grounding",
                f"Swapped {regulator.get('ref')} regulator input/output rails",
                self.validator.check_power_tree(graph_bad_regulator_order, spec),
                ["power_output_exceeds_input_voltage"],
            )

        graph_missing_decoupling = deepcopy(graph)
        graph_missing_decoupling["components"] = [
            component for component in graph_missing_decoupling.get("components", [])
            if component.get("category") != "decoupling"
        ]
        record(
            "missing_rail_decoupling",
            "power_integrity_grounding",
            "Removed all decoupling capacitors while powered IC loads remain",
            self.validator.check_power_integrity_estimate(graph_missing_decoupling, spec),
            ["rail_decoupling_missing"],
        )

        graph_missing_i2c_pullups = deepcopy(graph)
        i2c_nets = [
            net.get("name")
            for net in graph_missing_i2c_pullups.get("nets", [])
            if net.get("signal_class") == "i2c" or str(net.get("name", "")).upper().startswith("I2C")
        ]
        graph_missing_i2c_pullups["components"] = [
            component for component in graph_missing_i2c_pullups.get("components", [])
            if component.get("category") != "pullup"
        ]
        if i2c_nets:
            record(
                "missing_i2c_pullup",
                "interface_signal_integrity",
                "Removed all I2C pull-up components from the generated graph",
                self.validator.check_interface_integrity(graph_missing_i2c_pullups),
                ["i2c_pullup_missing"],
            )

        if {"CANH", "CANL"} <= {net.get("name") for net in graph.get("nets", [])}:
            graph_missing_can_termination = deepcopy(graph)
            graph_missing_can_termination["components"] = [
                component for component in graph_missing_can_termination.get("components", [])
                if component.get("category") != "termination"
            ]
            record(
                "missing_can_termination",
                "interface_signal_integrity",
                "Removed the CAN termination component across CANH/CANL",
                self.validator.check_interface_integrity(graph_missing_can_termination),
                ["can_termination_missing"],
            )

        usb_required_nets = {"USB_DP_RAW", "USB_DM_RAW", "USB_DP", "USB_DM", "GND"}
        if usb_required_nets - {"GND"} <= {net.get("name") for net in graph.get("nets", [])}:
            graph_missing_usb_esd = deepcopy(graph)
            graph_missing_usb_esd["components"] = [
                component
                for component in graph_missing_usb_esd.get("components", [])
                if not (
                    component.get("category") in {"usb_esd", "tvs"}
                    and usb_required_nets <= {pin.get("net") for pin in component.get("pins", []) if pin.get("net")}
                )
            ]
            record(
                "missing_usb_esd_bridge",
                "interface_signal_integrity",
                "Removed the USB raw-to-protected ESD bridge component",
                self.validator.check_interface_integrity(graph_missing_usb_esd),
                ["usb_esd_bridge_missing"],
            )

            usb_esd_component = next(
                (
                    component for component in graph.get("components", [])
                    if component.get("category") in {"usb_esd", "tvs"}
                    and usb_required_nets <= {pin.get("net") for pin in component.get("pins", []) if pin.get("net")}
                ),
                None,
            )
            if usb_esd_component:
                graph_bad_usb_esd_placement = deepcopy(graph)
                bad_esd = next(
                    component
                    for component in graph_bad_usb_esd_placement.get("components", [])
                    if component.get("ref") == usb_esd_component.get("ref")
                )
                envelope = spec.get("mechanical", {}).get("envelope", {})
                bad_esd["pcb_position_mm"] = [
                    float(envelope.get("board_width_mm", 0.0)) / 2.0,
                    float(envelope.get("board_height_mm", 0.0)) / 2.0,
                ]
                usb_esd_proposal = propose_placement(spec, graph_bad_usb_esd_placement)
                usb_esd_proposal.placements[usb_esd_component["ref"]] = replace(
                    usb_esd_proposal.placements[usb_esd_component["ref"]],
                    x_mm=bad_esd["pcb_position_mm"][0],
                    y_mm=bad_esd["pcb_position_mm"][1],
                    source="benchmark_forced_bad_usb_esd",
                )
                record(
                    "usb_esd_far_from_connector",
                    "layout_signal_integrity",
                    f"Moved USB ESD component {usb_esd_component.get('ref')} away from the USB connector",
                    check_layout_signal_integrity(usb_esd_proposal, graph_bad_usb_esd_placement, spec),
                    ["usb_esd_far_from_connector"],
                )

        graph_hot_near_logic = deepcopy(graph)
        hot_component = next(
            (
                component
                for component in graph_hot_near_logic.get("components", [])
                if component.get("category") in {"regulator", "efuse", "reverse_polarity", "safety_gate", "charger"}
            ),
            None,
        )
        logic_component = next((component for component in graph_hot_near_logic.get("components", []) if component.get("category") == "mcu"), None)
        if hot_component and logic_component:
            logic_position = logic_component.get("pcb_position_mm", [0.0, 0.0])
            hot_component["pcb_position_mm"] = [float(logic_position[0]) + 1.0, float(logic_position[1])]
            hot_proposal = propose_placement(spec, graph_hot_near_logic)
            hot_proposal.placements[hot_component["ref"]] = replace(
                hot_proposal.placements[hot_component["ref"]],
                x_mm=hot_component["pcb_position_mm"][0],
                y_mm=hot_component["pcb_position_mm"][1],
                source="benchmark_forced_hot_near_logic",
            )
            record(
                "hot_block_near_sensitive_logic",
                "layout_thermal_grounding",
                f"Moved {hot_component.get('ref')} next to MCU {logic_component.get('ref')}",
                check_layout_thermal_integrity(hot_proposal, graph_hot_near_logic, spec),
                ["thermal_sensitive_spacing_violation"],
            )

        targeted_decoupling = next(
            (
                component
                for component in graph.get("components", [])
                if component.get("category") == "decoupling" and component.get("decoupling_target_ref")
            ),
            None,
        )
        if targeted_decoupling:
            graph_bad_decoupling_placement = deepcopy(graph)
            bad_decap = next(
                component
                for component in graph_bad_decoupling_placement.get("components", [])
                if component.get("ref") == targeted_decoupling.get("ref")
            )
            bad_decap["pcb_position_mm"] = [0.0, 0.0]
            decoupling_proposal = propose_placement(spec, graph_bad_decoupling_placement)
            decoupling_proposal.placements[targeted_decoupling["ref"]] = replace(
                decoupling_proposal.placements[targeted_decoupling["ref"]],
                x_mm=0.0,
                y_mm=0.0,
                source="benchmark_forced_bad_decoupling",
            )
            record(
                "decoupling_far_from_target",
                "layout_power_integrity",
                f"Moved targeted decoupling capacitor {targeted_decoupling.get('ref')} away from {targeted_decoupling.get('decoupling_target_ref')}",
                check_placement(decoupling_proposal, graph_bad_decoupling_placement),
                ["decoupling_too_far_from_target"],
            )

        rf_component = next(
            (
                component for component in graph.get("components", [])
                if (
                    {"ble_mcu", "wifi_bt_mcu", "integral_pcb_antenna_required", "integral_antenna_keepout_required"}
                    & set(component.get("constraints", []))
                )
                or (
                    component.get("category") == "mcu"
                    and graph.get("design_basis", {}).get("integral_pcb_antenna_required")
                )
            ),
            None,
        )
        if rf_component:
            graph_bad_rf_placement = deepcopy(graph)
            rf_bad = next(component for component in graph_bad_rf_placement.get("components", []) if component.get("ref") == rf_component.get("ref"))
            envelope = spec.get("mechanical", {}).get("envelope", {})
            rf_bad["pcb_position_mm"] = [
                float(envelope.get("board_width_mm", 0.0)) / 2.0,
                float(envelope.get("board_height_mm", 0.0)) / 2.0,
            ]
            rf_proposal = propose_placement(spec, graph_bad_rf_placement)
            rf_proposal.placements[rf_component["ref"]] = replace(
                rf_proposal.placements[rf_component["ref"]],
                x_mm=rf_bad["pcb_position_mm"][0],
                y_mm=rf_bad["pcb_position_mm"][1],
                source="benchmark_forced_bad_rf",
            )
            record(
                "rf_antenna_not_edge_aligned",
                "layout_signal_integrity",
                f"Moved RF antenna component {rf_component.get('ref')} away from the board edge",
                check_layout_signal_integrity(rf_proposal, graph_bad_rf_placement, spec),
                ["rf_antenna_not_edge_aligned"],
            )

        motor_peak = float(spec.get("actuation", {}).get("motor_channel_peak_current_a", 0) or 0)
        if motor_peak > 0:
            spec_bad_connector_rating = deepcopy(spec)
            spec_bad_connector_rating.setdefault("assumptions", {}).setdefault("connector_current_rating", {})["value_a"] = max(0.1, motor_peak / 2.0)
            record(
                "connector_current_rating_violation",
                "layout_thermal_grounding",
                "Lowered connector current rating below motor-channel peak current",
                check_layout_thermal_integrity(propose_placement(spec_bad_connector_rating, graph), graph, spec_bad_connector_rating),
                ["connector_current_rating_below_peak"],
            )

        mechanical = spec.get("mechanical", {})
        if mechanical.get("vibration_environment") == "high" and mechanical.get("connectors_exposed", True):
            spec_missing_connector_retention = deepcopy(spec)
            fixtures = spec_missing_connector_retention.setdefault("mechanical", {}).setdefault("fixtures", {})
            fixtures.pop("cable_retention", None)
            fixtures.pop("connector_retention", None)
            record(
                "missing_connector_retention",
                "mechanical_connector_reliability",
                "Removed the cable/connector retention fixture from a high-vibration exposed-connector design",
                self.validator.check_mechanical_connector_retention(spec_missing_connector_retention, graph),
                ["connector_retention_missing"],
            )

        connector_interfaces = [
            item for item in mechanical.get("connector_interfaces", [])
            if item.get("ref") and item.get("side")
        ]
        if connector_interfaces:
            interface = connector_interfaces[0]
            graph_bad_cutout_alignment = deepcopy(graph)
            connector = next(
                (
                    component
                    for component in graph_bad_cutout_alignment.get("components", [])
                    if component.get("ref") == interface.get("ref")
                ),
                None,
            )
            if connector:
                envelope = spec.get("mechanical", {}).get("envelope", {})
                connector["pcb_position_mm"] = [
                    float(envelope.get("board_width_mm", 0.0)) / 2.0,
                    float(envelope.get("board_height_mm", 0.0)) / 2.0,
                ]
                record(
                    "connector_cutout_misaligned",
                    "mechanical_cutout_grounding",
                    f"Moved connector {connector.get('ref')} away from its declared {interface.get('side')} enclosure cutout",
                    self.validator.check_mechanical_connector_cutouts(spec, graph_bad_cutout_alignment),
                    ["connector_cutout_alignment_failed"],
                )

        mounting_holes = mechanical.get("mounting_holes", [])
        if mounting_holes:
            graph_bad_mounting_keepout = deepcopy(graph)
            mounting_components = graph_bad_mounting_keepout.get("components", [])
            target = next(
                (
                    component
                    for component in mounting_components
                    if component.get("ref") and not str(component.get("ref", "")).startswith("J")
                ),
                mounting_components[0] if mounting_components else None,
            )
            if target:
                hole = mounting_holes[0]
                target["pcb_position_mm"] = [float(hole["x_mm"]), float(hole["y_mm"])]
                record(
                    "component_on_mounting_hole",
                    "mechanical_mounting_grounding",
                    f"Moved component {target.get('ref')} onto a mounting-hole keepout",
                    self.validator.check_mechanical_mounting_integrity(spec, graph_bad_mounting_keepout),
                    ["mounting_hole_component_keepout_intrusion"],
                )

        if role_data.get("critical_roles"):
            role_data_without_resilience = deepcopy(role_data)
            role_data_without_resilience["alternatives"] = {}
            role_data_without_resilience["single_source_justifications"] = {}
            record(
                "missing_sourcing_resilience_strategy",
                "component_availability_lifecycle",
                "Removed role-set alternates and single-source mitigations for critical roles",
                self._sourcing_resilience_report(spec, graph, role_data_override=role_data_without_resilience),
                ["critical_role_resilience_missing"],
            )

            critical_role = sorted(role_data["critical_roles"])[0]
            role_data_missing_alternate = deepcopy(role_data)
            role_data_missing_alternate.setdefault("alternatives", {})[critical_role] = [{
                "component_id": "__MISSING_CURATED_ALTERNATE__",
                "resolution": "curated",
                "compatibility": {"pin_numbers": "exact", "footprint": "exact"},
                "rationale": "Synthetic benchmark alternate claims to be drop-in but has no curated component record.",
            }]
            record(
                "missing_curated_alternate_component",
                "component_availability_lifecycle",
                f"Added a critical-role alternate for {critical_role} that is absent from the curated component database",
                self._sourcing_resilience_report(spec, graph, role_data_override=role_data_missing_alternate),
                ["critical_alternate_component_missing"],
            )

        unavailable_components = deepcopy(graph["components"])
        unavailable_components[0]["lifecycle"] = "obsolete"
        unavailable_components[0].setdefault("sourcing", {})["status"] = "unresolved"
        record(
            "unavailable_or_obsolete_part",
            "component_availability_lifecycle",
            f"Marked {unavailable_components[0].get('ref')} obsolete and sourcing-unresolved",
            self.validator.check_sourcing(unavailable_components),
            ["lifecycle_risk", "sourcing_unresolved"],
        )

        stale_supplier_components = deepcopy(graph["components"])
        stale_supplier = stale_supplier_components[0]
        stale_supplier["lifecycle"] = "active"
        stale_supplier.setdefault("sourcing", {})["status"] = "resolved"
        stale_supplier["supplier_offer"] = {
            "provider": spec.get("sourcing", {}).get("provider", "curated"),
            "component_id": stale_supplier.get("component_id"),
            "sku": "STALE-BENCHMARK-SKU",
            "availability": "available",
            "stock": 100,
            "observed_at": "2020-01-01T00:00:00Z",
            "source_record": {"benchmark_mutation": "stale_supplier_evidence"},
        }
        record(
            "stale_supplier_evidence",
            "component_availability_lifecycle",
            f"Attached stale supplier availability evidence to {stale_supplier.get('ref')}",
            self.validator.check_sourcing(stale_supplier_components),
            ["supplier_evidence_stale"],
        )

        graph_bad_net = deepcopy(graph)
        graph_bad_net["nets"][0].setdefault("connected_pins", []).append("U999.1")
        record(
            "schematic_unknown_pin_endpoint",
            "schematic_structural_correctness",
            f"Added unknown endpoint U999.1 to net {graph_bad_net['nets'][0].get('name')}",
            self.validator.check_graph_pin_resolution(graph_bad_net),
            ["graph_pin_unresolved"],
        )

        graph_pin_net_mismatch = deepcopy(graph)
        mismatch_net = next((item for item in graph_pin_net_mismatch.get("nets", []) if item.get("connected_pins")), None)
        if mismatch_net:
            endpoint = mismatch_net["connected_pins"][0]
            ref, _, number = endpoint.partition(".")
            for component_item in graph_pin_net_mismatch.get("components", []):
                if component_item.get("ref") != ref:
                    continue
                for pin_item in component_item.get("pins", []):
                    if str(pin_item.get("number")) == number:
                        pin_item["net"] = "__WRONG_NET__"
                        break
                break
            record(
                "component_pin_net_mismatch",
                "schematic_structural_correctness",
                f"Changed {endpoint} pin net away from net {mismatch_net.get('name')}",
                self.validator.check_graph_pin_resolution(graph_pin_net_mismatch),
                ["graph_pin_net_mismatch"],
            )

        pinmap_bad = deepcopy(pinmap)
        pinmap_bad[0]["net_name"] = "__MISSING_HARDWARE_NET__"
        record(
            "firmware_pinmap_mismatch",
            "firmware_co_design",
            f"Mapped firmware signal {pinmap_bad[0].get('signal')} to a missing hardware net",
            self.validator.check_hw_sw_parity(graph, pinmap_bad),
            ["missing_hardware_net"],
        )

        pinmap_wrong_mcu_pin = deepcopy(pinmap)
        pinmap_wrong_mcu_pin[0]["mcu_pin"] = "__WRONG_MCU_PIN__"
        record(
            "firmware_mcu_pin_mismatch",
            "firmware_co_design",
            f"Kept firmware net {pinmap_wrong_mcu_pin[0].get('net_name')} but assigned it to a wrong MCU pin name",
            self.validator.check_hw_sw_parity(graph, pinmap_wrong_mcu_pin),
            ["firmware_mcu_pin_mismatch"],
        )

        try:
            motor_channels = int(spec.get("actuation", {}).get("motor_channels", 0) or 0)
        except (TypeError, ValueError):
            motor_channels = 0
        motor_pwm_rows = [
            item for item in pinmap
            if re.fullmatch(r"MOTOR\d+_PWM", str(item.get("signal", "")))
        ]
        if motor_channels > 0 and motor_pwm_rows:
            pinmap_missing_pwm = [
                item for item in pinmap
                if item is not motor_pwm_rows[-1]
            ]
            record(
                "firmware_motor_pwm_channel_missing",
                "firmware_co_design",
                f"Removed firmware pinmap coverage for required motor PWM channel {motor_pwm_rows[-1].get('signal')}",
                self._firmware_interface_contract_report(path, spec, graph, pinmap_missing_pwm),
                ["firmware_motor_pwm_channel_missing"],
            )

        record(
            "firmware_sensor_poll_missing_bus",
            "firmware_co_design",
            "Added a sensor_poll firmware module for an SPI sensor when no matching SPI bus exists in the hardware graph",
            self.validator.check_firmware_modules(
                [{
                    "id": "phantom_spi_sensor",
                    "behavior": "sensor_poll",
                    "bus": "spi",
                    "sensor": "imu",
                    "poll_interval_ms": 100,
                }],
                pinmap,
                spec=spec,
                graph=graph,
            ),
            ["firmware_sensor_bus_missing"],
        )

        if int(spec.get("actuation", {}).get("motor_channels", 0) or 0) > 0 and spec.get("safety", {}).get("emergency_stop", {}).get("required"):
            record(
                "missing_firmware_estop_shutdown_behavior",
                "firmware_co_design",
                "Removed the e-stop timeout_shutdown behavior while motor hardware remains present",
                self.validator.check_firmware_modules([], pinmap, spec=spec, graph=graph),
                ["missing_estop_shutdown_behavior"],
            )

        test_sources = self._firmware_test_sources(path)
        if any("CAN" in (name + text).upper() for name, text in test_sources.items()):
            test_sources_without_can = {
                name: text
                for name, text in test_sources.items()
                if "CAN" not in (name + text).upper()
            }
            record(
                "missing_firmware_can_bringup",
                "firmware_co_design",
                "Removed generated CAN bring-up test coverage while CAN hardware remains present",
                self._firmware_interface_contract_report(path, spec, graph, pinmap, test_sources=test_sources_without_can),
                ["firmware_can_bringup_missing"],
            )

        dependency_report = self._design_dependency_graph_report([
            GateReport(
                "pin_symbol_footprint",
                Status.FAIL,
                [Failure(FailureCategory.ELECTRICAL_SEMANTIC_ERROR, "synthetic_pin_contract_failure", "Synthetic prerequisite failure")],
            ),
            GateReport("firmware_pinmap", Status.PASS, []),
        ])
        record(
            "dependency_graph_prerequisite_violation",
            "long_horizon_agent_reliability",
            "Reported firmware_pinmap pass while pin_symbol_footprint failed",
            dependency_report,
            ["dependency_prerequisite_not_passed"],
        )

        detected = sum(1 for item in cases if item["detected"])
        result = {
            "status": "pass" if detected == len(cases) else "fail",
            "project": project,
            "benchmark": "hardware_grounding_v0",
            "summary": {
                "total_cases": len(cases),
                "detected_cases": detected,
                "missed_cases": len(cases) - detected,
            },
            "cases": cases,
            "physical_oracle_limits": [
                "This benchmark checks digital grounding failure detection. Thermal, EMI/EMC, SI/PI, vibration, ingress, connector fatigue, and board bring-up still require simulation or physical qualification evidence.",
            ],
            "artifact": str(benchmark_path),
        }
        write_json(benchmark_path, result)
        return result

    def generate_physical_qualification_plan(self, project: str) -> dict[str, Any]:
        path = self.workspace.require_project(project)
        spec = self.read_spec(project)
        graph_path = path / "electronics" / "generated" / "electrical_graph.json"
        graph = json.loads(graph_path.read_text(encoding="utf-8")) if graph_path.is_file() else {"components": [], "nets": []}
        plan_dir = path / "validation" / "physical"
        plan_path = plan_dir / "qualification_plan.json"
        markdown_path = plan_dir / "qualification_plan.md"
        evidence_dir = path / "validation" / "physical_evidence"
        tests = self._physical_qualification_tests(spec, graph)
        plan = {
            "artifact_type": "physical_qualification_plan",
            "status": "generated",
            "project": project,
            "revision": spec.get("project", {}).get("revision"),
            "evidence_directory": str(evidence_dir),
            "release_gate": "physical_qualification",
            "tests": tests,
            "oracle_boundary": [
                "Digital validators cannot certify thermal load behavior, EMI/EMC, SI/PI, vibration, ingress, connector fatigue, assembly quality, or board bring-up.",
                "Each required test needs approved external evidence before the physical_qualification gate can pass.",
            ],
        }
        write_json(plan_path, plan)
        atomic_write_text(markdown_path, self._physical_qualification_markdown(plan))
        return {
            "status": "generated",
            "project": project,
            "artifact": str(plan_path),
            "markdown": str(markdown_path),
            "required_tests": [item["id"] for item in tests if item.get("required_for_release")],
            "evidence_directory": str(evidence_dir),
            "plan": plan,
        }

    def record_physical_evidence(self, project: str, evidence: dict[str, Any], approved: bool = False) -> dict[str, Any]:
        path = self.workspace.require_project(project)
        plan_result = self.generate_physical_qualification_plan(project)
        test_ids = {item["id"] for item in plan_result["plan"]["tests"]}
        test_id = evidence.get("test_id")
        if test_id not in test_ids:
            return {"status": "blocked", "code": "unknown_physical_test", "test_id": test_id, "known_tests": sorted(test_ids)}
        status = evidence.get("status", "pass")
        if status not in {"pass", "fail", "blocked"}:
            return {"status": "blocked", "code": "invalid_evidence_status", "allowed": ["pass", "fail", "blocked"]}

        evidence_files: list[dict[str, Any]] = []
        for raw in evidence.get("evidence_files", []):
            file_path = Path(raw)
            item: dict[str, Any] = {"path": str(file_path)}
            if file_path.is_file():
                payload = file_path.read_bytes()
                item.update({"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)})
            else:
                item.update({"missing": True})
            evidence_files.append(item)

        observed_at = evidence.get("observed_at") or datetime.now(UTC).isoformat()
        record = {
            "artifact_type": "physical_qualification_evidence",
            "test_id": test_id,
            "status": status,
            "approved": bool(approved or evidence.get("approved")),
            "observed_at": observed_at,
            "summary": evidence.get("summary", ""),
            "operator": evidence.get("operator"),
            "instrumentation": evidence.get("instrumentation", []),
            "measurements": evidence.get("measurements", {}),
            "evidence_files": evidence_files,
            "source": evidence.get("source", "manual"),
        }
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", test_id).strip("_")
        stamp = re.sub(r"[^0-9A-Za-z]+", "", observed_at)[:20] or datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        target = path / "validation" / "physical_evidence" / f"{safe_id}-{stamp}.json"
        write_json(target, record)
        gate = self._physical_qualification_report(path)
        persist_report(path, gate)
        return {"status": "generated", "project": project, "record": str(target), "gate": gate.to_dict()}

    @staticmethod
    def _physical_qualification_tests(spec: dict[str, Any], graph: dict[str, Any]) -> list[dict[str, Any]]:
        system = spec.get("system", {})
        mechanical = spec.get("mechanical", {})
        firmware = spec.get("firmware", {})
        actuation = spec.get("actuation", {})
        nets = {item.get("name", "") for item in graph.get("nets", [])}
        categories = {item.get("category", "") for item in graph.get("components", [])}
        has_usb = bool({"USB_DP", "USB_DM"} & nets) or "usb" in categories
        has_can = bool({"CANH", "CANL"} & nets) or "can" in categories
        has_motor = int(actuation.get("motor_channels", 0) or 0) > 0
        high_vibration = mechanical.get("vibration_environment") == "high"
        connectors_exposed = bool(mechanical.get("connectors_exposed", True))
        battery = system.get("supply", {}).get("battery", {})
        peak_current = battery.get("pack_current_peak_a")

        def test(
            test_id: str,
            category: str,
            objective: str,
            evidence: list[str],
            criteria: list[str],
            artifacts: list[str],
            conditions: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            return {
                "id": test_id,
                "category": category,
                "required_for_release": True,
                "objective": objective,
                "evidence_required": evidence,
                "acceptance_criteria": criteria,
                "linked_artifacts": artifacts,
                "conditions": conditions or {},
            }

        tests = [
            test(
                "assembly_inspection",
                "assembly_quality",
                "Verify fabricated board and assembly match the released BOM, orientation, polarity, and connector plan.",
                ["photos", "assembly_checklist", "bom_lot_trace"],
                ["No reversed polarized parts", "Connector orientation matches assembly drawing", "No visible solder bridges or tombstoning"],
                ["fabrication/bom.csv", "docs/assembly_drawing.pdf", "manifest.json"],
            ),
            test(
                "current_limited_power_up",
                "bringup",
                "Power the board with current limiting and verify expected rails before enabling loads.",
                ["bench_supply_log", "rail_voltage_measurements", "thermal_image_optional"],
                ["No input current runaway", "All required rails within tolerance", "No component exceeds safe touch temperature during idle bring-up"],
                ["docs/bringup_guide.md", "firmware/pinmap.h"],
                {"pack_voltage_max": battery.get("pack_voltage_max"), "pack_current_peak_a": peak_current},
            ),
            test(
                "firmware_interface_bringup",
                "firmware_co_design",
                "Flash firmware and verify pinmap-controlled interfaces against the assembled board.",
                ["flash_log", "interface_probe_log", "firmware_revision_hash"],
                ["Firmware image matches released source hash", "Pinmap self-test passes", "Required buses enumerate expected devices"],
                ["firmware/source.zip", "firmware/devicetree.overlay", "firmware/pinmap.h"],
                {"target": firmware.get("target")},
            ),
            test(
                "thermal_load_profile",
                "thermal",
                "Measure thermal behavior under representative load and ambient assumptions.",
                ["thermal_camera_capture", "load_profile_log", "ambient_temperature_log"],
                ["No component exceeds datasheet or enclosure thermal limit", "Board remains stable for the required dwell time"],
                ["docs/known_risks.md", "validation/reports/placement_constraints.json"],
                {"motor_channels": actuation.get("motor_channels"), "motor_channel_peak_current_a": actuation.get("motor_channel_peak_current_a")},
            ),
            test(
                "emi_emc_prescan",
                "emi_emc",
                "Run conducted/radiated emissions pre-scan or document certified exemption scope.",
                ["emi_prescan_report", "test_setup_photos", "cable_configuration"],
                ["No unmitigated emissions finding in intended operating mode", "Cable and enclosure configuration matches release use case"],
                ["fabrication/gerbers.zip", "mechanical/assembly.step"],
            ),
            test(
                "signal_power_integrity_probe",
                "si_pi",
                "Probe high-risk signal and power nets under representative switching and load conditions.",
                ["oscilloscope_captures", "probe_points", "load_state_log"],
                ["No excessive rail droop or ringing on required rails", "Digital bus eye/timing margin is acceptable for the selected interfaces"],
                ["electronics/generated/electrical_graph.json", "docs/layout_preview.pdf"],
                {"usb": has_usb, "can": has_can, "motor_outputs": has_motor},
            ),
            test(
                "vibration_connector_retention",
                "mechanical_reliability",
                "Verify connectors, mounts, and enclosure remain secure under expected vibration and handling.",
                ["vibration_test_log", "post_test_inspection_photos", "connector_retention_measurements"],
                ["No connector disengagement", "No mounting hardware loosening", "No enclosure or PCB crack"],
                ["mechanical/assembly.step", "docs/assembly_drawing.pdf"],
                {"vibration_environment": mechanical.get("vibration_environment"), "connectors_exposed": connectors_exposed, "high_vibration": high_vibration},
            ),
        ]
        if connectors_exposed:
            tests.append(test(
                "ingress_esd_abuse_check",
                "environmental_safety",
                "Verify exposed connectors and enclosure openings match the intended ingress, ESD, and abuse assumptions.",
                ["ingress_test_report_or_waiver", "esd_test_log_or_waiver", "enclosure_photos"],
                ["No unreviewed exposed conductor path", "Ingress/IP assumption is tested or explicitly waived", "ESD handling risk is documented"],
                ["mechanical/enclosure.step", "docs/known_risks.md"],
                {"connectors_exposed": connectors_exposed},
            ))
        return tests

    @staticmethod
    def _physical_qualification_markdown(plan: dict[str, Any]) -> str:
        lines = [
            "# Physical Qualification Plan",
            "",
            f"Project: `{plan.get('project')}`",
            f"Revision: `{plan.get('revision')}`",
            "",
            "This plan defines external evidence required before physical qualification can be claimed.",
            "",
            "## Required Tests",
        ]
        for item in plan.get("tests", []):
            lines.extend([
                "",
                f"### {item['id']}",
                f"- Category: `{item['category']}`",
                f"- Objective: {item['objective']}",
                f"- Evidence: {', '.join(item['evidence_required'])}",
                f"- Acceptance: {'; '.join(item['acceptance_criteria'])}",
            ])
        lines.extend(["", "## Oracle Boundary", ""])
        lines.extend(f"- {item}" for item in plan.get("oracle_boundary", []))
        return "\n".join(lines) + "\n"

    def _physical_qualification_report(self, project_path: Path) -> GateReport:
        plan_path = project_path / "validation" / "physical" / "qualification_plan.json"
        if not plan_path.is_file():
            return GateReport(
                "physical_qualification",
                Status.BLOCKED,
                [Failure(FailureCategory.RELEASE_ERROR, "physical_qualification_plan_missing", "Generate the physical qualification plan before release")],
            )
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        evidence_dir = project_path / "validation" / "physical_evidence"
        evidence_by_test: dict[str, list[dict[str, Any]]] = {}
        if evidence_dir.is_dir():
            for item in sorted(evidence_dir.glob("*.json")):
                try:
                    record = json.loads(item.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    record = {"test_id": item.stem, "status": "fail", "approved": False, "decode_error": True}
                record["record_path"] = str(item)
                evidence_by_test.setdefault(record.get("test_id", ""), []).append(record)

        failures: list[Failure] = []
        passed = 0
        blocked = False
        for test in plan.get("tests", []):
            if not test.get("required_for_release"):
                continue
            test_id = test["id"]
            approved_records = [item for item in evidence_by_test.get(test_id, []) if item.get("approved")]
            passing_records = [item for item in approved_records if item.get("status") == "pass"]
            failing_records = [item for item in approved_records if item.get("status") == "fail"]
            if failing_records:
                failures.append(Failure(
                    FailureCategory.RELEASE_ERROR,
                    "physical_evidence_failed",
                    f"Approved physical evidence failed: {test_id}",
                    path=f"validation.physical_evidence.{test_id}",
                    details={"records": [item.get("record_path") for item in failing_records]},
                ))
            elif passing_records:
                passed += 1
            else:
                blocked = True
                failures.append(Failure(
                    FailureCategory.RELEASE_ERROR,
                    "physical_evidence_missing",
                    f"Required physical qualification evidence is missing or unapproved: {test_id}",
                    path=f"validation.physical_evidence.{test_id}",
                    details={"category": test.get("category"), "evidence_required": test.get("evidence_required", [])},
                    requires_user_decision=True,
                ))

        status = Status.BLOCKED if blocked else (Status.FAIL if failures else Status.PASS)
        return GateReport(
            "physical_qualification",
            status,
            failures,
            metrics={
                "required_tests": sum(1 for item in plan.get("tests", []) if item.get("required_for_release")),
                "approved_passed": passed,
                "missing_or_unapproved": sum(1 for failure in failures if failure.code == "physical_evidence_missing"),
                "failed": sum(1 for failure in failures if failure.code == "physical_evidence_failed"),
            },
            artifacts=[str(plan_path), str(project_path / "validation" / "physical" / "qualification_plan.md")],
            backend={"name": "physical-qualification-ledger", "digital_only": False},
        )

    def apply_repair_plan(self, project: str, check_result: dict[str, Any] | None = None, approved: bool = False) -> dict[str, Any]:
        checks = check_result or self.run_all_checks(project, include_external=False)
        plan = self.generate_repair_plan(project, checks)
        applied: list[dict[str, Any]] = []
        proposals: list[dict[str, Any]] = []
        for action in plan["actions"]:
            for patch in action.get("patches", []):
                if patch.get("requires_approval") and not approved:
                    proposals.append({"failure_code": action["failure_code"], "patch": patch, "reason": action["action"]})
                else:
                    patch_result = self._apply_spec_patch(project, patch)
                    if patch_result["status"] == "pass":
                        applied.append({**patch, "approval_granted": bool(patch.get("requires_approval") and approved)})
                    else:
                        proposals.append({"failure_code": action["failure_code"], "patch": patch, "reason": patch_result["message"]})
        if applied:
            self._append_decision_record(project, proposals, applied)
            iteration_id = self.workspace.snapshot(project, {"goal": "auto-repair", "patches_applied": len(applied)})
            return {"status": "pass", "applied": applied, "proposals": proposals, "iteration_id": iteration_id}
        if proposals:
            return {"status": "blocked", "applied": [], "proposals": proposals, "requires_user_decision": True}
        return {"status": "generated", "applied": [], "proposals": []}

    def design_until_release(self, project: str, max_iterations: int = 8, include_external: bool = False) -> dict[str, Any]:
        iterations = []
        for _ in range(max_iterations):
            result = self.run_design_iteration(project, include_external=include_external)
            iterations.append({"iteration_id": result["iteration_id"], "status": result["status"], "failed_gates": result["failed_gates"]})
            if result["release_gate"]["status"] == "pass":
                checks = self.run_all_checks(project, include_external=include_external)
                prepared = self.prepare_release(project, checks, native_checks_confirmed=include_external)
                if prepared.get("status") != "released":
                    return {"status": "blocked", "iterations": iterations, "release": prepared}
                frozen_reports = [self._report_from_dict(item) for item in checks["reports"]]
                gate = self.check_release_gate(project, reports=frozen_reports, include_external=False)
                bundle = self.export_release_bundle(project, gate_result=gate)
                if bundle.get("status") == "released":
                    return {"status": "released", "iterations": iterations, "release": bundle}
                return {"status": "blocked", "iterations": iterations, "release_gate": gate}
            repair = self.apply_repair_plan(project, self.run_all_checks(project, include_external=False))
            if repair.get("applied"):
                continue
            if repair.get("requires_user_decision"):
                return {"status": "blocked", "iterations": iterations, "repair": repair, "release_gate": result["release_gate"]}
            # No repair to apply and no user decision required. If the only failing gates are
            # structural BLOCKED gates (external toolchain + physical evidence + backend limitation),
            # the design is software-ready — the agent should rerun with include_external=True
            # (or install the missing toolchain) to get native gate results.
            software_failures = [g for g in result["failed_gates"] if g not in _STRUCTURAL_BLOCKED_GATES]
            if not software_failures:
                return {
                    "status": "software_gates_ready",
                    "iterations": iterations,
                    "message": (
                        "All software gates pass. Native toolchain gates (ERC, DRC, autoroute, Zephyr) "
                        "are pending. Rerun with include_external=True when the toolchain is available, "
                        "or install the missing tools (see hw_diagnose_environment)."
                    ),
                    "pending_external_gates": [g for g in result["failed_gates"] if g in _EXTERNAL_GATES],
                    "release_gate": result["release_gate"],
                }
            return {"status": "fail", "iterations": iterations, "repair": repair, "release_gate": result["release_gate"]}
        return {"status": "fail", "code": "max_iterations_exceeded", "iterations": iterations}

    def run_design_benchmark(
        self,
        include_external: bool = False,
        keep_projects: bool = False,
    ) -> dict[str, Any]:
        """End-to-end held-out benchmark: one-line intent → design_until_release, measures pass-rate."""
        run_id = os.urandom(4).hex()
        spec_results: list[dict[str, Any]] = []
        benchmark_dir = self.workspace.root / "benchmarks"
        benchmark_dir.mkdir(parents=True, exist_ok=True)

        for spec in _BENCHMARK_SPECS:
            project = f"bench_{spec['id']}_{run_id}"
            try:
                self.create_project(project, template=spec["template"])
                self.update_requirements(project, spec["intent"])
                result = self.design_until_release(project, include_external=include_external)
                project_path = self.workspace.require_project(project)
                gate_summary: dict[str, Any] = {}
                iterations_dir = project_path / "history" / "iterations"
                passed_gates: list[str] = []
                failed_gates: list[str] = []
                if iterations_dir.is_dir():
                    iteration_dirs = sorted(iterations_dir.iterdir())
                    if iteration_dirs:
                        result_json = iteration_dirs[-1] / "result.json"
                        if result_json.is_file():
                            last = json.loads(result_json.read_text(encoding="utf-8"))
                            passed_gates = last.get("passed_gates", [])
                            failed_gates = last.get("failed_gates", [])
                            blocked_gates = last.get("blocked_gates", [])
                sw_fail = [g for g in failed_gates if g not in _STRUCTURAL_BLOCKED_GATES]
                sw_total = len(passed_gates) + len(sw_fail)
                sw_pass_rate = len(passed_gates) / sw_total if sw_total else 1.0
                physical_report = self._physical_qualification_report(project_path)
                physical_summary = self._physical_qualification_benchmark_summary(physical_report)
                # Native gate rate: only gates that ACTUALLY RAN (not blocked). Blocked
                # means the toolchain for that architecture is unavailable — it's not a
                # design defect, so excluded from the denominator.
                native_passed = [g for g in passed_gates if g in _EXTERNAL_GATES]
                native_actually_failed = [g for g in failed_gates if g in _EXTERNAL_GATES and g not in blocked_gates]
                native_total = len(native_passed) + len(native_actually_failed)
                native_pass_rate = len(native_passed) / native_total if native_total else 0.0
                gate_summary = {
                    "pass": len(passed_gates),
                    "fail": len(failed_gates),
                    "software_fail": len(sw_fail),
                    "total": len(passed_gates) + len(failed_gates),
                    "software_gate_pass_rate": round(sw_pass_rate, 4),
                    "native_gate_pass_rate": round(native_pass_rate, 4),
                    "native_passed": native_passed,
                    "native_failed": native_actually_failed,
                }
                spec_results.append({
                    "id": spec["id"],
                    "template": spec["template"],
                    "intent": spec["intent"],
                    "status": result.get("status", "fail"),
                    "iterations": len(result.get("iterations", [])),
                    "gate_summary": gate_summary,
                    "software_gate_pass_rate": round(sw_pass_rate, 4),
                    "native_gate_pass_rate": round(native_pass_rate, 4),
                    "gates_passed_count": len(passed_gates),
                    "gates_failed_count": len(failed_gates),
                    "physical_qualification_summary": physical_summary,
                })
            except Exception as exc:
                spec_results.append({
                    "id": spec["id"],
                    "template": spec["template"],
                    "intent": spec["intent"],
                    "status": "error",
                    "iterations": 0,
                    "gate_summary": {},
                    "error": str(exc),
                })
            finally:
                if not keep_projects:
                    try:
                        project_path_cleanup = self.workspace.project_path(project)
                        if project_path_cleanup.exists():
                            shutil.rmtree(project_path_cleanup)
                    except Exception:
                        pass

        passed = sum(1 for r in spec_results if r["status"] == "released")
        sw_ready = sum(1 for r in spec_results if r["status"] in {"released", "software_gates_ready"})
        total = len(spec_results)
        pass_rate = passed / total if total else 0.0
        sw_ready_rate = sw_ready / total if total else 0.0
        iteration_counts = [r["iterations"] for r in spec_results if r["iterations"] > 0]
        mean_iterations = sum(iteration_counts) / len(iteration_counts) if iteration_counts else 0.0
        overall_status = "pass" if passed == total else ("partial" if passed > 0 else "fail")
        sw_rates = [r["software_gate_pass_rate"] for r in spec_results if "software_gate_pass_rate" in r]
        mean_sw_pass_rate = round(sum(sw_rates) / len(sw_rates), 4) if sw_rates else 0.0
        native_rates = [r["native_gate_pass_rate"] for r in spec_results if "native_gate_pass_rate" in r]
        mean_native_pass_rate = round(sum(native_rates) / len(native_rates), 4) if native_rates else 0.0
        physical_summaries = [r["physical_qualification_summary"] for r in spec_results if "physical_qualification_summary" in r]
        physical_required = sum(item.get("required_tests", 0) for item in physical_summaries)
        physical_passed = sum(item.get("approved_passed", 0) for item in physical_summaries)
        physical_missing = sum(item.get("missing_or_unapproved", 0) for item in physical_summaries)
        physical_failed = sum(item.get("failed", 0) for item in physical_summaries)
        physical_ready = sum(1 for item in physical_summaries if item.get("status") == "pass")
        physical_gap_categories = sorted({
            category
            for item in physical_summaries
            for category in item.get("gap_categories", [])
        })

        benchmark_result: dict[str, Any] = {
            "status": overall_status,
            "benchmark": "design_benchmark_v0",
            "include_external": include_external,
            "summary": {
                "total": total,
                "passed": passed,
                "software_gates_ready": sw_ready,
                "failed": total - passed,
                "pass_rate": round(pass_rate, 4),
                "software_gates_ready_rate": round(sw_ready_rate, 4),
                "mean_iterations": round(mean_iterations, 2),
                "software_gate_pass_rate": mean_sw_pass_rate,
                "native_gate_pass_rate": mean_native_pass_rate,
                "physical_qualification_ready": physical_ready,
                "physical_qualification_ready_rate": round(physical_ready / total, 4) if total else 0.0,
                "physical_qualification_required_tests": physical_required,
                "physical_qualification_approved_passed": physical_passed,
                "physical_qualification_missing_or_unapproved": physical_missing,
                "physical_qualification_failed": physical_failed,
                "physical_qualification_gap_categories": physical_gap_categories,
            },
            "specs": spec_results,
        }
        artifact_path = benchmark_dir / f"design_benchmark_{run_id}.json"
        write_json(artifact_path, benchmark_result)
        benchmark_result["artifact"] = artifact_path.as_posix()
        return benchmark_result

    @staticmethod
    def _physical_qualification_benchmark_summary(report: GateReport) -> dict[str, Any]:
        gap_categories = sorted({
            str(failure.details.get("category"))
            for failure in report.failures
            if failure.code == "physical_evidence_missing" and failure.details.get("category")
        })
        return {
            "status": report.status.value,
            "required_tests": int(report.metrics.get("required_tests", 0) if report.metrics else 0),
            "approved_passed": int(report.metrics.get("approved_passed", 0) if report.metrics else 0),
            "missing_or_unapproved": int(report.metrics.get("missing_or_unapproved", 0) if report.metrics else 0),
            "failed": int(report.metrics.get("failed", 0) if report.metrics else 0),
            "gap_categories": gap_categories,
            "gate": report.to_dict(),
        }

    # ------------------------------------------------------------------
    # Candidate lifecycle
    # ------------------------------------------------------------------

    def list_candidates(self, project: str) -> dict[str, Any]:
        path = self.workspace.require_project(project)
        candidates_dir = path / "exports" / "candidates"
        candidates = []
        if candidates_dir.is_dir():
            for item in sorted(candidates_dir.iterdir()):
                candidate_json = item / "candidate.json"
                if not (item.is_dir() and candidate_json.is_file()):
                    continue
                data = json.loads(candidate_json.read_text(encoding="utf-8"))
                backend = data.get("backend") or data.get("generated", {}).get("backend") or "reference"
                reports = data.get("checks", {}).get("reports", [])
                candidates.append({
                    "candidate_id": data.get("iteration_id", item.name),
                    "backend": backend,
                    "status": data.get("checks", {}).get("status", "unknown"),
                    "gate_summary": {
                        "pass": sum(1 for r in reports if r["status"] == "pass"),
                        "fail": sum(1 for r in reports if r["status"] == "fail"),
                        "blocked": sum(1 for r in reports if r["status"] == "blocked"),
                        "total": len(reports),
                    },
                })
        return {"status": "pass", "project": project, "candidates": candidates, "count": len(candidates)}

    def get_candidate(self, project: str, candidate_id: str) -> dict[str, Any]:
        path = self.workspace.require_project(project)
        candidate_json = path / "exports" / "candidates" / candidate_id / "candidate.json"
        if not candidate_json.is_file():
            return {"status": "blocked", "code": "candidate_not_found", "candidate_id": candidate_id}
        data = json.loads(candidate_json.read_text(encoding="utf-8"))
        return {"status": "pass", "project": project, "candidate_id": candidate_id, "candidate": data}

    def review_candidate(self, project: str, candidate_id: str) -> dict[str, Any]:
        """Summarise a candidate from its frozen gate reports — does not read the live reports dir."""
        path = self.workspace.require_project(project)
        candidate_json = path / "exports" / "candidates" / candidate_id / "candidate.json"
        if not candidate_json.is_file():
            return {"status": "blocked", "code": "candidate_not_found", "candidate_id": candidate_id}
        data = json.loads(candidate_json.read_text(encoding="utf-8"))
        backend = data.get("backend") or data.get("generated", {}).get("backend") or "reference"
        reports = data.get("checks", {}).get("reports", [])
        pass_gates = [r["gate"] for r in reports if r["status"] == "pass"]
        fail_gates = [r["gate"] for r in reports if r["status"] == "fail"]
        blocked_gates_list = [r["gate"] for r in reports if r["status"] == "blocked"]
        blocking_gates = [{"gate": r["gate"], "status": r["status"], "failure_count": len(r.get("failures", []))} for r in reports if r["status"] != "pass"]
        backend_release_eligible = backend in _RELEASE_ELIGIBLE_BACKENDS
        overall_status = data.get("checks", {}).get("status", "unknown")
        assumptions_summary: dict[str, Any] | None = None
        iteration_spec = path / "history" / "iterations" / candidate_id / "spec" / "system.yaml"
        if iteration_spec.is_file():
            raw_assumptions = _assumptions_as_dict(read_yaml(iteration_spec).get("assumptions", {}))
            unresolved_critical = sorted(name for name, a in raw_assumptions.items() if a.get("critical") and a.get("requires_user_review"))
            assumptions_summary = {"total": len(raw_assumptions), "unresolved_critical": len(unresolved_critical), "unresolved_critical_names": unresolved_critical}
        release_blocking: list[str] = []
        if not backend_release_eligible:
            release_blocking.append(f"backend '{backend}' is candidate-only")
        release_blocking.extend(r["gate"] for r in reports if r["status"] != "pass")
        if blocking_gates and not backend_release_eligible:
            recommendation = f"Backend '{backend}' is candidate-only — switch to tscircuit, kicad, python_netlist, or atopile."
        elif blocking_gates:
            recommendation = f"{len(blocking_gates)} gate(s) blocking. Use hw_generate_repair_plan to identify fixes."
        else:
            recommendation = "All gates pass — confirm with hw_check_release_gate (include_external=true) before promoting."
        return {
            "status": overall_status,
            "project": project,
            "candidate_id": candidate_id,
            "backend": backend,
            "backend_release_eligible": backend_release_eligible,
            "gate_summary": {"total": len(reports), "pass": len(pass_gates), "fail": len(fail_gates), "blocked": len(blocked_gates_list)},
            "blocking_gates": blocking_gates,
            "release_blocking_failures": release_blocking,
            "assumptions": assumptions_summary,
            "recommendation": recommendation,
        }

    def compare_candidates(self, project: str, candidate_a: str, candidate_b: str) -> dict[str, Any]:
        path = self.workspace.require_project(project)

        def _load(cid: str) -> dict[str, Any] | None:
            p = path / "exports" / "candidates" / cid / "candidate.json"
            return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None

        data_a = _load(candidate_a)
        data_b = _load(candidate_b)
        if data_a is None:
            return {"status": "blocked", "code": "candidate_not_found", "candidate_id": candidate_a}
        if data_b is None:
            return {"status": "blocked", "code": "candidate_not_found", "candidate_id": candidate_b}

        def _gate_sets(data: dict[str, Any]) -> tuple[set[str], set[str], set[str]]:
            reports = data.get("checks", {}).get("reports", [])
            # Exclude "external_gate_not_run" blocks — these reflect invocation context, not design quality
            real = [r for r in reports if not any(f.get("code") == "external_gate_not_run" for f in r.get("failures", []))]
            return (
                {r["gate"] for r in real if r["status"] == "pass"},
                {r["gate"] for r in real if r["status"] == "fail"},
                {r["gate"] for r in real if r["status"] == "blocked"},
            )

        pass_a, fail_a, block_a = _gate_sets(data_a)
        pass_b, fail_b, block_b = _gate_sets(data_b)
        blocking_a = fail_a | block_a
        blocking_b = fail_b | block_b

        readiness_delta: dict[str, Any] = {
            "blocking_gates_removed": sorted(blocking_a - blocking_b),
            "blocking_gates_added": sorted(blocking_b - blocking_a),
            "pass_count_delta": len(pass_b) - len(pass_a),
            "fail_count_delta": len(fail_b) - len(fail_a),
            "blocked_count_delta": len(block_b) - len(block_a),
        }

        def _iter_files(cid: str) -> set[str]:
            it = path / "history" / "iterations" / cid
            if not it.is_dir():
                return set()
            return {
                item.relative_to(it).as_posix()
                for domain in ("electronics", "mechanical", "firmware")
                for item in (it / domain).rglob("*")
                if item.is_file() and "build" not in item.parts
            }

        files_a = _iter_files(candidate_a)
        files_b = _iter_files(candidate_b)
        common = files_a & files_b
        changed_files = sorted(
            f for f in common
            if (path / "history" / "iterations" / candidate_a / f).read_text(encoding="utf-8", errors="replace")
            != (path / "history" / "iterations" / candidate_b / f).read_text(encoding="utf-8", errors="replace")
        )
        artifact_delta: dict[str, Any] = {
            "added": sorted(files_b - files_a),
            "removed": sorted(files_a - files_b),
            "changed": changed_files,
        }

        def _unresolved(cid: str) -> set[str]:
            sp = path / "history" / "iterations" / cid / "spec" / "system.yaml"
            if not sp.is_file():
                return set()
            raw = read_yaml(sp).get("assumptions", {})
            return {name for name, a in raw.items() if a.get("critical") and a.get("requires_user_review")}

        unresolved_a = _unresolved(candidate_a)
        unresolved_b = _unresolved(candidate_b)
        risk_delta: dict[str, Any] = {
            "new_physical_gaps": [],
            "resolved_assumptions": sorted(unresolved_a - unresolved_b),
            "new_unresolved_assumptions": sorted(unresolved_b - unresolved_a),
        }

        improved = len(readiness_delta["blocking_gates_removed"])
        worsened = len(readiness_delta["blocking_gates_added"])
        still_blocking = bool(blocking_b)
        if not improved and not worsened and readiness_delta["pass_count_delta"] == 0:
            recommendation = f"{candidate_b} is equivalent to {candidate_a} — no gate changes detected."
        elif improved and not worsened:
            suffix = " Still not release-ready." if still_blocking else " Promote with hw_check_release_gate."
            recommendation = f"{candidate_b} is better — {improved} gate(s) resolved.{suffix}"
        elif worsened and not improved:
            recommendation = f"{candidate_b} is worse — {worsened} new blocking gate(s). Revert or repair."
        else:
            suffix = " Still not release-ready." if still_blocking else " Promote with hw_check_release_gate."
            recommendation = f"{candidate_b} is mixed — {improved} resolved, {worsened} new blocking gates.{suffix}"

        return {
            "status": "pass",
            "project": project,
            "candidate_a": candidate_a,
            "candidate_b": candidate_b,
            "readiness_delta": readiness_delta,
            "artifact_delta": artifact_delta,
            "risk_delta": risk_delta,
            "recommendation": recommendation,
        }

    # ------------------------------------------------------------------
    # Fabrication review preparation
    # ------------------------------------------------------------------

    def prepare_fabrication_review(self, project: str, candidate_id: str | None = None) -> dict[str, Any]:
        """Build a structured fabrication review packet from frozen candidate state."""
        path = self.workspace.require_project(project)
        spec = self.read_spec(project)
        backend = spec.get("electronics", {}).get("backend", "reference")

        if candidate_id is None:
            candidates_dir = path / "exports" / "candidates"
            if candidates_dir.is_dir():
                valid = sorted(d for d in candidates_dir.iterdir() if d.is_dir() and (d / "candidate.json").is_file())
                if valid:
                    candidate_id = valid[-1].name

        reports: list[dict[str, Any]] = []
        provenance: dict[str, Any] = {}
        if candidate_id:
            cjson = path / "exports" / "candidates" / candidate_id / "candidate.json"
            if cjson.is_file():
                cdata = json.loads(cjson.read_text(encoding="utf-8"))
                reports = cdata.get("checks", {}).get("reports", [])
            prov_path = path / "electronics" / "generated" / "electrical_graph.json"
            if prov_path.is_file():
                provenance = json.loads(prov_path.read_text(encoding="utf-8")).get("provenance", {})

        gate_status = {r["gate"]: r["status"] for r in reports}
        pass_count = sum(1 for s in gate_status.values() if s == "pass")
        fail_gates = [g for g, s in gate_status.items() if s == "fail"]
        blocked_gates_list = [g for g, s in gate_status.items() if s == "blocked"]
        erc_status = gate_status.get("native_erc", "not_run")
        drc_status = gate_status.get("native_drc", "not_run")

        ref_fab = path / "exports" / "candidates" / "reference-fabrication" / "fabrication"
        cand_fab = (path / "exports" / "candidates" / candidate_id / "fabrication") if candidate_id else None

        def _fab_present(name: str) -> bool:
            return (ref_fab / name).is_file() or bool(cand_fab and (cand_fab / name).is_file())

        artifact_presence: dict[str, bool] = {
            "gerbers.zip": _fab_present("gerbers.zip"),
            "drill.zip": _fab_present("drill.zip"),
            "bom.csv": _fab_present("bom.csv") or (path / "electronics" / "generated" / "bom.csv").is_file(),
            "pick_and_place.csv": _fab_present("pick_and_place.csv"),
        }

        req_path = path / "spec" / "requirements.yaml"
        unresolved_requirements: list[dict[str, Any]] = []
        if req_path.is_file():
            req_data = read_yaml(req_path).get("requirements", {})
            unresolved_requirements = [r for r in req_data.get("active_unresolved", []) if r.get("release_blocking")]

        raw_assumptions = _assumptions_as_dict(spec.get("assumptions", {}))
        unresolved_assumptions = sorted(name for name, a in raw_assumptions.items() if a.get("critical") and a.get("requires_user_review"))

        backend_release_eligible = backend in _RELEASE_ELIGIBLE_BACKENDS
        has_blocking = bool(fail_gates or blocked_gates_list)
        all_fab_artifacts = all(artifact_presence.values())
        if not backend_release_eligible or not all_fab_artifacts or erc_status != "pass" or drc_status != "pass":
            label = "do_not_fabricate"
        elif has_blocking or unresolved_requirements or unresolved_assumptions:
            label = "review_only"
        else:
            label = "release_candidate"

        return {
            "status": "pass",
            "project": project,
            "candidate_id": candidate_id,
            "label": label,
            "backend": backend,
            "backend_release_eligible": backend_release_eligible,
            "gate_status": {
                "pass": pass_count,
                "fail": len(fail_gates),
                "blocked": len(blocked_gates_list),
                "fail_gates": fail_gates,
                "blocked_gates": blocked_gates_list,
            },
            "erc_status": erc_status,
            "drc_status": drc_status,
            "toolchain_versions": provenance.get("tool_versions", {}),
            "artifact_presence": artifact_presence,
            "unresolved_assumptions": unresolved_assumptions,
            "unresolved_requirements": unresolved_requirements,
            "physical_qualification_gaps": [
                "EMI/EMC compliance not validated by digital checks",
                "Full-load thermal behavior requires instrumented hardware testing",
                "Vibration life, connector fatigue, and ingress protection require physical qualification",
            ],
            "fab_review_checklist": [
                "[ ] Verify schematic revision matches fab package",
                "[ ] Confirm board stackup matches fabrication house constraints",
                "[ ] Verify BOM against supplier stock and lead times",
                "[ ] Confirm ERC/DRC status is pass (not blocked)",
                "[ ] Review all unresolved assumptions and requirements",
                "[ ] Verify pick-and-place orientation against assembly drawing",
                "[ ] Confirm enclosure STEP clears board component height",
                "[ ] Sign-off by responsible engineer before ordering",
            ],
        }

    # ------------------------------------------------------------------
    # Environment diagnosis
    # ------------------------------------------------------------------

    def diagnose_environment(self, target: str = "fabrication_release", backend: str | None = None) -> dict[str, Any]:
        """Return target-conditioned diagnosis: what is missing and what gates are blocked."""
        import shutil
        _TARGETS: dict[str, dict[str, Any]] = {
            "fabrication_release": {
                "description": "Fabrication-ready release — ERC, DRC, autorouting, and Gerber export",
                "required_tools": ["kicad_cli", "java"],
                "required_gates": ["native_erc", "native_drc", "kicad_library_crosscheck", "autoroute"],
            },
            "candidate": {
                "description": "Candidate artifacts — no native toolchain required",
                "required_tools": [],
                "required_gates": [],
            },
            "firmware_only": {
                "description": "Firmware build — ARM cross-compiler required",
                "required_tools": ["arm_none_eabi_gcc"],
                "required_gates": ["native_zephyr_build"],
            },
            "full_release": {
                "description": "Full release with all native validations",
                "required_tools": ["kicad_cli", "java", "arm_none_eabi_gcc", "cadquery_ocp"],
                "required_gates": ["native_erc", "native_drc", "kicad_library_crosscheck", "autoroute", "native_zephyr_build", "native_mechanical_validation"],
            },
        }
        if target not in _TARGETS:
            return {"status": "blocked", "code": "unknown_target", "target": target, "available_targets": sorted(_TARGETS)}

        tool_availability: dict[str, bool] = {
            "kicad_cli":         bool(resolve_tool("kicad-cli")),
            "java":              bool(self.freerouting._tools().get("java")),
            "node":              bool(shutil.which("node")),
            "arm_none_eabi_gcc": bool(shutil.which("arm-none-eabi-gcc")),
            "ato":               bool(shutil.which("ato")),
            "cadquery_ocp":      _cadquery_available(),
        }
        target_spec = _TARGETS[target]
        required_tools = list(target_spec["required_tools"])
        _backend_tools: dict[str, str] = {"tscircuit": "node", "kicad": "kicad_cli", "atopile": "ato"}
        if backend and backend in _backend_tools:
            extra = _backend_tools[backend]
            if extra not in required_tools:
                required_tools.append(extra)
        missing_tools = [t for t in required_tools if not tool_availability.get(t, False)]
        _tool_gates: dict[str, list[str]] = {
            "kicad_cli":         ["native_erc", "native_drc", "kicad_library_crosscheck"],
            "java":              ["autoroute"],
            "arm_none_eabi_gcc": ["native_zephyr_build"],
            "cadquery_ocp":      ["native_mechanical_validation"],
            "node":              ["tscircuit_compile", "tscircuit_manufacturing_export"],
            "ato":               ["atopile_compile"],
        }
        blocked_by_missing: set[str] = set()
        for t in missing_tools:
            blocked_by_missing.update(_tool_gates.get(t, []))
        blocked_gates = sorted(blocked_by_missing & set(target_spec["required_gates"]))

        _hints: dict[str, dict[str, list[str]]] = {
            "kicad_cli": {
                "macos": ["brew install kicad", "# or: make toolchains"],
                "linux": ["sudo apt-get install kicad", "# or: make toolchains"],
                "docker": ["docker run ghcr.io/mrcha033/hw-cli:latest hw ..."],
                "ci": ["# use ghcr.io/mrcha033/hw-cli:latest image in CI"],
            },
            "java": {
                "macos": ["brew install openjdk", "# or: make toolchains"],
                "linux": ["sudo apt-get install default-jre", "# or: make toolchains"],
                "docker": ["docker run ghcr.io/mrcha033/hw-cli:latest hw ..."],
            },
            "arm_none_eabi_gcc": {
                "macos": ["brew install arm-none-eabi-gcc", "# or: make toolchains"],
                "linux": ["sudo apt-get install gcc-arm-none-eabi", "# or: make toolchains"],
                "docker": ["docker run ghcr.io/mrcha033/hw-cli:latest hw ..."],
            },
            "cadquery_ocp": {
                "macos": ["uv pip install cadquery-ocp", "# or: make toolchains"],
                "linux": ["uv pip install cadquery-ocp", "# or: make toolchains"],
                "docker": ["docker run ghcr.io/mrcha033/hw-cli:latest hw ..."],
            },
            "node": {
                "macos": ["brew install node", "# or: make toolchains"],
                "linux": ["curl -fsSL https://fnm.vercel.app/install | bash && fnm use --install-if-missing 22"],
                "docker": ["docker run ghcr.io/mrcha033/hw-cli:latest hw ..."],
            },
        }
        install_hints: dict[str, list[str]] = {}
        for t in missing_tools:
            for platform_key, hints in _hints.get(t, {}).items():
                install_hints.setdefault(platform_key, []).extend(hints)

        return {
            "status": "pass" if not missing_tools else "fail",
            "target": target,
            "backend": backend,
            "description": target_spec["description"],
            "ready": not missing_tools,
            "missing_tools": missing_tools,
            "blocked_gates": blocked_gates,
            "install_hints": install_hints,
            "tool_availability": tool_availability,
        }

    def get_capabilities(self) -> dict[str, Any]:
        """Return available backends, external tools, and which gates each tool enables."""
        import shutil
        backends: dict[str, Any] = {
            "reference":      {"name": "reference",      "release_eligible": False, "candidate_only": True,  "release_tier": "candidate",   "fabrication_release_eligible": False, "description": "Reference intent generator — produces candidate artifacts only"},
            "tscircuit":      {"name": "tscircuit",      "release_eligible": True,  "candidate_only": False, "release_tier": "fabrication", "fabrication_release_eligible": True,  "description": "tscircuit Circuit JSON compiler — canonical fabrication backend via compiled KiCad export", "requires_tool": "node"},
            "kicad":          {"name": "kicad",          "release_eligible": True,  "candidate_only": False, "release_tier": "fabrication", "fabrication_release_eligible": True,  "description": "KiCad-native canonical fabrication backend", "requires_tool": "kicad_cli"},
            "python_netlist": {"name": "python_netlist", "release_eligible": True,  "candidate_only": False, "release_tier": "netlist",     "fabrication_release_eligible": False, "description": "Python netlist backend — release-eligible only at netlist tier"},
            "atopile":        {"name": "atopile",        "release_eligible": True,  "candidate_only": False, "release_tier": "hdl_source",  "fabrication_release_eligible": False, "description": "atopile backend — release-eligible HDL source; fabrication requires a KiCad bridge", "requires_tool": "ato"},
        }
        tools: dict[str, Any] = {
            "kicad_cli":         {"available": bool(resolve_tool("kicad-cli")),          "description": "KiCad CLI — native ERC, DRC, and Gerber export",        "gates": ["native_erc", "native_drc", "kicad_library_crosscheck"]},
            "java":              {"available": bool(self.freerouting.tools().get("java")), "description": "Java runtime — Freerouting autorouter",                    "gates": ["autoroute"]},
            "node":              {"available": bool(shutil.which("node")),               "description": "Node.js runtime (required by tscircuit toolchain; does not verify tscircuit package installation)", "gates": ["tscircuit_compile", "tscircuit_manufacturing_export"]},
            "arm_none_eabi_gcc": {"available": bool(shutil.which("arm-none-eabi-gcc")), "description": "ARM cross-compiler — native Zephyr firmware build",        "gates": ["native_zephyr_build"]},
            "ato":               {"available": bool(shutil.which("ato")),               "description": "atopile CLI — atopile backend compilation",                "gates": ["atopile_compile"]},
            "cadquery_ocp":      {"available": _cadquery_available(),                   "description": "cadquery-ocp — native mechanical CAD validation and export", "gates": ["native_mechanical_validation"]},
        }
        missing_external_gates: list[str] = sorted({gate for t in tools.values() if not t["available"] for gate in t["gates"]})
        return {
            "status": "pass",
            "backends": backends,
            "release_tiers": dict(_RELEASE_TIER_BY_BACKEND),
            "release_eligible_backends": [n for n, b in backends.items() if b["release_eligible"]],
            "fabrication_release_backends": [n for n in backends if n in _FABRICATION_RELEASE_BACKENDS],
            "netlist_release_backends": [n for n, b in backends.items() if b["release_tier"] == "netlist"],
            "source_release_backends": [n for n, b in backends.items() if b["release_tier"] == "hdl_source"],
            "candidate_only_backends": [n for n, b in backends.items() if b["candidate_only"]],
            "external_tools": tools,
            "missing_external_gates": missing_external_gates,
            "platform_root": str(self.root),
        }

    def review_release_readiness(self, project: str) -> dict[str, Any]:
        """Summarise release readiness from persisted gate reports without re-running checks."""
        path = self.workspace.require_project(project)
        spec = self.read_spec(project)
        backend = spec.get("electronics", {}).get("backend", "reference")

        persisted_reports: list[dict[str, Any]] = []
        report_mtimes: list[float] = []
        reports_dir = path / "validation" / "reports"
        if reports_dir.is_dir():
            for item in sorted(reports_dir.glob("*.json")):
                try:
                    data = json.loads(item.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and "gate" in data and "status" in data:
                        persisted_reports.append(data)
                        report_mtimes.append(item.stat().st_mtime)
                except (json.JSONDecodeError, OSError):
                    pass

        # Compare spec file timestamps vs report timestamps to detect possible staleness.
        # mtime is a best-effort heuristic — git checkouts can reset it.
        spec_dir = path / "spec"
        spec_mtimes = [f.stat().st_mtime for f in spec_dir.rglob("*") if f.is_file()] if spec_dir.is_dir() else []
        if not report_mtimes:
            data_freshness = "none"
        elif spec_mtimes:
            data_freshness = "possibly_stale" if max(spec_mtimes) > min(report_mtimes) else "current"
        else:
            data_freshness = "unknown"

        has_gate_data = bool(persisted_reports)
        pass_count = sum(1 for r in persisted_reports if r["status"] == "pass")
        fail_count = sum(1 for r in persisted_reports if r["status"] == "fail")
        blocked_count = sum(1 for r in persisted_reports if r["status"] == "blocked")
        blocking = [r for r in persisted_reports if r["status"] != "pass"]
        blocker_categories: list[str] = sorted({f["category"] for r in blocking for f in r.get("failures", [])})

        req_path = path / "spec" / "requirements.yaml"
        requirements_summary: dict[str, Any] | None = None
        release_blocking_requirements: list[dict[str, Any]] = []
        if req_path.is_file():
            req_data = read_yaml(req_path).get("requirements", {})
            active_unresolved = req_data.get("active_unresolved", [])
            release_blocking_requirements = [r for r in active_unresolved if r.get("release_blocking")]
            requirements_summary = {
                "active_lowered_count": len(req_data.get("active_lowered", [])),
                "active_unresolved_count": len(active_unresolved),
                "release_blocking_count": len(release_blocking_requirements),
                "release_blocking": release_blocking_requirements,
            }

        raw_assumptions = _assumptions_as_dict(spec.get("assumptions", {}))
        unresolved_critical = sorted(name for name, a in raw_assumptions.items() if a.get("critical") and a.get("requires_user_review"))
        assumptions_summary = {
            "total": len(raw_assumptions),
            "unresolved_critical": len(unresolved_critical),
            "unresolved_critical_names": unresolved_critical,
        }

        backend_release_eligible = backend in _RELEASE_ELIGIBLE_BACKENDS

        release_blocking_failures: list[str] = []
        if not backend_release_eligible:
            release_blocking_failures.append(f"backend '{backend}' is candidate-only and not release-eligible")
        release_blocking_failures.extend(r["gate"] for r in blocking)
        release_blocking_failures.extend(f"requirement:{req.get('source', req.get('id', '?'))}" for req in release_blocking_requirements)
        release_blocking_failures.extend(f"critical_assumption:{name}" for name in unresolved_critical)

        if not has_gate_data:
            overall_status = "blocked"
            recommendation = "No gate reports found. Run hw_run_all_checks first, then hw_review_release_readiness."
        elif not backend_release_eligible:
            overall_status = "blocked"
            recommendation = f"Backend '{backend}' is candidate-only. Switch to tscircuit, kicad, python_netlist, or atopile."
        elif blocking:
            overall_status = "blocked" if blocked_count > 0 else "fail"
            recommendation = f"{len(blocking)} gate(s) blocking. Run hw_generate_repair_plan → hw_apply_repair_plan."
        elif release_blocking_requirements:
            overall_status = "fail"
            recommendation = f"{len(release_blocking_requirements)} release-blocking requirement(s) unresolved. Use hw_update_requirements or hw_resolve_assumption."
        elif unresolved_critical:
            overall_status = "fail"
            recommendation = f"{len(unresolved_critical)} unresolved critical assumption(s). Use hw_resolve_assumption."
        elif data_freshness == "possibly_stale":
            overall_status = "pass"
            recommendation = "Persisted gates appear to pass but reports may be stale — the spec was modified after the last check run. Re-run hw_run_all_checks to refresh, then confirm with hw_check_release_gate (include_external=true)."
        else:
            overall_status = "pass"
            recommendation = "Persisted gates pass and no known blockers. Confirm with hw_check_release_gate (include_external=true) before hw_export_release_bundle."

        return {
            "status": overall_status,
            "release_eligible": False,
            "release_gate_authoritative": False,
            "readiness_estimate": overall_status,
            "candidate_only": not backend_release_eligible,
            "release_blocking_failures": release_blocking_failures,
            "project": project,
            "revision": spec["project"]["revision"],
            "backend": backend,
            "backend_release_eligible": backend_release_eligible,
            "gate_data": "persisted" if has_gate_data else "none",
            "data_freshness": data_freshness,
            "gate_summary": {"total": len(persisted_reports), "pass": pass_count, "fail": fail_count, "blocked": blocked_count},
            "blocking_gates": [{"gate": r["gate"], "status": r["status"], "failure_count": len(r.get("failures", []))} for r in blocking],
            "blocker_categories": blocker_categories,
            "requirements": requirements_summary,
            "assumptions": assumptions_summary,
            "physical_qualification_gaps": [
                "EMI/EMC compliance not validated by digital checks",
                "Full-load thermal behavior requires instrumented hardware testing",
                "Vibration life, connector fatigue, and ingress protection require physical qualification",
            ],
            "recommendation": recommendation,
        }

    def export_candidate_bundle(self, project: str) -> dict[str, Any]:
        """Export a candidate bundle from the current project state and run internal checks."""
        path = self.workspace.require_project(project)
        spec = self.read_spec(project)
        backend = spec.get("electronics", {}).get("backend", "reference")
        checks = self.run_all_checks(project, include_external=False)
        iteration_id = self.workspace.snapshot(project, {"goal": "candidate_export"})
        graph_path = path / "electronics" / "generated" / "electrical_graph.json"
        provenance = json.loads(graph_path.read_text(encoding="utf-8")).get("provenance", {}) if graph_path.is_file() else {}
        target = path / "exports" / "candidates" / iteration_id
        target.mkdir(parents=True, exist_ok=True)
        write_json(target / "candidate.json", {"status": "candidate", "candidate_only": True, "release_eligible": False, "iteration_id": iteration_id, "backend": backend, "checks": checks})
        sources = [(item, item.relative_to(path).as_posix()) for domain in ("electronics", "mechanical", "firmware") for item in (path / domain).rglob("*") if item.is_file() and "build" not in item.parts]
        bundle = target / f"{project}-{iteration_id}-candidate.zip"
        deterministic_zip(bundle, sources)
        write_manifest(target, target / "manifest.json", provenance=provenance, candidate_only=True)
        release_blocking_failures = [r["gate"] for r in checks["reports"] if r["status"] != "pass"]
        return {
            "status": "candidate",
            "candidate_only": True,
            "release_eligible": False,
            "release_blocking_failures": release_blocking_failures,
            "iteration_id": iteration_id,
            "backend": backend,
            "gate_status": checks.get("status", "unknown"),
            "bundle": str(bundle),
            "path": str(target),
        }

    # ------------------------------------------------------------------
    # Mechanical part design (agent-authored CAD)
    # ------------------------------------------------------------------

    def design_part(self, project: str, part_name: str, part_type: str, intent: dict[str, Any]) -> dict[str, Any]:
        """Design a parametric mechanical part from agent-specified intent."""
        from .backends.parts import PART_REGISTRY
        path = self.workspace.require_project(project)
        if part_type not in PART_REGISTRY:
            return {
                "status": "blocked",
                "code": "unknown_part_type",
                "part_type": part_type,
                "available_part_types": sorted(PART_REGISTRY),
            }
        parts_dir = path / "mechanical" / "parts" / part_name
        parts_dir.mkdir(parents=True, exist_ok=True)
        intent_path = parts_dir / "intent.json"
        write_json(intent_path, {"part_name": part_name, "part_type": part_type, "intent": intent})
        result = PART_REGISTRY[part_type]().design(intent, parts_dir, part_name)
        if result.get("status") not in ("blocked",):
            manifest = {
                "part_name": part_name,
                "part_type": part_type,
                "intent": intent,
                "artifacts": result.get("artifacts", []),
                "printability": result.get("printability", {}),
                "gate_status": result.get("gate_report", {}).get("status", "unknown"),
                "candidate_only": True,
                "release_eligible": False,
            }
            write_json(parts_dir / "part_manifest.json", manifest)
        return result

    def list_parts(self, project: str) -> dict[str, Any]:
        """List all designed parts for a project."""
        path = self.workspace.require_project(project)
        parts_root = path / "mechanical" / "parts"
        parts = []
        if parts_root.is_dir():
            for item in sorted(parts_root.iterdir()):
                if not item.is_dir():
                    continue
                manifest_path = item / "part_manifest.json"
                if manifest_path.is_file():
                    data = json.loads(manifest_path.read_text(encoding="utf-8"))
                    parts.append({
                        "part_name": data.get("part_name", item.name),
                        "part_type": data.get("part_type", "unknown"),
                        "gate_status": data.get("gate_status", "unknown"),
                        "printability": data.get("printability", {}).get("printable"),
                        "artifact_count": len(data.get("artifacts", [])),
                    })
                else:
                    intent_path = item / "intent.json"
                    if intent_path.is_file():
                        data = json.loads(intent_path.read_text(encoding="utf-8"))
                        parts.append({
                            "part_name": data.get("part_name", item.name),
                            "part_type": data.get("part_type", "unknown"),
                            "gate_status": "unknown",
                            "printability": None,
                            "artifact_count": 0,
                        })
        return {"status": "pass", "project": project, "parts": parts, "count": len(parts)}

    def get_part_types(self) -> dict[str, Any]:
        """Return available part types with their intent schemas."""
        from .backends.parts import PART_DESCRIPTIONS, PART_INTENT_SCHEMAS, PART_REGISTRY
        return {
            "status": "pass",
            "part_types": {
                pt: {
                    "description": PART_DESCRIPTIONS.get(pt, ""),
                    "intent_schema": PART_INTENT_SCHEMAS.get(pt, {}),
                }
                for pt in sorted(PART_REGISTRY)
            },
            "count": len(PART_REGISTRY),
        }

    # ------------------------------------------------------------------
    # Electronics topology authoring (Phase B)
    # ------------------------------------------------------------------

    def propose_circuit_block(self, category: str, interface: dict[str, Any] | None = None) -> dict[str, Any]:
        """Look up candidate components from the curated parts DB by category."""
        import yaml
        components_dir = self.parts_root / "components"
        if not components_dir.is_dir():
            return {"status": "blocked", "code": "parts_db_missing", "message": "Curated parts database not found"}
        candidates: list[dict[str, Any]] = []
        for path in sorted(components_dir.glob("*.yaml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for item in data.get("components", []):
                if item.get("category") == category:
                    candidates.append({
                        "component_id": item["id"],
                        "mpn": item.get("mpn", ""),
                        "manufacturer": item.get("manufacturer", ""),
                        "package": item.get("package", ""),
                        "lifecycle": item.get("lifecycle", "unknown"),
                        "pins": item.get("pins", []),
                    })
        return {
            "status": "pass",
            "category": category,
            "candidates": candidates,
            "count": len(candidates),
            "usage_hint": (
                "Select a component_id and call hw_add_circuit_block with ref, category, mpn, "
                "footprint, and connections (pin_name -> net_name mapping)."
            ),
        }

    def add_circuit_block(self, project: str, block: dict[str, Any]) -> dict[str, Any]:
        """Add an agent-authored circuit block to the project spec and re-run ERC."""
        path = self.workspace.require_project(project)
        ref = block.get("ref") or block.get("id")
        if not ref:
            return {"status": "blocked", "code": "missing_ref",
                    "message": "block must include 'ref' (e.g. 'U7')"}
        if not block.get("connections"):
            return {"status": "blocked", "code": "missing_connections",
                    "message": "block must include 'connections' mapping {pin_name: net_name}"}
        block = {**block, "ref": ref, "id": ref}

        # Check for ref conflict with base topology
        base_spec = {**self.read_spec(project), "agent_electronics": {}}
        from .reference_backend import build_graph, internal_erc
        base_graph = build_graph(base_spec)
        base_refs = {c["ref"] for c in base_graph["components"]}
        if ref in base_refs:
            return {
                "status": "blocked",
                "code": "ref_conflict",
                "message": f"Ref '{ref}' already exists in base topology. Choose a different ref.",
                "existing_refs": sorted(base_refs),
            }

        # Persist to spec/agent_blocks.yaml (key: agent_electronics, sorted after system.yaml)
        agent_path = path / "spec" / "agent_blocks.yaml"
        agent_data: dict[str, Any] = read_yaml(agent_path) if agent_path.is_file() else {}
        existing = agent_data.setdefault("agent_electronics", {}).get("blocks", [])
        existing = [b for b in existing if (b.get("ref") or b.get("id")) != ref]
        existing.append(block)
        agent_data["agent_electronics"]["blocks"] = existing
        write_yaml(agent_path, agent_data)

        merged_spec = self.read_spec(project)
        graph = build_graph(merged_spec)
        erc_report = internal_erc(graph)
        persist_report(path, erc_report)

        return {
            "status": erc_report.status.value,
            "project": project,
            "ref": ref,
            "components_total": len(graph["components"]),
            "nets_total": len(graph["nets"]),
            "gate_report": erc_report.to_dict(),
        }

    def list_circuit_blocks(self, project: str) -> dict[str, Any]:
        """List all agent-authored circuit blocks for a project."""
        path = self.workspace.require_project(project)
        agent_path = path / "spec" / "agent_blocks.yaml"
        blocks: list[dict[str, Any]] = []
        if agent_path.is_file():
            data = read_yaml(agent_path)
            blocks = data.get("agent_electronics", {}).get("blocks", [])
        return {"status": "pass", "project": project, "blocks": blocks, "count": len(blocks)}

    # ------------------------------------------------------------------
    # PCB placement constraints (Phase C)
    # ------------------------------------------------------------------

    def set_placement_constraint(self, project: str, constraint: dict[str, Any]) -> dict[str, Any]:
        """Add or update a PCB placement constraint in the project spec."""
        path = self.workspace.require_project(project)
        ref = constraint.get("ref")
        if not ref:
            return {"status": "blocked", "code": "missing_ref",
                    "message": "constraint must include 'ref' (the component reference to constrain)"}
        relationship = constraint.get("relationship")
        if relationship not in {"adjacent_to", "near_connector", "same_side", "opposite_side", "thermal_separation"}:
            return {
                "status": "blocked",
                "code": "invalid_relationship",
                "message": "relationship must be one of: adjacent_to, near_connector, same_side, opposite_side, thermal_separation",
            }

        # Check that ref exists in the current graph
        spec = self.read_spec(project)
        from .reference_backend import build_graph
        graph = build_graph(spec)
        known_refs = {c["ref"] for c in graph["components"]}
        if ref not in known_refs:
            return {
                "status": "blocked",
                "code": "ref_not_in_bom",
                "message": f"Ref '{ref}' is not in the current BOM. Add it first via hw_add_circuit_block.",
                "known_refs": sorted(known_refs),
            }
        if constraint.get("target") and constraint["target"] not in known_refs:
            return {
                "status": "blocked",
                "code": "target_not_in_bom",
                "message": f"Target '{constraint['target']}' is not in the current BOM.",
                "known_refs": sorted(known_refs),
            }

        agent_path = path / "spec" / "agent_blocks.yaml"
        agent_data: dict[str, Any] = read_yaml(agent_path) if agent_path.is_file() else {}
        existing = agent_data.setdefault("placement", {}).get("constraints", [])
        existing = [c for c in existing if c.get("ref") != ref]
        existing.append(constraint)
        agent_data["placement"]["constraints"] = existing
        write_yaml(agent_path, agent_data)

        # Re-run placement gate so the agent immediately sees compliance
        updated_spec = self.read_spec(project)
        gate_report = check_placement(propose_placement(updated_spec, graph), graph)
        persist_report(path, gate_report)

        return {
            "status": gate_report.status.value,
            "project": project,
            "ref": ref,
            "constraint": constraint,
            "constraint_count": len(existing),
            "placement_gate": {
                "status": gate_report.status.value,
                "errors": gate_report.metrics.get("errors", 0),
                "violations": [f.code for f in gate_report.failures if f.severity == "error"],
            },
        }

    def list_placement_constraints(self, project: str) -> dict[str, Any]:
        """List all agent-authored PCB placement constraints for a project."""
        path = self.workspace.require_project(project)
        agent_path = path / "spec" / "agent_blocks.yaml"
        constraints: list[dict[str, Any]] = []
        if agent_path.is_file():
            data = read_yaml(agent_path)
            constraints = data.get("placement", {}).get("constraints", [])
        return {"status": "pass", "project": project, "constraints": constraints, "count": len(constraints)}

    # ------------------------------------------------------------------
    # Firmware module authoring (Phase D)
    # ------------------------------------------------------------------

    def design_firmware_module(self, project: str, module_spec: dict[str, Any]) -> dict[str, Any]:
        """Author a firmware module from a behavior spec and write it into the project."""
        from .backends.firmware_modules import _RENDERERS, render_module
        from .validation import persist_report

        path = self.workspace.require_project(project)
        behavior = module_spec.get("behavior")
        if behavior not in _RENDERERS:
            return {
                "status": "blocked",
                "code": "unknown_behavior",
                "behavior": behavior,
                "available_behaviors": sorted(_RENDERERS),
            }
        mid = module_spec.get("id")
        if not mid or not mid.replace("_", "").isalnum():
            return {
                "status": "blocked",
                "code": "invalid_module_id",
                "message": "id must be a non-empty alphanumeric+underscore identifier",
            }

        try:
            output = render_module(module_spec)
        except Exception as exc:
            return {"status": "blocked", "code": "render_error", "message": str(exc)}

        modules_dir = path / "firmware" / "modules"
        modules_dir.mkdir(parents=True, exist_ok=True)
        c_path = modules_dir / f"{mid}.c"
        h_path = modules_dir / f"{mid}.h"
        from .io import atomic_write_text
        atomic_write_text(c_path, output.c_source)
        atomic_write_text(h_path, output.h_source)

        # Persist the module spec into firmware.yaml so generate_firmware picks it up
        firmware_yaml = path / "spec" / "firmware.yaml"
        firmware_data = read_yaml(firmware_yaml) if firmware_yaml.is_file() else {"firmware": {}}
        fw = firmware_data.setdefault("firmware", {})
        existing_modules: list[dict[str, Any]] = fw.get("modules", [])
        existing_modules = [m for m in existing_modules if m.get("id") != mid]
        existing_modules.append(module_spec)
        fw["modules"] = existing_modules
        write_yaml(firmware_yaml, firmware_data)

        # Run the firmware module validation gate
        spec = self.read_spec(project)
        pinmap_path = path / "firmware" / "generated" / "pinmap.json"
        import json as _json
        pinmap = _json.loads(pinmap_path.read_text(encoding="utf-8")) if pinmap_path.is_file() else []
        graph_path = path / "electronics" / "generated" / "electrical_graph.json"
        graph = _json.loads(graph_path.read_text(encoding="utf-8")) if graph_path.is_file() else {"components": [], "nets": []}
        gate_report = self.validator.check_firmware_modules(fw.get("modules", []), pinmap, spec=spec, graph=graph)
        persist_report(path, gate_report)

        artifacts = [str(c_path), str(h_path)]
        if output.dts_fragment:
            artifacts.append("devicetree.overlay (fragment ready — regenerate firmware to apply)")

        return {
            "status": gate_report.status.value,
            "project": project,
            "module_id": mid,
            "behavior": behavior,
            "artifacts": artifacts,
            "kconfig_flags": output.kconfig_flags,
            "stack_size_bytes": output.stack_size_bytes,
            "gate_report": gate_report.to_dict(),
        }

    def list_firmware_modules(self, project: str) -> dict[str, Any]:
        """List firmware modules currently authored in the project spec."""
        spec = self.read_spec(project)
        modules = spec.get("firmware", {}).get("modules", [])
        return {
            "status": "pass",
            "project": project,
            "modules": [
                {
                    "id": m.get("id"),
                    "behavior": m.get("behavior"),
                    "summary": _module_summary(m),
                }
                for m in modules
            ],
            "count": len(modules),
        }

    # ------------------------------------------------------------------
    # Unified agent workflow (Phase E)
    # ------------------------------------------------------------------

    def record_design_decision(self, project: str, domain: str, decision: str, rationale: str) -> dict[str, Any]:
        """Append a structured design decision to history/decisions.jsonl."""
        path = self.workspace.require_project(project)
        decisions_path = path / "history" / "decisions.jsonl"
        import uuid
        record = {
            "decision_id": f"dec_{uuid.uuid4().hex[:8]}",
            "timestamp": datetime.now(UTC).isoformat(),
            "domain": domain,
            "decision": decision,
            "rationale": rationale,
        }
        existing = []
        if decisions_path.is_file():
            import json as _json
            existing = [_json.loads(line) for line in decisions_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        existing.append(record)
        atomic_write_text(decisions_path, "\n".join(json.dumps(r) for r in existing) + "\n")
        return {
            "status": "pass",
            "project": project,
            "decision_id": record["decision_id"],
            "domain": domain,
            "decision": decision,
            "rationale": rationale,
        }

    def check_cross_domain_consistency(self, project: str) -> dict[str, Any]:
        """Validate that agent-authored content references valid artifacts across domains."""
        from .models import Failure, FailureCategory, Status
        path = self.workspace.require_project(project)
        spec = self.read_spec(project)
        from .reference_backend import build_graph
        graph = build_graph(spec)
        bom_refs = {c["ref"] for c in graph["components"]}
        failures: list[Failure] = []

        # Check placement constraints reference real BOM refs
        agent_path = path / "spec" / "agent_blocks.yaml"
        agent_data: dict[str, Any] = read_yaml(agent_path) if agent_path.is_file() else {}
        for constraint in agent_data.get("placement", {}).get("constraints", []):
            ref = constraint.get("ref")
            if ref and ref not in bom_refs:
                failures.append(Failure(FailureCategory.EDA_ERROR, "placement_ref_not_in_bom",
                    f"Placement constraint ref '{ref}' is not in the BOM", path=f"placement.constraints.{ref}"))
            target = constraint.get("target")
            if target and target not in bom_refs:
                failures.append(Failure(FailureCategory.EDA_ERROR, "placement_target_not_in_bom",
                    f"Placement constraint target '{target}' is not in the BOM", path=f"placement.constraints.{ref}"))

        # Check firmware modules reference signals in the pinmap
        pinmap_path = path / "firmware" / "generated" / "pinmap.json"
        if pinmap_path.is_file():
            pinmap = json.loads(pinmap_path.read_text(encoding="utf-8"))
            known_signals = {item.get("signal") for item in pinmap if item.get("signal")}
            for mod in spec.get("firmware", {}).get("modules", []):
                signal = mod.get("trigger", {}).get("signal")
                if signal and known_signals and signal not in known_signals:
                    failures.append(Failure(FailureCategory.FIRMWARE_ERROR, "firmware_signal_not_in_pinmap",
                        f"Firmware module '{mod.get('id')}' references signal '{signal}' not in pinmap",
                        path=f"firmware.modules.{mod.get('id')}"))

        from .models import GateReport
        report = GateReport("cross_domain_consistency",
                            Status.FAIL if failures else Status.PASS, failures,
                            metrics={"bom_refs": len(bom_refs), "constraints_checked": len(agent_data.get("placement", {}).get("constraints", []))},
                            backend={"name": "cross-domain-consistency", "deterministic": True})
        persist_report(path, report)
        return report.to_dict()

    def _apply_spec_patch(self, project: str, patch: dict[str, Any]) -> dict[str, Any]:
        if patch.get("operation") != "replace":
            return {"status": "fail", "message": f"Unsupported patch operation: {patch.get('operation')}"}
        section = patch["section"]
        spec_path = patch["spec_path"]
        value = patch["value"]
        file_path = self.workspace.require_project(project) / "spec" / f"{section}.yaml"
        if not file_path.is_file():
            return {"status": "fail", "message": f"Patch target section does not exist: {section}"}
        data = read_yaml(file_path)
        parts = spec_path.split(".")
        node: Any = data
        for part in parts[:-1]:
            if isinstance(node, list):
                # List traversal: find item by id field
                node = next((item for item in node if isinstance(item, dict) and item.get("id") == part), None)
                if node is None:
                    return {"status": "fail", "message": f"Patch target id does not exist: {part}"}
            else:
                if not isinstance(node, dict) or part not in node:
                    return {"status": "fail", "message": f"Patch target path does not exist: {spec_path}"}
                node = node[part]
        if isinstance(node, list):
            target_id = parts[-1]
            found = False
            for item in node:
                if isinstance(item, dict) and item.get("id") == target_id:
                    item["status"] = value
                    found = True
                    break
            if not found:
                return {"status": "fail", "message": f"Patch target id does not exist: {target_id}"}
        else:
            if not isinstance(node, dict):
                return {"status": "fail", "message": f"Patch parent is not a mapping: {spec_path}"}
            node[parts[-1]] = value
        write_yaml(file_path, data)
        return {"status": "pass", "message": "Patch applied"}

    def _append_decision_record(self, project: str, proposals: list[dict[str, Any]], applied: list[dict[str, Any]]) -> None:
        path = self.workspace.require_project(project) / "history" / "decisions.md"
        timestamp = datetime.now(UTC).isoformat()
        lines = [f"\n## Auto-repair {timestamp}\n"]
        if applied:
            lines.append("### Applied patches\n")
            for patch in applied:
                decision = "approved waiver/repair" if patch.get("approval_granted") else "automatic safe repair"
                lines.append(f"- `{patch['spec_path']}` -> `{patch['value']}` ({decision}; source `{patch.get('source_gate')}:{patch.get('source_failure')}`)")
        if proposals:
            lines.append("\n### Pending proposals (require user decision)\n")
            for p in proposals:
                lines.append(f"- [{p['failure_code']}] {p['reason']}")
        lines.append("")
        existing = path.read_text(encoding="utf-8")
        atomic_write_text(path, existing + "\n".join(lines))

    def _write_candidate_bundle(self, project: str, iteration_id: str, generated: dict[str, Any], checks: dict[str, Any]) -> dict[str, Any]:
        path = self.workspace.require_project(project)
        target = path / "exports" / "candidates" / iteration_id
        target.mkdir(parents=True, exist_ok=True)
        metadata = {"status": "candidate", "candidate_only": True, "release_eligible": False, "iteration_id": iteration_id, "generated": generated, "checks": checks}
        write_json(target / "candidate.json", metadata)
        sources = [(item, item.relative_to(path).as_posix()) for domain in ("electronics", "mechanical", "firmware") for item in (path / domain).rglob("*") if item.is_file() and "build" not in item.parts]
        bundle = target / f"{project}-{iteration_id}-candidate.zip"
        deterministic_zip(bundle, sources)
        graph_path = path / "electronics" / "generated" / "electrical_graph.json"
        provenance = json.loads(graph_path.read_text(encoding="utf-8")).get("provenance", {}) if graph_path.is_file() else {}
        write_manifest(target, target / "manifest.json", provenance=provenance, candidate_only=True)
        return {"status": "candidate", "candidate_only": True, "release_eligible": False, "iteration_id": iteration_id, "path": str(target), "bundle": str(bundle)}

    @staticmethod
    def _latest_board_step(project: Path) -> Path | None:
        candidates = [
            path for path in (project / "exports" / "candidates").rglob("board.step")
            if path.is_file()
        ] if (project / "exports" / "candidates").is_dir() else []
        return max(candidates, key=lambda path: path.stat().st_mtime_ns) if candidates else None

    @staticmethod
    def _geometry_report(path: Path, spec: dict[str, Any]) -> GateReport:
        stl = path / "exports" / spec["project"]["revision"] / "mechanical" / "enclosure.stl"
        # Geometry is generated after checks on the first iteration; spec-level fit remains authoritative there.
        failures = []
        if stl.exists():
            text = stl.read_text(encoding="utf-8")
            if text.count("facet normal") < 12 or not text.rstrip().endswith(("endsolid", "endsolid enclosure")):
                failures.append(Failure(FailureCategory.MECHANICAL_ERROR, "non_manifold_stl", "Generated STL is incomplete"))
        return GateReport("geometry", Status.FAIL if failures else Status.PASS, failures, metrics={"enclosure_dimensions_mm": spec["mechanical"]["enclosure_internal_mm"]}, backend={"name": "reference-geometry", "deterministic": True})

    @staticmethod
    def _artifact_integrity_report(release: Path, required: list[Path] | None = None) -> GateReport:
        manifest_path = release / "manifest.json"
        failures = []
        checked = 0
        if not manifest_path.is_file():
            failures.append(Failure(FailureCategory.RELEASE_ERROR, "missing_manifest", "Release manifest is missing"))
        else:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for entry in manifest.get("artifacts", []):
                artifact = release / entry["path"]
                checked += 1
                if not artifact.is_file():
                    failures.append(Failure(FailureCategory.RELEASE_ERROR, "manifest_file_missing", f"Manifest artifact is missing: {entry['path']}"))
                elif artifact.stat().st_size != entry["bytes"] or sha256(artifact) != entry["sha256"]:
                    failures.append(Failure(FailureCategory.RELEASE_ERROR, "checksum_mismatch", f"Artifact checksum mismatch: {entry['path']}"))
            if required:
                covered = {e["path"] for e in manifest.get("artifacts", [])}
                for artifact in required:
                    rel = artifact.relative_to(release).as_posix()
                    if rel not in covered:
                        failures.append(Failure(FailureCategory.RELEASE_ERROR, "required_artifact_uncovered_by_manifest", f"Required release artifact is not covered by manifest: {rel}"))
        return GateReport("artifact_integrity", Status.FAIL if failures else Status.PASS, failures, metrics={"checked_artifacts": checked}, artifacts=[str(manifest_path)] if manifest_path.exists() else [])

    def generate_design_report(self, project: str) -> dict[str, Any]:
        path = self.workspace.require_project(project)
        spec = self.read_spec(project)
        reports_dir = path / "validation" / "reports"
        reports = [
            report
            for item in sorted(reports_dir.glob("*.json"))
            if isinstance(report := json.loads(item.read_text(encoding="utf-8")), dict)
            and "gate" in report
            and "status" in report
        ]
        lines = [f"# Design Report: {project}", "", f"Generated: {datetime.now(UTC).isoformat()}", "", "## Scope", "", f"Target: {spec['project']['target_use']}; revision: {spec['project']['revision']}.", "", "## Validation", ""]
        lines.extend(f"- {item['gate']}: {item['status']} ({len(item.get('failures', []))} findings)" for item in reports)
        lines.extend(["", "## Known Physical Validation Gaps", "", "- Load current and thermal behavior require instrumented hardware testing.", "- EMI/EMC, vibration, abuse safety, transients, ingress, and connector fatigue are not certified by digital checks.", ""])
        output = path / "exports" / "working" / "documentation" / "design_report.md"
        atomic_write_text(output, "\n".join(lines))
        return {"status": "generated", "file": str(output)}

    def export_review(self, project: str) -> dict[str, Any]:
        """Write a normalized review bundle aggregating all gate reports and project metadata."""
        path = self.workspace.require_project(project)
        spec = self.read_spec(project)
        reports_dir = path / "validation" / "reports"
        gate_reports = sorted(
            [
                report
                for item in sorted(reports_dir.glob("*.json"))
                if isinstance(report := json.loads(item.read_text(encoding="utf-8")), dict)
                and "gate" in report
                and "status" in report
            ],
            key=lambda r: r["gate"],
        )
        gate_reports = _portable_review_value(gate_reports, self.workspace.root)

        # Placement summary from graph if available.
        graph_path = path / "electronics" / "generated" / "electrical_graph.json"
        placement_summary = None
        component_resolution_summary = None
        if graph_path.is_file():
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
            placement_data = graph.get("placement")
            if placement_data:
                placements = placement_data.get("placements", {})
                source_counts: dict[str, int] = {}
                for p in placements.values():
                    source_counts[p.get("source", "unknown")] = source_counts.get(p.get("source", "unknown"), 0) + 1
                unenforced = [c["kind"] for c in placement_data.get("constraints", []) if not c.get("enforced", True)]
                placement_summary = {
                    "board_width_mm": placement_data.get("board_width_mm"),
                    "board_height_mm": placement_data.get("board_height_mm"),
                    "placement_count": len(placements),
                    "constraint_count": len(placement_data.get("constraints", [])),
                    "source_counts": source_counts,
                    "unenforced_constraint_kinds": sorted(set(unenforced)),
                }
            res_report = graph.get("component_resolution_report")
            if res_report:
                metrics = res_report.get("metrics", {})
                component_resolution_summary = {
                    "resolved": metrics.get("resolved") or 0,
                    "requested": metrics.get("requested") or 0,
                    "supplier_provider": metrics.get("supplier_provider") or "unknown",
                    "status": res_report.get("status") or "unknown",
                }

        # Requirements summary.
        req_path = path / "spec" / "requirements.yaml"
        requirements_summary = None
        if req_path.is_file():
            from .io import read_yaml
            req_data = read_yaml(req_path).get("requirements", {})
            unresolved = req_data.get("active_unresolved", [])
            requirements_summary = {
                "active_lowered_count": len(req_data.get("active_lowered", [])),
                "active_unresolved_count": len(unresolved),
                "active_unresolved": unresolved,
            }

        # Assumptions summary.
        raw_assumptions = _assumptions_as_dict(spec.get("assumptions", {}))
        unresolved_critical = [name for name, a in raw_assumptions.items() if a.get("critical") and a.get("requires_user_review")]
        assumptions_summary = {
            "total": len(raw_assumptions),
            "unresolved_critical": len(unresolved_critical),
            "unresolved_critical_names": sorted(unresolved_critical),
        } if raw_assumptions else None

        # Gate summary counts.
        status_counts: dict[str, int] = {"pass": 0, "fail": 0, "blocked": 0, "other": 0}
        for r in gate_reports:
            s = r.get("status", "")
            if s in status_counts:
                status_counts[s] += 1
            else:
                status_counts["other"] += 1
        summary = {"total": len(gate_reports), **status_counts}

        # Iteration history — lean summaries only (stable fields, no mtime-derived data).
        iterations_dir = path / "history" / "iterations"
        iterations: list[dict[str, Any]] = []
        if iterations_dir.is_dir():
            for it_dir in sorted(iterations_dir.iterdir()):
                it_file = it_dir / "iteration.json"
                result_file = it_dir / "result.json"
                if not it_file.is_file():
                    continue
                it_data = json.loads(it_file.read_text(encoding="utf-8"))
                entry: dict[str, Any] = {
                    "iteration_id": it_data.get("iteration_id", it_dir.name),
                    "created_at": it_data.get("created_at", ""),
                    "goal": it_data.get("goal", ""),
                }
                if result_file.is_file():
                    result = json.loads(result_file.read_text(encoding="utf-8"))
                    entry["status"] = result.get("status", "")
                    entry["passed_gates"] = sorted(result.get("passed_gates", []))
                    entry["failed_gates"] = sorted(result.get("failed_gates", []))
                iterations.append(entry)

        # Release summary — read most recent exports/r*/manifest.json.
        release_summary = None
        artifacts: list[dict[str, Any]] = []
        exports_dir = path / "exports"
        if exports_dir.is_dir():
            release_dirs = sorted(d for d in exports_dir.iterdir() if d.is_dir() and d.name.startswith("r"))
            if release_dirs:
                latest = release_dirs[-1]
                manifest_path = latest / "manifest.json"
                if manifest_path.is_file():
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    artifact_list = manifest.get("artifacts", [])
                    release_summary = {
                        "release_id": latest.name,
                        "artifact_count": len(artifact_list),
                    }
                    # Keep only stable (path, sha256) pairs — no bytes/mtime.
                    artifacts = [{"path": a["path"], "sha256": a["sha256"]} for a in artifact_list if "path" in a and "sha256" in a]

        # Canonical content (generated_at excluded from hash for determinism).
        canonical: dict[str, Any] = {
            "bundle_version": "1.0",
            "project": {
                "name": project,
                "revision": spec["project"]["revision"],
                "target_use": spec["project"]["target_use"],
                "backend": spec.get("electronics", {}).get("backend", "reference"),
            },
            "gate_reports": gate_reports,
            "summary": summary,
            "placement": placement_summary,
            "component_resolution": component_resolution_summary,
            "requirements": requirements_summary,
            "assumptions": assumptions_summary,
            "iterations": iterations,
            "candidates": [],
            "release": release_summary,
            "artifacts": artifacts,
            "comments": [],
        }
        canonical_bytes = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        bundle_hash = hashlib.sha256(canonical_bytes).hexdigest()

        bundle: dict[str, Any] = {
            **canonical,
            "bundle_hash": bundle_hash,
            "generated_at": datetime.now(UTC).isoformat(),
        }

        output_dir = path / "exports" / "working" / "review"
        output_dir.mkdir(parents=True, exist_ok=True)
        bundle_path = output_dir / "bundle.json"
        write_json(bundle_path, bundle)

        from .review_report import generate_html_report
        html_path = generate_html_report(bundle_path)
        return {"status": "generated", "file": str(bundle_path), "html": str(html_path), "bundle_hash": bundle_hash, "gate_count": len(gate_reports)}

    def upload_review(self, project: str, destination: str | None = None) -> dict[str, Any]:
        """POST the review bundle to a hosted viewer endpoint."""
        export_result = self.export_review(project)
        bundle_path = Path(export_result["file"])
        if not destination:
            return {
                "status": "blocked",
                "code": "destination_required",
                "message": (
                    "Hosted upload requires an explicit destination URL. "
                    "Re-run with --destination <url> once you have configured a receiver endpoint "
                    "(e.g. hw serve-receiver --port 7476 on a shared machine)."
                ),
                "bundle": str(bundle_path),
                "bundle_hash": export_result["bundle_hash"],
            }
        import urllib.parse as _urlparse
        import urllib.request as _urlreq
        _parsed = _urlparse.urlparse(destination)
        if _parsed.scheme not in {"http", "https"}:
            return {
                "status": "blocked",
                "code": "invalid_destination",
                "message": "destination must be an http:// or https:// URL",
                "bundle": str(bundle_path),
                "bundle_hash": export_result["bundle_hash"],
            }
        bundle_bytes = bundle_path.read_bytes()
        req = _urlreq.Request(
            destination,
            data=bundle_bytes,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Bundle-Hash": export_result["bundle_hash"],
                "X-Bundle-Project": project,
            },
        )
        try:
            with _urlreq.urlopen(req, timeout=30) as resp:
                http_status = resp.getcode()
                body = resp.read(4096).decode(errors="replace")
        except Exception as exc:
            return {
                "status": "fail",
                "code": "upload_failed",
                "message": str(exc),
                "bundle": str(bundle_path),
                "bundle_hash": export_result["bundle_hash"],
                "destination": destination,
            }
        return {
            "status": "generated",
            "bundle": str(bundle_path),
            "bundle_hash": export_result["bundle_hash"],
            "destination": destination,
            "http_status": http_status,
            "response": body,
            "note": "Bundle uploaded. Ensure the receiver endpoint is on a private network if the bundle contains proprietary data.",
        }

    def export_standalone_review(self, project: str) -> dict[str, Any]:
        """Export a self-contained single-file HTML review (no server required)."""
        export_result = self.export_review(project)
        bundle_path = Path(export_result["file"])
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        comments_path = bundle_path.parent / "comments.jsonl"
        comments: list[dict] = []
        malformed_comment_lines = 0
        if comments_path.is_file():
            for line in comments_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        comments.append(json.loads(line))
                    except Exception:
                        malformed_comment_lines += 1
        from .review_viewer import build_standalone_html
        html = build_standalone_html({**bundle, "comments": comments})
        standalone_path = bundle_path.parent / "review_standalone.html"
        standalone_path.write_text(html, encoding="utf-8")
        result: dict[str, Any] = {
            "status": "generated",
            "file": str(standalone_path),
            "bundle_hash": export_result["bundle_hash"],
            "comment_count": len(comments),
            "note": "Self-contained HTML — share this file directly. No server required.",
        }
        if malformed_comment_lines:
            result["malformed_comment_lines"] = malformed_comment_lines
        return result

    def list_project_summaries(self) -> dict[str, Any]:
        """Return a summary of all projects in the workspace with their latest bundle status."""
        summaries = []
        for name in self.workspace.list_projects():
            bundle_path = self.workspace.project_path(name) / "exports" / "working" / "review" / "bundle.json"
            if bundle_path.is_file():
                try:
                    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
                    summary = bundle.get("summary", {})
                    summaries.append({
                        "name": name,
                        "has_bundle": True,
                        "generated_at": bundle.get("generated_at"),
                        "bundle_hash": (bundle.get("bundle_hash") or "")[:12],
                        "pass": summary.get("pass", 0),
                        "fail": summary.get("fail", 0),
                        "blocked": summary.get("blocked", 0),
                        "total": summary.get("total", 0),
                    })
                except Exception as exc:
                    summaries.append({"name": name, "has_bundle": False, "bundle_error": str(exc)})
            else:
                summaries.append({"name": name, "has_bundle": False})
        return {"status": "generated", "projects": summaries}

    def add_review_comment(
        self,
        project: str,
        text: str,
        target_type: str = "general",
        target_id: str | None = None,
        author: str | None = None,
        gate: str | None = None,
    ) -> dict[str, Any]:
        """Append a comment to the project's review comments sidecar."""
        import uuid
        from datetime import UTC, datetime
        path = self.workspace.require_project(project)
        comments_path = path / "exports" / "working" / "review" / "comments.jsonl"
        comments_path.parent.mkdir(parents=True, exist_ok=True)
        comments_path.touch(exist_ok=True)
        bundle_hash = ""
        bundle_path = comments_path.parent / "bundle.json"
        if bundle_path.is_file():
            try:
                bundle_hash = json.loads(bundle_path.read_text(encoding="utf-8")).get("bundle_hash", "")
            except Exception:
                pass
        entry: dict = {
            "id": str(uuid.uuid4()),
            "timestamp": datetime.now(UTC).isoformat(),
            "target_type": target_type,
            "target_id": target_id,
            "author": author,
            "gate": gate,
            "text": text,
            "bundle_hash": bundle_hash,
        }
        with comments_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        return {"status": "generated", "comment_id": entry["id"], "timestamp": entry["timestamp"]}

    def list_review_comments(self, project: str) -> dict[str, Any]:
        """Return all review comments for a project."""
        path = self.workspace.require_project(project)
        comments_path = path / "exports" / "working" / "review" / "comments.jsonl"
        comments: list[dict] = []
        malformed_lines = 0
        if comments_path.is_file():
            for line in comments_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        comments.append(json.loads(line))
                    except Exception:
                        malformed_lines += 1
        result: dict[str, Any] = {"status": "generated", "comments": comments, "count": len(comments)}
        if malformed_lines:
            result["malformed_lines"] = malformed_lines
        return result

    @staticmethod
    def _report_set(reports: list[GateReport]) -> dict[str, Any]:
        status = "pass" if all(item.passed for item in reports) else ("blocked" if any(item.status == Status.BLOCKED for item in reports) else "fail")
        return {"status": status, "reports": [item.to_dict() for item in reports]}

    @staticmethod
    def _report_from_dict(value: dict[str, Any]) -> GateReport:
        failures = [Failure(FailureCategory(item["category"]), item["code"], item["message"], item.get("severity", "error"), item.get("path"), item.get("details", {}), item.get("requires_user_decision", False)) for item in value.get("failures", [])]
        return GateReport(value["gate"], Status(value["status"]), failures, value.get("metrics", {}), value.get("artifacts", []), value.get("backend", {}))

    def _append_failures(self, project: str, iteration_id: str, checks: dict[str, Any]) -> None:
        path = self.workspace.require_project(project) / "history" / "failure_log.jsonl"
        existing = path.read_text(encoding="utf-8").strip()
        if existing == "[]":
            existing = ""
        entries = []
        for report in checks["reports"]:
            for failure in report["failures"]:
                entries.append(json.dumps({"iteration_id": iteration_id, "gate": report["gate"], **failure}, sort_keys=True))
        atomic_write_text(path, (existing + ("\n" if existing and entries else "") + "\n".join(entries) + ("\n" if entries else "")))
