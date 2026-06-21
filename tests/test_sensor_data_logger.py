from __future__ import annotations

import json
import runpy
from pathlib import Path

from hw_codesign.io import read_yaml, write_yaml
from hw_codesign.models import GateReport, Status


def _set_backend(service, project: str, backend: str) -> None:
    path = service.workspace.require_project(project) / "spec" / "system.yaml"
    payload = read_yaml(path)
    payload["electronics"]["backend"] = backend
    write_yaml(path, payload)


def _resolve_review_assumptions(service, project: str) -> None:
    for name, assumption in service.read_spec(project).get("assumptions", {}).items():
        if assumption.get("requires_user_review"):
            service.resolve_assumption(project, name, assumption.get("value") or "approved", approved=True)


def test_sensor_data_logger_template_generates_esp32_graph(service):
    project = "sensor_data_logger_board"
    service.create_project(project, template="sensor_data_logger")
    result = service.generate_electronics_only(project)

    assert result["resolution_report"]["status"] == "pass"
    graph_path = service.workspace.require_project(project) / "electronics" / "generated" / "electrical_graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    by_ref = {component["ref"]: component for component in graph["components"]}
    categories = {component["category"] for component in graph["components"]}

    assert graph["design_basis"]["architecture"] == "esp32s3_usb_i2c_sensor_data_logger"
    assert by_ref["U1"]["component_id"] == "esp32s3_wroom_1"
    assert by_ref["U1"]["mpn"] == "ESP32-S3-WROOM-1-N8"
    assert by_ref["U1"]["footprint"] == "RF_Module:ESP32-S3-WROOM-1"
    assert {"power_input", "tvs", "regulator", "mcu", "imu", "debug"}.issubset(categories)
    assert "motor_io" not in categories
    assert "can" not in categories
    assert "safety_gate" not in categories
    assert "USB_DP" in {net["name"] for net in graph["nets"]}
    assert "I2C_IMU_SCL" in {net["name"] for net in graph["nets"]}

    semantic_path = service.workspace.require_project(project) / "electronics" / "generated" / "semantic" / "semantic_schematic.json"
    semantic_code_path = semantic_path.with_suffix(".py")
    generated_files = {Path(path).name for path in result["files"]}
    semantic = json.loads(semantic_path.read_text(encoding="utf-8"))
    semantic_from_code = runpy.run_path(str(semantic_code_path))["semantic_schematic"]
    usb_dp = next(net for net in semantic["nets"] if net["name"] == "USB_DP")

    assert "semantic_schematic.json" in generated_files
    assert "semantic_schematic.py" in generated_files
    assert semantic_code_path.is_file()
    assert semantic["authoring_model"] == "semantic-first-pin-name-wiring"
    assert semantic_from_code == semantic
    assert any(connection["pin_name"] == "USB_D+" for connection in usb_dp["pin_name_connections"])


def test_sensor_data_logger_intent_files_are_sensor_specific(service):
    project = "sensor_data_logger_board"
    service.create_project(project, template="sensor_data_logger")
    project_path = service.workspace.require_project(project)
    stale = project_path / "electronics" / "intent" / "motor_channels.intent.md"
    stale.write_text("stale motor intent\n", encoding="utf-8")

    result = service.generate_electronics_only(project)
    generated_names = {Path(path).name for path in result["files"]}
    intent_dir = project_path / "electronics" / "intent"
    board_intent = (intent_dir / "board.intent.md").read_text(encoding="utf-8")
    power_intent = (intent_dir / "power_tree.intent.md").read_text(encoding="utf-8")
    data_logger_intent = (intent_dir / "data_logger.intent.md").read_text(encoding="utf-8")

    assert "data_logger.intent.md" in generated_names
    assert "motor_channels.intent.md" not in generated_names
    assert not stale.exists()
    assert "UsbCPowerInput" in board_intent
    assert "MotorChannel" not in board_intent
    assert "USB_VBUS" in power_intent
    assert "controller power" not in power_intent
    assert "usb_console = required" in data_logger_intent


