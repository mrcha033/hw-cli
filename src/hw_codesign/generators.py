from __future__ import annotations

import csv
import io
import json
from hashlib import sha256
from pathlib import Path
from typing import Any

from .io import atomic_write_text, write_json
from .provenance import artifact_provenance
from .reference_backend import build_graph, generate_kicad
from .resolver import ComponentResolver
from .board_layout import component_positions
from .placement import propose_placement
from .mechanical_contract import build_mechanical_contract


def generate_electronics(project: Path, spec: dict[str, Any], parts_root: Path, backend: str = "reference") -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    intent = project / "electronics" / "intent"
    source = project / "electronics" / "source"
    source.mkdir(parents=True, exist_ok=True)
    for stale in source.glob("*.ato"):
        stale.unlink()
    if backend != "tscircuit":
        import shutil
        shutil.rmtree(source / "tscircuit", ignore_errors=True)
        (project / "electronics" / "generated" / "tscircuit_netlist.json").unlink(missing_ok=True)
    header = "---\nartifact_type: design_intent\ncompiled: false\nrelease_eligible: false\nsource_of_truth: false\nbackend: reference\n---\n\n"
    files = _electronics_intent_files(spec, header)
    for stale_intent in intent.glob("*.intent.md"):
        if stale_intent.name not in files:
            stale_intent.unlink()
    for name, content in files.items():
        atomic_write_text(intent / name, content)
    graph = build_graph(spec)
    resolver = ComponentResolver(parts_root)
    role_set = spec.get("electronics", {}).get("role_set", "robotics_controller")
    resolved, resolution_report = resolver.resolve(spec, role_set, graph["components"])
    by_ref = {item.ref: item for item in resolved}
    for component in graph["components"]:
        match = by_ref.get(component["ref"])
        if not match:
            component["resolution"] = "unresolved"
            continue
        data = match.data
        component.update({"mpn": data["mpn"], "manufacturer": data["manufacturer"], "package": data["package"], "symbol": data["symbol"], "footprint": data["footprint"]["library_id"], "footprint_metadata": data["footprint"], "lifecycle": data["lifecycle"], "sourcing": data["sourcing"], "supplier_offer": data.get("supplier_offer"), "datasheet_evidence": data.get("datasheet_evidence", []), "constraints": data["constraints"], "review_status": data["review_status"], "resolution": match.resolution, "component_id": match.component_id, "resolution_provenance": match.provenance})
    proposal = propose_placement(spec, graph)
    for component in graph["components"]:
        placement = proposal.placements[component["ref"]]
        component["pcb_position_mm"] = [placement.x_mm, placement.y_mm]
    graph["placement"] = proposal.to_dict()
    graph["component_resolution"] = ComponentResolver.serialize(resolved)
    graph["component_provenance"] = {item.ref: item.provenance for item in resolved}
    graph["component_resolution_report"] = resolution_report.to_dict()
    graph["supplier_availability_report"] = resolver.supplier_availability_report.to_dict()
    graph["datasheet_evidence_report"] = resolver.datasheet_evidence_report.to_dict()
    graph["provenance"] = artifact_provenance(spec, parts_root, backend, release_eligible=False)
    graph_path = project / "electronics" / "generated" / "electrical_graph.json"
    write_json(graph_path, graph)
    semantic_files = _write_semantic_schematic(project, spec, graph)
    return [str(intent / name) for name in files] + [str(graph_path), *semantic_files, *generate_kicad(project, spec, graph)], [item for item in graph["component_resolution"]], resolution_report.to_dict()


