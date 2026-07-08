from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator

from .io import write_json
from .mechanical_contract import build_mechanical_contract
from .models import Failure, FailureCategory, GateReport, Status
from .resolver import SUPPLIER_EVIDENCE_MAX_AGE_DAYS, _evidence_is_stale
from .sourcing_policy import sourcing_waiver_failures

CONNECTOR_CATEGORIES = {"power_input", "can_connector", "usb", "estop", "motor_io"}

CURATED_REGULATOR_OUTPUT_CURRENT_A: dict[str, float] = {
    "AP2112K-3.3TRG1": 0.6,
    "LM22678TJ-5.0": 3.0,
    "LM76005RNPR": 5.0,
    "TPS62133RGTR": 3.0,
}

CURATED_REGULATOR_INPUT_VOLTAGE_RANGE_V: dict[str, tuple[float, float]] = {
    "AP2112K-3.3TRG1": (2.5, 6.0),
    "LM22678TJ-5.0": (4.5, 42.0),
    "LM76005RNPR": (3.5, 60.0),
    "TPS62133RGTR": (3.0, 17.0),
}

CURATED_REGULATOR_MIN_HEADROOM_V: dict[str, float] = {
    "AP2112K-3.3TRG1": 0.25,
    "LM22678TJ-5.0": 1.5,
    "LM76005RNPR": 0.5,
    "TPS62133RGTR": 0.4,
}

CAN_TERMINATION_OHMS = 120.0
CAN_TERMINATION_TOLERANCE_OHMS = 12.0
USB_C_RD_OHMS = 5100.0
USB_C_RD_TOLERANCE_OHMS = 510.0
BOOT_STRAP_MIN_BIAS_OHMS = 1_000.0
BOOT_STRAP_MAX_BIAS_OHMS = 1_000_000.0


