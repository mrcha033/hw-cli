from __future__ import annotations

import json
from copy import deepcopy


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


def test_mechanical_connector_retention_passes_high_vibration_template(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))

    report = service.validator.check_mechanical_connector_retention(service.read_spec(project), graph)

    assert report.status == "pass"
    assert report.metrics["required"] is True
    assert not report.metrics["missing_connector_refs"]


def test_high_vibration_exposed_connectors_require_retention_fixture(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    spec = deepcopy(service.read_spec(project))
    spec["mechanical"]["fixtures"].pop("cable_retention", None)

    report = service.validator.check_mechanical_connector_retention(spec, graph)

    assert report.status == "fail"
    assert {item.code for item in report.failures} == {"connector_retention_missing"}


def test_mechanical_connector_cutouts_pass_generated_template(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))

    report = service.validator.check_mechanical_connector_cutouts(service.read_spec(project), graph)

    assert report.status == "pass"
    assert report.metrics["required"] is True
    assert "J1" in report.metrics["interface_refs"]


def test_mechanical_connector_cutouts_reject_edge_misalignment(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    spec = service.read_spec(project)
    bad_graph = deepcopy(graph)
    connector = next(component for component in bad_graph["components"] if component["ref"] == "J1")
    envelope = spec["mechanical"]["envelope"]
    connector["pcb_position_mm"] = [
        envelope["board_width_mm"] / 2.0,
        envelope["board_height_mm"] / 2.0,
    ]

    report = service.validator.check_mechanical_connector_cutouts(spec, bad_graph)

    assert report.status == "fail"
    assert "connector_cutout_alignment_failed" in {item.code for item in report.failures}


def test_mechanical_connector_cutouts_reject_invalid_opening_geometry(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    spec = deepcopy(service.read_spec(project))
    next(item for item in spec["mechanical"]["connector_interfaces"] if item["ref"] == "J1")["opening_mm"] = [0.0, 10.0]
    next(item for item in spec["mechanical"]["connector_interfaces"] if item["ref"] == "J2")["center_z_mm"] = 27.0

    report = service.validator.check_mechanical_connector_cutouts(spec, graph)

    assert report.status == "fail"
    codes = {item.code for item in report.failures}
    assert "connector_cutout_opening_invalid" in codes
    assert "connector_cutout_opening_out_of_bounds" in codes
    assert {"J1", "J2"} <= set(report.metrics["invalid_opening_refs"])


def test_mechanical_mounting_integrity_passes_generated_template(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))

    report = service.validator.check_mechanical_mounting_integrity(service.read_spec(project), graph)

    assert report.status == "pass"
    assert report.metrics["holes_checked"] >= 3


def test_mechanical_mounting_integrity_rejects_component_on_hole(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    spec = service.read_spec(project)
    bad_graph = deepcopy(graph)
    target = next(component for component in bad_graph["components"] if not component["ref"].startswith("J"))
    hole = spec["mechanical"]["mounting_holes"][0]
    target["pcb_position_mm"] = [hole["x_mm"], hole["y_mm"]]

    report = service.validator.check_mechanical_mounting_integrity(spec, bad_graph)

    assert report.status == "fail"
    assert "mounting_hole_component_keepout_intrusion" in {item.code for item in report.failures}


def test_component_metadata_rejects_no_connect_pin_wired_to_net(service):
    component = {
        "ref": "U1",
        "resolution": "curated",
        "review_status": "approved",
        "symbol": {"verified": True, "expected_pins": ["1", "2"]},
        "footprint_metadata": {"verified": True, "expected_pads": ["1", "2"]},
        "pins": [
            {"number": "1", "name": "VIN", "role": "power_in", "net": "V3V3", "voltage_domain": "V3V3"},
            {"number": "2", "name": "NC", "role": "no_connect", "net": "GND"},
        ],
        "sourcing": {"status": "resolved"},
    }

    report = service.validator.check_component_metadata([component])

    assert report.status == "fail"
    assert "no_connect_pin_wired" in {item.code for item in report.failures}


def test_component_metadata_rejects_curated_no_connect_contract_violation(service):
    component = {
        "ref": "U1",
        "resolution": "curated",
        "review_status": "approved",
        "symbol": {"verified": True, "expected_pins": ["1", "2"]},
        "footprint_metadata": {"verified": True, "expected_pads": ["1", "2"]},
        "pin_contracts": {
            "1": {"number": "1", "name": "VIN", "electrical_type": "power_in", "voltage_domain": "V3V3"},
            "2": {"number": "2", "name": "NC", "electrical_type": "no_connect"},
        },
        "pins": [
            {"number": "1", "name": "VIN", "role": "power_in", "net": "V3V3", "voltage_domain": "V3V3"},
            {"number": "2", "name": "GPIO", "role": "bidirectional", "net": "GPIO1"},
        ],
        "sourcing": {"status": "resolved"},
    }

    report = service.validator.check_component_metadata([component])

    failures = {item.code: item for item in report.failures}
    assert report.status == "fail"
    assert "curated_no_connect_pin_contract_violation" in failures
    assert failures["curated_no_connect_pin_contract_violation"].details["expected_pin_name"] == "NC"


def test_mechanical_mounting_integrity_rejects_hole_edge_violation(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    spec = deepcopy(service.read_spec(project))
    spec["mechanical"]["mounting_holes"][0]["x_mm"] = 0.2

    report = service.validator.check_mechanical_mounting_integrity(spec, graph)

    assert report.status == "fail"
    assert "mounting_hole_edge_clearance_failed" in {item.code for item in report.failures}


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


def test_candidate_critic_records_physical_and_native_gaps_without_blocking(service, project):
    service.generate_all(project)
    checks = service.run_all_checks(project, include_external=False)
    report = next(item for item in checks["reports"] if item["gate"] == "candidate_critic")

    assert report["status"] == "pass"
    assert report["metrics"]["critic_version"] == "candidate_critic_v0"
    assert report["metrics"]["warnings"] >= 2
    assert report["metrics"]["errors"] == 0
    codes = {failure["code"] for failure in report["failures"]}
    assert {"physical_oracle_gap_open", "native_toolchain_gates_not_run"} <= codes


def test_candidate_critic_fails_false_release_eligibility_claim(service, project):
    service.generate_all(project)
    path = service.workspace.require_project(project)
    graph = json.loads((path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    graph["provenance"]["release_eligible"] = True
    report = service._candidate_critic_report(
        path,
        service.read_spec(project),
        graph,
        [service.validator.validate_spec(service.read_spec(project))],
        include_external=False,
    )

    assert report.status == "fail"
    assert "candidate_graph_claims_release_eligible" in {failure.code for failure in report.failures}


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


def test_power_tree_integrity_rejects_regulator_input_voltage_range_violation(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    bad_graph = deepcopy(graph)
    regulator = next(component for component in bad_graph["components"] if component["ref"] == "U5")
    vin = next(pin for pin in regulator["pins"] if pin["role"] == "power_in")
    vin["net"] = "VSYS"
    vin["voltage_domain"] = "VBAT"

    report = service.validator.check_power_tree(bad_graph, service.read_spec(project))

    assert report.status == "fail"
    failure = next(item for item in report.failures if item.code == "regulator_input_voltage_out_of_range")
    assert failure.details["ref"] == "U5"
    assert failure.details["input_voltage_max_v"] == 17.0
    assert failure.details["observed_input_voltage_max_v"] == 24.0


def test_power_integrity_estimate_passes_generated_graph(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))

    report = service.validator.check_power_integrity_estimate(graph, service.read_spec(project))

    assert report.status == "pass"
    assert report.metrics["rails_checked"] > 0
    assert "V3V3" in report.metrics["coverage"]
    assert report.metrics["regulator_current_limits"]["U5"]["output_current_limit_a"] == 3.0


def test_power_integrity_estimate_rejects_regulator_current_overload(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    spec = deepcopy(service.read_spec(project))
    rail = next(item for item in spec["system"]["supply"]["rails"] if item["name"] == "V3V3")
    rail["current_peak_a"] = 4.0

    report = service.validator.check_power_integrity_estimate(graph, spec)

    assert report.status == "fail"
    failure = next(item for item in report.failures if item.code == "regulator_output_current_exceeded")
    assert failure.details["ref"] == "U5"
    assert failure.details["rail"] == "V3V3"
    assert failure.details["output_current_limit_a"] == 3.0
    assert failure.details["declared_peak_current_a"] == 4.0


def test_power_integrity_estimate_rejects_missing_decoupling(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    graph["components"] = [component for component in graph["components"] if component.get("category") != "decoupling"]

    report = service.validator.check_power_integrity_estimate(graph, service.read_spec(project))

    assert report.status == "fail"
    assert "rail_decoupling_missing" in {item.code for item in report.failures}


def test_power_integrity_estimate_rejects_cap_without_ground_return(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    decoupling = next(component for component in graph["components"] if component.get("category") == "decoupling")
    ground_pin = next(pin for pin in decoupling["pins"] if pin.get("net") == "GND")
    ground_pin["net"] = "V3V3"

    report = service.validator.check_power_integrity_estimate(graph, service.read_spec(project))

    assert report.status == "fail"
    failure = next(item for item in report.failures if item.code == "capacitor_ground_return_missing")
    assert failure.details["ref"] == decoupling["ref"]
    assert "GND" not in failure.details["present_nets"]


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


def test_firmware_interface_contract_rejects_missing_motor_pwm_channel(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    pinmap = json.loads((project_path / "firmware" / "generated" / "pinmap.json").read_text(encoding="utf-8"))
    pinmap = [item for item in pinmap if item.get("signal") != "MOTOR12_PWM"]

    report = service._firmware_interface_contract_report(project_path, service.read_spec(project), graph, pinmap)

    assert report.status == "fail"
    failure = next(item for item in report.failures if item.code == "firmware_motor_pwm_channel_missing")
    assert failure.details["missing_channels"] == [12]


def test_firmware_modules_pass_generated_estop_shutdown_behavior(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    pinmap = json.loads((project_path / "firmware" / "generated" / "pinmap.json").read_text(encoding="utf-8"))
    modules = service.read_spec(project)["firmware"]["modules"]

    report = service.validator.check_firmware_modules(
        modules,
        pinmap,
        spec=service.read_spec(project),
        graph=graph,
        module_dir=project_path / "firmware" / "modules",
    )

    assert report.status == "pass"
    assert "estop_motor_shutdown" in report.metrics["required_behaviors"]
    assert report.metrics["artifact_check_enabled"] is True
    assert (project_path / "firmware" / "modules" / "motor_estop_watchdog.c").is_file()


def test_firmware_modules_reject_missing_or_stale_generated_artifacts(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    pinmap = json.loads((project_path / "firmware" / "generated" / "pinmap.json").read_text(encoding="utf-8"))
    modules = service.read_spec(project)["firmware"]["modules"]
    module_dir = project_path / "firmware" / "modules"
    (module_dir / "motor_estop_watchdog.c").unlink()
    (module_dir / "motor_estop_watchdog.h").write_text("/* stale generated header */\n", encoding="utf-8")

    report = service.validator.check_firmware_modules(
        modules,
        pinmap,
        spec=service.read_spec(project),
        graph=graph,
        module_dir=module_dir,
    )

    assert report.status == "fail"
    codes = {item.code for item in report.failures}
    assert "firmware_module_artifact_missing" in codes
    assert "firmware_module_artifact_stale" in codes


def test_hw_sw_parity_rejects_wrong_mcu_pin_name(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    pinmap = json.loads((project_path / "firmware" / "generated" / "pinmap.json").read_text(encoding="utf-8"))
    pinmap[0]["mcu_pin"] = "__WRONG_MCU_PIN__"

    report = service.validator.check_hw_sw_parity(graph, pinmap)

    assert report.status == "fail"
    assert "firmware_mcu_pin_mismatch" in {item.code for item in report.failures}


def test_hw_sw_parity_rejects_graph_pin_net_mismatch(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    pinmap = json.loads((project_path / "firmware" / "generated" / "pinmap.json").read_text(encoding="utf-8"))
    pinmap[0]["graph_pin"] = pinmap[1]["graph_pin"]

    report = service.validator.check_hw_sw_parity(graph, pinmap)

    assert report.status == "fail"
    assert "firmware_graph_pin_net_mismatch" in {item.code for item in report.failures}


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


def test_firmware_sensor_poll_rejects_missing_hardware_bus(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    pinmap = json.loads((project_path / "firmware" / "generated" / "pinmap.json").read_text(encoding="utf-8"))
    module = {
        "id": "phantom_spi_sensor",
        "behavior": "sensor_poll",
        "bus": "spi",
        "sensor": "imu",
        "poll_interval_ms": 100,
    }

    report = service.validator.check_firmware_modules([module], pinmap, spec=service.read_spec(project), graph=graph)

    assert report.status == "fail"
    assert "firmware_sensor_bus_missing" in {item.code for item in report.failures}


def test_firmware_sensor_poll_rejects_unresolved_sensor_target(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    pinmap = json.loads((project_path / "firmware" / "generated" / "pinmap.json").read_text(encoding="utf-8"))
    module = {
        "id": "phantom_i2c_sensor",
        "behavior": "sensor_poll",
        "bus": "i2c",
        "sensor": "unobtanium_gas_sensor",
        "poll_interval_ms": 100,
    }

    report = service.validator.check_firmware_modules([module], pinmap, spec=service.read_spec(project), graph=graph)

    assert report.status == "fail"
    assert "firmware_sensor_target_missing" in {item.code for item in report.failures}


def test_firmware_periodic_transmit_rejects_missing_transport(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    pinmap = json.loads((project_path / "firmware" / "generated" / "pinmap.json").read_text(encoding="utf-8"))
    module = {
        "id": "phantom_uart_telemetry",
        "behavior": "periodic_transmit",
        "transport": "uart",
        "interval_ms": 100,
        "frame": {"id": "0x10", "dlc": 4, "content": "status"},
    }

    report = service.validator.check_firmware_modules([module], pinmap, spec=service.read_spec(project), graph=graph)

    assert report.status == "fail"
    assert "firmware_transport_missing" in {item.code for item in report.failures}


def test_firmware_periodic_transmit_rejects_can_fd_dlc_without_can_fd_contract(service, project):
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    pinmap = json.loads((project_path / "firmware" / "generated" / "pinmap.json").read_text(encoding="utf-8"))
    module = {
        "id": "oversized_can_telemetry",
        "behavior": "periodic_transmit",
        "transport": "can",
        "interval_ms": 100,
        "frame": {"id": "0x100", "dlc": 64, "content": "status"},
    }

    report = service.validator.check_firmware_modules([module], pinmap, spec=service.read_spec(project), graph=graph)

    assert report.status == "fail"
    assert "firmware_can_frame_dlc_unsupported" in {item.code for item in report.failures}
