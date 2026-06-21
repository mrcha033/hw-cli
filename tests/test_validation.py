from __future__ import annotations

from copy import deepcopy
import json


def test_template_schema_is_valid_but_semantic_budget_fails(service, project):
    spec = service.read_spec(project)
    schema = service.validator.validate_spec(spec)
    semantic = service.validator.check_electrical_semantics(spec)
    assert schema.status == "pass"
    assert semantic.status == "fail"
    assert {item.code for item in semantic.failures} == {"current_budget_exceeded"}


def test_mechanical_clearance_passes_template(service, project):
    report = service.validator.check_mechanical(service.read_spec(project))
    assert report.status == "pass"


def test_pin_conflicts_and_net_mismatches_are_reported(service):
    report = service.validator.check_pinmap([
        {"signal": "I2C_SCL", "mcu_pin": "GPIO9", "net_name": "I2C_SCL"},
        {"signal": "MOTOR3_PWM", "mcu_pin": "GPIO9", "net_name": "PWM_WRONG"},
    ])
    assert report.status == "fail"
    assert {item.code for item in report.failures} == {"pin_conflict", "peripheral_mismatch"}


def test_release_rejects_unresolved_assumptions_and_missing_exports(service, project):
    service.generate_all(project)
    checks = service.run_all_checks(project, include_external=False)
    release = service.check_release_gate(project, [service._report_from_dict(item) for item in checks["reports"]])
    codes = {item["code"] for item in release["failures"]}
    assert release["status"] == "blocked"
    assert "unresolved_critical_assumption" in codes
    assert "missing_export" in codes


def test_bom_requires_approved_mpns(service):
    report = service.validator.check_bom([{"ref": "U1", "mpn": None}, {"ref": "R1", "mpn": "RC0603FR-0710KL"}])
    assert report.status == "fail"
    assert [item.code for item in report.failures] == ["missing_mpn"]


def test_graph_pin_resolution_rejects_component_pin_net_mismatch(service):
    report = service.validator.check_graph_pin_resolution({
        "components": [
            {"ref": "U1", "pins": [{"number": "1", "net": "V3V3"}]},
        ],
        "nets": [
            {"name": "GND", "connected_pins": ["U1.1"]},
        ],
    })

    assert report.status == "fail"
    assert {item.code for item in report.failures} == {"graph_pin_net_mismatch"}
    assert report.failures[0].details["endpoint"] == "U1.1"


def test_support_circuit_contract_rejects_miswired_protection(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    bad_graph = deepcopy(graph)
    reverse = next(component for component in bad_graph["components"] if component["category"] == "reverse_polarity")
    for pin in reverse["pins"]:
        if pin["name"] == "CATHODE":
            pin["net"] = "V5"
            break

    report = service._support_circuit_completeness_report(service.read_spec(project), bad_graph)

    assert report.status == "fail"
    failure = next(item for item in report.failures if item.code == "support_role_net_contract_failed")
    assert failure.details["role"] == "reverse_polarity"
    assert failure.details["missing_nets"] == ["VBAT"]


def test_power_tree_integrity_passes_generated_robotics_graph(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))

    report = service.validator.check_power_tree(graph, service.read_spec(project))

    assert report.status == "pass"
    assert {"VBAT_RAW", "VBAT_FUSED", "VBAT", "VSYS", "V5", "V3V3"} <= set(report.metrics["source_nets"])


