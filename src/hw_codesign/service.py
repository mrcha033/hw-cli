from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .backends.kicad import KiCadBackend
from .backends.freerouting import FreeroutingBackend
from .backends.mechanical import OpenCascadeMechanicalBackend
from .backends.zephyr import ZephyrBackend
from .backends.tscircuit import TSCircuitBackend
from .backends.python_netlist import PythonNetlistBackend
from .backends.atopile import AtopileBackend
from .backends.electronics import CONTRACT_STAGES
from .artifacts import deterministic_zip, sha256, simple_pdf, write_manifest
from .generators import firmware_profile, generate_bom, generate_electronics, generate_firmware, generate_mechanical
from .io import atomic_write_text, read_yaml, write_json, write_yaml
from .models import Failure, FailureCategory, GateReport, RepairPatch, Status
from .policy import ChangePolicy
from .provenance import artifact_provenance
from .resources import resource_root
from .placement import check_placement, propose_placement
from .reference_backend import build_firmware_reference, export_fabrication, export_mechanical, internal_drc, internal_erc
from .validation import Validator, persist_report
from .workspace import Workspace


_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_POSIX_ABSOLUTE_PATH = re.compile(r"^/(?:[^/\s]+/)*[^/\s]*$")


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
    if value == root_text:
        return "."
    if value.startswith(f"{root_text}/"):
        return Path(value).resolve().relative_to(root).as_posix()
    if root_text in value:
        return value.replace(root_text, ".")
    if _WINDOWS_ABSOLUTE_PATH.match(value):
        normalized = value.replace("\\", "/")
        return f"<host-path>/{normalized.split('/')[-1]}"
    if _POSIX_ABSOLUTE_PATH.match(value):
        return f"<host-path>/{Path(value).name}"
    return value


