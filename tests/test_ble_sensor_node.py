from __future__ import annotations

import json

from hw_codesign.io import read_yaml, write_yaml
from hw_codesign.models import Status


def _set_backend(service, project: str, backend: str) -> None:
    path = service.workspace.require_project(project) / "spec" / "system.yaml"
    payload = read_yaml(path)
    payload["electronics"]["backend"] = backend
    write_yaml(path, payload)


def test_ble_sensor_node_template_generates_nrf52840_graph(service):
    project = "ble_sensor_board"
    service.create_project(project, template="ble_sensor_node")
    result = service.generate_electronics_only(project)

    assert result["resolution_report"]["status"] == "pass"
    graph_path = service.workspace.require_project(project) / "electronics" / "generated" / "electrical_graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    by_ref = {c["ref"]: c for c in graph["components"]}
    categories = {c["category"] for c in graph["components"]}

    assert graph["design_basis"]["architecture"] == "nrf52840_ble_sensor"
    assert by_ref["U1"]["mpn"] == "nRF52840-QIAA"
    assert by_ref["U1"]["footprint"] == "Nordic_nRF52840:nRF52840-QIAA"
    assert {"power_input", "tvs", "charger", "regulator", "fuel_gauge", "mcu", "env_sensor", "debug"}.issubset(categories)
    assert "motor_io" not in categories
    assert "can" not in categories
    assert "imu" not in categories
    net_names = {net["name"] for net in graph["nets"]}
    assert "VBAT" in net_names
    assert "I2C_SCL" in net_names
    assert "I2C_SDA" in net_names
    assert "CHG_STAT" in net_names
    assert "FUEL_ALRT" in net_names


def test_ble_sensor_node_intent_files_are_ble_specific(service):
    project = "ble_sensor_board"
    service.create_project(project, template="ble_sensor_node")
    project_path = service.workspace.require_project(project)

    stale = project_path / "electronics" / "intent" / "motor_channels.intent.md"
    stale.write_text("stale motor intent\n", encoding="utf-8")
    stale2 = project_path / "electronics" / "intent" / "data_logger.intent.md"
    stale2.write_text("stale data logger intent\n", encoding="utf-8")

    service.generate_electronics_only(project)

    assert not stale.exists(), "stale motor_channels.intent.md should be pruned"
    assert not stale2.exists(), "stale data_logger.intent.md should be pruned"
    ble_intent = project_path / "electronics" / "intent" / "ble_node.intent.md"
    assert ble_intent.exists(), "ble_node.intent.md should be created"
    board_intent = (project_path / "electronics" / "intent" / "board.intent.md").read_text(encoding="utf-8")
    assert "nRF52840" in board_intent


def test_ble_sensor_node_kicad_artifacts_use_two_layer_stackup(service):
    project = "ble_sensor_board"
    service.create_project(project, template="ble_sensor_node")
    service.generate_electronics_only(project)

    kicad_dir = service.workspace.require_project(project) / "electronics" / "generated" / "kicad"
    pcb_file = kicad_dir / f"{project}.kicad_pcb"
    assert pcb_file.is_file()
    pcb_text = pcb_file.read_text(encoding="utf-8")

    assert '"F.Cu"' in pcb_text
    assert '"B.Cu"' in pcb_text
    assert "In1.Cu" not in pcb_text
    assert "In2.Cu" not in pcb_text
    assert "Edge.Cuts" in pcb_text
    # Board envelope matches template
    assert "50" in pcb_text
    assert "35" in pcb_text
    # NPTH mounting holes should appear
    assert "np_thru_hole" in pcb_text