def _write_semantic_schematic(project: Path, spec: dict[str, Any], graph: dict[str, Any]) -> list[str]:
    target = project / "electronics" / "generated" / "semantic"
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "semantic_schematic.json"
    code_path = target / "semantic_schematic.py"
    placements = graph.get("placement", {}).get("placements", {})
    constraints = graph.get("placement", {}).get("constraints", [])

    component_by_ref = {item["ref"]: item for item in graph.get("components", [])}

    def endpoint(pin_ref: str) -> dict[str, Any]:
        ref, _, pin_number = pin_ref.partition(".")
        component = component_by_ref.get(ref, {})
        pin_data = next((pin for pin in component.get("pins", []) if str(pin.get("number")) == pin_number), {})
        return {
            "component_ref": ref,
            "component_role": component.get("category"),
            "pin_number": pin_number,
            "pin_name": pin_data.get("name"),
            "pin_role": pin_data.get("role"),
            "mcu_pin": pin_data.get("mcu_pin"),
        }

    semantic = {
        "artifact_type": "semantic_schematic",
        "authoring_model": "semantic-first-pin-name-wiring",
        "project": project.name,
        "revision": spec.get("project", {}).get("revision"),
        "purpose": "LLM-suited schematic representation derived from typed graph; native EDA files are generated outputs.",
        "components": [
            {
                "ref": item.get("ref"),
                "role": item.get("category"),
                "value": item.get("value"),
                "component_id": item.get("component_id"),
                "mpn": item.get("mpn"),
                "manufacturer": item.get("manufacturer"),
                "package": item.get("package"),
                "footprint": item.get("footprint"),
                "pins": [
                    {
                        "number": pin.get("number"),
                        "name": pin.get("name"),
                        "net": pin.get("net"),
                        "role": pin.get("role"),
                        "voltage_domain": pin.get("voltage_domain"),
                        "mcu_pin": pin.get("mcu_pin"),
                    }
                    for pin in item.get("pins", [])
                ],
            }
            for item in sorted(graph.get("components", []), key=lambda item: item.get("ref", ""))
        ],
        "nets": [
            {
                "name": net.get("name"),
                "signal_class": net.get("signal_class"),
                "voltage_domain": net.get("voltage_domain"),
                "required_track_width_mm": net.get("required_track_width_mm"),
                "pin_name_connections": [endpoint(pin_ref) for pin_ref in sorted(net.get("connected_pins", []))],
            }
            for net in sorted(graph.get("nets", []), key=lambda item: item.get("name", ""))
        ],
        "relative_placement": {
            "board_width_mm": graph.get("placement", {}).get("board_width_mm"),
            "board_height_mm": graph.get("placement", {}).get("board_height_mm"),
            "placements": placements,
            "constraints": constraints,
        },
        "source_graph": str(project / "electronics" / "generated" / "electrical_graph.json"),
    }
    write_json(json_path, semantic)
    atomic_write_text(code_path, _semantic_schematic_code(semantic))
    return [str(json_path), str(code_path)]