class HardwareService:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        resources = resource_root(self.root)
        toolchain_root = Path(os.environ.get("HW_TOOLCHAIN_ROOT", self.root)).resolve()
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
        system_file = read_yaml(system_path); firmware_file = read_yaml(firmware_path); manufacturing_file = read_yaml(manufacturing_path)
        changed: list[str] = []
        patterns = [
            (r"(\d+)\s*(?:채널|channel)", lambda value: (system_file["actuation"].__setitem__("motor_channels", int(value)), "actuation.motor_channels")),
            (r"(?:각\s*채널\s*)?(?:피크\s*)?(\d+(?:\.\d+)?)\s*A", lambda value: (system_file["actuation"].__setitem__("motor_channel_peak_current_a", float(value)), "actuation.motor_channel_peak_current_a")),
            (r"(\d+(?:\.\d+)?)\s*V\s*(?:배터리|battery)", lambda value: (system_file["system"]["supply"]["battery"].__setitem__("pack_voltage_nominal", float(value)), "system.supply.battery.pack_voltage_nominal")),
            (r"(STM32H7\w*|ESP32S3|RP2040)", lambda value: (system_file["compute"]["mcu"].__setitem__("family", value.upper()), "compute.mcu.family")),
            (r"(\d+)\s*[- ]?layer", lambda value: (manufacturing_file["manufacturing"]["pcb"].__setitem__("layers", int(value)), "manufacturing.pcb.layers")),
        ]
        for pattern, setter in patterns:
            match = re.search(pattern, requirements_text, flags=re.IGNORECASE)
            if match:
                _, path = setter(match.group(1)); changed.append(path)
        lowered_text = requirements_text.lower()
        if "zephyr" in lowered_text:
            firmware_file["firmware"]["framework"] = "zephyr"; changed.append("firmware.framework")
        features = {"imu": "imu", "emergency stop": "e_stop", "e-stop": "e_stop", "비상 정지": "e_stop"}
        for token, key in features.items():
            if token in lowered_text:
                system_file["sensing"][key] = "required"; changed.append(f"sensing.{key}")
        write_yaml(system_path, system_file); write_yaml(firmware_path, firmware_file); write_yaml(manufacturing_path, manufacturing_file)
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
        matched = [(cat, label) for pat, cat, label in _unsupported if re.search(pat, requirements_text, re.IGNORECASE)]
        unsupported_constraints = [label for _, label in matched]
        req_file = read_yaml(req_path)
        if "requirements" not in req_file:
            req_file["requirements"] = {"raw_inputs": [], "active_lowered": [], "active_unresolved": []}
        existing_inputs = req_file["requirements"].get("raw_inputs", [])
        input_id = f"req_input_{len(existing_inputs) + 1:04d}"
        req_file["requirements"]["raw_inputs"] = [*existing_inputs, {"id": input_id, "text": requirements_text, "created_by": "user"}]
        req_file["requirements"]["active_lowered"] = [{"id": f"req_{i:04d}", "source": sp, "spec_path": sp, "status": "lowered"} for i, sp in enumerate(sorted(set(changed)), start=1)]
        req_file["requirements"]["active_unresolved"] = [
            {"id": f"req_unresolved_{i:04d}", "source": label, "category": cat, "status": "unresolved", "release_blocking": True, "reason": _reasons[cat]}
            for i, (cat, label) in enumerate(matched, start=1)
        ]
        write_yaml(req_path, req_file)
        return {"status": "generated", "has_unresolved_constraints": bool(unsupported_constraints), "mode": "replace_active_requirements", "changed_paths": sorted(set(changed)), "changed_files": [str(system_path), str(firmware_path), str(manufacturing_path), str(req_path)], "unresolved_requirements": unresolved_ambiguous, "unsupported_constraints": unsupported_constraints}

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
            reports.append(self.validator.check_component_metadata(graph["components"]))
            reports.append(self.validator.check_graph_pin_resolution(graph))
            reports.append(self.validator.check_hw_sw_parity(graph, pinmap))
        else:
            reports.append(GateReport("bom", Status.FAIL, [Failure(FailureCategory.BOM_ERROR, "missing_bom_source", "Generate electronics before checking the BOM")]))
        reports.extend([internal_erc(graph), internal_drc(path, spec, graph), build_firmware_reference(path)])
        if graph.get("components"):
            reports.append(check_placement(propose_placement(spec, graph), graph))
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
            library_failures = [failure for failure in [*erc.failures, *drc.failures] if failure.details.get("type") in {"lib_footprint_issues", "lib_footprint_mismatch"}]
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
        for report in reports:
            persist_report(path, report)
        return self._report_set(reports)

    def prepare_release(self, project: str, checks: dict[str, Any], native_checks_confirmed: bool = False) -> dict[str, Any]:
        path = self.workspace.require_project(project)
        spec = self.read_spec(project)
        backend = spec.get("electronics", {}).get("backend", "reference")
        if backend not in {"tscircuit", "kicad"}:
            return {"status": "blocked", "code": "compiled_electronics_backend_required", "reports": [GateReport("release_preparation", Status.BLOCKED, [Failure(FailureCategory.RELEASE_ERROR, "compiled_electronics_backend_required", "Release preparation requires a fully passing tscircuit or KiCad-native backend contract")]).to_dict()]}
        if not native_checks_confirmed:
            return {"status": "blocked", "code": "native_release_checks_required", "reports": checks["reports"]}
        if any(item["status"] != "pass" for item in checks["reports"]):
            return {"status": "blocked", "code": "release_gates_not_passed", "reports": checks["reports"]}
        unresolved_assumptions = [name for name, a in spec.get("assumptions", {}).items() if a.get("critical") and a.get("requires_user_review")]
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
        fabrication_report = self.kicad.export_manufacturing(path, staging)
        if fabrication_report.status == Status.PASS:
            files.extend(fabrication_report.artifacts)
        persist_report(path, fabrication_report)
        mechanical_report = self.mechanical.generate(spec, staging, graph=graph, board_step=staging / "mechanical" / "board.step", release_eligible=True)
        if mechanical_report.status == Status.PASS: files.extend(mechanical_report.artifacts)
        persist_report(path, mechanical_report)
        if mechanical_report.status != Status.PASS or fabrication_report.status != Status.PASS:
            shutil.rmtree(staging, ignore_errors=True)
            return {"status": "blocked" if Status.BLOCKED in {mechanical_report.status, fabrication_report.status} else "fail", "release_path": str(release), "reports": [fabrication_report.to_dict(), mechanical_report.to_dict()]}
        fabrication = staging / "fabrication"; fabrication.mkdir(parents=True, exist_ok=True)
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
        simple_pdf(f"{profile['model']} Schematic", [f"Components: {len(graph['components'])}", f"Nets: {len(graph['nets'])}", "See KiCad project for editable source."], docs / "schematic.pdf")
        simple_pdf("Assembly Drawing", ["Top-side placement is provided in fabrication/pick_and_place.csv.", "Verify connector orientation before assembly."], docs / "assembly_drawing.pdf")
        simple_pdf("Validation Report", statuses, docs / "validation_report.pdf")
        simple_pdf("Design Report", statuses + ["Physical qualification risks are listed in known_risks.md."], docs / "design_report.pdf")
        simple_pdf("Layout Preview", [f"Board envelope: {spec['mechanical']['envelope']['board_width_mm']} x {spec['mechanical']['envelope']['board_height_mm']} mm", f"Placement entries: {len(graph['components'])}"], docs / "layout_preview.pdf")
        simple_pdf("Bring-up Guide", self._release_bringup_pdf_lines(profile), docs / "bringup_guide.pdf")
        (staging / "fabrication" / "assembly_drawing.pdf").write_bytes((docs / "assembly_drawing.pdf").read_bytes())
        provenance = graph.get("provenance", {}) | {"release_eligible": True, "candidate_only": False}
        write_json(staging / "provenance.json", provenance)
        write_manifest(staging, staging / "manifest.json", provenance=provenance, candidate_only=False)
        release.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(release)
        files = [item.replace(str(staging), str(release), 1) for item in files]
        manifest = str(release / "manifest.json")
        reports = [fabrication_report.to_dict(), mechanical_report.to_dict()]
        for report in reports:
            report["artifacts"] = [item.replace(str(staging), str(release), 1) for item in report.get("artifacts", [])]
        return {"status": "released", "release_path": str(release), "files": files + [str(release / "firmware" / "source.zip"), manifest], "reports": reports}

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
        required = [
            release / "fabrication" / "gerbers.zip", release / "fabrication" / "drill.zip",
            release / "fabrication" / "bom.csv", release / "fabrication" / "pick_and_place.csv",
            release / "fabrication" / "assembly_drawing.pdf",
            release / "mechanical" / "board.step", release / "mechanical" / "enclosure.step", release / "mechanical" / "enclosure.stl", release / "mechanical" / "assembly.step",
            release / "mechanical" / "mechanical_manifest.json",
            release / "firmware" / "source.zip", release / "docs" / "design_report.md",
            release / "firmware" / "pinmap.h", release / "firmware" / "devicetree.overlay", release / "firmware" / "build_instructions.md",
            release / "docs" / "validation_report.json", release / "docs" / "bringup_guide.md", release / "docs" / "known_risks.md",
            release / "docs" / "schematic.pdf", release / "docs" / "layout_preview.pdf", release / "docs" / "design_report.pdf",
            release / "docs" / "validation_report.pdf", release / "docs" / "bringup_guide.pdf", release / "manifest.json",
        ]
        required.extend(release / "mechanical" / "variants" / f"enclosure_{variant['name']}.step" for variant in spec["mechanical"]["variants"])
        fixtures = spec["mechanical"].get("fixtures", {})
        if fixtures.get("mounting_plate", {}).get("enabled"):
            required.append(release / "mechanical" / "mounting_plate.step")
        if fixtures.get("frame_brackets", {}).get("enabled"):
            required.extend([release / "mechanical" / "frame_bracket_left.step", release / "mechanical" / "frame_bracket_right.step"])
        backend = spec.get("electronics", {}).get("backend", "reference")
        if backend not in {"tscircuit", "kicad"}:
            reports = [*reports, GateReport("backend_release_policy", Status.BLOCKED, [Failure(FailureCategory.RELEASE_ERROR, "compiled_electronics_backend_required", f"Backend {backend} is not release eligible")])]
        else:
            required_tsc_gates = {f"{backend}_{stage}" for stage in CONTRACT_STAGES}
            present_gates = {r.gate for r in reports}
            for gate_name in sorted(required_tsc_gates - present_gates):
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
        return {"status": "generated", "project": project, "requires_user_decision": requires_user_decision, "actions": actions}

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
        iteration_id = self.workspace.snapshot(project, {"goal": "make all release gates pass"})
        candidate = self._write_candidate_bundle(project, iteration_id, generated, checks)
        all_pass = all(item["status"] == "pass" for item in checks["reports"])
        blocked = any(item["status"] == "blocked" for item in checks["reports"])
        result = {
            "status": "pass" if all_pass else ("blocked" if blocked or repair_plan["requires_user_decision"] else "fail"),
            "iteration_id": iteration_id,
            "generated": generated,
            "passed_gates": [item["gate"] for item in checks["reports"] if item["status"] == "pass"],
            "failed_gates": [item["gate"] for item in checks["reports"] if item["status"] != "pass"],
            "repair_plan": repair_plan,
            "candidate": candidate,
            "release_gate": {"status": "pass" if all_pass else ("blocked" if blocked else "fail")},
        }
        write_json(self.workspace.require_project(project) / "history" / "iterations" / iteration_id / "result.json", result)
        self._append_failures(project, iteration_id, checks)
        return result

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
            return {"status": "fail", "iterations": iterations, "repair": repair, "release_gate": result["release_gate"]}
        return {"status": "fail", "code": "max_iterations_exceeded", "iterations": iterations}

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
        return {"status": "candidate", "candidate_only": True, "path": str(target), "bundle": str(bundle)}

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
        raw_assumptions = spec.get("assumptions", {})
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
        """Upload the review bundle to a hosted viewer.

        DEFERRED: This method builds and validates the local bundle but does not
        execute a real network upload without an explicit destination URL confirmed
        by the user.  Cloud execution (step 7) is intentionally out of scope for
        this implementation phase.
        """
        export_result = self.export_review(project)
        bundle_path = Path(export_result["file"])
        if not destination:
            return {
                "status": "blocked",
                "code": "destination_required",
                "message": (
                    "Hosted upload requires an explicit destination URL. "
                    "Re-run with --destination <url> once you have configured a viewer endpoint. "
                    "Cloud execution is deferred to a future implementation phase."
                ),
                "bundle": str(bundle_path),
                "bundle_hash": export_result["bundle_hash"],
            }
        # Mechanism stub: an actual HTTP PUT would go here once a destination is confirmed.
        return {
            "status": "blocked",
            "code": "hosted_upload_not_implemented",
            "message": "Hosted upload mechanism is defined but not yet wired to a live endpoint.",
            "bundle": str(bundle_path),
            "bundle_hash": export_result["bundle_hash"],
            "destination": destination,
        }

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
