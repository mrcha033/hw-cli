from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from hw_codesign.backends.freerouting import FreeroutingBackend
from hw_codesign.backends.kicad import KiCadBackend
from hw_codesign.errors import UnsafeChangeError
from hw_codesign.reference_backend import internal_drc, internal_erc
from hw_codesign.schematic_generator import generate_kicad_schematic


def test_generation_is_deterministic_and_cross_domain(service, project):
    first = service.generate_all(project)
    second = service.generate_all(project)
    assert first == second
    path = service.workspace.require_project(project)
    assert (path / "electronics" / "intent" / "board.intent.md").is_file()
    assert not list((path / "electronics" / "source").glob("*.ato"))
    assert (path / "mechanical" / "source" / "enclosure.py").is_file()
    assert (path / "firmware" / "generated" / "pinmap.h").is_file()
    assert (path / "electronics" / "generated" / "bom.csv").is_file()
    routing = json.loads((path / "electronics" / "generated" / "kicad" / "routing.json").read_text())
    assert routing["mode"] == "plane_preseeded"
    assert routing["signal_routing"] == "deferred_to_freerouting"


def test_robotics_controller_kicad_artifacts_keep_four_layer_stackup(service, project):
    service.generate_electronics_only(project)
    path = service.workspace.require_project(project)
    kicad_dir = path / "electronics" / "generated" / "kicad"
    legacy_schematic = (kicad_dir / f"{project}.sch").read_text(encoding="utf-8")
    board = (kicad_dir / f"{project}.kicad_pcb").read_text(encoding="utf-8")

    assert 'Title "Robot Controller"' in legacy_schematic
    assert '(0 "F.Cu" signal)' in board
    assert '(2 "In1.Cu" power)' in board
    assert '(4 "In2.Cu" power)' in board
    assert '(31 "B.Cu" signal)' in board
    assert '(net_name "GND") (layer "In1.Cu")' in board
    assert '(net_name "V5") (layer "In2.Cu")' in board


