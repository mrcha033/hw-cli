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
        required = ["reverse_polarity", "fuse_or_efuse", "tvs", "watchdog"]
        if channels > 0:
            required.append("motor_enable_gate")
        for protection in required:
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
            if category in transfer_categories:
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

        for component, inputs, outputs in transfers:
            known_inputs = [value for net in inputs if (value := _net_nominal_voltage(net, rail_nominal)) is not None]
            known_outputs = [value for net in outputs if (value := _net_nominal_voltage(net, rail_nominal)) is not None]
            input_domains = {_infer_power_domain(net) for net in inputs}
            output_domains = {_infer_power_domain(net) for net in outputs}
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
            "power_loads_checked": sum(
                1
                for component in graph.get("components", [])
                if component.get("category") not in source_categories
                for pin in component.get("pins", [])
                if pin.get("role") == "power_in"
            ),
        }
        return report

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
            pullup_refs = sorted(
                str(component.get("ref"))
                for component in components
                if _component_category_matches(component, {"pullup"})
                and net_name in _component_nets(component)
                and any(_is_positive_supply_net(pin.get("net"), nets_by_name) for pin in component.get("pins", []))
            )
            if not pullup_refs:
                failures.append(_failure(
                    FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                    "i2c_pullup_missing",
                    f"I2C net {net_name} has no pull-up to a positive logic rail",
                    "electronics.components",
                    net_name=net_name,
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
            if not any(_component_category_matches(component, {"termination"}) and _component_has_nets(component, {"CANH", "CANL"}) for component in components):
                failures.append(_failure(
                    FailureCategory.ELECTRICAL_SEMANTIC_ERROR,
                    "can_termination_missing",
                    "CANH/CANL are present without a termination component across the pair",
                    "electronics.components",
                    required_nets=["CANH", "CANL"],
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

        report = self._report("interface_integrity", failures)
        report.metrics = {
            **report.metrics,
            "i2c_nets_checked": len(i2c_nets),
            "can_pair_present": can_high and can_low,
            "usb_nets_checked": len(present_usb_nets),
            "usb_bridge_present": usb_bridge_present,
        }
        return report

    def check_hw_sw_parity(self, graph: dict[str, Any], assignments: Iterable[dict[str, Any]]) -> GateReport:
        hardware_nets = {item["name"] for item in graph.get("nets", [])}
        assignments = list(assignments)
        failures: list[Failure] = []
        for assignment in assignments:
            if assignment.get("net_name") not in hardware_nets:
                failures.append(_failure(FailureCategory.FIRMWARE_ERROR, "missing_hardware_net", f"Firmware signal has no electrical net: {assignment.get('net_name')}", "firmware.pin_assignments"))
        firmware_nets = {item.get("net_name") for item in assignments}
        ignored_mcu_nets = {"V3V3", "V5", "GND", "SWDIO", "SWCLK", "NRST"}
        required_nets = {
            pin.get("net")
            for component in graph.get("components", [])
            if component.get("category") == "mcu"
            for pin in component.get("pins", [])
            if pin.get("role") not in {"power_in", "power_out", "ground"} and pin.get("net") not in ignored_mcu_nets
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
    ) -> GateReport:
        from .backends.firmware_modules import _RENDERERS, BEHAVIOR_SCHEMAS
        failures: list[Failure] = []
        known_signals = {item.get("signal") for item in pinmap if item.get("signal")}
        seen_ids: set[str] = set()
        isr_count = 0
        total_stack = 0
        _ISR_BUDGET = 8
        _STACK_BUDGET_BYTES = 65536
        required_behaviors: list[str] = []
        graph = graph or {}

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

            # Render to get stack info
            try:
                from .backends.firmware_modules import render_module
                output = render_module(mod)
                total_stack += output.stack_size_bytes
                if output.is_isr:
                    isr_count += 1
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
    if component.get("category") in categories:
        return True
    return bool(set(component.get("constraints", [])) & categories)


def _component_has_nets(component: dict[str, Any], required_nets: set[str]) -> bool:
    return required_nets <= _component_nets(component)


def _is_positive_supply_net(net_name: str | None, nets_by_name: dict[str, dict[str, Any]]) -> bool:
    if not net_name or net_name == "GND":
        return False
    net = nets_by_name.get(net_name, {})
    if net.get("signal_class") == "power":
        return True
    return (_infer_power_domain(net_name) or "") not in {"", "GND"}


def persist_report(project_path: Path, report: GateReport) -> str:
    path = project_path / "validation" / "reports" / f"{report.gate}.json"
    write_json(path, report.to_dict())
    return str(path)
