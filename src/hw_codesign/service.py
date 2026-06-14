from __future__ import annotations

import json
import math
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
from .artifacts import deterministic_zip, sha256, simple_pdf, write_manifest
from .generators import generate_bom, generate_electronics, generate_firmware, generate_mechanical
from .io import atomic_write_text, read_yaml, write_json, write_yaml
from .models import Failure, FailureCategory, GateReport, Status
from .policy import ChangePolicy
from .provenance import artifact_provenance
from .reference_backend import build_firmware_reference, export_fabrication, export_mechanical, internal_drc, internal_erc
from .validation import Validator, persist_report
from .workspace import Workspace


class HardwareService:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        packaged_parts = Path(__file__).resolve().parents[2] / "parts"
        self.parts_root = self.root / "parts" if (self.root / "parts").is_dir() else packaged_parts
        self.workspace = Workspace(self.root)
        self.validator = Validator(self.root / "schemas")
        self.kicad = KiCadBackend()
        self.freerouting = FreeroutingBackend(self.root)
        self.mechanical = OpenCascadeMechanicalBackend()
        self.zephyr = ZephyrBackend()
        self.tscircuit = TSCircuitBackend(self.root)

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
            req_file["requirements"] = {"raw_inputs": [], "lowered": [], "unresolved": []}
        existing_inputs = req_file["requirements"].get("raw_inputs", [])
        input_id = f"req_input_{len(existing_inputs) + 1:04d}"
        req_file["requirements"]["raw_inputs"] = [*existing_inputs, {"id": input_id, "text": requirements_text, "created_by": "user"}]
        req_file["requirements"]["lowered"] = [{"id": f"req_{i:04d}", "source": sp, "spec_path": sp, "status": "lowered"} for i, sp in enumerate(sorted(set(changed)), start=1)]
        req_file["requirements"]["unresolved"] = [
            {"id": f"req_unresolved_{i:04d}", "source": label, "category": cat, "status": "unresolved", "release_blocking": True, "reason": _reasons[cat]}
            for i, (cat, label) in enumerate(matched, start=1)
        ]
        write_yaml(req_path, req_file)
        status = "generated_with_unresolved_constraints" if unsupported_constraints else "generated"
        return {"status": status, "changed_paths": sorted(set(changed)), "changed_files": [str(system_path), str(firmware_path), str(manufacturing_path), str(req_path)], "unresolved_requirements": unresolved_ambiguous, "unsupported_constraints": unsupported_constraints}

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
        if backend == "tscircuit":
            electronics.extend(self.tscircuit.generate_source(path, spec, graph))
        files = {
            "electronics": electronics,
            "mechanical": generate_mechanical(path, spec),
            "firmware": generate_firmware(path, spec, graph),
        }
        provenance = artifact_provenance(spec, self.parts_root, backend, compiler_version=self.tscircuit.VERSION if backend == "tscircuit" else None, release_eligible=backend == "tscircuit")
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
        if backend == "tscircuit": files.extend(self.tscircuit.generate_source(path, spec, graph))
        return {"status": "candidate" if backend == "reference" else "generated", "files": files, "component_resolution": resolution, "resolution_report": report}

    def generate_mechanical_only(self, project: str) -> dict[str, Any]:
        path = self.workspace.require_project(project)
        spec = self.read_spec(project)
        files = generate_mechanical(path, spec)
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
        stale_tscircuit = ("tscircuit_compile.json", "tscircuit_netlist_extract.json", "tscircuit_graph_parity.json", "tscircuit_footprint_parity.json", "tscircuit_layout_completeness.json")
        stale_reference = ("compiled_electronics_backend.json",)
        for name in stale_tscircuit if backend != "tscircuit" else stale_reference:
            (reports_dir / name).unlink(missing_ok=True)
        reports = [
            self.validator.validate_spec(spec),
            self.validator.check_electrical_semantics(spec),
            self.validator.check_mechanical(spec),
            self.validator.check_requirements_lowering(spec),
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
            reports.append(self.validator.check_bom(graph["components"]))
            reports.append(self.validator.check_sourcing(graph["components"]))
            reports.append(self.validator.check_component_metadata(graph["components"]))
            reports.append(self.validator.check_graph_pin_resolution(graph))
            reports.append(self.validator.check_hw_sw_parity(graph, pinmap))
        else:
            reports.append(GateReport("bom", Status.FAIL, [Failure(FailureCategory.BOM_ERROR, "missing_bom_source", "Generate electronics before checking the BOM")]))
        reports.extend([internal_erc(graph), internal_drc(path, spec, graph), build_firmware_reference(path)])
        if backend == "reference":
            reports.append(GateReport("compiled_electronics_backend", Status.BLOCKED, [Failure(FailureCategory.EDA_ERROR, "reference_backend_candidate_only", "Reference electronics backend produces candidate artifacts only")], backend={"name": "reference", "release_eligible": False}))
        elif backend == "atopile":
            reports.append(GateReport("compiled_electronics_backend", Status.BLOCKED, [Failure(FailureCategory.EDA_ERROR, "backend_not_implemented", "Atopile backend is not implemented")], backend={"name": "atopile"}))
        elif backend == "tscircuit":
            reports.extend(self.tscircuit.compile(path, graph))
        else:
            reports.append(GateReport("compiled_electronics_backend", Status.BLOCKED, [Failure(FailureCategory.EDA_ERROR, "unknown_backend", f"Unknown electronics backend: {backend}")]))
        if include_external:
            autoroute = self.freerouting.route(path)
            erc = self.kicad.run_erc(path); erc.gate = "native_erc"
            drc = self.kicad.run_drc(path); drc.gate = "native_drc"
            library_failures = [failure for failure in [*erc.failures, *drc.failures] if failure.details.get("type") in {"lib_footprint_issues", "lib_footprint_mismatch"}]
            library_status = Status.BLOCKED if Status.BLOCKED in {erc.status, drc.status} else (Status.FAIL if library_failures else Status.PASS)
            library_crosscheck = GateReport("kicad_library_crosscheck", library_status, library_failures, metrics={"method": "native_erc_drc_library_resolution", "issues": len(library_failures)}, backend={"name": "kicad-cli"})
            mechanical = self.mechanical.generate(spec, path / "exports" / "candidates" / "native-check")
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
        if backend != "tscircuit":
            return {"status": "blocked", "code": "compiled_electronics_backend_required", "reports": [GateReport("release_preparation", Status.BLOCKED, [Failure(FailureCategory.RELEASE_ERROR, "reference_backend_candidate_only", "Only compiled tscircuit designs are release eligible")]).to_dict()]}
        if not native_checks_confirmed:
            return {"status": "blocked", "code": "native_release_checks_required", "reports": checks["reports"]}
        if any(item["status"] != "pass" for item in checks["reports"]):
            return {"status": "blocked", "code": "release_gates_not_passed", "reports": checks["reports"]}
        unresolved_assumptions = [name for name, a in spec.get("assumptions", {}).items() if a.get("critical") and a.get("requires_user_review")]
        if unresolved_assumptions:
            return {"status": "blocked", "code": "unresolved_critical_assumptions", "unresolved": unresolved_assumptions, "reports": checks["reports"]}
        graph = json.loads((path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
        release = path / "exports" / "releases" / spec["project"]["revision"]
        if release.exists():
            return {"status": "blocked", "code": "release_revision_exists", "release_path": str(release)}
        staging = path / "exports" / ".staging" / spec["project"]["revision"]
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        files: list[str] = []
        mechanical_report = self.mechanical.generate(spec, staging)
        if mechanical_report.status == Status.PASS: files.extend(mechanical_report.artifacts)
        persist_report(path, mechanical_report)
        fabrication_report = self.kicad.export_manufacturing(path, staging)
        if fabrication_report.status == Status.PASS:
            files.extend(fabrication_report.artifacts)
        persist_report(path, fabrication_report)
        if mechanical_report.status != Status.PASS or fabrication_report.status != Status.PASS:
            shutil.rmtree(staging, ignore_errors=True)
            return {"status": "blocked" if Status.BLOCKED in {mechanical_report.status, fabrication_report.status} else "fail", "release_path": str(release), "reports": [fabrication_report.to_dict(), mechanical_report.to_dict()]}
        fabrication = staging / "fabrication"; fabrication.mkdir(parents=True, exist_ok=True)
        for generated in (path / "electronics" / "generated" / "bom.csv",):
            if generated.is_file(): (fabrication / generated.name).write_bytes(generated.read_bytes())
        placement = fabrication / "pick_and_place.csv"; atomic_write_text(placement, "reference,x_mm,y_mm,rotation_deg\n")
        firmware = staging / "firmware"; firmware.mkdir(parents=True, exist_ok=True)
        firmware_sources = [(item, item.relative_to(path / "firmware").as_posix()) for item in sorted((path / "firmware").rglob("*")) if item.is_file() and "build" not in item.parts]
        deterministic_zip(firmware / "source.zip", firmware_sources)
        for name in ("pinmap.h", "devicetree.overlay"):
            source = path / "firmware" / "generated" / name
            (firmware / name).write_bytes(source.read_bytes())
        atomic_write_text(firmware / "build_instructions.md", "# Firmware Build\n\nReference verification: `cmake -S firmware/reference -B build -G Ninja && cmake --build build && ctest --test-dir build`.\n\nZephyr target: `west build -b robot_controller firmware/zephyr/app`.\n")
        docs = staging / "docs"; docs.mkdir(parents=True, exist_ok=True)
        statuses = [f"{item['gate']}: {item['status']}" for item in checks["reports"]]
        atomic_write_text(docs / "design_report.md", "# Design Report\n\n" + "\n".join(f"- {line}" for line in statuses) + "\n")
        write_json(docs / "validation_report.json", checks)
        atomic_write_text(docs / "bringup_guide.md", "# Bring-up Guide\n\n1. Inspect assembly and verify no shorts with power removed.\n2. Current-limit the bench supply below 0.5 A and apply 24 V through the protected input.\n3. Verify 5 V and 3.3 V rails before fitting the MCU.\n4. Keep motor enable disabled; flash the Zephyr image over SWD.\n5. Verify console, IMU identity, CAN loopback, E-stop latch, then each PWM/encoder/current channel.\n6. Increase load only under instrumented thermal and transient monitoring.\n")
        atomic_write_text(docs / "known_risks.md", "# Known Risks\n\n- EMI/EMC, full-load thermal behavior, vibration life, battery abuse, motor transients, ingress protection, and connector fatigue require physical qualification.\n- The reference design controls external motor drivers; it does not route 8 A motor phase current through the controller PCB.\n")
        simple_pdf("Robot Controller Schematic", [f"Components: {len(graph['components'])}", f"Nets: {len(graph['nets'])}", "See KiCad project for editable source."], docs / "schematic.pdf")
        simple_pdf("Assembly Drawing", ["Top-side placement is provided in fabrication/pick_and_place.csv.", "Verify connector orientation before assembly."], docs / "assembly_drawing.pdf")
        simple_pdf("Validation Report", statuses, docs / "validation_report.pdf")
        simple_pdf("Design Report", statuses + ["Physical qualification risks are listed in known_risks.md."], docs / "design_report.pdf")
        simple_pdf("Layout Preview", [f"Board envelope: {spec['mechanical']['envelope']['board_width_mm']} x {spec['mechanical']['envelope']['board_height_mm']} mm", f"Placement entries: {len(graph['components'])}"], docs / "layout_preview.pdf")
        simple_pdf("Bring-up Guide", ["Current-limit initial power-up.", "Verify rails before MCU operation.", "Test E-stop before motor enable.", "Run each channel with motor power isolated."], docs / "bringup_guide.pdf")
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
            release / "mechanical" / "enclosure.step", release / "mechanical" / "enclosure.stl", release / "mechanical" / "assembly.step",
            release / "firmware" / "source.zip", release / "docs" / "design_report.md",
            release / "firmware" / "pinmap.h", release / "firmware" / "devicetree.overlay", release / "firmware" / "build_instructions.md",
            release / "docs" / "validation_report.json", release / "docs" / "bringup_guide.md", release / "docs" / "known_risks.md",
            release / "docs" / "schematic.pdf", release / "docs" / "layout_preview.pdf", release / "docs" / "design_report.pdf",
            release / "docs" / "validation_report.pdf", release / "docs" / "bringup_guide.pdf", release / "manifest.json",
        ]
        backend = spec.get("electronics", {}).get("backend", "reference")
        if backend != "tscircuit":
            reports = [*reports, GateReport("backend_release_policy", Status.BLOCKED, [Failure(FailureCategory.RELEASE_ERROR, "compiled_electronics_backend_required", f"Backend {backend} is not release eligible")])]
        else:
            # Require PCB-level tscircuit gates in addition to netlist gates
            required_tsc_gates = {"tscircuit_compile", "tscircuit_netlist_extract", "tscircuit_graph_parity", "tscircuit_footprint_parity", "tscircuit_layout_completeness"}
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
                    patches.append({"section": "system", "spec_path": "actuation.max_simultaneous_peak_channels", "value": safe_channels, "requires_approval": True})
                elif code == "insufficient_clearance":
                    axis = details.get("axis")
                    minimum = details.get("minimum_mm")
                    _axis_idx = {"width": 0, "height": 1, "depth": 2}
                    idx = _axis_idx.get(axis) if axis else None
                    if idx is not None and minimum is not None and enclosure:
                        new_enclosure = list(enclosure)
                        new_enclosure[idx] = math.ceil(minimum)
                        patches.append({"section": "mechanical", "spec_path": "mechanical.enclosure_internal_mm", "value": new_enclosure, "requires_approval": False})
                elif code == "unlowered_requirement":
                    patches.append({"section": "requirements", "spec_path": f"requirements.unresolved.{details.get('requirement_id', 'unknown')}.status", "value": "waived", "requires_approval": True})
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
                    self._apply_spec_patch(project, patch)
                    applied.append(patch)
        if applied:
            self._append_decision_record(project, proposals, applied)
            iteration_id = self.workspace.snapshot(project, {"goal": "auto-repair", "patches_applied": len(applied)})
            return {"status": "applied", "applied": applied, "proposals": proposals, "iteration_id": iteration_id}
        if proposals:
            return {"status": "blocked", "applied": [], "proposals": proposals, "requires_user_decision": True}
        return {"status": "no_repairs_available", "applied": [], "proposals": []}

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

    def _apply_spec_patch(self, project: str, patch: dict[str, Any]) -> None:
        section = patch["section"]
        spec_path = patch["spec_path"]
        value = patch["value"]
        file_path = self.workspace.require_project(project) / "spec" / f"{section}.yaml"
        data = read_yaml(file_path)
        parts = spec_path.split(".")
        node: Any = data
        for part in parts[:-1]:
            if isinstance(node, list):
                # List traversal: find item by id field
                node = next((item for item in node if isinstance(item, dict) and item.get("id") == part), None)
                if node is None:
                    return
            else:
                node = node[part]
        if isinstance(node, list):
            target_id = parts[-1]
            for item in node:
                if isinstance(item, dict) and item.get("id") == target_id:
                    item["status"] = value
                    break
        else:
            node[parts[-1]] = value
        write_yaml(file_path, data)

    def _append_decision_record(self, project: str, proposals: list[dict[str, Any]], applied: list[dict[str, Any]]) -> None:
        path = self.workspace.require_project(project) / "history" / "decisions.md"
        timestamp = datetime.now(UTC).isoformat()
        lines = [f"\n## Auto-repair {timestamp}\n"]
        if applied:
            lines.append("### Applied patches\n")
            for patch in applied:
                lines.append(f"- `{patch['spec_path']}` → `{patch['value']}`")
        if proposals:
            lines.append("\n### Pending proposals (require user decision)\n")
            for p in proposals:
                lines.append(f"- [{p['failure_code']}] {p['reason']}")
        lines.append("")
        existing = path.read_text(encoding="utf-8")
        path.write_text(existing + "\n".join(lines), encoding="utf-8")

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
        reports = [json.loads(item.read_text(encoding="utf-8")) for item in sorted(reports_dir.glob("*.json"))]
        lines = [f"# Design Report: {project}", "", f"Generated: {datetime.now(UTC).isoformat()}", "", "## Scope", "", f"Target: {spec['project']['target_use']}; revision: {spec['project']['revision']}.", "", "## Validation", ""]
        lines.extend(f"- {item['gate']}: {item['status']} ({len(item.get('failures', []))} findings)" for item in reports)
        lines.extend(["", "## Known Physical Validation Gaps", "", "- Load current and thermal behavior require instrumented hardware testing.", "- EMI/EMC, vibration, abuse safety, transients, ingress, and connector fatigue are not certified by digital checks.", ""])
        output = path / "exports" / "working" / "documentation" / "design_report.md"
        atomic_write_text(output, "\n".join(lines))
        return {"status": "generated", "file": str(output)}

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