def _semantic_schematic_code(semantic: dict[str, Any]) -> str:
    lines = [
        '"""Generated executable semantic schematic for agent review.',
        "",
        "This file is intentionally compact and pin-name based. It can be executed",
        "to reconstruct the normalized semantic_schematic model. Native EDA/CAD",
        "artifacts are generated from typed artifacts; edit the spec or graph-producing",
        "blocks when changing a generated design.",
        '"""',
        "",
        "from hw_codesign.semantic_schematic import SemanticBoard, pin",
        "",
        "board = SemanticBoard(",
        f"    project={semantic['project']!r},",
        f"    revision={semantic.get('revision')!r},",
        f"    purpose={semantic.get('purpose')!r},",
        f"    source_graph={semantic.get('source_graph')!r},",
        f"    board_width_mm={semantic.get('relative_placement', {}).get('board_width_mm')!r},",
        f"    board_height_mm={semantic.get('relative_placement', {}).get('board_height_mm')!r},",
        ")",
        "component = board.component",
        "net = board.net",
        "connect = board.connect",
        "place = board.place",
        "constraint = board.constraint",
        "",
    ]
    for component in semantic["components"]:
        lines.extend([
            "component(",
            f"    {component['ref']!r},",
            f"    role={component.get('role')!r},",
            f"    value={component.get('value')!r},",
            f"    component_id={component.get('component_id')!r},",
            f"    mpn={component.get('mpn')!r},",
            f"    manufacturer={component.get('manufacturer')!r},",
            f"    package={component.get('package')!r},",
            f"    footprint={component.get('footprint')!r},",
            "    pins=[",
        ])
        for item in component.get("pins", []):
            lines.append(
                "        pin("
                f"{item.get('number')!r}, {item.get('name')!r}, net={item.get('net')!r}, "
                f"role={item.get('role')!r}, voltage_domain={item.get('voltage_domain')!r}, "
                f"mcu_pin={item.get('mcu_pin')!r}),"
            )
        lines.extend(["    ],", ")", ""])
    lines.append("")
    for net in semantic["nets"]:
        lines.append(
            "net("
            f"{net.get('name')!r}, signal_class={net.get('signal_class')!r}, "
            f"voltage_domain={net.get('voltage_domain')!r}, "
            f"required_track_width_mm={net.get('required_track_width_mm')!r})"
        )
        for endpoint in net["pin_name_connections"]:
            lines.append(
                "connect("
                f"{endpoint['component_ref']!r}, pin={endpoint.get('pin_name')!r}, "
                f"number={endpoint.get('pin_number')!r}, net={net.get('name')!r}, "
                f"role={endpoint.get('pin_role')!r}, mcu_pin={endpoint.get('mcu_pin')!r})"
            )
        lines.append("")
    lines.append("")
    for ref, placement in sorted(semantic["relative_placement"].get("placements", {}).items()):
        lines.append(
            "place("
            f"{ref!r}, data={placement!r})"
        )
    lines.append("")
    for constraint in semantic["relative_placement"].get("constraints", []):
        lines.append(
            f"constraint(data={constraint!r})"
        )
    lines.append("")
    lines.append("semantic_schematic = board.to_dict()")
    return "\n".join(lines) + "\n"


def _electronics_intent_files(spec: dict[str, Any], header: str) -> dict[str, str]:
    channels = int(spec["actuation"]["motor_channels"])
    mcu_family = spec["compute"]["mcu"]["family"]
    role_set = spec.get("electronics", {}).get("role_set", "")
    supply_type = spec.get("system", {}).get("supply", {}).get("battery", {}).get("type", "battery")
    board_module_lines = [
        "# Generated high-level hardware intent. Edit spec, then regenerate.",
        f"module {mcu_family}Board:",
        f"  mcu = new {mcu_family}",
    ]
    if supply_type == "usb_pd":
        board_module_lines.append("  power_input = new UsbCPowerInput")
    else:
        board_module_lines.append("  power_input = new ProtectedPowerInput")
    if spec.get("sensing", {}).get("imu") == "required":
        board_module_lines.append("  imu = new IMU")
    if channels > 0:
        board_module_lines.append(f"  motor_channels = new MotorChannel[{channels}]")
    if spec.get("safety", {}).get("emergency_stop", {}).get("required"):
        board_module_lines.append("  emergency_stop = new FailSafeEmergencyStop")

    files = {
        "board.intent.md": header + "\n".join(board_module_lines) + "\n",
        "sensor_bus.intent.md": header + "# IMU and external sensor buses\n",
        "connectors.intent.md": header + "# Exposed power and debug connectors\n",
    }
    if supply_type == "usb_pd":
        files["power_tree.intent.md"] = header + "# USB-C power input and rail intent\nUSB_VBUS -> usb_esd -> regulator_3v3\n"
    else:
        files["power_tree.intent.md"] = header + "# VBAT protection and rail intent\nVBAT -> fuse_or_efuse -> reverse_polarity -> tvs -> controller power\nVBAT -> buck_5v -> regulator_3v3\n"
    if channels > 0:
        files["motor_channels.intent.md"] = header + f"# Channel intent\nchannels = {channels}\npeak_current_per_channel_a = {spec['actuation']['motor_channel_peak_current_a']}\n"
    elif role_set == "ble_sensor_node":
        files["ble_node.intent.md"] = header + "# BLE sensor node intent\nble_peripheral = required\ni2c_env_sensor = required\nfuel_gauge = required\nbattery_charger = required\n"
    elif role_set == "sensor_data_logger":
        files["data_logger.intent.md"] = header + "# Sensor data logger intent\nusb_console = required\ni2c_imu = required\nlocal_storage = not_modelled\n"
    return files