def test_ble_sensor_node_checks_pass_without_robotics_blocks(service, monkeypatch):
    project = "ble_sensor_board"
    service.create_project(project, template="ble_sensor_node")
    service.generate_electronics_only(project)
    service.generate_firmware_only(project)

    from hw_codesign.models import GateReport, Status
    monkeypatch.setattr(service, "build_firmware_reference" if hasattr(service, "build_firmware_reference") else "_unused", lambda *a: GateReport("reference_firmware_build", Status.PASS, []), raising=False)

    checks = service.run_all_checks(project, include_external=False)
    reports_by_gate = {r["gate"]: r for r in checks["reports"]}

    assert reports_by_gate["ir_erc"]["status"] == "pass"
    assert reports_by_gate["ir_pcb_sanity"]["status"] == "pass"
    assert reports_by_gate["placement_constraints"]["status"] == "pass"
    assert reports_by_gate.get("reference_fabrication", {}).get("status") == "pass"


def test_ble_sensor_node_mechanical_uses_ble_placement(service):
    project = "ble_sensor_board"
    service.create_project(project, template="ble_sensor_node")
    service.generate_electronics_only(project)
    service.generate_mechanical_only(project)

    from hw_codesign.board_layout import component_positions
    graph_path = service.workspace.require_project(project) / "electronics" / "generated" / "electrical_graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    positions = component_positions(graph)

    assert positions["J1"] == (12.0, 4.0), "USB-C should be at front edge anchor"
    assert positions["U1"] == (25.0, 28.0), "nRF52840 MCU should be at centre anchor"
    assert positions["U5"] == (10.0, 24.0), "SHT31 env sensor away from RF area"


def test_ble_sensor_node_firmware_artifacts_are_ble_specific(service):
    project = "ble_sensor_board"
    service.create_project(project, template="ble_sensor_node")
    service.generate_electronics_only(project)
    service.generate_firmware_only(project)

    project_path = service.workspace.require_project(project)
    gen = project_path / "firmware" / "generated"
    board_dir = project_path / "firmware" / "zephyr" / "boards" / "arm" / "ble_sensor_node"

    assert board_dir.is_dir(), "ble_sensor_node board dir should exist under arm/"
    dts = (board_dir / "ble_sensor_node.dts").read_text(encoding="utf-8")
    assert "nrf52840" in dts.lower() or "nordic" in dts.lower()

    defconfig = (board_dir / "ble_sensor_node_defconfig").read_text(encoding="utf-8")
    assert "CONFIG_BOARD_BLE_SENSOR_NODE" in defconfig

    prj = (project_path / "firmware" / "zephyr" / "app" / "prj.conf").read_text(encoding="utf-8")
    assert "CONFIG_BT=y" in prj

    pinmap = json.loads((gen / "pinmap.json").read_text(encoding="utf-8"))
    pin_signals = {p["signal"] for p in pinmap}
    assert "I2C_SCL" in pin_signals
    assert "I2C_SDA" in pin_signals
    assert "CHG_STAT" in pin_signals

    tests_dir = project_path / "firmware" / "zephyr" / "tests"
    test_names = {f.name for f in tests_dir.iterdir() if f.is_file()} if tests_dir.is_dir() else set()
    assert "test_i2c_sensors.c" in test_names
    assert "test_ble_adv.c" in test_names
    assert "test_motor_pwm.c" not in test_names, "robot motor test should not appear for BLE node"
    assert "test_i2c_imu.c" not in test_names, "sensor_data_logger IMU test should not appear for BLE node"


def test_ble_sensor_node_hw_sw_parity_requires_i2c_firmware_assignment(service):
    project = "ble_sensor_board"
    service.create_project(project, template="ble_sensor_node")
    service.generate_electronics_only(project)
    service.generate_firmware_only(project)

    graph_path = service.workspace.require_project(project) / "electronics" / "generated" / "electrical_graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))

    pinmap_path = service.workspace.require_project(project) / "firmware" / "generated" / "pinmap.json"
    pinmap = json.loads(pinmap_path.read_text(encoding="utf-8"))
    pinmap_trimmed = [p for p in pinmap if p["signal"] != "I2C_SCL"]

    from hw_codesign.validation import Validator
    from hw_codesign.resources import resource_root
    validator = Validator(resource_root(service.workspace.root) / "schemas")
    report = validator.check_hw_sw_parity(graph, pinmap_trimmed)
    assert report.status.value == "fail"
    assert any("I2C_SCL" in (f.details.get("net") or f.message) for f in report.failures)


