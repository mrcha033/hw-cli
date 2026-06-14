from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator

from .io import write_json
from .models import Failure, FailureCategory, GateReport, Status


def _failure(category: FailureCategory, code: str, message: str, path: str | None = None, **details: Any) -> Failure:
    return Failure(category=category, code=code, message=message, path=path, details=details)


class Validator:
    def __init__(self, schema_root: Path):
        self.schema_root = schema_root

    def validate_spec(self, spec: dict[str, Any]) -> GateReport:
        import json

        failures: list[Failure] = []
        schema = json.loads((self.schema_root / "system.schema.json").read_text(encoding="utf-8"))
        for error in sorted(Draft202012Validator(schema).iter_errors(spec), key=lambda item: list(item.path)):
            path = ".".join(str(item) for item in error.absolute_path)
            failures.append(_failure(FailureCategory.SPEC_ERROR, "schema_violation", error.message, path))

        battery = spec.get("system", {}).get("supply", {}).get("battery", {})
        nominal = battery.get("pack_voltage_nominal")
        maximum = battery.get("pack_voltage_max")
        if isinstance(nominal, (int, float)) and isinstance(maximum, (int, float)) and nominal > maximum:
            failures.append(_failure(FailureCategory.SPEC_ERROR, "contradictory_requirement", "Battery nominal voltage exceeds maximum voltage", "system.supply.battery"))
        assumptions = spec.get("assumptions", {})
        for name, assumption in assumptions.items():
            if assumption.get("critical") and not assumption.get("requires_user_review") and "resolved_value" not in assumption:
                failures.append(_failure(FailureCategory.SPEC_ERROR, "unsafe_assumption", f"Critical assumption {name} is not marked for review", f"assumptions.{name}"))
        return self._report("spec_schema", failures)

    def check_electrical_semantics(self, spec: dict[str, Any]) -> GateReport:
        failures: list[Failure] = []
        system = spec.get("system", {})
        battery = system.get("supply", {}).get("battery", {})
        actuation = spec.get("actuation", {})
        safety = spec.get("safety", {})
        channels = actuation.get("motor_channels", 0)
        per_channel = actuation.get("motor_channel_peak_current_a", 0)
        battery_peak = battery.get("pack_current_peak_a", 0)
        concurrent = actuation.get("max_simultaneous_peak_channels", channels)
        if concurrent and per_channel and battery_peak and concurrent * per_channel > battery_peak:
            failures.append(_failure(
                FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                "current_budget_exceeded",
                f"Motor concurrent peak demand {concurrent * per_channel:.1f} A exceeds battery peak budget {battery_peak:.1f} A",
                "actuation",
                estimated_peak_a=concurrent * per_channel,
                available_peak_a=battery_peak,
            ))
        protections = set(safety.get("required_protections", []))
        for protection in ("reverse_polarity", "fuse_or_efuse", "tvs", "watchdog", "motor_enable_gate"):
            if protection not in protections:
                failures.append(_failure(FailureCategory.ELECTRICAL_SEMANTIC_ERROR, "missing_protection", f"Required protection is missing: {protection}", "safety.required_protections"))
        estop = safety.get("emergency_stop", {})
        if spec.get("sensing", {}).get("e_stop") == "required" and not estop.get("fail_safe_hardware_path"):
            failures.append(_failure(FailureCategory.ELECTRICAL_SEMANTIC_ERROR, "unsafe_estop", "Emergency stop requires a fail-safe hardware path", "safety.emergency_stop"))
        return self._report("semantic_electrical", failures)

    def check_mechanical(self, spec: dict[str, Any]) -> GateReport:
        failures: list[Failure] = []
        mechanical = spec.get("mechanical", {})
        envelope = mechanical.get("envelope", {})
        enclosure = mechanical.get("enclosure_internal_mm", [])
        if len(enclosure) == 3 and envelope:
            board = (envelope.get("board_width_mm", 0), envelope.get("board_height_mm", 0))
            heights = envelope.get("board_thickness_mm", 0) + envelope.get("max_component_height_top_mm", 0) + envelope.get("max_component_height_bottom_mm", 0)
            required = (board[0] + 4.0, board[1] + 4.0, heights + 2.0)
            axes = ("width", "height", "depth")
            for axis, actual, minimum in zip(axes, enclosure, required, strict=True):
                if actual < minimum:
                    failures.append(_failure(FailureCategory.MECHANICAL_ERROR, "insufficient_clearance", f"Enclosure {axis} {actual} mm is below required {minimum} mm", "mechanical.enclosure_internal_mm", axis=axis, actual_mm=actual, minimum_mm=minimum))
        wall = mechanical.get("wall_thickness_mm")
        minimum_wall = spec.get("manufacturing", {}).get("mechanical", {}).get("min_wall_thickness_mm")
        if wall is not None and minimum_wall is not None and wall < minimum_wall:
            failures.append(_failure(FailureCategory.MECHANICAL_ERROR, "wall_too_thin", f"Wall thickness {wall} mm is below manufacturing minimum {minimum_wall} mm", "mechanical.wall_thickness_mm"))
        return self._report("mechanical_fit", failures)

    def check_pinmap(self, assignments: Iterable[dict[str, Any]]) -> GateReport:
        assignments = list(assignments)
        counts = Counter(item.get("mcu_pin") for item in assignments if item.get("mcu_pin"))
        failures = [
            _failure(FailureCategory.FIRMWARE_ERROR, "pin_conflict", f"MCU pin {pin} has {count} assignments", details={"pin": pin})
            for pin, count in counts.items() if count > 1
        ]
        for item in assignments:
            if item.get("signal") != item.get("net_name"):
                failures.append(_failure(FailureCategory.FIRMWARE_ERROR, "peripheral_mismatch", f"Signal {item.get('signal')} does not match net {item.get('net_name')}", "firmware.pin_assignments"))
        return self._report("firmware_pinmap", failures)

    def check_bom(self, components: Iterable[dict[str, Any]]) -> GateReport:
        components = list(components)
        failures: list[Failure] = []
        for component in components:
            if not component.get("mpn"):
                failures.append(_failure(FailureCategory.BOM_ERROR, "missing_mpn", f"Component {component.get('ref', 'unknown')} has no approved MPN", "electronics.components"))
        return self._report("bom", failures)

    def check_sourcing(self, components: Iterable[dict[str, Any]]) -> GateReport:
        failures: list[Failure] = []
        for component in components:
            ref = component.get("ref", "unknown")
            if component.get("lifecycle") != "active":
                failures.append(_failure(FailureCategory.BOM_ERROR, "lifecycle_risk", f"{ref} is not marked active", "electronics.components"))
            sourcing = component.get("sourcing") or {}
            if not component.get("manufacturer") or not sourcing.get("supplier_skus"):
                failures.append(_failure(FailureCategory.BOM_ERROR, "missing_sourcing_metadata", f"{ref} lacks manufacturer or supplier SKU", "electronics.components"))
            if sourcing.get("status") not in {"resolved", "waived"}:
                failures.append(_failure(FailureCategory.BOM_ERROR, "sourcing_unresolved", f"{ref} sourcing is not resolved or waived", "electronics.components"))
            if not component.get("pins"):
                failures.append(_failure(FailureCategory.BOM_ERROR, "missing_pin_mapping", f"{ref} has no symbol-to-footprint pin mapping", "electronics.components"))
        return self._report("sourcing", failures)

    def check_component_metadata(self, components: Iterable[dict[str, Any]]) -> GateReport:
        failures: list[Failure] = []
        components = list(components)
        for component in components:
            ref = component.get("ref", "?")
            if component.get("resolution") != "curated":
                failures.append(_failure(FailureCategory.BOM_ERROR, "component_not_curated", f"{ref} is not curated", ref))
            if component.get("review_status") != "approved":
                failures.append(_failure(FailureCategory.BOM_ERROR, "component_not_approved", f"{ref} review is not approved", ref))
            symbol = component.get("symbol", {})
            footprint = component.get("footprint_metadata", {})
            if not symbol.get("verified") or not footprint.get("verified"):
                failures.append(_failure(FailureCategory.BOM_ERROR, "unverified_library_metadata", f"{ref} symbol/footprint metadata is not verified", ref))
            pins = component.get("pins", [])
            if not pins:
                failures.append(_failure(FailureCategory.BOM_ERROR, "empty_pin_map", f"{ref} has no pins", ref))
                continue
            numbers = [str(pin.get("number")) for pin in pins]
            if len(numbers) != len(set(numbers)):
                failures.append(_failure(FailureCategory.BOM_ERROR, "duplicate_pin_number", f"{ref} has duplicate pin numbers", ref))
            symbol_pins = set(map(str, symbol.get("expected_pins", [])))
            pads = set(map(str, footprint.get("expected_pads", [])))
            for pin in pins:
                number = str(pin.get("number"))
                if number not in symbol_pins:
                    failures.append(_failure(FailureCategory.BOM_ERROR, "symbol_pin_missing", f"{ref}.{number} is absent from curated symbol contract", ref))
                if number not in pads:
                    failures.append(_failure(FailureCategory.BOM_ERROR, "footprint_pad_missing", f"{ref}.{number} is absent from curated footprint contract", ref))
                if pin.get("role") in {"power_in", "power_out", "ground"} and not pin.get("voltage_domain"):
                    failures.append(_failure(FailureCategory.ELECTRICAL_SEMANTIC_ERROR, "power_pin_domain_missing", f"{ref}.{number} lacks a voltage domain", ref))
            if component.get("sourcing", {}).get("status") not in {"resolved", "waived"}:
                failures.append(_failure(FailureCategory.BOM_ERROR, "sourcing_unresolved", f"{ref} sourcing is unresolved", ref))
        report = self._report("component_provenance", failures)
        report.metrics = {"components_checked": len(components)}
        return report

    def check_graph_pin_resolution(self, graph: dict[str, Any]) -> GateReport:
        known = {f"{component['ref']}.{pin['number']}" for component in graph.get("components", []) for pin in component.get("pins", [])}
        failures = [_failure(FailureCategory.ELECTRICAL_SEMANTIC_ERROR, "graph_pin_unresolved", f"Net {net.get('name')} references unknown pin {endpoint}") for net in graph.get("nets", []) for endpoint in net.get("connected_pins", []) if endpoint not in known]
        return self._report("pin_symbol_footprint", failures)

    def check_hw_sw_parity(self, graph: dict[str, Any], assignments: Iterable[dict[str, Any]]) -> GateReport:
        hardware_nets = {item["name"] for item in graph.get("nets", [])}
        assignments = list(assignments)
        failures: list[Failure] = []
        for assignment in assignments:
            if assignment.get("net_name") not in hardware_nets:
                failures.append(_failure(FailureCategory.FIRMWARE_ERROR, "missing_hardware_net", f"Firmware signal has no electrical net: {assignment.get('net_name')}", "firmware.pin_assignments"))
        required_prefixes = ("MOTOR", "CAN_", "I2C_IMU_", "ESTOP_")
        firmware_nets = {item.get("net_name") for item in assignments}
        for net in hardware_nets:
            if net.startswith(required_prefixes) and net not in firmware_nets and not net.endswith(("CURRENT", "ENC")) and net not in {"CANH", "CANL", "ESTOP_GATE"}:
                failures.append(_failure(FailureCategory.FIRMWARE_ERROR, "missing_firmware_assignment", f"Electrical signal lacks firmware assignment: {net}", "electronics.nets"))
        return self._report("hw_sw_parity", failures)

    def check_requirements_lowering(self, spec: dict[str, Any]) -> GateReport:
        failures: list[Failure] = []
        for item in spec.get("requirements", {}).get("unresolved", []):
            if item.get("release_blocking", True) and item.get("status") != "waived":
                failures.append(_failure(
                    FailureCategory.SPEC_ERROR,
                    "unlowered_requirement",
                    f"Requirement was not lowered into formal spec: {item.get('source')}",
                    "requirements.unresolved",
                    requirement_id=item.get("id"),
                    requirement_category=item.get("category"),
                    reason=item.get("reason"),
                ))
        return self._report("requirements_lowering", failures)

    @staticmethod
    def release_gate(reports: list[GateReport], assumptions: dict[str, Any], required_artifacts: list[Path]) -> GateReport:
        failures: list[Failure] = []
        blocked = False
        for report in reports:
            if report.status != Status.PASS:
                blocked |= report.status == Status.BLOCKED
                code = report.failures[0].code if report.gate == "backend_release_policy" and report.failures else "failed_gate"
                failures.append(_failure(FailureCategory.RELEASE_ERROR, code, f"Required gate did not pass: {report.gate}", details={"status": report.status.value, "failure_codes": [failure.code for failure in report.failures]}))
        for name, assumption in assumptions.items():
            if assumption.get("critical") and assumption.get("requires_user_review"):
                blocked = True
                failures.append(Failure(FailureCategory.RELEASE_ERROR, "unresolved_critical_assumption", f"Critical assumption requires review: {name}", path=f"assumptions.{name}", requires_user_decision=True))
        for artifact in required_artifacts:
            if not artifact.is_file():
                failures.append(_failure(FailureCategory.RELEASE_ERROR, "missing_export", f"Required release artifact is missing: {artifact.name}", str(artifact)))
        report = Validator._report("release", failures)
        if blocked:
            report.status = Status.BLOCKED
        return report

    @staticmethod
    def _report(gate: str, failures: list[Failure]) -> GateReport:
        return GateReport(gate=gate, status=Status.FAIL if failures else Status.PASS, failures=failures, metrics={"errors": sum(item.severity == "error" for item in failures), "warnings": sum(item.severity == "warning" for item in failures)})


def persist_report(project_path: Path, report: GateReport) -> str:
    path = project_path / "validation" / "reports" / f"{report.gate}.json"
    write_json(path, report.to_dict())
    return str(path)