def generate_mechanical(project: Path, spec: dict[str, Any], graph: dict[str, Any] | None = None) -> list[str]:
    source = project / "mechanical" / "source"
    source.mkdir(parents=True, exist_ok=True)
    graph = graph or {"components": []}
    contract = build_mechanical_contract(spec, graph)
    contract_path = source / "mechanical_contract.json"
    write_json(contract_path, contract)
    atomic_write_text(source / "assembly.py", '''"""Generated parametric mechanical assembly entrypoint."""
import argparse
import json
from pathlib import Path
from hw_codesign.backends.mechanical import OpenCascadeMechanicalBackend

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--board-step", type=Path)
    args = parser.parse_args()
    contract = json.loads(Path(__file__).with_name("mechanical_contract.json").read_text(encoding="utf-8"))
    report = OpenCascadeMechanicalBackend().generate_from_contract(contract, args.output, board_step=args.board_step)
    print(json.dumps(report.to_dict(), sort_keys=True))
    raise SystemExit(0 if report.status == "pass" else 1)
''')
    atomic_write_text(source / "enclosure_variants.py", "VARIANTS = " + repr(contract["variants"]) + "\nSELECTED_VARIANT = " + repr(contract["selected_variant"]) + "\n")
    atomic_write_text(
        source / "enclosure.py",
        "BOARD = " + repr(contract["board"]) + "\nENCLOSURE = " + repr(contract["enclosure"]) + "\nCLEARANCES = " + repr(contract["clearances"]) + "\n",
    )
    atomic_write_text(source / "fixtures.py", "FIXTURES = " + repr(contract["fixtures"]) + "\nMOUNTING_HOLES = " + repr(contract["mounting_holes"]) + "\n")
    atomic_write_text(source / "connector_cutouts.py", "CONNECTOR_CUTOUTS = " + repr(contract["connector_cutouts"]) + "\n")
    source_files = sorted([contract_path, *source.glob("*.py")])
    manifest = {
        "artifact_type": "mechanical_source",
        "backend": "opencascade",
        "selected_variant": contract["selected_variant"],
        "variants": [item["name"] for item in contract["variants"]],
        "source_spec_hash": artifact_provenance(spec, project / "parts", "opencascade")["source_spec_hash"],
        "sources": [{"path": path.name, "sha256": sha256(path.read_bytes()).hexdigest()} for path in source_files],
        "release_eligible": False,
    }
    manifest_path = source / "source_manifest.json"
    write_json(manifest_path, manifest)
    return [str(path) for path in [*source_files, manifest_path]]


def _pin_assignments(channels: int) -> list[dict[str, Any]]:
    assignments = [
        {"signal": "I2C_IMU_SCL", "mcu_pin": "PB8", "net_name": "I2C_IMU_SCL"},
        {"signal": "I2C_IMU_SDA", "mcu_pin": "PB9", "net_name": "I2C_IMU_SDA"},
        {"signal": "CAN_RX", "mcu_pin": "PD0", "net_name": "CAN_RX"},
        {"signal": "CAN_TX", "mcu_pin": "PD1", "net_name": "CAN_TX"},
        {"signal": "ESTOP_IN", "mcu_pin": "PC13", "net_name": "ESTOP_IN"},
    ]
    assignments.extend({"signal": f"MOTOR{index}_PWM", "mcu_pin": f"PWM{index}", "net_name": f"MOTOR{index}_PWM"} for index in range(1, channels + 1))
    return assignments