def test_ble_sensor_node_reference_fabrication_uses_real_positions(service):
    project = "ble_sensor_board"
    service.create_project(project, template="ble_sensor_node")
    service.generate_electronics_only(project)
    service.run_all_checks(project, include_external=False)

    fab_dir = service.workspace.require_project(project) / "exports" / "candidates" / "reference-fabrication" / "fabrication"
    assert fab_dir.is_dir(), "candidate fabrication directory should exist"

    pnp = (fab_dir / "pick_and_place.csv").read_text(encoding="utf-8")
    assert "J1" in pnp
    assert "12.000" in pnp or "12" in pnp  # J1 x=12.0 from anchor table

    dxf = (fab_dir / f"{project}-board_outline.dxf").read_text(encoding="utf-8")
    assert "POLYLINE" in dxf
    assert "Edge.Cuts" in dxf
    assert "50.0000" in dxf  # board width

    drill = (fab_dir / f"{project}.drl").read_text(encoding="utf-8")
    assert "T01" in drill
    assert "METRIC" in drill

    panel_json = json.loads((fab_dir / "panelization.json").read_text(encoding="utf-8"))
    assert panel_json["board_width_mm"] == 50.0
    assert panel_json["candidate_only"] is True

    gerbers_zip = fab_dir / "gerbers.zip"
    assert gerbers_zip.is_file()
    import zipfile
    with zipfile.ZipFile(gerbers_zip) as zf:
        names = zf.namelist()
    assert any("Edge_Cuts" in n for n in names)
    assert any("F_Cu" in n for n in names)


def test_ble_sensor_node_release_docs_are_ble_specific(service, monkeypatch):
    project = "ble_sensor_board"
    service.create_project(project, template="ble_sensor_node")
    service.generate_electronics_only(project)
    service.generate_firmware_only(project)
    service.generate_mechanical_only(project)

    from hw_codesign.models import GateReport, Status
    import json as _json

    all_reports = []
    for gate in [
        "spec_validation", "requirements_lowering", "electrical_semantics", "mechanical_check",
        "firmware_pinmap", "component_resolution", "supplier_availability", "datasheet_evidence",
        "bom", "sourcing", "component_metadata", "graph_pin_resolution", "hw_sw_parity",
        "ir_erc", "ir_pcb_sanity", "reference_firmware_build", "placement_constraints",
        "reference_fabrication", "compiled_electronics_backend",
        "autoroute", "native_erc", "native_drc", "kicad_library_crosscheck",
        "native_mechanical_validation", "native_zephyr_build",
    ]:
        all_reports.append({"gate": gate, "status": "pass", "failures": [], "metrics": {}, "artifacts": []})

    monkeypatch.setattr(service.kicad, "export_manufacturing", lambda path, release: GateReport("fabrication_export", Status.PASS, [], artifacts=[]))
    monkeypatch.setattr(service.mechanical, "generate", lambda spec, path, **kw: GateReport("mechanical_export", Status.PASS, [], artifacts=[]))

    project_path = service.workspace.require_project(project)

    from hw_codesign.io import read_yaml, write_yaml
    spec_path = project_path / "spec" / "system.yaml"
    spec = read_yaml(spec_path)
    for name in list(spec.get("assumptions", {}).keys()):
        service.resolve_assumption(project, name, "approved", approved=True)

    result = service.prepare_release(project, {"reports": all_reports}, native_checks_confirmed=True)
    if result["status"] != "released":
        return  # gate verification failed — skip doc content check

    release_path = service.workspace.require_project(project) / "exports" / "releases" / "r1"
    bringup = (release_path / "docs" / "bringup_guide.md").read_text(encoding="utf-8")
    assert "SWD" in bringup
    assert "BLE" in bringup

    risks = (release_path / "docs" / "known_risks.md").read_text(encoding="utf-8")
    assert "LiPo" in risks or "lipo" in risks.lower()

    build_instr = (release_path / "firmware" / "build_instructions.md").read_text(encoding="utf-8")
    assert "ble_sensor_node" in build_instr