def test_power_tree_integrity_rejects_unreachable_power_load(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    bad_graph = deepcopy(graph)
    regulator = next(component for component in bad_graph["components"] if component["category"] == "regulator")
    vin = next(pin for pin in regulator["pins"] if pin["role"] == "power_in")
    vin["net"] = "__UNPOWERED_RAIL__"
    vin["voltage_domain"] = None

    report = service.validator.check_power_tree(bad_graph, service.read_spec(project))

    assert report.status == "fail"
    assert "power_net_unreachable" in {item.code for item in report.failures}


def test_power_tree_integrity_rejects_regulator_voltage_inversion(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    bad_graph = deepcopy(graph)
    regulator = next(component for component in bad_graph["components"] if component["ref"] == "U4")
    vin = next(pin for pin in regulator["pins"] if pin["role"] == "power_in")
    vout = next(pin for pin in regulator["pins"] if pin["role"] == "power_out")
    vin["net"], vout["net"] = vout["net"], vin["net"]
    vin["voltage_domain"], vout["voltage_domain"] = vout["voltage_domain"], vin["voltage_domain"]

    report = service.validator.check_power_tree(bad_graph, service.read_spec(project))

    assert report.status == "fail"
    assert "power_output_exceeds_input_voltage" in {item.code for item in report.failures}


def test_interface_integrity_passes_generated_robotics_graph(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))

    report = service.validator.check_interface_integrity(graph)

    assert report.status == "pass"
    assert report.metrics["i2c_nets_checked"] >= 2
    assert report.metrics["can_pair_present"] is True
    assert report.metrics["usb_bridge_present"] is True


def test_interface_integrity_rejects_missing_i2c_pullups(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    bad_graph = deepcopy(graph)
    bad_graph["components"] = [component for component in bad_graph["components"] if component["category"] != "pullup"]

    report = service.validator.check_interface_integrity(bad_graph)

    assert report.status == "fail"
    assert "i2c_pullup_missing" in {item.code for item in report.failures}


def test_interface_integrity_rejects_missing_can_termination(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    bad_graph = deepcopy(graph)
    bad_graph["components"] = [component for component in bad_graph["components"] if component["category"] != "termination"]

    report = service.validator.check_interface_integrity(bad_graph)

    assert report.status == "fail"
    assert "can_termination_missing" in {item.code for item in report.failures}


def test_interface_integrity_rejects_missing_usb_esd_bridge(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    bad_graph = deepcopy(graph)
    bad_graph["components"] = [component for component in bad_graph["components"] if component["category"] != "usb_esd"]

    report = service.validator.check_interface_integrity(bad_graph)

    assert report.status == "fail"
    assert "usb_esd_bridge_missing" in {item.code for item in report.failures}


def test_firmware_interface_contract_passes_generated_robotics_artifacts(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    pinmap = json.loads((project_path / "firmware" / "generated" / "pinmap.json").read_text(encoding="utf-8"))

    report = service._firmware_interface_contract_report(project_path, service.read_spec(project), graph, pinmap)

    assert report.status == "pass"
    assert {"i2c", "can", "motor_pwm", "estop"} <= set(report.metrics["required_interfaces"])


def test_firmware_interface_contract_rejects_missing_can_bringup(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    pinmap = json.loads((project_path / "firmware" / "generated" / "pinmap.json").read_text(encoding="utf-8"))
    sources = service._firmware_test_sources(project_path)
    sources_without_can = {name: text for name, text in sources.items() if "CAN" not in (name + text).upper()}

    report = service._firmware_interface_contract_report(
        project_path,
        service.read_spec(project),
        graph,
        pinmap,
        test_sources=sources_without_can,
    )

    assert report.status == "fail"
    assert "firmware_can_bringup_missing" in {item.code for item in report.failures}


def test_firmware_modules_pass_generated_estop_shutdown_behavior(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    pinmap = json.loads((project_path / "firmware" / "generated" / "pinmap.json").read_text(encoding="utf-8"))
    modules = service.read_spec(project)["firmware"]["modules"]

    report = service.validator.check_firmware_modules(modules, pinmap, spec=service.read_spec(project), graph=graph)

    assert report.status == "pass"
    assert "estop_motor_shutdown" in report.metrics["required_behaviors"]
    assert (project_path / "firmware" / "modules" / "motor_estop_watchdog.c").is_file()


def test_firmware_modules_reject_missing_estop_shutdown_behavior(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    pinmap = json.loads((project_path / "firmware" / "generated" / "pinmap.json").read_text(encoding="utf-8"))

    report = service.validator.check_firmware_modules([], pinmap, spec=service.read_spec(project), graph=graph)

    assert report.status == "fail"
    assert "missing_estop_shutdown_behavior" in {item.code for item in report.failures}


def test_firmware_modules_reject_estop_shutdown_without_motor_disable(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    pinmap = json.loads((project_path / "firmware" / "generated" / "pinmap.json").read_text(encoding="utf-8"))
    module = deepcopy(service.read_spec(project)["firmware"]["modules"][0])
    module["action"]["disable_all"] = "telemetry_only"

    report = service.validator.check_firmware_modules([module], pinmap, spec=service.read_spec(project), graph=graph)

    assert report.status == "fail"
    assert "unsafe_estop_shutdown_action" in {item.code for item in report.failures}