def firmware_profile(spec: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
    architecture = graph.get("design_basis", {}).get("architecture")
    if architecture == "nrf52840_ble_sensor":
        return {
            "project": "ble_sensor_node",
            "reference_project": "ble_sensor_node_bsp",
            "board_name": "ble_sensor_node",
            "board_arch": "arm",
            "dts_include": "#include <nordic/nrf52840.dtsi>",
            "model": "HW Co-design nRF52840 BLE Sensor Node",
            "compatible": "hw,ble-sensor-node",
            "defconfig": "CONFIG_SOC_NRF52840=y\nCONFIG_BOARD_BLE_SENSOR_NODE=y\n",
            "kconfig_board": 'config BOARD_BLE_SENSOR_NODE\n  bool "nRF52840 BLE Sensor Node"\n',
            "kconfig_default": 'if BOARD_BLE_SENSOR_NODE\nconfig BOARD\n  default "ble_sensor_node"\nendif\n',
            "prj_conf": "CONFIG_GPIO=y\nCONFIG_I2C=y\nCONFIG_BT=y\nCONFIG_BT_PERIPHERAL=y\nCONFIG_SENSOR=y\nCONFIG_USB_DEVICE_STACK=y\n",
            "tests": {
                "test_i2c_sensors.c": "/* Bring-up test stub: verify SHT31 identity and fuel gauge I2C. */\n",
                "test_ble_adv.c": "/* Bring-up test stub: verify BLE advertising packet tx. */\n",
                "test_usb_connection.c": "/* Bring-up test stub: verify USB data connection and charging/status path. */\n",
            },
        }
    if architecture == "esp32s3_usb_i2c_sensor_data_logger":
        return {
            "project": "sensor_data_logger",
            "reference_project": "sensor_data_logger_bsp",
            "board_name": "sensor_data_logger",
            "board_arch": "xtensa",
            "dts_include": "#include <espressif/esp32s3.dtsi>",
            "model": "HW Co-design ESP32-S3 Sensor Data Logger",
            "compatible": "hw,sensor-data-logger",
            "defconfig": "CONFIG_SOC_ESP32S3=y\nCONFIG_BOARD_SENSOR_DATA_LOGGER=y\n",
            "kconfig_board": 'config BOARD_SENSOR_DATA_LOGGER\n  bool "ESP32-S3 Sensor Data Logger"\n',
            "kconfig_default": 'if BOARD_SENSOR_DATA_LOGGER\nconfig BOARD\n  default "sensor_data_logger"\nendif\n',
            "prj_conf": "CONFIG_GPIO=y\nCONFIG_I2C=y\nCONFIG_UART_CONSOLE=y\nCONFIG_USB_DEVICE_STACK=y\n",
            "tests": {"test_i2c_imu.c": "/* Bring-up test stub: verify IMU identity and samples. */\n", "test_usb_console.c": "/* Bring-up test stub: verify USB console enumeration. */\n"},
        }
    return {
        "project": "robot_controller",
        "reference_project": "robot_controller_bsp",
        "board_name": "robot_controller",
        "board_arch": "arm",
        "dts_include": "#include <st/h7/stm32h743Xi.dtsi>",
        "model": "HW Co-design Robot Controller",
        "compatible": "hw,robot-controller",
        "defconfig": "CONFIG_SOC_STM32H743XX=y\nCONFIG_BOARD_ROBOT_CONTROLLER=y\n",
        "kconfig_board": 'config BOARD_ROBOT_CONTROLLER\n  bool "Robot Controller"\n',
        "kconfig_default": 'if BOARD_ROBOT_CONTROLLER\nconfig BOARD\n  default "robot_controller"\nendif\n',
        "prj_conf": "CONFIG_GPIO=y\nCONFIG_I2C=y\nCONFIG_CAN=y\nCONFIG_PWM=y\nCONFIG_USB_DEVICE_STACK=y\n",
        "tests": {
            "test_i2c_imu.c": "/* Bring-up test stub: verify IMU identity and samples. */\n",
            "test_motor_pwm.c": "/* Bring-up test stub: scope PWM with motor power disabled. */\n",
            "test_can_loopback.c": "/* Bring-up test stub: verify CAN loopback before external bus connection. */\n",
            "test_estop_fail_safe.c": "/* Bring-up test stub: verify ESTOP fail-safe path disables motor outputs. */\n",
            "test_usb_connection.c": "/* Bring-up test stub: verify USB connection before console or bootloader use. */\n",
        },
    }


def generate_firmware(project: Path, spec: dict[str, Any], graph: dict[str, Any] | None = None) -> list[str]:
    from .backends.firmware_modules import render_module
    generated = project / "firmware" / "generated"
    app = project / "firmware" / "zephyr" / "app"
    graph = graph or json.loads((project / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    profile = firmware_profile(spec, graph)
    mcu = next((item for item in graph.get("components", []) if item.get("category") == "mcu"), None)
    assignments = []
    if mcu:
        for item in mcu.get("pins", []):
            net = item.get("net", "")
            if net not in {"V3V3", "GND", "SWDIO", "SWCLK", "NRST"}:
                assignments.append({"signal": net, "mcu_pin": item.get("mcu_pin", item["name"]), "net_name": net, "graph_pin": f"{mcu['ref']}.{item['number']}"})
    write_json(generated / "pinmap.json", assignments)
    pinmap = "#pragma once\n\n" + "\n".join(f'#define PIN_{item["signal"]} "{item["mcu_pin"]}"' for item in assignments) + "\n"
    atomic_write_text(generated / "pinmap.h", pinmap)
    overlay_lines = ["/ {\n  chosen { zephyr,console = &usart3; };\n};"]
    atomic_write_text(generated / "board.cmake", "# Generated Zephyr board integration entrypoint.\n")
    atomic_write_text(generated / "kconfig.defconfig", "# Generated project defaults.\n")
    # Collect module-level kconfig additions so prj.conf is written once
    module_kconfig: list[str] = []
    modules = spec.get("firmware", {}).get("modules", [])
    modules_dir = project / "firmware" / "modules"
    module_files: list[str] = []
    if modules:
        modules_dir.mkdir(parents=True, exist_ok=True)
        cmake_sources: list[str] = []
        live_ids: set[str] = set()
        for mod_spec in modules:
            output = render_module(mod_spec)
            live_ids.add(output.id)
            c_path = modules_dir / f"{output.id}.c"
            h_path = modules_dir / f"{output.id}.h"
            atomic_write_text(c_path, output.c_source)
            atomic_write_text(h_path, output.h_source)
            module_files.extend([str(c_path), str(h_path)])
            cmake_sources.append(f"firmware/modules/{output.id}.c")
            if output.dts_fragment:
                overlay_lines.append(f"\n/* {output.id} */\n{output.dts_fragment}")
            for flag in output.kconfig_flags:
                if flag not in module_kconfig:
                    module_kconfig.append(flag)
        modules_cmake = modules_dir / "CMakeLists.txt"
        sources_block = "\n".join(f"  ../../../{src}" for src in cmake_sources)
        atomic_write_text(modules_cmake, f"# Generated firmware modules\ntarget_sources(app PRIVATE\n{sources_block}\n)\n")
        module_files.append(str(modules_cmake))
        for stale_c in modules_dir.glob("*.c"):
            if stale_c.stem not in live_ids:
                stale_c.unlink()
        for stale_h in modules_dir.glob("*.h"):
            if stale_h.stem not in live_ids:
                stale_h.unlink()
    overlay = "\n".join(overlay_lines) + "\n"
    atomic_write_text(generated / "devicetree.overlay", overlay)
    prj_conf = profile["prj_conf"]
    if module_kconfig:
        existing_flags = {line.strip() for line in prj_conf.splitlines() if line.strip().startswith("CONFIG_")}
        new_flags = sorted(f for f in set(module_kconfig) if f not in existing_flags)
        if new_flags:
            prj_conf = prj_conf.rstrip("\n") + "\n# firmware modules\n" + "\n".join(new_flags) + "\n"
    atomic_write_text(app / "CMakeLists.txt", f"cmake_minimum_required(VERSION 3.20.0)\nfind_package(Zephyr REQUIRED HINTS $ENV{{ZEPHYR_BASE}})\nproject({profile['project']})\ntarget_sources(app PRIVATE src/main.c)\n" + ("include(${CMAKE_CURRENT_SOURCE_DIR}/../../../firmware/modules/CMakeLists.txt)\n" if modules else ""))
    atomic_write_text(app / "prj.conf", prj_conf)
    atomic_write_text(app / "src" / "main.c", '#include <zephyr/kernel.h>\nint main(void) { return 0; }\n')
    tests = project / "firmware" / "zephyr" / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    for stale_test in tests.glob("test_*.c"):
        if stale_test.name not in profile["tests"]:
            stale_test.unlink()
    for name, content in profile["tests"].items():
        atomic_write_text(tests / name, content)
    board_dir = project / "firmware" / "zephyr" / "boards" / profile["board_arch"] / profile["board_name"]
    board_name = profile["board_name"]
    atomic_write_text(board_dir / f"{board_name}.dts", f'/dts-v1/;\n{profile["dts_include"]}\n/ {{ model = "{profile["model"]}"; compatible = "{profile["compatible"]}"; chosen {{ zephyr,console = &usart3; }}; }};\n')
    atomic_write_text(board_dir / f"{board_name}_defconfig", profile["defconfig"])
    atomic_write_text(board_dir / "Kconfig.board", profile["kconfig_board"])
    atomic_write_text(board_dir / "Kconfig.defconfig", profile["kconfig_default"])
    reference = project / "firmware" / "reference"
    motor_pwm_count = len([item for item in assignments if item["signal"].endswith("_PWM")])
    pin_self_test = " && ".join(f'PIN_{item["signal"]}[0] != 0' for item in assignments) or "1"
    estop_required = bool(spec.get("safety", {}).get("emergency_stop", {}).get("required"))
    estop_assert = " assert(board_estop_is_fail_safe());" if estop_required else ""
    atomic_write_text(reference / "CMakeLists.txt", f"cmake_minimum_required(VERSION 3.20)\nproject({profile['reference_project']} C)\nenable_testing()\nadd_library(board_bsp src/board.c)\ntarget_include_directories(board_bsp PUBLIC include ../generated)\nadd_executable(bringup_tests tests/bringup_tests.c)\ntarget_link_libraries(bringup_tests PRIVATE board_bsp)\nadd_test(NAME bringup_tests COMMAND bringup_tests)\n")
    atomic_write_text(reference / "include" / "board.h", "#pragma once\n#include <stddef.h>\nsize_t board_motor_channel_count(void);\nint board_estop_is_fail_safe(void);\nint board_pinmap_self_test(void);\n")
    atomic_write_text(reference / "src" / "board.c", f'#include "board.h"\n#include "pinmap.h"\nsize_t board_motor_channel_count(void) {{ return {motor_pwm_count}; }}\nint board_estop_is_fail_safe(void) {{ return {1 if estop_required else 0}; }}\nint board_pinmap_self_test(void) {{ return {pin_self_test}; }}\n')
    atomic_write_text(reference / "tests" / "bringup_tests.c", f'#include "board.h"\n#include <assert.h>\n#include <stdio.h>\nint main(void) {{ assert(board_motor_channel_count() == {motor_pwm_count});{estop_assert} assert(board_pinmap_self_test()); puts("bringup tests passed"); return 0; }}\n')
    bsp_files = [str(path) for path in sorted(generated.iterdir()) if path.name != "provenance.json"]
    return bsp_files + module_files + [str(app / "src" / "main.c"), *[str(tests / name) for name in sorted(profile["tests"])], str(board_dir / f"{board_name}.dts"), str(reference / "CMakeLists.txt")]


def generate_bom(project: Path) -> str:
    graph = json.loads((project / "electronics" / "generated" / "electrical_graph.json").read_text(encoding="utf-8"))
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["reference", "category", "mpn", "quantity", "risk"])
    writer.writeheader()
    for component in graph["components"]:
        writer.writerow({"reference": component["ref"], "category": component["category"], "mpn": component["mpn"] or "UNRESOLVED", "quantity": 1, "risk": "high" if not component["mpn"] else "normal"})
    path = project / "electronics" / "generated" / "bom.csv"
    atomic_write_text(path, output.getvalue())
    return str(path)