def test_sensor_data_logger_kicad_artifacts_use_sensor_identity_and_two_layer_stackup(service):
    project = "sensor_data_logger_board"
    service.create_project(project, template="sensor_data_logger")
    service.generate_electronics_only(project)
    project_path = service.workspace.require_project(project)
    kicad_dir = project_path / "electronics" / "generated" / "kicad"
    legacy_schematic = (kicad_dir / f"{project}.sch").read_text(encoding="utf-8")
    board = (kicad_dir / f"{project}.kicad_pcb").read_text(encoding="utf-8")

    assert 'Title "ESP32-S3 Sensor Data Logger"' in legacy_schematic
    assert 'Title "Robot Controller"' not in legacy_schematic
    assert '(0 "F.Cu" signal)' in board
    assert '(31 "B.Cu" signal)' in board
    assert '"In1.Cu"' not in board
    assert '"In2.Cu"' not in board
    assert '(zone ' in board
    assert '(net_name "GND") (layer "B.Cu")' in board
    assert '(net_name "V3V3")' not in board


def test_sensor_data_logger_checks_do_not_require_robotics_blocks(service):
    project = "sensor_data_logger_board"
    service.create_project(project, template="sensor_data_logger")
    service.generate_all(project)
    checks = service.run_all_checks(project, include_external=False)
    reports = {report["gate"]: report for report in checks["reports"]}

    assert reports["component_resolution"]["status"] == "pass"
    assert reports["datasheet_evidence"]["status"] == "pass"
    assert reports["sourcing_resilience"]["status"] == "pass"
    assert reports["semantic_schematic_roundtrip"]["status"] == "pass"
    assert reports["semantic_schematic_roundtrip"]["metrics"]["code_roundtrip_exact"] is True
    assert reports["firmware_pinmap"]["status"] == "pass"
    assert reports["firmware_modules"]["status"] == "pass"
    assert reports["firmware_interface_contract"]["status"] == "pass"
    assert reports["ir_erc"]["status"] == "pass"
    assert reports["placement_constraints"]["status"] == "pass"
    assert reports["layout_thermal_integrity"]["status"] == "pass"
    assert reports["layout_signal_integrity"]["status"] == "pass"
    assert reports["reference_firmware_build"]["status"] == "pass"
    assert "missing_required_block" not in {
        failure["code"]
        for failure in reports["ir_erc"]["failures"]
    }
    assert "off_board" not in {
        failure["code"]
        for failure in reports["placement_constraints"]["failures"]
    }


def test_semantic_schematic_roundtrip_gate_fails_on_code_drift(service):
    project = "sensor_data_logger_board"
    service.create_project(project, template="sensor_data_logger")
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    code_path = project_path / "electronics" / "generated" / "semantic" / "semantic_schematic.py"
    code_path.write_text(code_path.read_text(encoding="utf-8") + "\nsemantic_schematic['nets'][0]['name'] = 'DRIFTED_NET'\n", encoding="utf-8")

    checks = service.run_all_checks(project, include_external=False)
    report = next(item for item in checks["reports"] if item["gate"] == "semantic_schematic_roundtrip")
    failure_codes = {failure["code"] for failure in report["failures"]}

    assert report["status"] == "fail"
    assert "semantic_schematic_roundtrip_mismatch" in failure_codes


def test_sensor_data_logger_mechanical_contract_uses_sensor_placement(service):
    project = "sensor_data_logger_board"
    service.create_project(project, template="sensor_data_logger")
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    contract = json.loads((project_path / "mechanical" / "source" / "mechanical_contract.json").read_text(encoding="utf-8"))
    by_ref = {component["ref"]: component for component in graph["components"]}
    j1_cutout = next(item for item in contract["connector_cutouts"] if item["ref"] == "J1")
    board = contract["board"]

    assert by_ref["J1"]["pcb_position_mm"] == [18.0, 4.0]
    assert j1_cutout["pcb_position_mm"] == by_ref["J1"]["pcb_position_mm"]
    assert j1_cutout["electrical_category"] == "power_input"
    assert j1_cutout["enclosure_position_mm"] == [21.0, 7.0]
    assert all(0.0 <= item["x_mm"] <= board["width_mm"] and 0.0 <= item["y_mm"] <= board["height_mm"] for item in board["component_height_map"])