def test_ir_pcb_sanity_rejects_layers_outside_stackup(service):
    project = "sensor_logger_bad_layer_artifact"
    service.create_project(project, template="sensor_data_logger")
    service.generate_electronics_only(project)
    path = service.workspace.require_project(project)
    spec = service.read_spec(project)
    graph = json.loads((path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    board_path = path / "electronics" / "generated" / "kicad" / f"{project}.kicad_pcb"
    board = board_path.read_text(encoding="utf-8")
    board = board.replace('    (31 "B.Cu" signal)', '    (2 "In1.Cu" power)\n    (31 "B.Cu" signal)', 1)
    board = board.replace(
        '  (gr_rect',
        '  (zone (net 1) (net_name "GND") (layer "In1.Cu") (polygon (pts (xy 1 1) (xy 2 1) (xy 2 2) (xy 1 2))))\n  (gr_rect',
        1,
    )
    board_path.write_text(board, encoding="utf-8")

    report = internal_drc(path, spec, graph)

    assert report.status.value == "fail"
    codes = {failure.code for failure in report.failures}
    assert "pcb_stackup_layer_mismatch" in codes
    assert "pcb_layer_not_in_stackup" in codes
    assert "In1.Cu" in report.metrics["declared_copper_layers"]


def test_kicad_artifact_selection_prefers_canonical_board(service):
    project = "rp2040_kicad_stale_duplicate_check"
    service.create_project(project, template="rp2040_usb_device")
    service.generate_electronics_only(project)
    path = service.workspace.require_project(project)
    kicad_dir = path / "electronics" / "generated" / "kicad"
    canonical = kicad_dir / f"{project}.kicad_pcb"
    stale = kicad_dir / f"{project} 2.kicad_pcb"
    stale.write_text('(kicad_pcb (version 20240108) (generator stale_duplicate))\n', encoding="utf-8")

    assert KiCadBackend()._design_file(path, "*.kicad_pcb") == canonical

    service.generate_electronics_only(project)
    assert canonical.is_file()
    assert not stale.exists()


def test_rp2040_qspi_flash_uses_all_quad_data_lines(service):
    project = "rp2040_usb_device_check"
    service.create_project(project, template="rp2040_usb_device")
    service.generate_electronics_only(project)
    path = service.workspace.require_project(project)
    graph = json.loads((path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    provenance_report = service.validator.check_component_metadata(graph["components"])
    assert provenance_report.status.value == "pass"
    interface_report = service.validator.check_interface_integrity(graph)
    assert interface_report.status.value == "pass"
    assert interface_report.metrics["usb_bridge_present"] is True
    power_tree_report = service.validator.check_power_tree(graph, service.read_spec(project))
    assert power_tree_report.status.value == "pass"
    assert {"USB_VBUS", "V3V3"} <= set(power_tree_report.metrics["source_nets"])
    power_integrity_report = service.validator.check_power_integrity_estimate(graph, service.read_spec(project))
    assert power_integrity_report.status.value == "pass"
    input_bulk = next(item for item in graph["components"] if item["ref"] == "C4")
    assert input_bulk["category"] == "bulk_cap"
    assert {pin["net"] for pin in input_bulk["pins"]} == {"USB_VBUS", "GND"}

    flash = next(item for item in graph["components"] if item["ref"] == "U3")
    flash_pins = {pin["number"]: pin for pin in flash["pins"]}
    assert flash_pins["3"]["name"] == "~WP"
    assert flash_pins["3"]["net"] == "QSPI_D2"
    assert flash_pins["3"]["role"] == "bidirectional"
    assert flash_pins["7"]["name"] == "~HOLD"
    assert flash_pins["7"]["net"] == "QSPI_D3"
    assert flash_pins["7"]["role"] == "bidirectional"

    nets = {net["name"]: set(net["connected_pins"]) for net in graph["nets"]}
    assert {"J1.3", "D1.1"} <= nets["USB_DP_RAW"]
    assert {"J1.4", "D1.3"} <= nets["USB_DM_RAW"]
    assert {"D1.2", "U2.4"} <= nets["USB_DP"]
    assert {"D1.4", "U2.3"} <= nets["USB_DM"]
    assert {"U2.10", "U3.3"} <= nets["QSPI_D2"]
    assert {"U2.11", "U3.7"} <= nets["QSPI_D3"]

    positions = {item["ref"]: item["pcb_position_mm"] for item in graph["components"]}
    assert positions["J1"] == [3.0, 10.0]
    assert positions["U2"] == [18.0, 4.0]
    assert positions["U3"] == [33.0, 2.0]

    routing = json.loads((path / "electronics" / "generated" / "kicad" / "routing.json").read_text(encoding="utf-8"))
    assert routing["status"] == "generated"
    assert routing["failures"] == []
    spec = service.read_spec(project)
    assert spec["manufacturing"]["pcb"]["min_clearance_mm"] >= 0.15
    assert spec["manufacturing"]["pcb"]["min_track_width_mm"] >= 0.15
    ir_drc_report = internal_drc(path, spec, graph)
    assert ir_drc_report.status.value == "pass"

    board = (path / "electronics" / "generated" / "kicad" / f"{project}.kicad_pcb").read_text(encoding="utf-8")
    assert 'footprint "Package_DFN_QFN:QFN-56-1EP_7x7mm_P0.4mm_EP3.2x3.2mm"' in board
    assert 'footprint "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"' in board


@pytest.mark.parametrize(
    "template",
    [
        "esp32_wifi_gateway",
        "avr_32u4_hid",
        "stm32g0_power_monitor",
        "samd21_sensor_hub",
        "nrf52840_dongle",
    ],
)
def test_usb_templates_bridge_raw_connector_nets_to_protected_usb_nets(service, template):
    project = f"{template}_usb_bridge_check"
    service.create_project(project, template=template)
    service.generate_electronics_only(project)
    path = service.workspace.require_project(project)
    graph = json.loads((path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))

    report = service.validator.check_interface_integrity(graph)
    codes = {failure.code for failure in report.failures}
    assert "usb_differential_pair_incomplete" not in codes
    assert "usb_esd_bridge_missing" not in codes
    assert report.metrics["usb_bridge_present"] is True

    metadata_report = service.validator.check_component_metadata(graph["components"])
    assert [failure.code for failure in metadata_report.failures if failure.path == "D1"] == []

    d1 = next(component for component in graph["components"] if component["ref"] == "D1")
    assert {pin["net"] for pin in d1["pins"] if pin.get("net")} == {
        "USB_DP_RAW",
        "USB_DM_RAW",
        "USB_DP",
        "USB_DM",
        "GND",
    }
    nets = {net["name"]: set(net["connected_pins"]) for net in graph["nets"]}
    assert {"J1.3", "D1.1"} <= nets["USB_DP_RAW"]
    assert {"J1.4", "D1.3"} <= nets["USB_DM_RAW"]
    assert "D1.2" in nets["USB_DP"]
    assert "D1.4" in nets["USB_DM"]


@pytest.mark.parametrize("template", ["lora_sensor_node", "samd21_sensor_hub", "stm32g0_power_monitor"])
def test_i2c_templates_include_recognized_pullups_to_logic_rail(service, template):
    project = f"{template}_i2c_pullup_check"
    service.create_project(project, template=template)
    service.generate_electronics_only(project)
    path = service.workspace.require_project(project)
    graph = json.loads((path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))

    report = service.validator.check_interface_integrity(graph)

    assert report.status.value == "pass"
    assert "i2c_pullup_missing" not in {failure.code for failure in report.failures}
    nets = {net["name"]: set(net["connected_pins"]) for net in graph["nets"]}
    components_by_ref = {component["ref"]: component for component in graph["components"]}
    for net_name in ("I2C_SCL", "I2C_SDA"):
        pullup_refs = {
            endpoint.partition(".")[0]
            for endpoint in nets[net_name]
            if components_by_ref[endpoint.partition(".")[0]].get("category")
            in {"pullup", "i2c_pullup", "resistor_4k7"}
            and any(pin.get("net") == "V3V3" for pin in components_by_ref[endpoint.partition(".")[0]].get("pins", []))
        }
        assert pullup_refs


def test_samd21_sensor_hub_uses_curated_sensor_and_debug_pin_contracts(service):
    project = "samd21_sensor_contract_check"
    service.create_project(project, template="samd21_sensor_hub")
    service.generate_all(project)
    path = service.workspace.require_project(project)
    graph = json.loads((path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    mechanical_contract = json.loads((path / "mechanical" / "source" / "mechanical_contract.json").read_text(encoding="utf-8"))

    metadata_report = service.validator.check_component_metadata(graph["components"])
    assert metadata_report.status.value == "pass"
    assert mechanical_contract["connector_cutouts"]
    assert service.validator.validate_spec(service.read_spec(project)).status.value == "pass"
    assert service.validator.check_electrical_semantics(service.read_spec(project)).status.value == "pass"
    assert service.validator.check_mechanical(service.read_spec(project)).status.value == "pass"
    firmware_modules_report = service.validator.check_firmware_modules(
        service.read_spec(project)["firmware"]["modules"],
        json.loads((path / "firmware" / "generated" / "pinmap.json").read_text(encoding="utf-8")),
        spec=service.read_spec(project),
        graph=graph,
        module_dir=path / "firmware" / "modules",
    )
    assert firmware_modules_report.status.value == "pass"
    assert internal_erc(graph).status.value == "pass"

    components = {component["ref"]: component for component in graph["components"]}
    mcu_pins = {pin["number"]: pin for pin in components["U2"]["pins"]}
    assert mcu_pins["27"]["net"] == "IMU_INT1"
    imu_pins = {pin["number"]: pin for pin in components["U3"]["pins"]}
    assert imu_pins["2"]["name"] == "SDX"
    assert imu_pins["2"]["net"] == "I2C_SDA"
    assert imu_pins["3"]["name"] == "SCX"
    assert imu_pins["3"]["net"] == "I2C_SCL"
    assert imu_pins["4"]["net"] == "IMU_INT1"
    assert imu_pins["5"]["net"] is None
    assert imu_pins["5"]["role"] == "no_connect"

    env_pins = {pin["number"]: pin for pin in components["U4"]["pins"]}
    assert {number: env_pins[number]["name"] for number in map(str, range(1, 9))} == {
        "1": "VDD",
        "2": "VDDIO",
        "3": "GND",
        "4": "SDI_SDA",
        "5": "SCK_SCL",
        "6": "SDO_ADDR",
        "7": "CSB",
        "8": "GND2",
    }
    assert env_pins["4"]["net"] == "I2C_SDA"
    assert env_pins["5"]["net"] == "I2C_SCL"
    assert env_pins["8"]["net"] == "GND"
    assert env_pins["8"]["role"] == "ground"

    debug_pins = {pin["number"]: pin for pin in components["J2"]["pins"]}
    assert debug_pins["6"]["name"] == "SWO"
    assert debug_pins["6"]["net"] is None
    assert debug_pins["6"]["role"] == "no_connect"
    assert debug_pins["7"]["name"] == "KEY"
    assert debug_pins["7"]["net"] is None
    assert debug_pins["7"]["role"] == "no_connect"
    assert debug_pins["8"]["name"] == "NC"
    assert debug_pins["8"]["net"] is None
    assert debug_pins["8"]["role"] == "no_connect"
    assert debug_pins["10"]["name"] == "RESET"
    assert debug_pins["10"]["net"] == "MCU_NRST"

    vddcore_cap = components["C6"]
    assert vddcore_cap["component_id"] == "grm188_1uf"
    assert {pin["net"] for pin in vddcore_cap["pins"]} == {"VDDCORE", "GND"}

    positions = {item["ref"]: item["pcb_position_mm"] for item in graph["components"]}
    assert positions["J1"] == [12.0, 3.0]
    assert positions["D1"] == [18.0, 3.0]
    assert positions["U3"] == [36.0, 12.0]
    assert positions["U4"] == [36.0, 21.0]
    assert positions["J2"] == [47.0, 21.0]

    routing = json.loads((path / "electronics" / "generated" / "kicad" / "routing.json").read_text(encoding="utf-8"))
    assert routing["status"] == "generated"
    assert routing["failures"] == []
    assert internal_drc(path, service.read_spec(project), graph).status.value == "pass"


def test_generic_i2c_pullup_on_wrong_rail_fails_interface_integrity(service):
    project = "lora_sensor_node_i2c_wrong_rail_check"
    service.create_project(project, template="lora_sensor_node")
    service.generate_electronics_only(project)
    path = service.workspace.require_project(project)
    graph = json.loads((path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    bad_graph = deepcopy(graph)
    for component in bad_graph["components"]:
        if component.get("category") != "resistor_4k7":
            continue
        if not any(pin.get("net") in {"I2C_SCL", "I2C_SDA"} for pin in component.get("pins", [])):
            continue
        supply_pin = next(pin for pin in component["pins"] if pin.get("net") == "V3V3")
        supply_pin["net"] = "VBAT"

    report = service.validator.check_interface_integrity(bad_graph)

    assert report.status.value == "fail"
    failures = [failure for failure in report.failures if failure.code == "i2c_pullup_voltage_mismatch"]
    assert {failure.details["net_name"] for failure in failures} == {"I2C_SCL", "I2C_SDA"}
    for failure in failures:
        assert failure.details["pullup_rails"] == ["VBAT"]
        assert failure.details["endpoint_supply_nets"] == ["V3V3"]


@pytest.mark.parametrize(
    "template",
    [
        "ble_sensor_node",
        "usb_hid_controller",
        "lora_sensor_node",
        "bldc_esc",
        "esp32_wifi_gateway",
        "stm32g0_power_monitor",
        "rp2040_usb_device",
        "samd21_sensor_hub",
        "nrf52840_dongle",
    ],
)
def test_ap2112_regulators_match_curated_sot23_5_pin_contract(service, template):
    project = f"{template}_ap2112_pin_contract_check"
    service.create_project(project, template=template)
    service.generate_electronics_only(project)
    path = service.workspace.require_project(project)
    graph = json.loads((path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    regulators = [
        component
        for component in graph["components"]
        if component.get("mpn") == "AP2112K-3.3TRG1"
    ]

    assert regulators
    metadata_report = service.validator.check_component_metadata(graph["components"])
    for regulator in regulators:
        pins = {pin["number"]: pin for pin in regulator["pins"]}
        input_net = pins["3"]["net"]
        assert {number: pins[number]["name"] for number in ("1", "2", "3", "4", "5")} == {
            "1": "EN",
            "2": "GND",
            "3": "VIN",
            "4": "NC",
            "5": "VOUT",
        }
        assert pins["1"]["net"] == input_net
        assert pins["1"]["role"] == "input"
        assert pins["2"]["net"] == "GND"
        assert pins["2"]["role"] == "ground"
        assert pins["3"]["role"] == "power_in"
        assert pins["4"]["net"] is None
        assert pins["4"]["role"] == "no_connect"
        assert pins["5"]["net"] == "V3V3"
        assert pins["5"]["role"] == "power_out"
        regulator_failures = [
            failure
            for failure in metadata_report.failures
            if failure.details.get("ref") == regulator["ref"] or failure.path == regulator["ref"]
        ]
        assert regulator_failures == []


@pytest.mark.parametrize("template", ["esp32_wifi_gateway", "stm32g0_power_monitor", "nrf52840_dongle"])
def test_usb_vbus_loads_have_input_bulk_capacitor(service, template):
    project = f"{template}_usb_vbus_bulk_check"
    service.create_project(project, template=template)
    service.generate_electronics_only(project)
    path = service.workspace.require_project(project)
    spec = service.read_spec(project)
    graph = json.loads((path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))

    report = service.validator.check_power_integrity_estimate(graph, spec)

    assert report.status.value == "pass"
    assert "rail_decoupling_missing" not in {failure.code for failure in report.failures}
    assert report.metrics["coverage"]["USB_VBUS"]["bulk"]
    bulk_caps = [
        component
        for component in graph["components"]
        if component.get("category") == "bulk_cap"
        and {pin.get("net") for pin in component.get("pins", [])} == {"USB_VBUS", "GND"}
    ]
    assert bulk_caps


def test_bldc_esc_includes_reverse_polarity_and_can_termination_support(service):
    project = "bldc_esc_support_check"
    service.create_project(project, template="bldc_esc")
    result = service.generate_electronics_only(project)
    path = service.workspace.require_project(project)
    spec = service.read_spec(project)
    graph = json.loads((path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))

    assert result["resolution_report"]["status"] == "pass"
    support_report = service._support_circuit_completeness_report(spec, graph)
    interface_report = service.validator.check_interface_integrity(graph)
    power_tree_report = service.validator.check_power_tree(graph, spec)
    power_integrity_report = service.validator.check_power_integrity_estimate(graph, spec)

    assert support_report.status.value == "pass"
    assert interface_report.status.value == "pass"
    assert power_tree_report.status.value == "pass"
    assert "missing_required_protection_role" not in {failure.code for failure in support_report.failures}
    assert "can_termination_missing" not in {failure.code for failure in interface_report.failures}
    assert [
        failure.details["rail"]
        for failure in power_integrity_report.failures
        if failure.code == "rail_bulk_cap_missing"
    ] == ["V3V3"]

    components = {component["ref"]: component for component in graph["components"]}
    assert {pin["net"] for pin in components["Q4"]["pins"] if pin.get("net")} == {"VBAT_RAW", "VBAT", "GND"}
    assert {pin["net"] for pin in components["R3"]["pins"]} == {"CANH", "CANL"}
    roles = {
        (item["ref"], item["role"], item["component_id"], item["resolution"])
        for item in graph["component_resolution"]
    }
    assert ("Q4", "reverse_polarity", "lm74700qdbvrq1", "curated") in roles
    assert ("R3", "resistor_120r", "rc0603_120r", "curated") in roles


def test_rp2040_firmware_profile_and_stack_modules_are_graph_grounded(service):
    project = "rp2040_usb_device_firmware_check"
    service.create_project(project, template="rp2040_usb_device")
    service.generate_all(project)
    path = service.workspace.require_project(project)
    spec = service.read_spec(project)
    graph = json.loads((path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    pinmap = json.loads((path / "firmware" / "generated" / "pinmap.json").read_text(encoding="utf-8"))

    board_dir = path / "firmware" / "zephyr" / "boards" / "arm" / "rp2040_usb_device"
    dts = (board_dir / "rp2040_usb_device.dts").read_text(encoding="utf-8")
    defconfig = (board_dir / "rp2040_usb_device_defconfig").read_text(encoding="utf-8")
    prj_conf = (path / "firmware" / "zephyr" / "app" / "prj.conf").read_text(encoding="utf-8")
    pin_signals = {item["signal"] for item in pinmap}

    assert "rp2040.dtsi" in dts
    assert "zephyr,console = &uart0" in dts
    assert "stm32h743" not in dts.lower()
    assert "CONFIG_SOC_RP2040=y" in defconfig
    assert "CONFIG_SPI=y" in prj_conf
    assert "CONFIG_USB_DEVICE_STACK=y" in prj_conf
    assert {"USB_DP", "USB_DM", "QSPI_CLK", "QSPI_D2", "QSPI_D3"} <= pin_signals
    assert (path / "firmware" / "modules" / "usb_hid_stack.c").is_file()

    modules_report = service.validator.check_firmware_modules(
        spec["firmware"]["modules"],
        pinmap,
        spec=spec,
        graph=graph,
    )
    assert modules_report.status.value == "pass"
    assert modules_report.metrics["module_count"] == 4


def test_kicad_schematic_preserves_duplicate_power_pins(tmp_path):
    graph = {
        "components": [{
            "ref": "J1",
            "value": "DUPLICATE_POWER_PINS",
            "footprint": "Connector_Generic:Conn_01x04",
            "mpn": "TEST",
            "manufacturer": "test",
            "supplier_sku": "TEST",
            "pins": [
                {"number": "1", "name": "GND1", "net": "GND"},
                {"number": "2", "name": "GND2", "net": "GND"},
                {"number": "3", "name": "VDD1", "net": "V3V3"},
                {"number": "4", "name": "VDD2", "net": "V3V3"},
            ],
        }],
        "nets": [{"name": "GND"}, {"name": "V3V3"}],
    }
    output = tmp_path / "duplicate_power_pins.kicad_sch"

    generate_kicad_schematic("duplicate_power_pins", graph, output)

    text = output.read_text(encoding="utf-8")
    assert "Connector_Generic:Conn_01x04" in text
    assert text.count('"GND"') >= 2
    assert text.count('"V3V3"') >= 2


def test_freerouting_log_parser_distinguishes_complete_and_incomplete():
    assert FreeroutingBackend._final_unrouted("final score: 988.64 (1 unrouted)\n") == 1
    assert FreeroutingBackend._final_unrouted("final score: 992.25\nSaving board\n") == 0


def test_freerouting_timeout_is_bounded_and_reported(tmp_path, monkeypatch):
    project = tmp_path / "timeout_board"
    target = project / "electronics" / "generated" / "kicad"
    target.mkdir(parents=True)
    (target / "timeout_board.kicad_pcb").write_text("(kicad_pcb)\n", encoding="utf-8")
    tools = {
        "java": tmp_path / "java",
        "jar": tmp_path / "freerouting.jar",
        "kicad_python": tmp_path / "python",
    }
    for path in tools.values():
        path.write_text("", encoding="utf-8")
    backend = FreeroutingBackend(tmp_path)
    observed = {}

    monkeypatch.setattr(backend, "_tools", lambda: tools)
    monkeypatch.setattr(
        backend,
        "_pcbnew",
        lambda *args: subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr=""),
    )

    def fake_run(command, **kwargs):
        observed["timeout"] = kwargs["timeout"]
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], output="partial", stderr="still running")

    monkeypatch.setattr("hw_codesign.backends.freerouting.subprocess.run", fake_run)

    report = backend.route(project, timeout_seconds=3)

    assert observed["timeout"] == 3
    assert report.status.value == "fail"
    assert report.failures[0].code == "autoroute_timeout"
    assert report.failures[0].details["timeout_seconds"] == 3
    assert report.backend["timeout_seconds"] == 3


def test_iteration_records_reports_history_and_repair_plan(service, project):
    result = service.run_design_iteration(project, include_external=False)
    path = service.workspace.require_project(project)
    assert result["iteration_id"] == "0001"
    assert result["status"] == "blocked"
    assert "semantic_electrical" in result["failed_gates"]
    assert result["repair_plan"]["requires_user_decision"] is True
    assert (path / "history" / "iterations" / "0001" / "result.json").is_file()
    assert (path / "history" / "iterations" / "0001" / "electronics" / "intent" / "board.intent.md").is_file()
    assert (path / "history" / "iterations" / "0001" / "firmware" / "generated" / "pinmap.h").is_file()
    log = (path / "history" / "failure_log.jsonl").read_text(encoding="utf-8")
    assert "current_budget_exceeded" in log


def test_external_backends_report_real_availability_and_violations(service, project, monkeypatch):
    service.generate_all(project)
    monkeypatch.setattr("shutil.which", lambda _: None)
    checks = service.run_all_checks(project, include_external=True)
    external = [item for item in checks["reports"] if item["gate"] in {"native_erc", "native_drc", "native_zephyr_build"}]
    firmware = next(item for item in external if item["gate"] == "native_zephyr_build")
    assert firmware["status"] == "blocked"
    assert firmware["failures"][0]["code"] == "tool_unavailable"
    for item in (entry for entry in external if entry["gate"] in {"native_erc", "native_drc"} and entry["status"] == "pass"):
        assert item["metrics"]["violations"] == 0
        assert item["backend"]["available"] is True


def test_design_report_states_physical_gaps(service, project):
    service.generate_all(project)
    service.run_all_checks(project, include_external=False)
    output = Path(service.generate_design_report(project)["file"])
    text = output.read_text(encoding="utf-8")
    assert "EMI/EMC" in text
    assert "instrumented hardware testing" in text


def test_safety_spec_changes_require_explicit_approval(service, project):
    with pytest.raises(UnsafeChangeError):
        service.update_spec(project, "safety", {"emergency_stop": {"required": False}})
    result = service.update_spec(project, "safety", {"emergency_stop": {"required": True}}, user_approved=True)
    assert result["user_approved"] is True


def test_design_until_release_blocks_reference_pipeline(service, project):
    # Critical assumptions require explicit user approval; resolve them before the loop.
    spec = service.read_spec(project)
    for name, assumption in spec.get("assumptions", {}).items():
        if assumption.get("requires_user_review"):
            service.resolve_assumption(project, name, assumption.get("value") or "approved", approved=True)
    result = service.design_until_release(project, max_iterations=4, include_external=False)
    assert result["status"] == "blocked"
    assert not (service.workspace.require_project(project) / "exports" / "releases" / "r1").exists()


def test_natural_language_requirements_update_structured_spec(service, project):
    result = service.update_requirements(project, "16 channel 24V battery, peak 6A, STM32H7, IMU, emergency stop, Zephyr, 6-layer, external driver, forced cooling")
    spec = service.read_spec(project)
    assert result["status"] == "generated"
    assert spec["actuation"]["motor_channels"] == 16
    assert spec["actuation"]["motor_channel_peak_current_a"] == 6.0
    assert spec["system"]["supply"]["battery"]["pack_voltage_nominal"] == 24.0
    assert spec["actuation"]["motor_type"] == "external_driver_modules"
    assert spec["mechanical"]["cooling"] == "forced_air"
    assert spec["manufacturing"]["pcb"]["layers"] == 6
    assert spec["firmware"]["framework"] == "zephyr"
    assert spec.get("requirements", {}).get("active_unresolved", []) == []
    ir = result["compiler_ir"]
    assert ir["version"] == "requirements_ir_v1"
    lowered = {item["spec_path"]: item for item in ir["lowered_fields"]}
    assert lowered["actuation.motor_channels"]["value"] == 16
    assert lowered["actuation.motor_channels"]["field_type"] == "integer"
    assert lowered["actuation.motor_channels"]["source_range"] == {"start": 0, "end": 10}
    assert "power_integrity_estimate" in lowered["actuation.motor_channels"]["affected_gates"]
    assert "layout_thermal_integrity" in lowered["actuation.motor_channels"]["affected_gates"]
    assert lowered["actuation.motor_type"]["value"] == "external_driver_modules"
    assert lowered["mechanical.cooling"]["value"] == "forced_air"
    resolved = {item["assumption_key"]: item for item in ir["resolved_assumptions"]}
    assert {"motor_type", "cooling"} <= set(resolved)
    lowered_tokens = [item for item in ir["tokens"] if item["kind"] == "lowered_field"]
    assert any(item["spec_path"] == "actuation.motor_channels" and item["source_span"] == "16 channel" for item in lowered_tokens)
    assert ir["required_human_approvals"] == []


def test_requirements_lower_can_telemetry_into_firmware_module(service, project):
    result = service.update_requirements(
        project,
        "16 channel 24V battery external driver forced cooling CAN telemetry id 0x321 dlc 8 every 10 ms",
    )

    assert result["status"] == "generated"
    assert result["has_unresolved_constraints"] is False
    spec = service.read_spec(project)
    modules = {module["id"]: module for module in spec["firmware"]["modules"]}
    module = modules["can_telemetry_periodic"]
    assert module == {
        "id": "can_telemetry_periodic",
        "behavior": "periodic_transmit",
        "transport": "can",
        "interval_ms": 10,
        "frame": {"id": "0x321", "dlc": 8, "content": "telemetry"},
        "source": "requirements_ir_v1",
    }
    lowered = {item["spec_path"]: item for item in result["compiler_ir"]["lowered_fields"]}
    field = lowered["firmware.modules.can_telemetry_periodic"]
    assert field["field_type"] == "firmware_module"
    assert field["value"]["behavior"] == "periodic_transmit"
    assert "firmware_modules" in field["affected_gates"]

    service.generate_all(project)
    project_path = service.workspace.require_project(project)
    graph = json.loads((project_path / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    pinmap = json.loads((project_path / "firmware" / "generated" / "pinmap.json").read_text(encoding="utf-8"))
    report = service.validator.check_firmware_modules(
        service.read_spec(project)["firmware"]["modules"],
        pinmap,
        spec=service.read_spec(project),
        graph=graph,
        module_dir=project_path / "firmware" / "modules",
    )
    assert report.status == "pass"


def test_requirements_do_not_lower_can_fd_telemetry_as_classical_can(service, project):
    result = service.update_requirements(
        project,
        "external driver forced cooling CAN-FD telemetry id 0x321 dlc 64 every 10 ms",
    )

    assert result["has_unresolved_constraints"] is True
    spec = service.read_spec(project)
    modules = {module["id"] for module in spec["firmware"].get("modules", [])}
    assert "can_telemetry_periodic" not in modules
    unresolved = spec["requirements"]["active_unresolved"]
    bus_protocol = next(item for item in unresolved if item["category"] == "bus_protocol")
    assert bus_protocol["field_type"] == "unsupported_constraint"
    assert "firmware_interface_contract" in bus_protocol["affected_gates"]


def test_requirements_relower_existing_firmware_module_into_active_ir(service, project):
    service.update_requirements(
        project,
        "external driver forced cooling CAN telemetry id 0x100 dlc 8 every 100 ms",
    )
    result = service.update_requirements(
        project,
        "external driver forced cooling CAN telemetry id 0x200 dlc 4 every 25 ms",
    )

    spec = service.read_spec(project)
    modules = [module for module in spec["firmware"]["modules"] if module["id"] == "can_telemetry_periodic"]
    assert len(modules) == 1
    assert modules[0]["interval_ms"] == 25
    assert modules[0]["frame"] == {"id": "0x200", "dlc": 4, "content": "telemetry"}
    lowered = {item["spec_path"]: item for item in result["compiler_ir"]["lowered_fields"]}
    assert lowered["firmware.modules.can_telemetry_periodic"]["value"]["interval_ms"] == 25


def test_requirements_ir_affected_gates_are_concrete_report_names(service, project):
    result = service.update_requirements(
        project,
        "16 channel 24V battery, IP67, CAN-FD, ASIL-B, 8A continuous, JLCPCB assembly, impedance-controlled",
    )
    service.generate_all(project)
    checks = service.run_all_checks(project, include_external=False)
    emitted_gates = {
        gate
        for item in [*result["compiler_ir"]["lowered_fields"], *result["compiler_ir"]["tokens"]]
        for gate in item.get("affected_gates", [])
    }
    report_gates = {report["gate"] for report in checks["reports"]}
    release_only_gates = {"artifact_integrity"}

    assert emitted_gates <= report_gates | release_only_gates
    assert not (emitted_gates & {"electrical_semantics", "power_budget", "thermal_integrity", "manufacturing_export", "safety_requirements"})


def test_requirements_update_result_matches_public_schema(service, project):
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    from hw_codesign.contracts import SHARED_SCHEMAS, TOOL_REGISTRY
    from hw_codesign.mcp_server import _enrich

    result = service.update_requirements(
        project,
        "16 channel 24V battery, IP67, CAN-FD, ASIL-B, 8A continuous, JLCPCB assembly, impedance-controlled",
    )
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema))
        for schema in SHARED_SCHEMAS.values()
    )

    Draft202012Validator(
        SHARED_SCHEMAS["requirements_update_result"],
        registry=registry,
    ).validate(result)
    Draft202012Validator(
        TOOL_REGISTRY["hw_update_requirements"].to_dict()["output_schema"],
        registry=registry,
    ).validate(_enrich(result))
    assert TOOL_REGISTRY["hw_update_requirements"].output_schema["$ref"].endswith("requirements_update_result")


def test_unsupported_constraints_persist_to_spec_and_block_validation(service, project):
    """update_requirements with high-risk constraints that cannot be lowered must:
    - return status='generated' with has_unresolved_constraints=true
    - persist constraints to spec so validate_spec fails with unlowered_constraint_in_spec
    - thereby block release promotion without requiring agent to inspect the return value."""
    result = service.update_requirements(
        project,
        "16 channel 24V battery, IP67, CAN-FD, ASIL-B, 8A continuous, JLCPCB assembly, impedance-controlled"
    )
    assert result["status"] == "generated"
    assert result["has_unresolved_constraints"] is True
    assert result["unsupported_constraints"]
    assert result["required_human_approvals"]
    assert "firmware_interface_contract" in result["affected_gates"]
    spec = service.read_spec(project)
    assert spec.get("requirements", {}).get("active_unresolved"), "Constraints must be persisted to spec/requirements.yaml"
    unresolved = spec["requirements"]["active_unresolved"]
    assert all(item["required_human_approvals"] for item in unresolved)
    assert all(item["affected_gates"] for item in unresolved)
    unsupported_unresolved = [item for item in unresolved if item["field_type"] == "unsupported_constraint"]
    assert all(item["source_range"]["end"] > item["source_range"]["start"] for item in unsupported_unresolved)
    assert unsupported_unresolved
    assert spec["requirements"]["compiler_ir"]["unsupported_constraints"]
    unresolved_tokens = [item for item in spec["requirements"]["compiler_ir"]["tokens"] if item["kind"] == "unsupported_constraint"]
    assert {item["category"] for item in unresolved_tokens} >= {"ip_protection", "bus_protocol", "functional_safety", "current_rating", "manufacturing_service", "pcb_stackup"}
    assert all(item["required_human_approvals"] for item in unresolved_tokens)
    checks = service.run_all_checks(project, include_external=False)
    assert checks["status"] != "pass"
    codes = {f["code"] for report in checks["reports"] for f in report["failures"]}
    assert "unlowered_requirement" in codes


def test_requirements_compiler_preserves_physical_qualification_constraints(service, project):
    result = service.update_requirements(
        project,
        "USB-C PD sink with EMI/EMC pre-compliance, MIL-STD-810 vibration, thermal limit 70C",
    )

    assert result["status"] == "generated"
    assert result["has_unresolved_constraints"] is True
    assert "physical_qualification" in result["affected_gates"]
    spec = service.read_spec(project)
    unsupported = spec["requirements"]["compiler_ir"]["unsupported_constraints"]
    by_category = {item["category"]: item for item in unsupported}

    assert {
        "usb_power_delivery",
        "emi_emc_compliance",
        "vibration_environment",
        "thermal_limit",
    } <= set(by_category)
    assert by_category["usb_power_delivery"]["source_span"] == "USB-C PD"
    assert "firmware_interface_contract" in by_category["usb_power_delivery"]["affected_gates"]
    assert "layout_signal_integrity" in by_category["emi_emc_compliance"]["affected_gates"]
    assert "mechanical_connector_retention" in by_category["vibration_environment"]["affected_gates"]
    assert "layout_thermal_integrity" in by_category["thermal_limit"]["affected_gates"]
    assert all(item["release_blocking"] for item in unsupported)
    assert all(item["required_human_approvals"] for item in unsupported)

    checks = service.run_all_checks(project, include_external=False)
    lowering = next(report for report in checks["reports"] if report["gate"] == "requirements_lowering")
    assert lowering["status"] != "pass"
    assert "unlowered_requirement" in {failure["code"] for failure in lowering["failures"]}


def test_update_requirements_replaces_active_unresolved_constraints(service, project):
    """update_requirements uses replace semantics for active_unresolved: a later call with no
    unsupported constraints clears active_unresolved even if the prior call had blockers.
    raw_inputs is append-only (audit log), so both calls are preserved there."""
    r1 = service.update_requirements(project, "IP67 impedance-controlled")
    assert r1["mode"] == "replace_active_requirements"
    assert service.read_spec(project).get("requirements", {}).get("active_unresolved")
    service.update_requirements(project, "16 channel 24V battery, Zephyr, external driver, forced cooling")
    spec = service.read_spec(project)
    assert spec.get("requirements", {}).get("active_unresolved", []) == []
    assert len(spec["requirements"]["raw_inputs"]) == 2, "raw_inputs must accumulate across calls"
    checks = service.run_all_checks(project, include_external=False)
    codes = {f["code"] for report in checks["reports"] for f in report["failures"]}
    assert "unlowered_requirement" not in codes


# ---------------------------------------------------------------------------
# Candidate lifecycle
# ---------------------------------------------------------------------------

def test_list_candidates_empty_before_iteration(service, project):
    result = service.list_candidates(project)
    assert result["status"] == "pass"
    assert result["candidates"] == []
    assert result["count"] == 0


def test_list_candidates_finds_candidates_after_iteration(service, project):
    service.run_design_iteration(project, include_external=False)
    result = service.list_candidates(project)
    assert result["count"] >= 1
    c = result["candidates"][0]
    assert "candidate_id" in c
    assert "backend" in c
    assert "gate_summary" in c
    # Scratch dirs (reference-fabrication, native-check) must NOT appear
    assert all(c["candidate_id"].isdigit() for c in result["candidates"])


def test_get_candidate_missing_returns_blocked(service, project):
    result = service.get_candidate(project, "9999")
    assert result["status"] == "blocked"
    assert result["code"] == "candidate_not_found"


def test_get_candidate_returns_frozen_data(service, project):
    service.run_design_iteration(project, include_external=False)
    candidates = service.list_candidates(project)
    cid = candidates["candidates"][0]["candidate_id"]
    result = service.get_candidate(project, cid)
    assert result["status"] == "pass"
    assert result["candidate_id"] == cid
    assert "checks" in result["candidate"]


def test_review_candidate_uses_frozen_checks_not_live_reports(service, project):
    service.run_design_iteration(project, include_external=False)
    cid = service.list_candidates(project)["candidates"][0]["candidate_id"]
    review = service.review_candidate(project, cid)
    assert review["status"] in {"pass", "fail", "blocked", "unknown"}
    assert "gate_summary" in review
    assert "blocking_gates" in review
    assert "recommendation" in review
    # Run fresh checks (changes live reports dir) — frozen review must be stable
    service.run_all_checks(project, include_external=False)
    review2 = service.review_candidate(project, cid)
    assert review2["gate_summary"] == review["gate_summary"]


def test_compare_candidates_detects_no_change(service, project):
    service.run_design_iteration(project, include_external=False)
    service.run_design_iteration(project, include_external=False)
    candidates = service.list_candidates(project)["candidates"]
    assert len(candidates) >= 2
    a, b = candidates[0]["candidate_id"], candidates[1]["candidate_id"]
    result = service.compare_candidates(project, a, b)
    assert result["status"] == "pass"
    assert "readiness_delta" in result
    assert "artifact_delta" in result
    assert "risk_delta" in result
    assert "recommendation" in result
    delta = result["readiness_delta"]
    assert isinstance(delta["blocking_gates_removed"], list)
    assert isinstance(delta["blocking_gates_added"], list)
    assert isinstance(delta["pass_count_delta"], int)


def test_compare_candidates_missing_returns_blocked(service, project):
    service.run_design_iteration(project, include_external=False)
    cid = service.list_candidates(project)["candidates"][0]["candidate_id"]
    result = service.compare_candidates(project, cid, "9999")
    assert result["status"] == "blocked"
    assert result["code"] == "candidate_not_found"


# ---------------------------------------------------------------------------
# Fabrication review preparation
# ---------------------------------------------------------------------------

def test_prepare_fabrication_review_before_any_candidate(service, project):
    result = service.prepare_fabrication_review(project)
    assert result["status"] == "pass"
    assert result["label"] == "do_not_fabricate"
    assert "artifact_presence" in result
    assert "fab_review_checklist" in result
    assert len(result["fab_review_checklist"]) >= 5


def test_prepare_fabrication_review_after_iteration(service, project):
    service.run_design_iteration(project, include_external=False)
    cid = service.list_candidates(project)["candidates"][0]["candidate_id"]
    result = service.prepare_fabrication_review(project, candidate_id=cid)
    assert result["status"] == "pass"
    assert result["label"] in {"do_not_fabricate", "review_only", "release_candidate"}
    assert result["candidate_id"] == cid
    assert "erc_status" in result
    assert "drc_status" in result
    assert isinstance(result["unresolved_assumptions"], list)
    assert isinstance(result["physical_qualification_gaps"], list)


# ---------------------------------------------------------------------------
# Environment diagnosis
# ---------------------------------------------------------------------------

def test_get_capabilities_partitions_release_tiers(service, project):
    result = service.get_capabilities()

    assert result["release_tiers"] == {
        "reference": "candidate",
        "python_netlist": "netlist",
        "atopile": "hdl_source",
        "tscircuit": "fabrication",
        "kicad": "fabrication",
    }
    assert set(result["release_eligible_backends"]) == {"tscircuit", "kicad", "python_netlist", "atopile"}
    assert result["canonical_fabrication_backends"] == ["tscircuit", "kicad"]
    assert result["canonical_fabrication_flow"] == {
        "tscircuit": "tscircuit -> Circuit JSON -> KiCad manufacturing bridge",
        "kicad": "native KiCad schematic/PCB -> KiCad CLI manufacturing export",
    }
    assert set(result["fabrication_release_backends"]) == {"tscircuit", "kicad"}
    assert result["netlist_release_backends"] == ["python_netlist"]
    assert result["source_release_backends"] == ["atopile"]
    assert result["candidate_only_backends"] == ["reference"]
    assert result["backends"]["python_netlist"]["fabrication_release_eligible"] is False
    assert result["backends"]["python_netlist"]["release_tier"] == "netlist"
    assert result["backends"]["atopile"]["fabrication_release_eligible"] is False
    assert result["backends"]["tscircuit"]["fabrication_release_eligible"] is True
    assert result["backends"]["kicad"]["release_tier"] == "fabrication"


def test_diagnose_environment_candidate_target_always_ready(service, project):
    result = service.diagnose_environment(target="candidate")
    assert result["status"] == "pass"
    assert result["ready"] is True
    assert result["missing_tools"] == []
    assert result["blocked_gates"] == []


def test_diagnose_environment_returns_structured_output(service, project):
    result = service.diagnose_environment(target="fabrication_release")
    assert result["target"] == "fabrication_release"
    assert isinstance(result["ready"], bool)
    assert isinstance(result["missing_tools"], list)
    assert isinstance(result["blocked_gates"], list)
    assert isinstance(result["install_hints"], dict)
    assert isinstance(result["tool_availability"], dict)


def test_diagnose_environment_backend_adds_tool_requirement(service, project, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd: None if cmd == "node" else "/usr/bin/java")
    result = service.diagnose_environment(target="candidate", backend="tscircuit")
    assert "node" in result["missing_tools"]


def test_diagnose_environment_unknown_target_is_blocked(service, project):
    result = service.diagnose_environment(target="nonexistent_target")
    assert result["status"] == "blocked"
    assert result["code"] == "unknown_target"