CATEGORY_PIN_ROLE_CONTRACTS: dict[str, list[dict[str, Any]]] = {
    "power_input": [
        {"names": ("VBAT", "VBUS"), "roles": ("power_in",)},
        {"names": ("GND",), "roles": ("ground",), "voltage_domains": ("GND",)},
    ],
    "reverse_polarity": [
        {"names": ("ANODE",), "roles": ("power_in",)},
        {"names": ("CATHODE",), "roles": ("power_out",)},
        {"names": ("GND",), "roles": ("ground",), "voltage_domains": ("GND",)},
    ],
    "efuse": [
        {"names": ("IN",), "roles": ("power_in",)},
        {"names": ("OUT",), "roles": ("power_out",)},
        {"names": ("GND",), "roles": ("ground",), "voltage_domains": ("GND",)},
        {"names": ("SHDN", "EN"), "roles": ("input",)},
    ],
    "regulator": [
        {"names": ("VIN",), "roles": ("power_in",)},
        {"names": ("VOUT",), "roles": ("power_out",)},
        {"names": ("GND",), "roles": ("ground",), "voltage_domains": ("GND",)},
    ],
    "mcu": [
        {"roles": ("power_in",)},
        {"roles": ("ground",), "voltage_domains": ("GND",)},
    ],
    "imu": [
        {"names": ("VDD",), "roles": ("power_in",)},
        {"names": ("GND",), "roles": ("ground",), "voltage_domains": ("GND",)},
        {"names": ("SCL", "SCX", "SCK_SCL"), "roles": ("open_drain", "input", "bidirectional")},
        {"names": ("SDA", "SDX", "SDI_SDA"), "roles": ("open_drain", "bidirectional")},
        {"names": ("INT1", "INT"), "roles": ("output", "input")},
    ],
    "can": [
        {"names": ("VCC",), "roles": ("power_in",)},
        {"names": ("GND",), "roles": ("ground",), "voltage_domains": ("GND",)},
        {"names": ("RXD",), "roles": ("output",)},
        {"names": ("TXD",), "roles": ("input",)},
        {"names": ("CANH",), "roles": ("bidirectional",)},
        {"names": ("CANL",), "roles": ("bidirectional",)},
    ],
    "can_connector": [
        {"names": ("CANH",), "roles": ("bidirectional",)},
        {"names": ("CANL",), "roles": ("bidirectional",)},
        {"names": ("GND",), "roles": ("ground",), "voltage_domains": ("GND",)},
    ],
    "estop": [
        {"names": ("ESTOP",), "roles": ("input",)},
        {"names": ("GND",), "roles": ("ground",), "voltage_domains": ("GND",)},
    ],
    "safety_gate": [
        {"names": ("VCC",), "roles": ("power_in",)},
        {"names": ("IN",), "roles": ("input",)},
        {"names": ("OUT",), "roles": ("output",)},
        {"names": ("GND",), "roles": ("ground",), "voltage_domains": ("GND",)},
    ],
    "usb": [
        {"names": ("VBUS",), "roles": ("power_in",), "voltage_domains": ("USB_5V",)},
        {"names": ("GND",), "roles": ("ground",), "voltage_domains": ("GND",)},
        {"names": ("D+", "DP", "USB_D+"), "roles": ("bidirectional",)},
        {"names": ("D-", "DM", "USB_D-"), "roles": ("bidirectional",)},
    ],
    "usb_esd": [
        {"names": ("DP_IN",), "roles": ("bidirectional",)},
        {"names": ("DP_OUT",), "roles": ("bidirectional",)},
        {"names": ("DM_IN",), "roles": ("bidirectional",)},
        {"names": ("DM_OUT",), "roles": ("bidirectional",)},
        {"names": ("GND",), "roles": ("ground",), "voltage_domains": ("GND",)},
    ],
    "motor_io": [
        {"names": ("V5",), "roles": ("power_out",), "voltage_domains": ("V5",)},
        {"names": ("GND",), "roles": ("ground",), "voltage_domains": ("GND",)},
        {"names": ("PWM",), "roles": ("output",)},
        {"names": ("CURRENT",), "roles": ("analog",)},
        {"names": ("ENC",), "roles": ("input",)},
        {"names": ("ESTOP",), "roles": ("output",)},
    ],
}


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
        mechanical_schema_path = self.schema_root / "mechanical.schema.json"
        if mechanical_schema_path.is_file():
            mechanical_schema = json.loads(mechanical_schema_path.read_text(encoding="utf-8"))
            for error in sorted(Draft202012Validator(mechanical_schema).iter_errors(spec.get("mechanical", {})), key=lambda item: list(item.path)):
                path = ".".join(["mechanical", *(str(item) for item in error.absolute_path)])
                failures.append(_failure(FailureCategory.SPEC_ERROR, "mechanical_schema_violation", error.message, path))
        requirements_schema_path = self.schema_root / "requirements.schema.json"
        if requirements_schema_path.is_file():
            requirements_schema = json.loads(requirements_schema_path.read_text(encoding="utf-8"))
            requirements = spec.get("requirements", {})
            for error in sorted(Draft202012Validator(requirements_schema).iter_errors(requirements), key=lambda item: list(item.path)):
                path = ".".join(["requirements", *(str(item) for item in error.absolute_path)])
                failures.append(_failure(FailureCategory.SPEC_ERROR, "requirements_schema_violation", error.message, path))
            requirement_ids = [item.get("id") for key in ("raw_inputs", "active_lowered", "active_unresolved") for item in requirements.get(key, [])]
            if len(requirement_ids) != len(set(requirement_ids)):
                failures.append(_failure(FailureCategory.SPEC_ERROR, "duplicate_requirement_id", "Requirement identifiers must be unique", "requirements"))

        battery = spec.get("system", {}).get("supply", {}).get("battery", {})
        nominal = battery.get("pack_voltage_nominal")
        maximum = battery.get("pack_voltage_max")
        if isinstance(nominal, (int, float)) and isinstance(maximum, (int, float)) and nominal > maximum:
            failures.append(_failure(FailureCategory.SPEC_ERROR, "contradictory_requirement", "Battery nominal voltage exceeds maximum voltage", "system.supply.battery"))
        assumptions = spec.get("assumptions", {})
        if isinstance(assumptions, dict):
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
        battery_type = str(battery.get("type", "")).lower()
        input_connector = str(spec.get("electronics", {}).get("power", {}).get("input_connector", "")).lower()
        usb_powered = "usb" in battery_type or "usb" in input_connector
        required = ["watchdog"]
        if channels > 0:
            required.extend(["reverse_polarity", "fuse_or_efuse", "tvs", "motor_enable_gate"])
        elif not usb_powered and battery_type not in {"", "coin_cell", "lisocl2_primary_cell"}:
            required.extend(["reverse_polarity", "fuse_or_efuse"])
        if safety.get("esd_protection") == "required" or usb_powered:
            required.append("usb_esd")
        for protection in required:
            alternatives = {"usb_esd": {"usb_esd", "tvs"}, "tvs": {"tvs", "usb_esd"}}.get(protection, {protection})
            if not protections.intersection(alternatives):
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

    def check_mechanical_connector_retention(self, spec: dict[str, Any], graph: dict[str, Any]) -> GateReport:
        failures: list[Failure] = []
        mechanical = spec.get("mechanical", {})
        high_vibration = str(mechanical.get("vibration_environment", "")).lower() == "high"
        connectors_exposed = bool(mechanical.get("connectors_exposed", True))
        interface_refs = {
            str(item.get("ref"))
            for item in mechanical.get("connector_interfaces", [])
            if item.get("ref")
        }
        electrical_connector_refs = {
            str(item.get("ref"))
            for item in graph.get("components", [])
            if item.get("ref") and item.get("category") in CONNECTOR_CATEGORIES
        }
        required_refs = sorted(interface_refs | electrical_connector_refs)
        retention = (
            mechanical.get("fixtures", {}).get("cable_retention")
            or mechanical.get("fixtures", {}).get("connector_retention")
            or {}
        )
        retained_refs = {str(ref) for ref in retention.get("connector_refs", [])}
        wildcard_retention = "*" in retained_refs
        retention_method = str(retention.get("retention", retention.get("method", ""))).strip()
        required = high_vibration and connectors_exposed and bool(required_refs)
        missing_refs = [] if wildcard_retention else sorted(set(required_refs) - retained_refs)

        if required and not retention.get("enabled"):
            failures.append(_failure(
                FailureCategory.MECHANICAL_ERROR,
                "connector_retention_missing",
                "High-vibration exposed connectors require a cable or connector retention fixture contract",
                "mechanical.fixtures.cable_retention",
                connector_refs=required_refs,
            ))
        elif required:
            if missing_refs:
                failures.append(_failure(
                    FailureCategory.MECHANICAL_ERROR,
                    "connector_retention_incomplete",
                    "Connector retention fixture does not cover every exposed connector",
                    "mechanical.fixtures.cable_retention.connector_refs",
                    missing_refs=missing_refs,
                    retained_refs=sorted(retained_refs),
                ))
            if not retention_method or retention_method == "none":
                failures.append(_failure(
                    FailureCategory.MECHANICAL_ERROR,
                    "connector_retention_method_missing",
                    "Connector retention fixture must declare the retention method for review",
                    "mechanical.fixtures.cable_retention.retention",
                ))

        report = self._report("mechanical_connector_retention", failures)
        report.metrics = {
            **report.metrics,
            "required": required,
            "high_vibration": high_vibration,
            "connectors_exposed": connectors_exposed,
            "required_connector_refs": required_refs,
            "retained_connector_refs": sorted(retained_refs),
            "missing_connector_refs": missing_refs if required else [],
            "retention_method": retention_method,
        }
        return report

    def check_mechanical_connector_cutouts(self, spec: dict[str, Any], graph: dict[str, Any]) -> GateReport:
        failures: list[Failure] = []
        mechanical = spec.get("mechanical", {})
        interface_refs = {
            str(item.get("ref"))
            for item in mechanical.get("connector_interfaces", [])
            if item.get("ref")
        }
        electrical_connector_refs = {
            str(item.get("ref"))
            for item in graph.get("components", [])
            if item.get("ref") and item.get("category") in CONNECTOR_CATEGORIES
        }
        requires_cutout_contract = mechanical.get("enclosure") == "required" or bool(interface_refs)
        if not requires_cutout_contract:
            report = self._report("mechanical_connector_cutouts", [])
            report.metrics = {
                **report.metrics,
                "required": False,
                "connector_refs": sorted(electrical_connector_refs),
                "interface_refs": sorted(interface_refs),
            }
            return report

        missing_cutout_refs = sorted(electrical_connector_refs - interface_refs)
        if missing_cutout_refs:
            failures.append(_failure(
                FailureCategory.MECHANICAL_ERROR,
                "connector_cutout_missing",
                "Electrical connectors lack mechanical connector-interface cutouts",
                "mechanical.connector_interfaces",
                references=missing_cutout_refs,
            ))

        try:
            contract = build_mechanical_contract(spec, graph)
        except Exception as exc:  # noqa: BLE001 - malformed specs should report a gate failure, not abort checks
            failures.append(_failure(
                FailureCategory.MECHANICAL_ERROR,
                "mechanical_contract_unbuildable",
                f"Mechanical connector contract could not be built: {exc}",
                "mechanical",
            ))
            return self._report("mechanical_connector_cutouts", failures)

        board = contract["board"]
        enclosure = contract["enclosure"]
        clearances = contract["clearances"]
        board_width = float(board["width_mm"])
        board_height = float(board["height_mm"])
        internal = enclosure.get("internal_mm", [0.0, 0.0, 0.0])
        max_edge_distance = float(clearances.get("max_connector_edge_distance_mm", 6.0))
        global_tolerance = float(clearances.get("tolerance_mm", 0.0))
        alignment_failures: list[str] = []
        missing_component_refs: list[str] = []
        invalid_opening_refs: list[str] = []
        out_of_bounds_refs: list[str] = []

        for cutout in contract.get("connector_cutouts", []):
            ref = str(cutout.get("ref"))
            opening = cutout.get("opening_mm")
            center_z = cutout.get("center_z_mm")
            if not isinstance(opening, list) or len(opening) < 2:
                invalid_opening_refs.append(ref)
                failures.append(_failure(
                    FailureCategory.MECHANICAL_ERROR,
                    "connector_cutout_opening_invalid",
                    f"Connector {ref} cutout must declare width and height",
                    "mechanical.connector_interfaces.opening_mm",
                    ref=ref,
                    opening_mm=opening,
                ))
            else:
                numeric_opening = True
                try:
                    opening_width = float(opening[0])
                    opening_height = float(opening[1])
                    center_z_mm = float(center_z) if center_z is not None else None
                except (TypeError, ValueError):
                    numeric_opening = False
                    invalid_opening_refs.append(ref)
                    failures.append(_failure(
                        FailureCategory.MECHANICAL_ERROR,
                        "connector_cutout_opening_invalid",
                        f"Connector {ref} cutout opening and center height must be numeric",
                        "mechanical.connector_interfaces",
                        ref=ref,
                        opening_mm=opening,
                        center_z_mm=center_z,
                    ))
                    opening_width = 0.0
                    opening_height = 0.0
                    center_z_mm = None
                if numeric_opening and (opening_width <= 0.0 or opening_height <= 0.0):
                    invalid_opening_refs.append(ref)
                    failures.append(_failure(
                        FailureCategory.MECHANICAL_ERROR,
                        "connector_cutout_opening_invalid",
                        f"Connector {ref} cutout opening dimensions must be positive",
                        "mechanical.connector_interfaces.opening_mm",
                        ref=ref,
                        opening_mm=opening,
                    ))
                elif center_z_mm is not None and len(internal) >= 3:
                    lower_z = center_z_mm - (opening_height / 2.0)
                    upper_z = center_z_mm + (opening_height / 2.0)
                    if lower_z < -global_tolerance or upper_z > float(internal[2]) + global_tolerance:
                        invalid_opening_refs.append(ref)
                        failures.append(_failure(
                            FailureCategory.MECHANICAL_ERROR,
                            "connector_cutout_opening_out_of_bounds",
                            f"Connector {ref} cutout opening does not fit within enclosure height",
                            "mechanical.connector_interfaces",
                            ref=ref,
                            opening_mm=opening,
                            center_z_mm=center_z_mm,
                            opening_lower_z_mm=lower_z,
                            opening_upper_z_mm=upper_z,
                            enclosure_height_mm=float(internal[2]),
                        ))
            if not cutout.get("component_present"):
                missing_component_refs.append(ref)
                failures.append(_failure(
                    FailureCategory.MECHANICAL_ERROR,
                    "connector_reference_missing",
                    f"Connector cutout references absent electrical connector {ref}",
                    "mechanical.connector_interfaces",
                    ref=ref,
                ))
                continue
            position = cutout.get("pcb_position_mm")
            if not isinstance(position, list) or len(position) < 2:
                failures.append(_failure(
                    FailureCategory.MECHANICAL_ERROR,
                    "connector_position_missing",
                    f"Connector {ref} has no PCB placement for cutout alignment",
                    "electronics.components",
                    ref=ref,
                ))
                continue
            x = float(position[0])
            y = float(position[1])
            position_tolerance = float(cutout.get("position_tolerance_mm", 0.0))
            tolerance = global_tolerance + position_tolerance
            if x < -tolerance or x > board_width + tolerance or y < -tolerance or y > board_height + tolerance:
                out_of_bounds_refs.append(ref)
                failures.append(_failure(
                    FailureCategory.MECHANICAL_ERROR,
                    "connector_position_out_of_bounds",
                    f"Connector {ref} placement is outside the PCB envelope",
                    "electronics.components.pcb_position_mm",
                    ref=ref,
                    pcb_position_mm=position,
                    board_width_mm=board_width,
                    board_height_mm=board_height,
                ))

            side = str(cutout.get("side"))
            edge_distances = {
                "front": y,
                "rear": board_height - y,
                "left": x,
                "right": board_width - x,
            }
            edge_distance = edge_distances.get(side)
            limit = max_edge_distance + tolerance
            if edge_distance is None:
                failures.append(_failure(
                    FailureCategory.MECHANICAL_ERROR,
                    "connector_cutout_side_invalid",
                    f"Connector {ref} declares unsupported enclosure side {side}",
                    "mechanical.connector_interfaces.side",
                    ref=ref,
                    side=side,
                ))
            elif edge_distance > limit:
                alignment_failures.append(ref)
                failures.append(_failure(
                    FailureCategory.MECHANICAL_ERROR,
                    "connector_cutout_alignment_failed",
                    f"Connector {ref} is {edge_distance:.1f} mm from the {side} board edge, beyond cutout tolerance",
                    "mechanical.connector_interfaces",
                    ref=ref,
                    side=side,
                    edge_distance_mm=edge_distance,
                    limit_mm=limit,
                ))

            enclosure_position = cutout.get("enclosure_position_mm")
            if isinstance(enclosure_position, list) and len(enclosure_position) >= 2:
                ex = float(enclosure_position[0])
                ey = float(enclosure_position[1])
                if ex < -tolerance or ex > float(internal[0]) + tolerance or ey < -tolerance or ey > float(internal[1]) + tolerance:
                    failures.append(_failure(
                        FailureCategory.MECHANICAL_ERROR,
                        "connector_cutout_out_of_bounds",
                        f"Connector {ref} cutout position is outside the enclosure internal envelope",
                        "mechanical.connector_interfaces",
                        ref=ref,
                        enclosure_position_mm=enclosure_position,
                        enclosure_internal_mm=internal,
                    ))

        report = self._report("mechanical_connector_cutouts", failures)
        report.metrics = {
            **report.metrics,
            "required": True,
            "connector_refs": sorted(electrical_connector_refs),
            "interface_refs": sorted(interface_refs),
            "missing_cutout_refs": missing_cutout_refs,
            "missing_component_refs": sorted(missing_component_refs),
            "invalid_opening_refs": sorted(set(invalid_opening_refs)),
            "alignment_failure_refs": sorted(alignment_failures),
            "out_of_bounds_refs": sorted(out_of_bounds_refs),
            "max_connector_edge_distance_mm": max_edge_distance,
        }
        return report

    def check_mechanical_mounting_integrity(self, spec: dict[str, Any], graph: dict[str, Any]) -> GateReport:
        failures: list[Failure] = []
        mechanical = spec.get("mechanical", {})
        envelope = mechanical.get("envelope", {})
        board_width = float(envelope.get("board_width_mm", 0.0) or 0.0)
        board_height = float(envelope.get("board_height_mm", 0.0) or 0.0)
        assembly_clearance = float(mechanical.get("assembly_clearance_mm", 0.0) or 0.0)
        manufacturing_clearance = float(spec.get("manufacturing", {}).get("pcb", {}).get("min_clearance_mm", 0.0) or 0.0)
        mounting_holes = mechanical.get("mounting_holes", [])
        checked_holes = 0
        collision_refs: list[str] = []
        invalid_holes: list[int] = []

        for index, hole in enumerate(mounting_holes):
            checked_holes += 1
            x = float(hole.get("x_mm", 0.0) or 0.0)
            y = float(hole.get("y_mm", 0.0) or 0.0)
            diameter = float(hole.get("diameter_mm", 0.0) or 0.0)
            radius = diameter / 2.0
            edge_margin = radius + manufacturing_clearance
            keepout_radius = radius + assembly_clearance

            if diameter <= 0:
                invalid_holes.append(index)
                failures.append(_failure(
                    FailureCategory.MECHANICAL_ERROR,
                    "mounting_hole_diameter_invalid",
                    "Mounting-hole diameter must be positive",
                    f"mechanical.mounting_holes[{index}].diameter_mm",
                    diameter_mm=diameter,
                ))
                continue
            if x < edge_margin or x > board_width - edge_margin or y < edge_margin or y > board_height - edge_margin:
                invalid_holes.append(index)
                failures.append(_failure(
                    FailureCategory.MECHANICAL_ERROR,
                    "mounting_hole_edge_clearance_failed",
                    "Mounting hole drill plus manufacturing clearance extends outside the PCB envelope",
                    f"mechanical.mounting_holes[{index}]",
                    hole_mm=[x, y],
                    diameter_mm=diameter,
                    edge_margin_mm=round(edge_margin, 3),
                    board_width_mm=board_width,
                    board_height_mm=board_height,
                ))

            for component in graph.get("components", []):
                ref = component.get("ref")
                position = component.get("pcb_position_mm")
                if not ref or not isinstance(position, list) or len(position) < 2:
                    continue
                distance = math.hypot(float(position[0]) - x, float(position[1]) - y)
                if distance < keepout_radius:
                    collision_refs.append(str(ref))
                    failures.append(_failure(
                        FailureCategory.MECHANICAL_ERROR,
                        "mounting_hole_component_keepout_intrusion",
                        f"{ref} placement intrudes into mounting-hole screw/standoff keepout",
                        "electronics.components.pcb_position_mm",
                        ref=ref,
                        hole_index=index,
                        hole_mm=[x, y],
                        distance_mm=round(distance, 3),
                        keepout_radius_mm=round(keepout_radius, 3),
                    ))

        report = self._report("mechanical_mounting_integrity", failures)
        report.metrics = {
            **report.metrics,
            "holes_checked": checked_holes,
            "invalid_hole_indices": sorted(set(invalid_holes)),
            "collision_refs": sorted(set(collision_refs)),
            "assembly_clearance_mm": assembly_clearance,
            "manufacturing_clearance_mm": manufacturing_clearance,
            "oracle_boundary": "center-point screw/standoff keepout precheck; native CAD and board DRC remain authoritative for courtyard and fastener geometry",
        }
        return report

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
            if not component.get("manufacturer") or (sourcing.get("status") != "waived" and not sourcing.get("supplier_skus")):
                failures.append(_failure(FailureCategory.BOM_ERROR, "missing_sourcing_metadata", f"{ref} lacks manufacturer or supplier SKU", "electronics.components"))
            if sourcing.get("status") not in {"resolved", "waived"}:
                failures.append(_failure(FailureCategory.BOM_ERROR, "sourcing_unresolved", f"{ref} sourcing is not resolved or waived", "electronics.components"))
            failures.extend(sourcing_waiver_failures(sourcing, path="electronics.components"))
            offer = component.get("supplier_offer") or {}
            if offer and sourcing.get("status") != "waived":
                availability = offer.get("availability")
                if availability in {"out_of_stock", "discontinued"}:
                    failures.append(_failure(
                        FailureCategory.BOM_ERROR,
                        "supplier_unavailable",
                        f"{ref} is {availability} at {offer.get('provider', 'supplier')}",
                        "electronics.components",
                        provider=offer.get("provider"),
                        availability=availability,
                    ))
                elif availability == "available":
                    if not offer.get("sku") or not offer.get("observed_at"):
                        failures.append(_failure(
                            FailureCategory.BOM_ERROR,
                            "supplier_availability_unknown",
                            f"{ref} lacks current supplier availability evidence",
                            "electronics.components",
                            provider=offer.get("provider"),
                        ))
                    elif _evidence_is_stale(offer["observed_at"]):
                        failures.append(_failure(
                            FailureCategory.BOM_ERROR,
                            "supplier_evidence_stale",
                            f"{ref} supplier availability evidence is older than {SUPPLIER_EVIDENCE_MAX_AGE_DAYS} days",
                            "electronics.components",
                            provider=offer.get("provider"),
                            observed_at=offer.get("observed_at"),
                            max_age_days=SUPPLIER_EVIDENCE_MAX_AGE_DAYS,
                        ))
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
            missing_symbol_pins = sorted(symbol_pins - set(numbers), key=_pin_sort_key)
            missing_footprint_pads = sorted(pads - set(numbers), key=_pin_sort_key)
            if missing_symbol_pins:
                failures.append(_failure(
                    FailureCategory.BOM_ERROR,
                    "symbol_pin_unmapped",
                    f"{ref} omits curated symbol pins from the component pin map",
                    ref,
                    missing_pins=missing_symbol_pins,
                    expected_pins=sorted(symbol_pins, key=_pin_sort_key),
                    mapped_pins=sorted(set(numbers), key=_pin_sort_key),
                ))
            if missing_footprint_pads:
                failures.append(_failure(
                    FailureCategory.BOM_ERROR,
                    "footprint_pad_unmapped",
                    f"{ref} omits curated footprint pads from the component pin map",
                    ref,
                    missing_pads=missing_footprint_pads,
                    expected_pads=sorted(pads, key=_pin_sort_key),
                    mapped_pins=sorted(set(numbers), key=_pin_sort_key),
                ))
            pin_contracts = _pin_contracts_by_number(component)
            for pin in pins:
                number = str(pin.get("number"))
                footprint_only_no_connect = pin.get("footprint_only") is True and pin.get("role") == "no_connect"
                if _pin_is_no_connect(pin) and pin.get("net"):
                    failures.append(_failure(
                        FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                        "no_connect_pin_wired",
                        f"{ref}.{number} is declared no-connect but is wired to {pin.get('net')}",
                        ref,
                        ref=ref,
                        pin_number=number,
                        pin_name=pin.get("name"),
                        net=pin.get("net"),
                    ))
                contract = pin_contracts.get(number)
                if contract:
                    failures.extend(_curated_pin_contract_failures(component, pin, contract))
                if number not in symbol_pins and not footprint_only_no_connect:
                    failures.append(_failure(FailureCategory.BOM_ERROR, "symbol_pin_missing", f"{ref}.{number} is absent from curated symbol contract", ref))
                if number not in pads:
                    failures.append(_failure(FailureCategory.BOM_ERROR, "footprint_pad_missing", f"{ref}.{number} is absent from curated footprint contract", ref))
                if pin.get("role") in {"power_in", "power_out", "ground"} and not pin.get("voltage_domain"):
                    failures.append(_failure(FailureCategory.ELECTRICAL_SEMANTIC_ERROR, "power_pin_domain_missing", f"{ref}.{number} lacks a voltage domain", ref))
            failures.extend(_component_pin_role_contract_failures(component))
            sourcing = component.get("sourcing", {})
            if sourcing.get("status") not in {"resolved", "waived"}:
                failures.append(_failure(FailureCategory.BOM_ERROR, "sourcing_unresolved", f"{ref} sourcing is unresolved", ref))
            failures.extend(sourcing_waiver_failures(sourcing, path=ref))
        report = self._report("component_provenance", failures)
        report.metrics = {"components_checked": len(components)}
        return report

    def check_graph_pin_resolution(self, graph: dict[str, Any]) -> GateReport:
        pin_nets = {
            f"{component['ref']}.{pin['number']}": pin.get("net")
            for component in graph.get("components", [])
            for pin in component.get("pins", [])
        }
        failures = []
        for net in graph.get("nets", []):
            net_name = net.get("name")
            for endpoint in net.get("connected_pins", []):
                if endpoint not in pin_nets:
                    failures.append(_failure(
                        FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                        "graph_pin_unresolved",
                        f"Net {net_name} references unknown pin {endpoint}",
                    ))
                elif pin_nets[endpoint] != net_name:
                    failures.append(_failure(
                        FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                        "graph_pin_net_mismatch",
                        f"Net {net_name} references {endpoint}, but the component pin is assigned to {pin_nets[endpoint]}",
                        "electronics.nets",
                        endpoint=endpoint,
                        net_name=net_name,
                        pin_net=pin_nets[endpoint],
                    ))
        return self._report("pin_symbol_footprint", failures)

    def check_power_tree(self, graph: dict[str, Any], spec: dict[str, Any] | None = None) -> GateReport:
        failures: list[Failure] = []
        spec = spec or {}
        net_domains = {item.get("name"): item.get("voltage_domain") for item in graph.get("nets", [])}
        rail_nominal = _rail_nominal_voltages(spec)
        source_categories = {"power_input", "battery", "usb"}
        transfer_categories = {"fuse", "reverse_polarity", "efuse", "regulator", "charger"}
        reachable: set[str] = set()
        transfers: list[tuple[dict[str, Any], set[str], set[str]]] = []
        regulator_voltage_limits: dict[str, dict[str, Any]] = {}
        regulator_enable_biases: dict[str, list[dict[str, Any]]] = {}
        component_voltage_limits: dict[str, list[dict[str, Any]]] = {}

        for component in graph.get("components", []):
            category = component.get("category")
            power_inputs = {
                pin.get("net")
                for pin in component.get("pins", [])
                if pin.get("role") == "power_in" and pin.get("net")
            }
            power_outputs = {
                pin.get("net")
                for pin in component.get("pins", [])
                if pin.get("role") == "power_out" and pin.get("net")
            }
            if category == "fuse":
                power_inputs |= {pin.get("net") for pin in component.get("pins", []) if str(pin.get("name", "")).upper() == "IN" and pin.get("net")}
                power_outputs |= {pin.get("net") for pin in component.get("pins", []) if str(pin.get("name", "")).upper() == "OUT" and pin.get("net")}
            if category in source_categories:
                reachable |= power_inputs
            if _component_category_matches(component, transfer_categories):
                transfers.append((component, power_inputs, power_outputs))
                if not power_inputs or not power_outputs:
                    failures.append(_failure(
                        FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                        "power_transfer_pin_missing",
                        f"{component.get('ref', '?')} lacks a modelled power input or output pin",
                        "electronics.components",
                        ref=component.get("ref"),
                        component_category=category,
                        power_inputs=sorted(power_inputs),
                        power_outputs=sorted(power_outputs),
                    ))
                if _component_category_matches(component, {"regulator"}):
                    for enable_pin in _regulator_enable_pins(component):
                        details = _regulator_enable_bias_details(
                            enable_pin,
                            component,
                            graph.get("components", []),
                            rail_nominal,
                            power_outputs,
                        )
                        regulator_enable_biases.setdefault(str(component.get("ref", "?")), []).append(details)
                        if not details["enabled"]:
                            failures.append(_failure(
                                FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                                "regulator_enable_unbiased",
                                f"{component.get('ref', '?')}.{enable_pin.get('number')} {enable_pin.get('name')} is not biased to an enabled rail",
                                "electronics.components",
                                ref=component.get("ref"),
                                mpn=component.get("mpn"),
                                pin_number=enable_pin.get("number"),
                                pin_name=enable_pin.get("name"),
                                net_name=enable_pin.get("net"),
                                expected_bias="pullup_or_direct_positive_rail",
                                candidate_bias_components=details["candidate_bias_components"],
                            ))
            for pin in component.get("pins", []):
                role = pin.get("role")
                if role not in {"power_in", "power_out", "ground"}:
                    continue
                net_name = pin.get("net")
                expected_domain = net_domains.get(net_name) or _infer_power_domain(net_name)
                if expected_domain and pin.get("voltage_domain") != expected_domain:
                    failures.append(_failure(
                        FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                        "power_pin_voltage_domain_mismatch",
                        f"{component.get('ref', '?')}.{pin.get('number')} voltage domain {pin.get('voltage_domain')} does not match net {net_name} domain {expected_domain}",
                        "electronics.components",
                        ref=component.get("ref"),
                        pin_number=pin.get("number"),
                        net_name=net_name,
                        pin_voltage_domain=pin.get("voltage_domain"),
                        net_voltage_domain=expected_domain,
                    ))

        changed = True
        while changed:
            changed = False
            for _, inputs, outputs in transfers:
                if inputs & reachable:
                    before = len(reachable)
                    reachable |= outputs
                    changed = changed or len(reachable) != before

        for component in graph.get("components", []):
            category = component.get("category")
            for pin in component.get("pins", []):
                if pin.get("role") != "power_in" or not pin.get("net") or category in source_categories:
                    continue
                if pin["net"] not in reachable:
                    failures.append(_failure(
                        FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                        "power_net_unreachable",
                        f"{component.get('ref', '?')}.{pin.get('number')} requires unreachable power net {pin['net']}",
                        "electronics.components",
                        ref=component.get("ref"),
                        pin_number=pin.get("number"),
                        net_name=pin["net"],
                    ))
                if _component_category_matches(component, {"regulator"}):
                    continue
                supply_range = _component_supply_voltage_range_v(component, pin)
                if supply_range is None:
                    continue
                min_supply_v, max_supply_v = supply_range
                observed_voltage_v = _net_nominal_voltage(str(pin["net"]), rail_nominal)
                if observed_voltage_v is None:
                    continue
                component_voltage_limits.setdefault(str(component.get("ref", "?")), []).append({
                    "pin_number": pin.get("number"),
                    "pin_name": pin.get("name"),
                    "net_name": pin.get("net"),
                    "mpn": component.get("mpn"),
                    "supply_voltage_min_v": min_supply_v,
                    "supply_voltage_max_v": max_supply_v,
                    "observed_voltage_v": observed_voltage_v,
                })
                below_min = min_supply_v is not None and observed_voltage_v < min_supply_v - 0.05
                above_max = max_supply_v is not None and observed_voltage_v > max_supply_v + 0.05
                if below_min or above_max:
                    failures.append(_failure(
                        FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                        "component_supply_voltage_out_of_range",
                        f"{component.get('ref', '?')}.{pin.get('number')} supply rail is outside the grounded component input range",
                        "electronics.components",
                        ref=component.get("ref"),
                        mpn=component.get("mpn"),
                        pin_number=pin.get("number"),
                        pin_name=pin.get("name"),
                        net_name=pin.get("net"),
                        supply_voltage_min_v=min_supply_v,
                        supply_voltage_max_v=max_supply_v,
                        observed_voltage_v=observed_voltage_v,
                    ))

        for component, inputs, outputs in transfers:
            known_inputs = [value for net in inputs if (value := _net_nominal_voltage(net, rail_nominal)) is not None]
            known_outputs = [value for net in outputs if (value := _net_nominal_voltage(net, rail_nominal)) is not None]
            input_domains = {_infer_power_domain(net) for net in inputs}
            output_domains = {_infer_power_domain(net) for net in outputs}
            if _component_category_matches(component, {"regulator"}) and known_inputs:
                input_range = _component_input_voltage_range_v(component)
                if input_range is not None:
                    min_input_v, max_input_v = input_range
                    observed_min = min(known_inputs)
                    observed_max = max(known_inputs)
                    regulator_voltage_limits[str(component.get("ref", "?"))] = {
                        "input_nets": sorted(inputs),
                        "mpn": component.get("mpn"),
                        "input_voltage_min_v": min_input_v,
                        "input_voltage_max_v": max_input_v,
                        "observed_input_voltage_min_v": observed_min,
                        "observed_input_voltage_max_v": observed_max,
                    }
                    if observed_min < min_input_v - 0.05 or observed_max > max_input_v + 0.05:
                        failures.append(_failure(
                            FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                            "regulator_input_voltage_out_of_range",
                            f"{component.get('ref', '?')} input rail is outside the grounded regulator VIN range",
                            "electronics.components",
                            ref=component.get("ref"),
                            mpn=component.get("mpn"),
                            input_nets=sorted(inputs),
                            input_voltage_min_v=min_input_v,
                            input_voltage_max_v=max_input_v,
                            observed_input_voltage_min_v=observed_min,
                            observed_input_voltage_max_v=observed_max,
                        ))
                min_headroom_v = _component_regulator_headroom_v(component)
                if min_headroom_v is not None and known_outputs:
                    observed_headroom_v = min(known_inputs) - max(known_outputs)
                    regulator_voltage_limits.setdefault(str(component.get("ref", "?")), {}).update({
                        "output_nets": sorted(outputs),
                        "min_dropout_headroom_v": min_headroom_v,
                        "observed_headroom_v": observed_headroom_v,
                    })
                    if observed_headroom_v < min_headroom_v - 0.05:
                        failures.append(_failure(
                            FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                            "regulator_dropout_headroom_insufficient",
                            f"{component.get('ref', '?')} input/output rails leave insufficient regulator dropout headroom",
                            "electronics.components",
                            ref=component.get("ref"),
                            mpn=component.get("mpn"),
                            input_nets=sorted(inputs),
                            output_nets=sorted(outputs),
                            min_dropout_headroom_v=min_headroom_v,
                            observed_headroom_v=observed_headroom_v,
                            observed_input_voltage_min_v=min(known_inputs),
                            observed_output_voltage_max_v=max(known_outputs),
                        ))
            if input_domains == output_domains:
                continue
            if known_inputs and known_outputs and max(known_outputs) > max(known_inputs) + 0.05:
                failures.append(_failure(
                    FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                    "power_output_exceeds_input_voltage",
                    f"{component.get('ref', '?')} output rail exceeds its known input rail voltage",
                    "electronics.components",
                    ref=component.get("ref"),
                    component_category=component.get("category"),
                    input_nets=sorted(inputs),
                    output_nets=sorted(outputs),
                    max_input_v=max(known_inputs),
                    max_output_v=max(known_outputs),
                ))

        report = self._report("power_tree_integrity", failures)
        report.metrics = {
            **report.metrics,
            "source_nets": sorted(reachable),
            "transfer_components": len(transfers),
            "regulator_voltage_limits": regulator_voltage_limits,
            "regulator_enable_biases": regulator_enable_biases,
            "component_voltage_limits": component_voltage_limits,
            "power_loads_checked": sum(
                1
                for component in graph.get("components", [])
                if component.get("category") not in source_categories
                for pin in component.get("pins", [])
                if pin.get("role") == "power_in"
            ),
        }
        return report

    def check_power_integrity_estimate(self, graph: dict[str, Any], spec: dict[str, Any] | None = None) -> GateReport:
        spec = spec or {}
        failures: list[Failure] = []
        components = graph.get("components", [])
        rail_current = _rail_current_peaks(spec)
        power_nets = {
            pin.get("net")
            for component in components
            for pin in component.get("pins", [])
            if pin.get("net") and _infer_power_domain(pin.get("net")) not in {None, "GND"}
        }
        rail_names = sorted((set(rail_current) | {str(net) for net in power_nets}) - {"GND"})
        source_categories = {"power_input", "battery", "usb"}
        capacitor_categories = {"decoupling", "bulk_cap"}
        transfer_categories = {"fuse", "reverse_polarity", "efuse", "regulator", "charger", "tvs", "safety_gate"}
        coverage: dict[str, dict[str, Any]] = {}
        regulator_current_limits: dict[str, dict[str, Any]] = {}

        for component in components:
            if component.get("category") not in capacitor_categories:
                continue
            nets = _component_nets(component)
            capacitor_power_nets = sorted(
                net for net in nets
                if _infer_power_domain(net) not in {None, "GND"}
            )
            if capacitor_power_nets and "GND" not in nets:
                failures.append(_failure(
                    FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                    "capacitor_ground_return_missing",
                    f"{component.get('ref', '?')} is modelled as a rail capacitor but has no ground return",
                    "electronics.components",
                    ref=component.get("ref"),
                    component_category=component.get("category"),
                    power_nets=capacitor_power_nets,
                    present_nets=sorted(nets),
                ))

        for component in components:
            if not _component_category_matches(component, {"regulator"}):
                continue
            output_pins = [
                pin for pin in component.get("pins", [])
                if pin.get("role") == "power_out" and pin.get("net")
            ]
            current_limit_a = _component_output_current_limit_a(component)
            if current_limit_a is None:
                continue
            for pin in output_pins:
                rail = str(pin["net"])
                requested_current_a = float(rail_current.get(rail, 0.0) or 0.0)
                regulator_current_limits[str(component.get("ref", "?"))] = {
                    "rail": rail,
                    "mpn": component.get("mpn"),
                    "output_current_limit_a": current_limit_a,
                    "declared_peak_current_a": requested_current_a,
                }
                if requested_current_a > current_limit_a + 1e-9:
                    failures.append(_failure(
                        FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                        "regulator_output_current_exceeded",
                        f"{component.get('ref', '?')} output current rating {current_limit_a:.2f} A is below declared {rail} peak current {requested_current_a:.2f} A",
                        "electronics.components",
                        ref=component.get("ref"),
                        mpn=component.get("mpn"),
                        rail=rail,
                        output_current_limit_a=current_limit_a,
                        declared_peak_current_a=requested_current_a,
                    ))

        for rail in rail_names:
            loads = sorted({
                str(component.get("ref"))
                for component in components
                if component.get("category") not in source_categories | capacitor_categories
                and not _component_category_matches(component, transfer_categories)
                and any(pin.get("role") == "power_in" and pin.get("net") == rail for pin in component.get("pins", []))
            })
            if not loads:
                continue
            decoupling_refs = sorted({
                str(component.get("ref"))
                for component in components
                if component.get("category") == "decoupling"
                and _component_has_nets(component, {rail, "GND"})
            })
            bulk_refs = sorted({
                str(component.get("ref"))
                for component in components
                if component.get("category") == "bulk_cap"
                and _component_has_nets(component, {rail, "GND"})
            })
            high_current_connector_loads = sorted({
                str(component.get("ref"))
                for component in components
                if component.get("category") in {"motor_io", "can_connector", "usb"}
                and any(pin.get("role") == "power_in" and pin.get("net") == rail for pin in component.get("pins", []))
            })
            current_a = float(rail_current.get(rail, 0.0) or 0.0)
            coverage[rail] = {
                "loads": loads,
                "decoupling": decoupling_refs,
                "bulk": bulk_refs,
                "declared_peak_current_a": current_a,
                "high_current_connector_loads": high_current_connector_loads,
            }
            if not decoupling_refs and not bulk_refs:
                failures.append(_failure(
                    FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                    "rail_decoupling_missing",
                    f"Rail {rail} feeds active loads without any local decoupling or bulk capacitor in the graph",
                    "electronics.components",
                    rail=rail,
                    loads=loads,
                ))
            if current_a >= 0.5 and not bulk_refs:
                severity = "error" if high_current_connector_loads else "warning"
                failures.append(Failure(
                    FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                    "rail_bulk_cap_missing",
                    f"Rail {rail} declares {current_a:.2f} A peak current without a bulk capacitor in the graph",
                    severity=severity,
                    path="electronics.components",
                    details={
                        "rail": rail,
                        "declared_peak_current_a": current_a,
                        "loads": loads,
                        "high_current_connector_loads": high_current_connector_loads,
                    },
                ))

        error_count = sum(1 for failure in failures if failure.severity == "error")
        warning_count = sum(1 for failure in failures if failure.severity == "warning")
        return GateReport(
            "power_integrity_estimate",
            Status.FAIL if error_count else Status.PASS,
            failures,
            metrics={
                "errors": error_count,
                "warnings": warning_count,
                "rails_checked": len(coverage),
                "coverage": coverage,
                "regulator_current_limits": regulator_current_limits,
                "oracle_boundary": "Heuristic decoupling and bulk-cap coverage only; transient, impedance, and stability still require simulation or bench evidence.",
            },
            backend={"name": "power-integrity-estimator", "authority": "heuristic"},
        )

    def check_interface_integrity(self, graph: dict[str, Any]) -> GateReport:
        failures: list[Failure] = []
        nets_by_name = {net.get("name"): net for net in graph.get("nets", []) if net.get("name")}
        net_names = set(nets_by_name)
        components = graph.get("components", [])

        i2c_nets = sorted(
            name
            for name, net in nets_by_name.items()
            if net.get("signal_class") == "i2c" or str(name).upper().startswith("I2C")
        )
        for net_name in i2c_nets:
            pullup_components = [
                component for component in components
                if _component_is_i2c_pullup(component, net_name, nets_by_name)
            ]
            pullup_refs_for_net = {str(component.get("ref")) for component in pullup_components if component.get("ref")}
            pullup_rails = sorted({
                str(pin.get("net"))
                for component in pullup_components
                for pin in component.get("pins", [])
                if pin.get("net") != net_name and _is_positive_supply_net(pin.get("net"), nets_by_name)
            })
            pullup_refs = sorted(str(component.get("ref")) for component in pullup_components if pullup_rails)
            if not pullup_refs:
                failures.append(_failure(
                    FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                    "i2c_pullup_missing",
                    f"I2C net {net_name} has no pull-up to a positive logic rail",
                    "electronics.components",
                    net_name=net_name,
                ))
            endpoint_supply_nets = _signal_endpoint_supply_nets(
                net_name,
                nets_by_name,
                components,
                excluded_categories={"pullup"},
                excluded_refs=pullup_refs_for_net,
            )
            if pullup_rails and endpoint_supply_nets and not (set(pullup_rails) & set(endpoint_supply_nets)):
                failures.append(_failure(
                    FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                    "i2c_pullup_voltage_mismatch",
                    f"I2C net {net_name} pull-up rail does not match the powered endpoint rail",
                    "electronics.components",
                    net_name=net_name,
                    pullup_refs=pullup_refs,
                    pullup_rails=pullup_rails,
                    endpoint_supply_nets=endpoint_supply_nets,
                ))

        can_high = "CANH" in net_names
        can_low = "CANL" in net_names
        if can_high != can_low:
            failures.append(_failure(
                FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                "can_pair_incomplete",
                "CAN interface exposes only one side of the differential pair",
                "electronics.nets",
                canh_present=can_high,
                canl_present=can_low,
            ))
        if can_high and can_low:
            termination_components = [
                component for component in components
                if _component_category_matches(component, {"termination"})
                and _component_has_nets(component, {"CANH", "CANL"})
            ]
            if not termination_components:
                failures.append(_failure(
                    FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                    "can_termination_missing",
                    "CANH/CANL are present without a termination component across the pair",
                    "electronics.components",
                    required_nets=["CANH", "CANL"],
                ))
            for component in termination_components:
                resistance_ohms = _component_resistance_ohms(component)
                if resistance_ohms is None:
                    failures.append(_failure(
                        FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                        "can_termination_value_missing",
                        f"{component.get('ref', '?')} terminates CANH/CANL but has no grounded resistance value",
                        "electronics.components",
                        ref=component.get("ref"),
                        value=component.get("value"),
                        expected_ohms=CAN_TERMINATION_OHMS,
                    ))
                elif abs(resistance_ohms - CAN_TERMINATION_OHMS) > CAN_TERMINATION_TOLERANCE_OHMS:
                    failures.append(_failure(
                        FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                        "can_termination_value_invalid",
                        f"{component.get('ref', '?')} CAN termination is {resistance_ohms:g} ohms, expected approximately {CAN_TERMINATION_OHMS:g} ohms",
                        "electronics.components",
                        ref=component.get("ref"),
                        value=component.get("value"),
                        resistance_ohms=resistance_ohms,
                        expected_ohms=CAN_TERMINATION_OHMS,
                        tolerance_ohms=CAN_TERMINATION_TOLERANCE_OHMS,
                    ))
            if not any(_component_category_matches(component, {"can"}) and _component_has_nets(component, {"CANH", "CANL"}) for component in components):
                failures.append(_failure(
                    FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                    "can_transceiver_missing",
                    "CANH/CANL are present without a CAN transceiver connected to both nets",
                    "electronics.components",
                    required_nets=["CANH", "CANL"],
                ))
            if not any(_component_category_matches(component, {"can_connector"}) and _component_has_nets(component, {"CANH", "CANL"}) for component in components):
                failures.append(_failure(
                    FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                    "can_connector_missing",
                    "CANH/CANL are present without a connector connected to both nets",
                    "electronics.components",
                    required_nets=["CANH", "CANL"],
                ))

        usb_nets = {"USB_DP_RAW", "USB_DM_RAW", "USB_DP", "USB_DM"}
        present_usb_nets = usb_nets & net_names
        usb_bridge_present = False
        if present_usb_nets:
            if present_usb_nets != usb_nets:
                failures.append(_failure(
                    FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                    "usb_differential_pair_incomplete",
                    "USB interface is missing raw or protected D+/D- nets",
                    "electronics.nets",
                    missing_nets=sorted(usb_nets - present_usb_nets),
                    present_nets=sorted(present_usb_nets),
                ))
            usb_bridge_present = any(
                _component_category_matches(component, {"usb_esd", "tvs"})
                and _component_has_nets(component, usb_nets | {"GND"})
                for component in components
            )
            if present_usb_nets == usb_nets and not usb_bridge_present:
                failures.append(_failure(
                    FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                    "usb_esd_bridge_missing",
                    "USB raw connector nets are not bridged to protected USB nets through an ESD/protection component",
                    "electronics.components",
                    required_nets=sorted(usb_nets | {"GND"}),
                ))

        usb_c_connectors = [
            component for component in components
            if _component_category_matches(component, {"usb", "power_input"})
            and _is_usb_c_connector(component)
        ]
        usb_c_cc_nets_checked: set[str] = set()
        for connector in usb_c_connectors:
            connector_pins_by_name = {str(pin.get("name", "")).upper(): pin for pin in connector.get("pins", [])}
            for pin_name, expected_net in (("CC1", "USB_CC1"), ("CC2", "USB_CC2")):
                cc_net = connector_pins_by_name.get(pin_name, {}).get("net")
                if not cc_net:
                    failures.append(_failure(
                        FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                        "usb_c_cc_net_missing",
                        f"USB-C connector {connector.get('ref', '?')} is missing {pin_name} sink configuration net",
                        "electronics.components",
                        ref=connector.get("ref"),
                        pin_name=pin_name,
                        expected_net=expected_net,
                    ))
                    continue
                usb_c_cc_nets_checked.add(str(cc_net))
                pulldown_candidates = [
                    component for component in components
                    if component is not connector
                    and _component_has_nets(component, {str(cc_net), "GND"})
                    and _component_resistance_ohms(component) is not None
                ]
                valid_pulldowns = [
                    component for component in pulldown_candidates
                    if abs((_component_resistance_ohms(component) or 0.0) - USB_C_RD_OHMS) <= USB_C_RD_TOLERANCE_OHMS
                ]
                if not pulldown_candidates:
                    failures.append(_failure(
                        FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                        "usb_c_cc_pulldown_missing",
                        f"USB-C {pin_name} net {cc_net} has no Rd pulldown to ground",
                        "electronics.components",
                        ref=connector.get("ref"),
                        pin_name=pin_name,
                        net_name=cc_net,
                        expected_ohms=USB_C_RD_OHMS,
                    ))
                elif not valid_pulldowns:
                    failures.append(_failure(
                        FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                        "usb_c_cc_pulldown_value_invalid",
                        f"USB-C {pin_name} net {cc_net} pulldown is not approximately {USB_C_RD_OHMS:g} ohms",
                        "electronics.components",
                        ref=connector.get("ref"),
                        pin_name=pin_name,
                        net_name=cc_net,
                        candidate_refs=[component.get("ref") for component in pulldown_candidates],
                        candidate_ohms=[_component_resistance_ohms(component) for component in pulldown_candidates],
                        expected_ohms=USB_C_RD_OHMS,
                        tolerance_ohms=USB_C_RD_TOLERANCE_OHMS,
                    ))

        report = self._report("interface_integrity", failures)
        report.metrics = {
            **report.metrics,
            "i2c_nets_checked": len(i2c_nets),
            "can_pair_present": can_high and can_low,
            "usb_nets_checked": len(present_usb_nets),
            "usb_bridge_present": usb_bridge_present,
            "usb_c_connectors_checked": len(usb_c_connectors),
            "usb_c_cc_nets_checked": len(usb_c_cc_nets_checked),
        }
        return report

    def check_hw_sw_parity(self, graph: dict[str, Any], assignments: Iterable[dict[str, Any]]) -> GateReport:
        hardware_nets = {item["name"] for item in graph.get("nets", [])}
        assignments = list(assignments)
        failures: list[Failure] = []
        mcu_refs = {component.get("ref") for component in graph.get("components", []) if component.get("category") == "mcu"}
        mcu_pins_by_endpoint: dict[str, dict[str, Any]] = {
            f"{component.get('ref')}.{pin.get('number')}": pin
            for component in graph.get("components", [])
            if component.get("ref") in mcu_refs
            for pin in component.get("pins", [])
        }
        for assignment in assignments:
            if assignment.get("net_name") not in hardware_nets:
                failures.append(_failure(FailureCategory.FIRMWARE_ERROR, "missing_hardware_net", f"Firmware signal has no electrical net: {assignment.get('net_name')}", "firmware.pin_assignments"))
            graph_pin = assignment.get("graph_pin")
            if not graph_pin:
                continue
            ref, _, _number = str(graph_pin).partition(".")
            pin = mcu_pins_by_endpoint.get(str(graph_pin))
            if ref not in mcu_refs or pin is None:
                failures.append(_failure(
                    FailureCategory.FIRMWARE_ERROR,
                    "firmware_graph_pin_unresolved",
                    f"Firmware assignment references non-MCU or unknown graph pin: {graph_pin}",
                    "firmware.pin_assignments",
                    graph_pin=graph_pin,
                ))
                continue
            if assignment.get("net_name") != pin.get("net"):
                failures.append(_failure(
                    FailureCategory.FIRMWARE_ERROR,
                    "firmware_graph_pin_net_mismatch",
                    f"Firmware assignment for {assignment.get('signal')} points at {graph_pin}, but that MCU pin is wired to {pin.get('net')}",
                    "firmware.pin_assignments",
                    signal=assignment.get("signal"),
                    graph_pin=graph_pin,
                    assigned_net=assignment.get("net_name"),
                    hardware_net=pin.get("net"),
                ))
            expected_mcu_pin = pin.get("mcu_pin") or pin.get("name") or pin.get("number")
            if assignment.get("mcu_pin") and assignment.get("mcu_pin") != expected_mcu_pin:
                failures.append(_failure(
                    FailureCategory.FIRMWARE_ERROR,
                    "firmware_mcu_pin_mismatch",
                    f"Firmware assignment for {assignment.get('signal')} uses MCU pin {assignment.get('mcu_pin')} but {graph_pin} is {expected_mcu_pin}",
                    "firmware.pin_assignments",
                    signal=assignment.get("signal"),
                    graph_pin=graph_pin,
                    assigned_mcu_pin=assignment.get("mcu_pin"),
                    expected_mcu_pin=expected_mcu_pin,
                ))
        firmware_nets = {item.get("net_name") for item in assignments}
        ignored_mcu_nets = {"V3V3", "V5", "GND", "SWDIO", "SWCLK", "NRST"}
        required_nets = {
            pin.get("net")
            for component in graph.get("components", [])
            if component.get("category") == "mcu"
            for pin in component.get("pins", [])
            if pin.get("role") not in {"power_in", "power_out", "ground", "no_connect"} and pin.get("net") and pin.get("net") not in ignored_mcu_nets
        }
        for net in sorted(required_nets):
            if net not in firmware_nets:
                failures.append(_failure(FailureCategory.FIRMWARE_ERROR, "missing_firmware_assignment", f"Electrical signal lacks firmware assignment: {net}", "electronics.nets"))
        return self._report("hw_sw_parity", failures)

    def check_firmware_modules(
        self,
        modules: list[dict[str, Any]],
        pinmap: list[dict[str, Any]],
        spec: dict[str, Any] | None = None,
        graph: dict[str, Any] | None = None,
        module_dir: Path | str | None = None,
    ) -> GateReport:
        from .backends.firmware_modules import _RENDERERS
        failures: list[Failure] = []
        known_signals = {item.get("signal") for item in pinmap if item.get("signal")}
        known_net_names = {item.get("net_name") for item in pinmap if item.get("net_name")}
        seen_ids: set[str] = set()
        isr_count = 0
        total_stack = 0
        _ISR_BUDGET = 8
        _STACK_BUDGET_BYTES = 65536
        required_behaviors: list[str] = []
        rendered_module_ids: set[str] = set()
        module_artifact_dir = Path(module_dir) if module_dir is not None else None
        graph = graph or {}
        graph_nets = {
            str(net.get("name"))
            for net in graph.get("nets", [])
            if net.get("name")
        }
        graph_signal_classes = {
            str(net.get("signal_class")).lower()
            for net in graph.get("nets", [])
            if net.get("signal_class")
        }
        firmware_signal_space = {
            str(item)
            for item in {*known_signals, *known_net_names, *graph_nets}
            if item
        }
        component_labels = {
            str(value).lower()
            for component in graph.get("components", [])
            for value in (
                component.get("category"),
                component.get("role"),
                component.get("value"),
                component.get("mpn"),
                component.get("component_id"),
            )
            if value
        }

        def hardware_has_bus(bus: str) -> bool:
            bus = bus.lower()
            if bus == "can":
                return (
                    {"CANH", "CANL"} <= graph_nets
                    or {"CAN_RX", "CAN_TX"} <= firmware_signal_space
                    or "can" in component_labels
                )
            if bus == "uart":
                return any(signal.upper().startswith(("UART", "USART")) for signal in firmware_signal_space)
            if bus == "i2c":
                return "i2c" in graph_signal_classes or any(signal.upper().startswith("I2C") for signal in firmware_signal_space)
            if bus == "spi":
                return "spi" in graph_signal_classes or any(signal.upper().startswith(("SPI", "QSPI")) for signal in firmware_signal_space)
            if bus == "adc":
                return "adc" in graph_signal_classes or any("ADC" in signal.upper() for signal in firmware_signal_space)
            return False

        for mod in modules:
            mid = mod.get("id", "<unnamed>")
            behavior = mod.get("behavior")

            if mid in seen_ids:
                failures.append(_failure(
                    FailureCategory.FIRMWARE_ERROR, "duplicate_module_id",
                    f"Module id '{mid}' is duplicated", f"firmware.modules.{mid}",
                ))
            seen_ids.add(mid)

            if behavior not in _RENDERERS:
                failures.append(_failure(
                    FailureCategory.FIRMWARE_ERROR, "unknown_behavior",
                    f"Module '{mid}' has unknown behavior '{behavior}'",
                    f"firmware.modules.{mid}.behavior",
                    available=sorted(_RENDERERS),
                ))
                continue

            # Signal reference check for timeout_shutdown
            if behavior == "timeout_shutdown":
                signal = mod.get("trigger", {}).get("signal")
                if signal and known_signals and signal not in known_signals:
                    failures.append(_failure(
                        FailureCategory.FIRMWARE_ERROR, "unresolved_signal_reference",
                        f"Module '{mid}' references signal '{signal}' not in pinmap",
                        f"firmware.modules.{mid}.trigger.signal",
                        signal=signal,
                        known_signals=sorted(known_signals),
                    ))
                timeout_ms = mod.get("trigger", {}).get("timeout_ms")
                if timeout_ms is not None and int(timeout_ms) < 1:
                    failures.append(_failure(
                        FailureCategory.FIRMWARE_ERROR, "invalid_timeout",
                        f"Module '{mid}' timeout_ms must be >= 1",
                        f"firmware.modules.{mid}.trigger.timeout_ms",
                    ))
                isr_count += 1  # timer ISR

            if behavior == "periodic_transmit":
                transport = str(mod.get("transport", "can")).lower()
                if transport not in {"can", "uart", "i2c"}:
                    failures.append(_failure(
                        FailureCategory.FIRMWARE_ERROR,
                        "firmware_transport_unsupported",
                        f"Module '{mid}' requests unsupported transport '{transport}'",
                        f"firmware.modules.{mid}.transport",
                        transport=transport,
                        supported_transports=["can", "uart", "i2c"],
                    ))
                elif not hardware_has_bus(transport):
                    failures.append(_failure(
                        FailureCategory.FIRMWARE_ERROR,
                        "firmware_transport_missing",
                        f"Module '{mid}' transmits over {transport.upper()}, but the hardware graph exposes no matching bus",
                        f"firmware.modules.{mid}.transport",
                        transport=transport,
                        known_signals=sorted(firmware_signal_space),
                    ))
                interval_ms = mod.get("interval_ms")
                if interval_ms is not None and int(interval_ms) < 1:
                    failures.append(_failure(
                        FailureCategory.FIRMWARE_ERROR,
                        "invalid_transmit_interval",
                        f"Module '{mid}' interval_ms must be >= 1",
                        f"firmware.modules.{mid}.interval_ms",
                    ))
                frame = mod.get("frame") or {}
                dlc = frame.get("dlc")
                if transport == "can" and dlc is not None and int(dlc) > 8:
                    failures.append(_failure(
                        FailureCategory.FIRMWARE_ERROR,
                        "firmware_can_frame_dlc_unsupported",
                        f"Module '{mid}' declares CAN DLC {dlc}, but the current hardware/firmware contract supports classical CAN frames only",
                        f"firmware.modules.{mid}.frame.dlc",
                        transport=transport,
                        dlc=int(dlc),
                        max_classical_can_dlc=8,
                    ))

            if behavior == "sensor_poll":
                bus = str(mod.get("bus", "i2c")).lower()
                if not hardware_has_bus(bus):
                    failures.append(_failure(
                        FailureCategory.FIRMWARE_ERROR,
                        "firmware_sensor_bus_missing",
                        f"Module '{mid}' polls over {bus.upper()}, but the hardware graph exposes no matching bus",
                        f"firmware.modules.{mid}.bus",
                        bus=bus,
                        known_signals=sorted(firmware_signal_space),
                    ))
                sensor = mod.get("sensor")
                if sensor:
                    sensor_text = str(sensor).lower()
                    if not any(sensor_text in label or label in sensor_text for label in component_labels):
                        failures.append(_failure(
                            FailureCategory.FIRMWARE_ERROR,
                            "firmware_sensor_target_missing",
                            f"Module '{mid}' polls sensor '{sensor}', but no matching component is resolved in the hardware graph",
                            f"firmware.modules.{mid}.sensor",
                            sensor=sensor,
                            known_components=sorted(component_labels),
                        ))
                poll_interval = mod.get("poll_interval_ms")
                if poll_interval is not None and int(poll_interval) < 1:
                    failures.append(_failure(
                        FailureCategory.FIRMWARE_ERROR,
                        "invalid_poll_interval",
                        f"Module '{mid}' poll_interval_ms must be >= 1",
                        f"firmware.modules.{mid}.poll_interval_ms",
                    ))

            if behavior == "interface_stack":
                missing_nets = sorted(
                    str(net)
                    for net in mod.get("required_nets", [])
                    if str(net) not in firmware_signal_space
                )
                if missing_nets:
                    failures.append(_failure(
                        FailureCategory.FIRMWARE_ERROR,
                        "firmware_stack_net_missing",
                        f"Module '{mid}' declares stack nets not present in the firmware pinmap or hardware graph",
                        f"firmware.modules.{mid}.required_nets",
                        missing_nets=missing_nets,
                        known_signals=sorted(firmware_signal_space),
                    ))

            # Render to get stack info
            try:
                from .backends.firmware_modules import render_module
                output = render_module(mod)
                rendered_module_ids.add(output.id)
                total_stack += output.stack_size_bytes
                if output.is_isr:
                    isr_count += 1
                if module_artifact_dir is not None:
                    expected_artifacts = {
                        "c_source": (module_artifact_dir / f"{output.id}.c", output.c_source),
                        "h_source": (module_artifact_dir / f"{output.id}.h", output.h_source),
                    }
                    for artifact_type, (artifact_path, expected_text) in expected_artifacts.items():
                        if not artifact_path.is_file():
                            failures.append(_failure(
                                FailureCategory.FIRMWARE_ERROR,
                                "firmware_module_artifact_missing",
                                f"Rendered firmware module '{output.id}' is missing {artifact_type}",
                                "firmware.modules",
                                module_id=output.id,
                                artifact_type=artifact_type,
                                artifact_path=str(artifact_path),
                            ))
                            continue
                        actual_text = artifact_path.read_text(encoding="utf-8")
                        if actual_text != expected_text:
                            failures.append(_failure(
                                FailureCategory.FIRMWARE_ERROR,
                                "firmware_module_artifact_stale",
                                f"Rendered firmware module '{output.id}' {artifact_type} does not match the current spec",
                                "firmware.modules",
                                module_id=output.id,
                                artifact_type=artifact_type,
                                artifact_path=str(artifact_path),
                            ))
            except Exception as exc:
                failures.append(_failure(
                    FailureCategory.FIRMWARE_ERROR, "module_render_error",
                    f"Module '{mid}' failed to render: {exc}",
                    f"firmware.modules.{mid}",
                ))

        if spec:
            try:
                motor_channels = int(spec.get("actuation", {}).get("motor_channels", 0) or 0)
            except (TypeError, ValueError):
                motor_channels = 0
            estop_required = (
                bool(spec.get("safety", {}).get("emergency_stop", {}).get("required"))
                or spec.get("sensing", {}).get("e_stop") == "required"
                or any(component.get("category") == "estop" for component in graph.get("components", []))
            )
            if motor_channels > 0 and estop_required:
                required_behaviors.append("estop_motor_shutdown")
                candidates = [
                    module for module in modules
                    if module.get("behavior") == "timeout_shutdown"
                    and module.get("trigger", {}).get("signal") == "ESTOP_IN"
                ]
                if not candidates:
                    failures.append(_failure(
                        FailureCategory.FIRMWARE_ERROR,
                        "missing_estop_shutdown_behavior",
                        "Motor e-stop requirements need a timeout_shutdown firmware module triggered by ESTOP_IN",
                        "firmware.modules",
                        required_behavior="estop_motor_shutdown",
                        required_trigger="ESTOP_IN",
                        suggested_module={
                            "id": "motor_estop_watchdog",
                            "behavior": "timeout_shutdown",
                            "trigger": {"signal": "ESTOP_IN", "timeout_ms": 100},
                            "action": {"disable_all": "motor_enables", "assert": "FAULT_LED"},
                        },
                    ))
                else:
                    safe_groups = {"motor_enables", "motor_outputs", "motor_power", "motor_channels"}
                    if not any(module.get("action", {}).get("disable_all") in safe_groups for module in candidates):
                        failures.append(_failure(
                            FailureCategory.FIRMWARE_ERROR,
                            "unsafe_estop_shutdown_action",
                            "ESTOP_IN timeout_shutdown module does not disable a recognized motor output group",
                            "firmware.modules",
                            required_behavior="estop_motor_shutdown",
                            accepted_disable_groups=sorted(safe_groups),
                            observed_disable_groups=sorted({
                                str(module.get("action", {}).get("disable_all"))
                                for module in candidates
                                if module.get("action", {}).get("disable_all") is not None
                            }),
                        ))

        if isr_count > _ISR_BUDGET:
            failures.append(_failure(
                FailureCategory.FIRMWARE_ERROR, "isr_budget_exceeded",
                f"Module ISR count {isr_count} exceeds budget of {_ISR_BUDGET}",
                "firmware.modules",
                isr_count=isr_count, budget=_ISR_BUDGET,
            ))
        if total_stack > _STACK_BUDGET_BYTES:
            failures.append(_failure(
                FailureCategory.FIRMWARE_ERROR, "stack_budget_exceeded",
                f"Aggregate module stack {total_stack} B exceeds budget of {_STACK_BUDGET_BYTES} B",
                "firmware.modules",
                total_stack_bytes=total_stack, budget_bytes=_STACK_BUDGET_BYTES,
            ))

        report = self._report("firmware_modules", failures)
        report.metrics = {
            "module_count": len(modules),
            "isr_count": isr_count,
            "total_stack_bytes": total_stack,
            "required_behaviors": required_behaviors,
            "artifact_check_enabled": module_artifact_dir is not None,
            "rendered_module_ids": sorted(rendered_module_ids),
        }
        return report

    def check_requirements_lowering(self, spec: dict[str, Any]) -> GateReport:
        failures: list[Failure] = []
        for item in spec.get("requirements", {}).get("active_unresolved", []):
            if item.get("release_blocking", True) and item.get("status") != "waived":
                failures.append(_failure(
                    FailureCategory.SPEC_ERROR,
                    "unlowered_requirement",
                    f"Requirement was not lowered into formal spec: {item.get('source')}",
                    "requirements.unresolved",
                    requirement_id=item.get("id"),
                    requirement_category=item.get("category"),
                    field_type=item.get("field_type"),
                    assumption_key=item.get("assumption_key"),
                    reason=item.get("reason"),
                    required_human_approvals=item.get("required_human_approvals", []),
                    affected_gates=item.get("affected_gates", []),
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
        assumptions_dict = assumptions if isinstance(assumptions, dict) else {}
        for name, assumption in assumptions_dict.items():
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


def _infer_power_domain(net_name: str | None) -> str | None:
    if not net_name:
        return None
    if net_name == "GND":
        return "GND"
    if net_name in {"VBAT", "VBAT_RAW", "VBAT_FUSED", "VSYS"} or net_name.startswith("VBAT"):
        return "VBAT"
    if net_name == "V5":
        return "V5"
    if net_name == "USB_VBUS":
        return "USB_5V"
    if net_name == "V3V3":
        return "V3V3"
    return None


def _component_pin_role_contract_failures(component: dict[str, Any]) -> list[Failure]:
    category = component.get("category")
    contract = CATEGORY_PIN_ROLE_CONTRACTS.get(str(category))
    if not contract:
        return []
    pins = component.get("pins", [])
    failures: list[Failure] = []
    ref = component.get("ref", "?")
    for requirement in contract:
        expected_names = {str(name).upper() for name in requirement.get("names", [])}
        expected_roles = set(requirement.get("roles", []))
        expected_domains = set(requirement.get("voltage_domains", []))
        matching_pins = [
            pin for pin in pins
            if not expected_names or str(pin.get("name", "")).upper() in expected_names
        ]
        if not matching_pins:
            failures.append(_failure(
                FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                "component_pin_contract_missing",
                f"{ref} is missing a required {category} pin contract",
                "electronics.components",
                ref=ref,
                component_category=category,
                expected_pin_names=sorted(expected_names),
                expected_roles=sorted(expected_roles),
            ))
            continue
        role_matches = [pin for pin in matching_pins if not expected_roles or pin.get("role") in expected_roles]
        if not role_matches:
            failures.append(_failure(
                FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                "component_pin_role_mismatch",
                f"{ref} has the required {category} pin by name, but its electrical role is wrong",
                "electronics.components",
                ref=ref,
                component_category=category,
                expected_pin_names=sorted(expected_names),
                expected_roles=sorted(expected_roles),
                observed=[{"number": pin.get("number"), "name": pin.get("name"), "role": pin.get("role")} for pin in matching_pins],
            ))
            continue
        if expected_domains and not any(pin.get("voltage_domain") in expected_domains for pin in role_matches):
            failures.append(_failure(
                FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                "component_pin_voltage_domain_mismatch",
                f"{ref} has the required {category} pin role, but its voltage domain is wrong",
                "electronics.components",
                ref=ref,
                component_category=category,
                expected_pin_names=sorted(expected_names),
                expected_voltage_domains=sorted(expected_domains),
                observed=[{"number": pin.get("number"), "name": pin.get("name"), "voltage_domain": pin.get("voltage_domain")} for pin in role_matches],
            ))
    return failures


def _pin_contracts_by_number(component: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = component.get("pin_contracts") or {}
    if isinstance(raw, dict):
        return {
            str(number): dict(contract)
            for number, contract in raw.items()
            if isinstance(contract, dict)
        }
    if isinstance(raw, list):
        return {
            str(contract.get("number")): dict(contract)
            for contract in raw
            if isinstance(contract, dict) and contract.get("number") is not None
        }
    return {}


def _curated_pin_contract_failures(component: dict[str, Any], pin: dict[str, Any], contract: dict[str, Any]) -> list[Failure]:
    ref = component.get("ref", "?")
    number = str(pin.get("number"))
    expected_type = str(contract.get("electrical_type", "")).lower()
    observed_role = str(pin.get("role") or pin.get("electrical_type") or "").lower()
    failures: list[Failure] = []
    if expected_type == "no_connect" and (not _pin_is_no_connect(pin) or pin.get("net")):
        failures.append(_failure(
            FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
            "curated_no_connect_pin_contract_violation",
            f"{ref}.{number} is a no-connect pin in the curated part contract but the graph models it as connected",
            "electronics.components",
            ref=ref,
            pin_number=number,
            pin_name=pin.get("name"),
            net=pin.get("net"),
            expected_pin_name=contract.get("name"),
            expected_electrical_type=contract.get("electrical_type"),
            observed_role=pin.get("role"),
        ))
    if expected_type in {"power_in", "power_out", "ground"} and observed_role != expected_type:
        failures.append(_failure(
            FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
            "curated_pin_electrical_type_mismatch",
            f"{ref}.{number} is {expected_type} in the curated part contract but the graph models it as {observed_role or '<unset>'}",
            "electronics.components",
            ref=ref,
            pin_number=number,
            pin_name=pin.get("name"),
            expected_pin_name=contract.get("name"),
            expected_electrical_type=contract.get("electrical_type"),
            observed_role=pin.get("role"),
            observed_electrical_type=pin.get("electrical_type"),
        ))
    expected_domain = contract.get("voltage_domain")
    if expected_domain and pin.get("voltage_domain") != expected_domain:
        failures.append(_failure(
            FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
            "curated_pin_voltage_domain_mismatch",
            f"{ref}.{number} voltage domain does not match the curated part contract",
            "electronics.components",
            ref=ref,
            pin_number=number,
            pin_name=pin.get("name"),
            expected_pin_name=contract.get("name"),
            expected_voltage_domain=expected_domain,
            observed_voltage_domain=pin.get("voltage_domain"),
        ))
    return failures


def _pin_sort_key(value: str) -> tuple[int, int | str]:
    text = str(value)
    return (0, int(text)) if text.isdigit() else (1, text)


def _pin_is_no_connect(pin: dict[str, Any]) -> bool:
    return (
        str(pin.get("role", "")).lower() == "no_connect"
        or str(pin.get("electrical_type", "")).lower() == "no_connect"
        or str(pin.get("name", "")).upper() in {"NC", "DNC", "N/C", "NO_CONNECT"}
        or pin.get("generated_no_connect") is True
    )


def _rail_nominal_voltages(spec: dict[str, Any]) -> dict[str, float]:
    values: dict[str, float] = {"GND": 0.0, "USB_VBUS": 5.0, "V5": 5.0, "V3V3": 3.3}
    battery = spec.get("system", {}).get("supply", {}).get("battery", {})
    if isinstance(battery.get("pack_voltage_nominal"), (int, float)):
        for name in ("VBAT", "VBAT_RAW", "VBAT_FUSED", "VSYS"):
            values[name] = float(battery["pack_voltage_nominal"])
    for rail in spec.get("system", {}).get("supply", {}).get("rails", []):
        name = rail.get("name")
        if not name:
            continue
        if isinstance(rail.get("voltage"), (int, float)):
            values[name] = float(rail["voltage"])
        elif isinstance(rail.get("voltage_min"), (int, float)) and isinstance(rail.get("voltage_max"), (int, float)):
            values[name] = (float(rail["voltage_min"]) + float(rail["voltage_max"])) / 2.0
    return values


def _rail_current_peaks(spec: dict[str, Any]) -> dict[str, float]:
    values: dict[str, float] = {}
    battery = spec.get("system", {}).get("supply", {}).get("battery", {})
    if isinstance(battery.get("pack_current_peak_a"), (int, float)):
        for name in ("VBAT", "VBAT_RAW", "VBAT_FUSED", "VSYS", "USB_VBUS"):
            values[name] = float(battery["pack_current_peak_a"])
    for rail in spec.get("system", {}).get("supply", {}).get("rails", []):
        name = rail.get("name")
        if name and isinstance(rail.get("current_peak_a"), (int, float)):
            values[name] = float(rail["current_peak_a"])
    return values


def _net_nominal_voltage(net_name: str, values: dict[str, float]) -> float | None:
    if net_name in values:
        return values[net_name]
    domain = _infer_power_domain(net_name)
    if domain in values:
        return values[domain]
    return None


def _component_nets(component: dict[str, Any]) -> set[str]:
    return {pin.get("net") for pin in component.get("pins", []) if pin.get("net")}


def _component_category_matches(component: dict[str, Any], categories: set[str]) -> bool:
    category = str(component.get("category", ""))
    if category in categories:
        return True
    if "regulator" in categories and category.startswith("regulator_"):
        return True
    if "pullup" in categories and category.endswith("_pullup"):
        return True
    return bool(set(component.get("constraints", [])) & categories)


def _component_is_i2c_pullup(
    component: dict[str, Any],
    net_name: str,
    nets_by_name: dict[str, dict[str, Any]],
) -> bool:
    if net_name not in _component_nets(component):
        return False
    positive_rails = {
        str(pin.get("net"))
        for pin in component.get("pins", [])
        if pin.get("net") != net_name and _is_positive_supply_net(pin.get("net"), nets_by_name)
    }
    if not positive_rails:
        return False
    if _component_category_matches(component, {"pullup"}):
        return True
    category = str(component.get("category", "")).lower()
    footprint = str(component.get("footprint", "")).upper()
    resistor_like = category.startswith("resistor") or category.endswith("_resistor") or footprint.startswith("R")
    return resistor_like and _component_resistance_ohms(component) is not None


def _component_output_current_limit_a(component: dict[str, Any]) -> float | None:
    for key in ("max_output_current_a", "output_current_a", "current_limit_a"):
        value = _number(component.get(key))
        if value is not None:
            return value
    for container_key in ("electrical_limits", "ratings", "datasheet_limits"):
        container = component.get(container_key)
        if not isinstance(container, dict):
            continue
        for key in ("max_output_current_a", "output_current_a", "current_limit_a"):
            value = _number(container.get(key))
            if value is not None:
                return value
    for constraint in component.get("constraints", []):
        value = _current_limit_from_constraint(str(constraint))
        if value is not None:
            return value
    mpn = str(component.get("mpn") or "").upper()
    return CURATED_REGULATOR_OUTPUT_CURRENT_A.get(mpn)


def _component_input_voltage_range_v(component: dict[str, Any]) -> tuple[float, float] | None:
    for source in _rating_sources(component):
        min_v = _number(source.get("input_voltage_min_v")) or _number(source.get("vin_min_v"))
        max_v = _number(source.get("input_voltage_max_v")) or _number(source.get("vin_max_v"))
        if min_v is not None and max_v is not None:
            return min_v, max_v
    constraint_range = _input_voltage_range_from_constraints(component.get("constraints", []))
    if constraint_range is not None:
        return constraint_range
    mpn = str(component.get("mpn") or "").upper()
    return CURATED_REGULATOR_INPUT_VOLTAGE_RANGE_V.get(mpn)


def _component_regulator_headroom_v(component: dict[str, Any]) -> float | None:
    for source in _rating_sources(component):
        value = (
            _number(source.get("min_dropout_headroom_v"))
            or _number(source.get("minimum_headroom_v"))
            or _number(source.get("dropout_voltage_v"))
            or _number(source.get("dropout_v"))
        )
        if value is not None:
            return value
    for constraint in component.get("constraints", []):
        text = str(constraint).strip().lower().replace("-", "_")
        mv_match = re.search(r"(?P<mv>\d+(?:\.\d+)?)\s*mv_dropout", text)
        if mv_match:
            return float(mv_match.group("mv")) / 1000.0
        vp_match = re.search(r"(?P<int>\d+)v(?P<frac>\d+)_dropout", text)
        if vp_match:
            return float(f"{vp_match.group('int')}.{vp_match.group('frac')}")
        v_match = re.search(r"(?P<v>\d+(?:p\d+|\.\d+)?)v(?:\d+)?_dropout", text)
        if v_match:
            return float(v_match.group("v").replace("p", "."))
    mpn = str(component.get("mpn") or "").upper()
    return CURATED_REGULATOR_MIN_HEADROOM_V.get(mpn)


def _regulator_enable_pins(component: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        pin for pin in component.get("pins", [])
        if str(pin.get("name") or "").upper().replace("~", "").replace("#", "") in {"EN", "ENABLE"}
    ]


def _regulator_enable_bias_details(
    enable_pin: dict[str, Any],
    regulator: dict[str, Any],
    components: list[dict[str, Any]],
    rail_nominal: dict[str, float],
    regulator_output_nets: set[str],
) -> dict[str, Any]:
    target_net = enable_pin.get("net")
    candidates: list[dict[str, Any]] = []
    if (
        target_net
        and target_net not in regulator_output_nets
        and (voltage := _net_nominal_voltage(str(target_net), rail_nominal)) is not None
        and voltage > 0.0
    ):
        return {
            "pin_number": enable_pin.get("number"),
            "pin_name": enable_pin.get("name"),
            "net_name": target_net,
            "enabled": True,
            "bias_source": "direct_positive_rail",
            "bias_voltage_v": voltage,
            "candidate_bias_components": candidates,
        }
    if target_net:
        for component in components:
            if component is regulator:
                continue
            nets = {pin.get("net") for pin in component.get("pins", []) if pin.get("net")}
            if target_net not in nets:
                continue
            resistance_ohms = _component_resistance_ohms(component)
            positive_rails = sorted(
                str(net) for net in nets
                if net != target_net
                and net not in regulator_output_nets
                and (voltage := _net_nominal_voltage(str(net), rail_nominal)) is not None
                and voltage > 0.0
            )
            candidate = {
                "ref": component.get("ref"),
                "category": component.get("category"),
                "nets": sorted(str(net) for net in nets),
                "positive_rails": positive_rails,
                "resistance_ohms": resistance_ohms,
            }
            candidates.append(candidate)
            if positive_rails and resistance_ohms is not None and 100.0 <= resistance_ohms <= 1_000_000.0:
                return {
                    "pin_number": enable_pin.get("number"),
                    "pin_name": enable_pin.get("name"),
                    "net_name": target_net,
                    "enabled": True,
                    "bias_source": "resistive_pullup",
                    "bias_component": component.get("ref"),
                    "bias_rails": positive_rails,
                    "resistance_ohms": resistance_ohms,
                    "candidate_bias_components": candidates,
                }
    return {
        "pin_number": enable_pin.get("number"),
        "pin_name": enable_pin.get("name"),
        "net_name": target_net,
        "enabled": False,
        "bias_source": None,
        "candidate_bias_components": candidates,
    }


def _component_supply_voltage_range_v(
    component: dict[str, Any],
    pin: dict[str, Any] | None = None,
) -> tuple[float | None, float | None] | None:
    for source in _rating_sources(component):
        min_v = (
            _number(source.get("supply_voltage_min_v"))
            or _number(source.get("input_voltage_min_v"))
            or _number(source.get("vcc_min_v"))
            or _number(source.get("vdd_min_v"))
            or _number(source.get("vin_min_v"))
        )
        max_v = (
            _number(source.get("supply_voltage_max_v"))
            or _number(source.get("input_voltage_max_v"))
            or _number(source.get("vcc_max_v"))
            or _number(source.get("vdd_max_v"))
            or _number(source.get("vin_max_v"))
        )
        if min_v is not None or max_v is not None:
            return min_v, max_v
    return _supply_voltage_range_from_constraints(component.get("constraints", []), pin)


def boot_strap_bias_failures(graph: dict[str, Any]) -> list[Failure]:
    components = graph.get("components", [])
    failures: list[Failure] = []
    for component in components:
        if str(component.get("category", "")).lower() != "mcu":
            continue
        for pin in component.get("pins", []):
            rule = _boot_strap_rule_for_pin(pin)
            if rule is None:
                continue
            expected_net, bias = rule
            target_net = pin.get("net")
            if not target_net:
                failures.append(Failure(
                    FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                    "boot_strap_bias_missing",
                    f"{component.get('ref', '?')}.{pin.get('number')} {pin.get('name')} boot strap is not connected to a biased net",
                    path="electronics.components",
                    details={
                        "ref": component.get("ref"),
                        "pin_number": pin.get("number"),
                        "pin_name": pin.get("name"),
                        "expected_bias": bias,
                        "expected_net": expected_net,
                    },
                ))
                continue
            if target_net == expected_net:
                continue
            valid_refs: list[str] = []
            candidate_refs: list[dict[str, Any]] = []
            for bias_component in components:
                if bias_component is component:
                    continue
                pins = bias_component.get("pins", [])
                nets = {item.get("net") for item in pins if item.get("net")}
                if target_net not in nets:
                    continue
                resistance_ohms = _component_resistance_ohms(bias_component)
                candidate = {
                    "ref": bias_component.get("ref"),
                    "category": bias_component.get("category"),
                    "nets": sorted(nets),
                    "resistance_ohms": resistance_ohms,
                }
                candidate_refs.append(candidate)
                if expected_net in nets and resistance_ohms is not None and BOOT_STRAP_MIN_BIAS_OHMS <= resistance_ohms <= BOOT_STRAP_MAX_BIAS_OHMS:
                    valid_refs.append(str(bias_component.get("ref")))
            if not valid_refs:
                failures.append(Failure(
                    FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                    "boot_strap_bias_missing",
                    f"{component.get('ref', '?')}.{pin.get('number')} {pin.get('name')} boot strap net {target_net} is not biased {bias} to {expected_net}",
                    path="electronics.components",
                    details={
                        "ref": component.get("ref"),
                        "pin_number": pin.get("number"),
                        "pin_name": pin.get("name"),
                        "net_name": target_net,
                        "expected_bias": bias,
                        "expected_net": expected_net,
                        "min_bias_ohms": BOOT_STRAP_MIN_BIAS_OHMS,
                        "max_bias_ohms": BOOT_STRAP_MAX_BIAS_OHMS,
                        "candidate_bias_components": candidate_refs,
                    },
                ))
    return failures


def _boot_strap_rule_for_pin(pin: dict[str, Any]) -> tuple[str, str] | None:
    pin_name = str(pin.get("name") or "").upper().replace("~", "").replace("#", "")
    net_name = str(pin.get("net") or "").upper()
    if pin_name == "HWB" or net_name == "HWB":
        return "GND", "pulldown"
    if pin_name == "BOOT0" or net_name in {"BOOT0", "BOOT0_GND"}:
        return "GND", "pulldown"
    if pin_name == "BOOTSEL" or net_name == "BOOTSEL":
        return "V3V3", "pullup"
    return None


def _rating_sources(component: dict[str, Any]) -> list[dict[str, Any]]:
    sources = [component]
    for key in ("electrical_limits", "ratings", "datasheet_limits"):
        value = component.get(key)
        if isinstance(value, dict):
            sources.append(value)
    return sources


def _input_voltage_range_from_constraints(constraints: Iterable[Any]) -> tuple[float, float] | None:
    minimum: float | None = None
    maximum: float | None = None
    for constraint in constraints:
        text = str(constraint).strip().lower().replace("-", "_")
        range_match = re.search(r"(?P<min>\d+(?:p\d+|\.\d+)?)v_to_(?P<max>\d+(?:p\d+|\.\d+)?)v_(?:input|vin|vm)\b", text)
        if range_match:
            return float(range_match.group("min").replace("p", ".")), float(range_match.group("max").replace("p", "."))
        max_match = re.search(r"(?P<max>\d+(?:p\d+|\.\d+)?)v_(?:input|vin|max_input|max_vin)\b", text)
        if max_match:
            maximum = float(max_match.group("max").replace("p", "."))
        min_match = re.search(r"(?:min_input|min_vin)_(?P<min>\d+(?:p\d+|\.\d+)?)v\b", text)
        if min_match:
            minimum = float(min_match.group("min").replace("p", "."))
    if minimum is not None and maximum is not None:
        return minimum, maximum
    return None


def _supply_voltage_range_from_constraints(
    constraints: Iterable[Any],
    pin: dict[str, Any] | None = None,
) -> tuple[float | None, float | None] | None:
    minimum: float | None = None
    maximum: float | None = None
    pin_name = str((pin or {}).get("name") or "").strip().lower().replace("-", "_")
    for constraint in constraints:
        text = str(constraint).strip().lower().replace("-", "_")
        if text in {"3v3_only", "3v3_supply"}:
            return 3.0, 3.6
        if text in {"3v3_compatible", "3v3_logic_compatible"}:
            maximum = min(maximum, 3.6) if maximum is not None else 3.6
            continue
        if text in {"5v_operation", "5v_supply"}:
            return 4.5, 5.5
        range_match = re.search(
            r"(?P<min>\d+(?:[vp.]\d+)?v?)_to_(?P<max>\d+(?:[vp.]\d+)?v?)(?:_(?P<target>supply|input|vin|vm))?\b",
            text,
        )
        if range_match:
            if not _pin_matches_voltage_constraint_target(pin_name, range_match.group("target")):
                continue
            return _voltage_number(range_match.group("min")), _voltage_number(range_match.group("max"))
        max_match = re.search(
            r"(?P<max>\d+(?:[vp.]\d+)?v?)_(?P<target>max|supply|max_supply|supply_max|input|input_max|vin|vin_max|max_input|max_vin|bus_max)\b",
            text,
        )
        if max_match:
            if not _pin_matches_voltage_constraint_target(pin_name, max_match.group("target")):
                continue
            value = _voltage_number(max_match.group("max"))
            maximum = min(maximum, value) if maximum is not None else value
    if minimum is not None or maximum is not None:
        return minimum, maximum
    return None


def _pin_matches_voltage_constraint_target(pin_name: str, target: str | None) -> bool:
    if not target:
        return True
    normalized = target.strip().lower()
    if normalized in {"supply", "max", "max_supply", "supply_max"}:
        return True
    if normalized in {"input", "input_max", "max_input", "vin", "vin_max", "max_vin"}:
        return not pin_name or pin_name in {"vin", "in", "v_in", "vbat", "vbus", "vm", "pvdd"}
    if normalized in {"vm", "bus_max"}:
        return not pin_name or pin_name in {"vm", "pvdd", "vbat", "vbus", "bus"}
    return True


def _voltage_number(value: str) -> float:
    text = value.strip().lower()
    if text.endswith("v"):
        text = text[:-1]
    text = text.replace("p", ".").replace("v", ".")
    return float(text)


def _current_limit_from_constraint(text: str) -> float | None:
    normalized = text.strip().lower().replace("-", "_")
    match = re.search(r"(?P<value>\d+(?:p\d+|\.\d+)?)\s*(?P<unit>ma|a)_(?:max|output|rated|limit)\b", normalized)
    if not match:
        match = re.search(r"(?P<value>\d+(?:p\d+|\.\d+)?)\s*(?P<unit>ma|a)\b", normalized)
    if not match:
        return None
    value = float(match.group("value").replace("p", "."))
    if match.group("unit") == "ma":
        value /= 1000.0
    return value


def _component_resistance_ohms(component: dict[str, Any]) -> float | None:
    for source in _rating_sources(component):
        for key in ("resistance_ohms", "ohms", "resistance"):
            value = _number(source.get(key))
            if value is not None:
                return value
    for key in ("value", "mpn", "supplier_sku"):
        value = _resistance_ohms_from_text(component.get(key))
        if value is not None:
            return value
    for constraint in component.get("constraints", []):
        value = _resistance_ohms_from_text(constraint)
        if value is not None:
            return value
    return None


def _resistance_ohms_from_text(value: Any) -> float | None:
    text = str(value or "").strip().lower().replace("ω", "r").replace("ohm", "r")
    if not text:
        return None
    compact_match = re.search(r"(?<![a-z0-9])(?P<int>\d+)(?P<unit>[rkm])(?P<frac>\d+)(?![a-z0-9])", text)
    if compact_match:
        number = float(f"{compact_match.group('int')}.{compact_match.group('frac')}")
        unit = compact_match.group("unit")
        if unit == "k":
            number *= 1000.0
        elif unit == "m":
            number *= 1_000_000.0
        return number
    match = re.search(r"(?<![a-z0-9])(?P<int>\d+(?:\.\d+)?)(?:r(?P<frac>\d+))?(?P<unit>[rkm])?(?![a-z0-9])", text)
    if not match:
        return None
    number = float(f"{match.group('int')}.{match.group('frac')}" if match.group("frac") else match.group("int"))
    unit = match.group("unit") or "r"
    if unit == "k":
        number *= 1000.0
    elif unit == "m":
        number *= 1_000_000.0
    return number


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _component_has_nets(component: dict[str, Any], required_nets: set[str]) -> bool:
    return required_nets <= _component_nets(component)


def _is_usb_c_connector(component: dict[str, Any]) -> bool:
    text = " ".join(str(component.get(key) or "") for key in ("value", "mpn", "footprint")).upper()
    return "USB-C" in text or "USB_C" in text or "USB4105" in text


def _is_positive_supply_net(net_name: str | None, nets_by_name: dict[str, dict[str, Any]]) -> bool:
    if not net_name or net_name == "GND":
        return False
    net = nets_by_name.get(net_name, {})
    if net.get("signal_class") == "power":
        return True
    return (_infer_power_domain(net_name) or "") not in {"", "GND"}


def _signal_endpoint_supply_nets(
    net_name: str,
    nets_by_name: dict[str, dict[str, Any]],
    components: list[dict[str, Any]],
    excluded_categories: set[str] | None = None,
    excluded_refs: set[str] | None = None,
) -> list[str]:
    excluded_categories = excluded_categories or set()
    excluded_refs = excluded_refs or set()
    components_by_ref = {
        str(component.get("ref")): component
        for component in components
        if component.get("ref")
    }
    endpoint_refs = {
        str(endpoint).partition(".")[0]
        for endpoint in nets_by_name.get(net_name, {}).get("connected_pins", [])
        if "." in str(endpoint)
    }
    supply_nets: set[str] = set()
    for ref in endpoint_refs:
        component = components_by_ref.get(ref)
        if ref in excluded_refs:
            continue
        if not component or _component_category_matches(component, excluded_categories):
            continue
        for pin in component.get("pins", []):
            if pin.get("role") == "power_in" and _is_positive_supply_net(pin.get("net"), nets_by_name):
                supply_nets.add(str(pin["net"]))
    return sorted(supply_nets)


def persist_report(project_path: Path, report: GateReport) -> str:
    path = project_path / "validation" / "reports" / f"{report.gate}.json"
    write_json(path, report.to_dict())
    return str(path)