def test_sensor_data_logger_firmware_artifacts_are_sensor_specific(service):
    project = "sensor_data_logger_board"
    service.create_project(project, template="sensor_data_logger")
    service.generate_all(project)
    project_path = service.workspace.require_project(project)

    app_cmake = (project_path / "firmware" / "zephyr" / "app" / "CMakeLists.txt").read_text(encoding="utf-8")
    prj_conf = (project_path / "firmware" / "zephyr" / "app" / "prj.conf").read_text(encoding="utf-8")
    board_dir = project_path / "firmware" / "zephyr" / "boards" / "xtensa" / "sensor_data_logger"
    reference_cmake = (project_path / "firmware" / "reference" / "CMakeLists.txt").read_text(encoding="utf-8")
    pinmap_h = (project_path / "firmware" / "generated" / "pinmap.h").read_text(encoding="utf-8")

    assert "project(sensor_data_logger)" in app_cmake
    assert "CONFIG_I2C=y" in prj_conf
    assert "CONFIG_USB_DEVICE_STACK=y" in prj_conf
    assert "CONFIG_CAN" not in prj_conf
    assert "CONFIG_PWM" not in prj_conf
    assert (board_dir / "sensor_data_logger.dts").is_file()
    assert "ESP32-S3 Sensor Data Logger" in (board_dir / "sensor_data_logger.dts").read_text(encoding="utf-8")
    assert "CONFIG_BOARD_SENSOR_DATA_LOGGER=y" in (board_dir / "sensor_data_logger_defconfig").read_text(encoding="utf-8")
    assert "project(sensor_data_logger_bsp C)" in reference_cmake
    assert (project_path / "firmware" / "zephyr" / "tests" / "test_usb_console.c").is_file()
    assert not (project_path / "firmware" / "zephyr" / "tests" / "test_motor_pwm.c").exists()
    assert "PIN_I2C_IMU_SCL" in pinmap_h
    assert "PIN_USB_DP" in pinmap_h
    assert "PIN_USB_DM" in pinmap_h
    assert "PIN_UART_TX" in pinmap_h
    assert "PIN_IMU_INT" in pinmap_h
    assert "PIN_MOTOR" not in pinmap_h


def test_sensor_data_logger_hw_sw_parity_requires_usb_firmware_assignment(service):
    project = "sensor_data_logger_board"
    service.create_project(project, template="sensor_data_logger")
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    pinmap = json.loads((project_path / "firmware" / "generated" / "pinmap.json").read_text(encoding="utf-8"))
    pinmap = [item for item in pinmap if item["net_name"] != "USB_DP"]

    report = service.validator.check_hw_sw_parity(graph, pinmap)

    assert report.status == "fail"
    assert any(failure.code == "missing_firmware_assignment" and "USB_DP" in failure.message for failure in report.failures)


def test_sensor_data_logger_firmware_regeneration_removes_inactive_profile_tests(service):
    project = "sensor_data_logger_board"
    service.create_project(project, template="sensor_data_logger")
    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    stale = project_path / "firmware" / "zephyr" / "tests" / "test_motor_pwm.c"
    stale.write_text("/* stale robot test */\n", encoding="utf-8")

    service.generate_firmware_only(project)

    assert not stale.exists()
    assert (project_path / "firmware" / "zephyr" / "tests" / "test_usb_console.c").is_file()


def test_sensor_data_logger_release_docs_are_sensor_specific(service, monkeypatch):
    project = "sensor_data_logger_board"
    service.create_project(project, template="sensor_data_logger")
    _set_backend(service, project, "tscircuit")
    service.generate_all(project)
    _resolve_review_assumptions(service, project)

    monkeypatch.setattr(
        service.kicad,
        "export_manufacturing",
        lambda path, release: GateReport("fabrication_export", Status.PASS),
    )
    monkeypatch.setattr(
        service.mechanical,
        "generate",
        lambda spec, target, **kwargs: GateReport("mechanical_export", Status.PASS),
    )

    result = service.prepare_release(project, {"reports": [{"gate": "all_required", "status": "pass"}]}, native_checks_confirmed=True)
    release_path = service.workspace.require_project(project) / "exports" / "releases" / "r1"
    build_instructions = (release_path / "firmware" / "build_instructions.md").read_text(encoding="utf-8")
    bringup = (release_path / "docs" / "bringup_guide.md").read_text(encoding="utf-8")
    risks = (release_path / "docs" / "known_risks.md").read_text(encoding="utf-8")

    assert result["status"] == "released"
    assert "west build -b sensor_data_logger firmware/zephyr/app" in build_instructions
    assert "robot_controller" not in build_instructions
    assert "USB-C VBUS" in bringup
    assert "CAN loopback" not in bringup
    assert "motor" not in bringup.lower()
    assert "USB-powered" in risks
    assert "high-current power behavior" in risks
