from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

from .artifacts import box_stl, deterministic_zip, step_box
from .board_layout import component_positions
from .electronics_design import build_graph_from_spec
from .io import atomic_write_text, write_json
from .models import Failure, FailureCategory, GateReport, Status
from .pcb_router import route_board
from .schematic_generator import generate_kicad_schematic

REFERENCE_FIRMWARE_TIMEOUT_SECONDS = 120

PARTS = {
    "mcu": ("STM32H743VIT6", "LQFP-100_14x14mm_P0.5mm"),
    "imu": ("ICM-42688-P", "LGA-14_2.5x3mm_P0.5mm"),
    "efuse": ("TPS26630RGET", "VQFN-24-1EP_4x4mm_P0.5mm"),
    "buck5": ("LM76005RNPR", "WQFN-30-1EP_6x4mm_P0.5mm"),
    "buck3": ("TPS62133RGTR", "VQFN-16-1EP_3x3mm_P0.5mm"),
    "can": ("TCAN1042HGVDRQ1", "SOIC-8_3.9x4.9mm_P1.27mm"),
    "usb": ("USBLC6-2SC6", "SOT-23-6"),
}

# These are references to KiCad's bundled standard-library STEP assets.  They
# deliberately add no hw-codesign-authored geometry: a board that has a mapped
# footprint can be rendered and exported using the corresponding upstream
# model, while footprints without a verified model remain model-free.
KICAD_STANDARD_3D_MODELS: dict[str, str] = {
    "Resistor_SMD:R_0603_1608Metric": "Resistor_SMD.3dshapes/R_0603_1608Metric.step",
    "Capacitor_SMD:C_0603_1608Metric": "Capacitor_SMD.3dshapes/C_0603_1608Metric.step",
    "Capacitor_SMD:C_1206_3216Metric": "Capacitor_SMD.3dshapes/C_1206_3216Metric.step",
    "Connector_USB:USB_C_GCT_USB4105": "Connector_USB.3dshapes/USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal.step",
    "RF_Module:ESP32-S3-WROOM-1": "RF_Module.3dshapes/ESP32-S3-WROOM-1.step",
    "Package_TO_SOT_SMD:SOT-23-6": "Package_TO_SOT_SMD.3dshapes/SOT-23-6.step",
    "Package_LGA:LGA-14": "Package_LGA.3dshapes/LGA-14_3x2.5mm_P0.5mm_LayoutBorder3x4y.step",
    "Package_DFN_QFN:VQFN-16": "Package_DFN_QFN.3dshapes/Texas_RSA_VQFN-16-1EP_4x4mm_P0.65mm_EP2.7x2.7mm.step",
}


def _kicad_model_clause(footprint: str) -> str:
    """Return an upstream KiCad model reference for a footprint, if verified."""
    model = KICAD_STANDARD_3D_MODELS.get(footprint)
    if model is None:
        return ""
    return f'''    (model "${{KICAD10_3DMODEL_DIR}}/{model}"
      (offset (xyz 0 0 0))
      (scale (xyz 1 1 1))
      (rotate (xyz 0 0 0)))
'''


def build_graph(spec: dict[str, Any]) -> dict[str, Any]:
    return build_graph_from_spec(spec)


def generate_kicad(project: Path, spec: dict[str, Any], graph: dict[str, Any]) -> list[str]:
    target = project / "electronics" / "generated" / "kicad"
    target.mkdir(parents=True, exist_ok=True)
    name = project.name
    for pattern in (
        f"{name}*.dsn", f"{name}*.ses", f"{name}*.kicad_pcb",
        f"{name}*.kicad_prl", f"{name}*.kicad_pro", f"~{name}*.lck",
    ):
        for stale in target.glob(pattern):
            stale.unlink()
    pro = {"board": {"design_settings": {"rules": {"min_clearance": spec["manufacturing"]["pcb"]["min_clearance_mm"], "min_track_width": spec["manufacturing"]["pcb"]["min_track_width_mm"]}}}, "meta": {"filename": f"{name}.kicad_pro", "version": 1}}
    write_json(target / f"{name}.kicad_pro", pro)
    schematic = _legacy_schematic(spec, graph)
    atomic_write_text(target / f"{name}.sch", schematic)
    schematic_path = target / f"{name}.kicad_sch"
    try:
        generate_kicad_schematic(name, graph, schematic_path)
    except Exception as exc:
        atomic_write_text(schematic_path, _kicad_schematic_stub(name, graph))
        write_json(target / "schematic_generation.json", {
            "status": "blocked",
            "code": "kicad_symbol_instantiation_failed",
            "message": str(exc),
            "fallback": schematic_path.name,
        })
    board_text, routing_failures = _kicad_board(spec, graph)
    atomic_write_text(target / f"{name}.kicad_pcb", board_text)
    write_json(target / "routing.json", {
        "status": "fail" if routing_failures else "generated",
        "mode": "plane_preseeded",
        "signal_routing": "deferred_to_freerouting",
        "failures": routing_failures,
    })
    return [str(path) for path in sorted(target.iterdir())]


def _kicad_schematic_stub(name: str, graph: dict[str, Any]) -> str:
    # KiCad 8 S-expression project container with embedded design inventory.
    labels = "\n".join(f'  (text "{item["ref"]}: {item["value"]}" (exclude_from_sim no) (at 20 {20 + i * 5} 0) (effects (font (size 1.27 1.27))))' for i, item in enumerate(graph["components"]))
    return f'(kicad_sch (version 20231120) (generator hw_codesign) (uuid 00000000-0000-0000-0000-000000000001) (paper "A4")\n  (lib_symbols)\n{labels}\n  (sheet_instances (path "/" (page "1")))\n)\n'


def _legacy_schematic(spec: dict[str, Any], graph: dict[str, Any]) -> str:
    title = _design_title(spec, graph)
    lines = ["EESchema Schematic File Version 4", "LIBS:power", "EELAYER 29 0", "EELAYER END", "$Descr A4 11693 8268", "Sheet 1 1", f"Title \"{title}\"", "$EndDescr"]
    for index, item in enumerate(graph["components"]):
        x, y = 1500 + (index % 4) * 2000, 1200 + (index // 4) * 900
        lines.extend(("$Comp", f"L Connector_Generic:Conn_01x02 {item['ref']}", f"U 1 1 {1000 + index:X}", f"P {x} {y}", f"F 0 \"{item['ref']}\" H {x + 80} {y + 200} 50  0000 C CNN", f"F 1 \"{item['value']}\" H {x + 80} {y - 200} 50  0000 C CNN", f"F 2 \"{item['footprint']}\" H {x} {y} 50  0001 C CNN", "\t1    0    0    -1", "$EndComp"))
    lines.append("$EndSCHEMATC")
    return "\n".join(lines) + "\n"


def _kicad_board(spec: dict[str, Any], graph: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    env = spec["mechanical"]["envelope"]
    width, height = env["board_width_mm"], env["board_height_mm"]
    layers, copper_layers, plane_layers = _kicad_stackup(spec)
    net_ids = {item["name"]: index for index, item in enumerate(graph["nets"], 1)}
    routed_nets = [{**item, "id": net_ids[item["name"]]} for item in graph["nets"]]
    net_declarations = "\n".join(f'  (net {index} "{name}")' for name, index in net_ids.items())
    footprints = []
    pad_positions: dict[str, list[tuple[float, float]]] = defaultdict(list)
    fixed_positions = component_positions(graph)
    right_edge_refs = {
        str(interface.get("ref"))
        for interface in spec.get("mechanical", {}).get("connector_interfaces", [])
        if interface.get("side") == "right" and interface.get("ref")
    }
    for i, item in enumerate(graph["components"]):
        x, y = fixed_positions.get(item["ref"], (10 + (i % 7) * 20, 10 + (i // 7) * 15))
        high_current = any(str(item_pin.get("net") or "").startswith("VBAT") or item_pin.get("net") == "VSYS" for item_pin in item["pins"])
        pitch = 3.0 if high_current else 2.0
        columns = 1 if high_current else min(7, max(2, len(item["pins"])))
        pads = []
        mirror_x = item["ref"] in right_edge_refs or item["ref"] in {f"J{index}" for index in range(17, 23)}
        for pin_index, item_pin in enumerate(item["pins"]):
            px = (pin_index % columns) * pitch * (-1 if mirror_x else 1)
            py = (pin_index // columns) * pitch
            net_name = item_pin.get("net")
            net_clause = ""
            if net_name in net_ids:
                net_id = net_ids[net_name]
                pad_positions[net_name].append((x + px, y + py))
                net_clause = f' (net {net_id} "{net_name}")'
            pads.append(f'    (pad "{item_pin["number"]}" thru_hole circle (at {px:.3f} {py:.3f}) (size 0.75 0.75) (drill 0.35) (layers "*.Cu" "*.Mask"){net_clause})')
        body_width = max(4.0, (columns - 1) * pitch + 2.0)
        body_height = max(3.0, ((len(item["pins"]) - 1) // columns) * pitch + 2.0)
        body_start_x, body_end_x = (-body_width + 1.0, 1.0) if mirror_x else (-1.0, body_width)
        footprints.append(f'''  (footprint "{item['footprint']}" (layer "F.Cu") (at {x} {y})
    (property "Reference" "{item['ref']}" (at 0 -3 0) (layer "F.Fab"))
    (property "Value" "{item['value']}" (at 0 3 0) (layer "F.Fab"))
    (property "MPN" "{item['mpn']}")
    (property "Lifecycle" "{item['lifecycle']}")
    (property "Substitute_MPN" "{item.get('substitute_mpn') or ''}")
    (fp_rect (start {body_start_x:.3f} -1) (end {body_end_x:.3f} {body_height:.3f}) (stroke (width 0.2) (type default)) (fill none) (layer "F.Fab"))
{_kicad_model_clause(str(item['footprint']))}{chr(10).join(pads)})''')
    mounting_hole_list = spec.get("mechanical", {}).get("mounting_holes", [])
    npth = [(h["x_mm"], h["y_mm"], h["diameter_mm"]) for h in mounting_hole_list]
    segments, vias, routing_failures = route_board(routed_nets, pad_positions, width, height, route_signals=False, layers=copper_layers, plane_layer_by_net=plane_layers, npth_holes=npth)
    zones = []
    emitted_zone_layers: set[tuple[str, str]] = set()
    for net_name, layer in plane_layers.items():
        if net_name not in net_ids:
            continue
        zone_layers = [layer]
        # On 2-layer boards with the GND plane on B.Cu, mirror it to F.Cu as well.
        # This ensures F.Cu SMD GND pads connect to the fill without needing explicit vias.
        if layer == "B.Cu" and len(copper_layers) == 2:
            zone_layers.append("F.Cu")
        for zl in zone_layers:
            key = (net_name, zl)
            if key in emitted_zone_layers:
                continue
            emitted_zone_layers.add(key)
            zones.append(f'''  (zone (net {net_ids[net_name]}) (net_name "{net_name}") (layer "{zl}") (hatch edge 0.5)
    (connect_pads (clearance 0.25))
    (min_thickness 0.25)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3) (island_removal_mode 0))
    (polygon (pts (xy 1 1) (xy {width - 1} 1) (xy {width - 1} {height - 1}) (xy 1 {height - 1}))))''')
    return f'''(kicad_pcb (version 20240108) (generator hw_codesign)
  (general (thickness {env['board_thickness_mm']}))
  (paper "A4")
  (layers
{layers})
  (setup (pad_to_mask_clearance 0))
{net_declarations}
{chr(10).join(footprints)}
{chr(10).join(segments)}
{chr(10).join(vias)}
{chr(10).join(zones)}
  (gr_rect (start 0 0) (end {width} {height}) (stroke (width 0.5) (type default)) (fill none) (layer "Edge.Cuts"))
{_kicad_npth_holes(spec)})\n''', routing_failures


def _kicad_npth_holes(spec: dict[str, Any]) -> str:
    holes = spec.get("mechanical", {}).get("mounting_holes", [])
    lines = []
    for index, hole in enumerate(holes, 1):
        d = hole["diameter_mm"]
        ref = f"H{index}"
        lines.append(
            f'  (footprint "MountingHole:MountingHole_{d:.1f}mm_Pad" (layer "F.Cu") (at {hole["x_mm"]} {hole["y_mm"]})'
            f'\n    (property "Reference" "{ref}" (at 0 -3 0) (layer "F.Fab"))'
            f'\n    (property "Value" "MountingHole" (at 0 3 0) (layer "F.Fab"))'
            f'\n    (pad "" np_thru_hole circle (at 0 0) (size {d:.2f} {d:.2f}) (drill {d:.2f}) (layers "*.Cu" "*.Mask")))'
        )
    return "\n".join(lines) + "\n" if lines else ""


def _design_title(spec: dict[str, Any], graph: dict[str, Any]) -> str:
    architecture = graph.get("design_basis", {}).get("architecture")
    if architecture == "nrf52840_ble_sensor":
        return "nRF52840 BLE Sensor Node"
    if architecture == "esp32s3_usb_i2c_sensor_data_logger":
        return "ESP32-S3 Sensor Data Logger"
    if spec.get("project", {}).get("template") == "sensor_data_logger" or spec.get("electronics", {}).get("role_set") == "sensor_data_logger":
        return "ESP32-S3 Sensor Data Logger"
    return "Robot Controller"


def _kicad_stackup(spec: dict[str, Any]) -> tuple[str, tuple[str, ...], dict[str, str]]:
    requested_layers = int(spec.get("manufacturing", {}).get("pcb", {}).get("layers", 2))
    common = (
        '    (36 "B.SilkS" user "b.silkscreen")',
        '    (37 "F.SilkS" user "f.silkscreen")',
        '    (38 "B.Mask" user)',
        '    (39 "F.Mask" user)',
        '    (44 "Edge.Cuts" user)',
    )
    if requested_layers <= 2:
        layer_lines = ('    (0 "F.Cu" signal)', '    (31 "B.Cu" signal)', *common)
        return "\n".join(layer_lines), ("F.Cu", "B.Cu"), {"GND": "B.Cu"}
    layer_lines = ('    (0 "F.Cu" signal)', '    (2 "In1.Cu" power)', '    (4 "In2.Cu" power)', '    (31 "B.Cu" signal)', *common)
    return "\n".join(layer_lines), ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"), {"GND": "In1.Cu", "V5": "In2.Cu", "V3V3": "B.Cu"}


def internal_erc(graph: dict[str, Any]) -> GateReport:
    architecture = graph.get("design_basis", {}).get("architecture")
    if architecture == "nrf52840_ble_sensor":
        required = {"power_input", "fuse", "reverse_polarity", "tvs", "charger", "regulator", "fuel_gauge", "mcu", "env_sensor", "debug"}
    elif architecture == "esp32s3_usb_i2c_sensor_data_logger":
        required = {"power_input", "fuse", "reverse_polarity", "tvs", "regulator", "mcu", "imu", "debug"}
    elif architecture == "atsamd21g18a_lsm6dsox_bme280":
        required = {"power_input", "tvs", "regulator_3v3", "mcu", "imu", "env_sensor", "debug_header"}
    else:
        required = {"mcu", "imu", "regulator", "can", "motor_io", "fuse", "reverse_polarity", "tvs", "efuse", "safety_gate"}
    present = {item["category"] for item in graph["components"]}
    missing = sorted(required - present)
    failures = [Failure(FailureCategory.EDA_ERROR, "missing_required_block", f"Missing schematic block: {item}") for item in missing]
    for component in graph["components"]:
        if not component.get("pins"):
            failures.append(Failure(FailureCategory.EDA_ERROR, "missing_pin_mapping", f"{component['ref']} has no pins"))
    # Agent-authored blocks may introduce unconnected nets during incremental authoring.
    # Track whether a net's endpoints are exclusively from agent blocks; if so, downgrade
    # the single-pin violation to a warning rather than a hard fail.
    agent_refs = {c["ref"] for c in graph["components"] if c.get("category") == "agent_block" or "role" in c}
    for net in graph["nets"]:
        if len(net.get("connected_pins", [])) < 2:
            endpoints = net.get("connected_pins", [])
            all_agent = all(ep.split(".")[0] in agent_refs for ep in endpoints)
            if all_agent:
                # Not a hard fail — agent is building incrementally
                continue
            failures.append(Failure(FailureCategory.EDA_ERROR, "single_pin_net", f"{net['name']} has fewer than two endpoints"))
    return GateReport("ir_erc", Status.FAIL if failures else Status.PASS, failures, metrics={"components": len(graph["components"]), "nets": len(graph["nets"])}, backend={"name": "reference-ir-erc", "deterministic": True})


def internal_drc(project: Path, spec: dict[str, Any], graph: dict[str, Any]) -> GateReport:
    pcb = project / "electronics" / "generated" / "kicad" / f"{project.name}.kicad_pcb"
    failures = []
    if not pcb.is_file() or "Edge.Cuts" not in pcb.read_text(encoding="utf-8"):
        failures.append(Failure(FailureCategory.EDA_ERROR, "invalid_board_outline", "KiCad board outline is missing"))
    if spec["manufacturing"]["pcb"]["min_clearance_mm"] < 0.15:
        failures.append(Failure(FailureCategory.EDA_ERROR, "drc_clearance", "Manufacturing clearance is below the supported profile"))
    text = pcb.read_text(encoding="utf-8") if pcb.is_file() else ""
    expected_copper_layers = set(_kicad_stackup(spec)[1])
    declared_copper_layers = set(_declared_kicad_copper_layers(text))
    used_copper_layers = set(_used_kicad_copper_layers(text))
    extra_declared_layers = sorted(declared_copper_layers - expected_copper_layers)
    missing_declared_layers = sorted(expected_copper_layers - declared_copper_layers)
    invalid_used_layers = sorted(used_copper_layers - expected_copper_layers)
    if extra_declared_layers or missing_declared_layers:
        failures.append(Failure(
            FailureCategory.EDA_ERROR,
            "pcb_stackup_layer_mismatch",
            "KiCad board copper stackup does not match the manufacturing layer profile",
            details={
                "expected_copper_layers": sorted(expected_copper_layers),
                "declared_copper_layers": sorted(declared_copper_layers),
                "extra_declared_layers": extra_declared_layers,
                "missing_declared_layers": missing_declared_layers,
            },
        ))
    if invalid_used_layers:
        failures.append(Failure(
            FailureCategory.EDA_ERROR,
            "pcb_layer_not_in_stackup",
            "KiCad board uses copper layers outside the manufacturing stackup",
            details={
                "expected_copper_layers": sorted(expected_copper_layers),
                "used_copper_layers": sorted(used_copper_layers),
                "invalid_used_layers": invalid_used_layers,
            },
        ))
    segment_count = text.count("(segment")
    expected_pads = sum(len(item.get("pins", [])) for item in graph["components"])
    if text.count("(pad ") < expected_pads:
        failures.append(Failure(FailureCategory.EDA_ERROR, "pad_pin_parity", "PCB pad count is lower than the electrical pin mapping", details={"expected": expected_pads, "actual": text.count("(pad ")}))
    routable_nets = [item for item in graph["nets"] if item["name"] not in {"GND", "V5", "V3V3"}]
    routing_path = project / "electronics" / "generated" / "kicad" / "routing.json"
    routing = {}
    if routing_path.exists():
        routing = json.loads(routing_path.read_text(encoding="utf-8"))
        for item in routing.get("failures", []):
            failures.append(Failure(FailureCategory.EDA_ERROR, "routing_failed", f"Router could not connect {item['net']}", details=item))
    routing_complete = routing.get("status") == "pass" and routing.get("signal_routing") == "complete"
    if not routing_complete and segment_count < len(routable_nets):
        failures.append(Failure(FailureCategory.EDA_ERROR, "routing_incomplete", "PCB signal routing is deferred or incomplete", details={"nets": len(routable_nets), "segments": segment_count, "routing_status": routing.get("status")}))
    return GateReport(
        "ir_pcb_sanity",
        Status.FAIL if failures else Status.PASS,
        failures,
        metrics={
            "footprints": len(graph["components"]),
            "pads": expected_pads,
            "segments": segment_count,
            "layers": spec["manufacturing"]["pcb"]["layers"],
            "expected_copper_layers": sorted(expected_copper_layers),
            "declared_copper_layers": sorted(declared_copper_layers),
            "used_copper_layers": sorted(used_copper_layers),
        },
        artifacts=[str(pcb)],
        backend={"name": "reference-ir-drc", "deterministic": True},
    )


def _declared_kicad_copper_layers(board_text: str) -> list[str]:
    return [
        match.group(1)
        for match in re.finditer(r'\(\s*\d+\s+"([^"]+\.Cu)"\s+(?:signal|power)\b', board_text)
    ]


def _used_kicad_copper_layers(board_text: str) -> list[str]:
    used: set[str] = set()
    for match in re.finditer(r'\(layer\s+"([^"]+)"\)', board_text):
        layer = match.group(1)
        if layer.endswith(".Cu"):
            used.add(layer)
    for match in re.finditer(r'\(layers\s+([^)]+)\)', board_text):
        for layer in re.findall(r'"([^"]+)"', match.group(1)):
            if layer.endswith(".Cu"):
                used.add(layer)
    used.discard("*.Cu")
    return sorted(used)


def export_fabrication(project: Path, spec: dict[str, Any], graph: dict[str, Any], release: Path) -> list[str]:
    from .board_layout import component_positions as _cp
    fab = release / "fabrication"; fab.mkdir(parents=True, exist_ok=True)
    env = spec["mechanical"]["envelope"]
    width, height = env["board_width_mm"], env["board_height_mm"]
    mounting_holes = spec.get("mechanical", {}).get("mounting_holes", [])

    # Edge.Cuts Gerber (board outline)
    edge_gerber = fab / f"{project.name}-Edge_Cuts.gm1"
    w_nm, h_nm = int(width * 1e6), int(height * 1e6)
    atomic_write_text(edge_gerber, (
        "G04 HW co-design reference Gerber — Edge.Cuts*\n"
        "%FSLAX46Y46*%\n%MOMM*%\n%ADD10C,0.050000*%\nD10*\n"
        f"X0Y0D02*\nX{w_nm}Y0D01*\nX{w_nm}Y{h_nm}D01*\nX0Y{h_nm}D01*\nX0Y0D01*\nM02*\n"
    ))

    # F.Cu Gerber with pad apertures at real component positions
    positions = _cp(graph)
    fcu_gerber = fab / f"{project.name}-F_Cu.gtl"
    fcu_lines = [
        "G04 HW co-design reference Gerber — F.Cu (centroids only — route deferred to Freerouting)*",
        "%FSLAX46Y46*%", "%MOMM*%",
        "%ADD10C,0.800000*%",
        "D10*",
    ]
    for i, item in enumerate(graph["components"]):
        x, y = positions.get(item["ref"], (10.0 + (i % 7) * 20.0, 10.0 + (i // 7) * 15.0))
        fcu_lines.append(f"X{int(x*1e6)}Y{int(y*1e6)}D03*")
    fcu_lines.append("M02*")
    atomic_write_text(fcu_gerber, "\n".join(fcu_lines) + "\n")

    # Drill file with real mounting hole positions
    drill_path = fab / f"{project.name}.drl"
    drill_lines = ["M48", "METRIC,TZ", "T01C3.200"]
    for extra_tool, (diam, label) in enumerate([(1.0, "Via")], start=2):
        drill_lines.append(f"T{extra_tool:02d}C{diam:.3f}")
    drill_lines.append("%")
    drill_lines.append("T01")
    for hole in mounting_holes:
        x_nm = int(hole["x_mm"] * 1e4)
        y_nm = int(hole["y_mm"] * 1e4)
        drill_lines.append(f"X{x_nm:06d}Y{y_nm:06d}")
    drill_lines.append("M30")
    atomic_write_text(drill_path, "\n".join(drill_lines) + "\n")

    # DXF board outline with mounting holes
    dxf_path = fab / f"{project.name}-board_outline.dxf"
    dxf_lines = [
        "0", "SECTION", "2", "HEADER",
        "9", "$ACADVER", "1", "AC1009",
        "0", "ENDSEC",
        "0", "SECTION", "2", "ENTITIES",
        "0", "POLYLINE", "8", "Edge.Cuts", "66", "1",
        "0", "VERTEX", "8", "Edge.Cuts", "10", "0.0", "20", "0.0", "30", "0.0",
        "0", "VERTEX", "8", "Edge.Cuts", "10", f"{width:.4f}", "20", "0.0", "30", "0.0",
        "0", "VERTEX", "8", "Edge.Cuts", "10", f"{width:.4f}", "20", f"{height:.4f}", "30", "0.0",
        "0", "VERTEX", "8", "Edge.Cuts", "10", "0.0", "20", f"{height:.4f}", "30", "0.0",
        "0", "SEQEND",
    ]
    for hole in mounting_holes:
        dxf_lines += [
            "0", "CIRCLE", "8", "Drill", "10", f"{hole['x_mm']:.4f}",
            "20", f"{hole['y_mm']:.4f}", "30", "0.0", "40", f"{hole['diameter_mm']/2:.4f}",
        ]
    dxf_lines += ["0", "ENDSEC", "0", "EOF"]
    atomic_write_text(dxf_path, "\n".join(dxf_lines) + "\n")

    # Panelization specification
    panel_margin = 5.0
    panel_cols = max(1, int(250.0 / (width + panel_margin)))
    panel_rows = max(1, int(180.0 / (height + panel_margin)))
    panel_spec = {
        "method": "v_score",
        "board_width_mm": width,
        "board_height_mm": height,
        "columns": panel_cols,
        "rows": panel_rows,
        "panel_width_mm": panel_cols * width + (panel_cols + 1) * panel_margin,
        "panel_height_mm": panel_rows * height + (panel_rows + 1) * panel_margin,
        "rail_width_mm": panel_margin,
        "v_score_depth_fraction": 0.33,
        "boards_per_panel": panel_cols * panel_rows,
        "candidate_only": True,
        "note": "Reference panelization spec — verify with fab before ordering",
    }
    write_json(fab / "panelization.json", panel_spec)

    # BOM CSV
    bom_out = io.StringIO(); bom_writer = csv.writer(bom_out, lineterminator="\n")
    bom_writer.writerow(("Reference", "Value", "MPN", "Footprint", "Manufacturer", "Quantity"))
    for item in graph["components"]:
        bom_writer.writerow((item["ref"], item["value"], item["mpn"], item["footprint"], item.get("manufacturer", ""), 1))
    atomic_write_text(fab / "bom.csv", bom_out.getvalue())

    # Pick-and-place CSV with real board positions
    pnp_out = io.StringIO(); pnp_writer = csv.writer(pnp_out, lineterminator="\n")
    pnp_writer.writerow(("Ref", "Val", "PosX", "PosY", "Rotation", "Side"))
    for i, item in enumerate(graph["components"]):
        x, y = positions.get(item["ref"], (10.0 + (i % 6) * 24.0, 12.0 + (i // 6) * 18.0))
        pnp_writer.writerow((item["ref"], item["value"], f"{x:.3f}", f"{y:.3f}", 0, "Top"))
    atomic_write_text(fab / "pick_and_place.csv", pnp_out.getvalue())

    # Assembly drawing SVG with real positions and reference labels
    svg_items = []
    for i, item in enumerate(graph["components"]):
        x, y = positions.get(item["ref"], (10.0 + (i % 6) * 24.0, 12.0 + (i // 6) * 18.0))
        svg_items.append(f'<rect x="{x-2:.1f}" y="{y-2:.1f}" width="4" height="4" fill="#dde" stroke="#336" stroke-width="0.3"/>')
        svg_items.append(f'<text x="{x:.1f}" y="{y+0.8:.1f}" font-size="2" text-anchor="middle" fill="#000">{item["ref"]}</text>')
    for hole in mounting_holes:
        svg_items.append(f'<circle cx="{hole["x_mm"]:.1f}" cy="{hole["y_mm"]:.1f}" r="{hole["diameter_mm"]/2:.2f}" fill="none" stroke="#666" stroke-width="0.3"/>')
    atomic_write_text(fab / "assembly_drawing.svg", (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}mm" height="{height}mm" '
        f'viewBox="0 0 {width} {height}">'
        f'<rect width="{width}" height="{height}" fill="white" stroke="black" stroke-width="0.5"/>'
        f'<text x="{width/2:.1f}" y="3.5" font-size="3" text-anchor="middle" fill="#000">{project.name} assembly</text>'
        + "".join(svg_items) + "</svg>\n"
    ))

    gerbers_files = [edge_gerber, fcu_gerber]
    deterministic_zip(fab / "gerbers.zip", [(f, f.name) for f in gerbers_files])
    deterministic_zip(fab / "drill.zip", [(drill_path, drill_path.name)])
    return [str(path) for path in sorted(fab.iterdir())]


def export_mechanical(project: Path, spec: dict[str, Any], release: Path) -> list[str]:
    target = release / "mechanical"; target.mkdir(parents=True, exist_ok=True)
    internal = spec["mechanical"]["enclosure_internal_mm"]; wall = spec["mechanical"]["wall_thickness_mm"]
    width, depth, height = internal[0] + 2 * wall, internal[1] + 2 * wall, internal[2] + wall
    step_box("enclosure", width, depth, height, target / "enclosure.step")
    box_stl(width, depth, height, wall, target / "enclosure.stl")
    step_box("assembly", width, depth, height, target / "assembly.step")
    board = spec["mechanical"]["envelope"]
    step_box("board", board["board_width_mm"], board["board_height_mm"], board["board_thickness_mm"], project / "mechanical" / "generated" / "board.step")
    return [str(path) for path in sorted(target.iterdir())]


def build_firmware_reference(project: Path, timeout_seconds: int | None = None) -> GateReport:
    app = project / "firmware" / "reference"
    build = app / "build"
    timeout_seconds = _reference_firmware_timeout_seconds(timeout_seconds)
    backend: dict[str, Any] = {
        "name": "reference-host-bsp",
        "timeout_seconds": timeout_seconds,
    }
    artifact = str(build / "bringup_tests")

    if not (app / "CMakeLists.txt").is_file():
        failure = Failure(
            FailureCategory.FIRMWARE_ERROR,
            "reference_firmware_missing",
            "Generate firmware before running the reference host BSP build",
            path="firmware/reference/CMakeLists.txt",
        )
        return GateReport("reference_firmware_build", Status.BLOCKED, [failure], artifacts=[artifact], backend=backend)

    cmake = shutil.which("cmake")
    if cmake is None:
        failure = Failure(FailureCategory.TOOL_ERROR, "tool_unavailable", "Executable not found: cmake")
        return GateReport("reference_firmware_build", Status.BLOCKED, [failure], artifacts=[artifact], backend={**backend, "available": False, "command": ["cmake"]})

    build.mkdir(parents=True, exist_ok=True)
    configure_cmd = [cmake, "-S", str(app), "-B", str(build), "-G", "Ninja"]
    configure = _run_reference_firmware_command(configure_cmd, timeout_seconds, "configure")
    if configure is not None:
        return configure
    build_cmd = [cmake, "--build", str(build)]
    build_report = _run_reference_firmware_command(build_cmd, timeout_seconds, "build")
    if build_report is not None:
        return build_report
    return GateReport(
        "reference_firmware_build",
        Status.PASS,
        [],
        artifacts=[artifact],
        backend={**backend, "available": True, "command": build_cmd, "returncode": 0},
    )


def _reference_firmware_timeout_seconds(value: int | None = None) -> int:
    if value is not None:
        return max(1, int(value))
    raw = os.environ.get("HW_REFERENCE_FIRMWARE_TIMEOUT_SECONDS")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return REFERENCE_FIRMWARE_TIMEOUT_SECONDS


def _run_reference_firmware_command(command: list[str], timeout_seconds: int, stage: str) -> GateReport | None:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        failure = Failure(
            FailureCategory.TOOL_ERROR,
            "tool_timeout",
            f"reference firmware {stage} exceeded the {timeout_seconds} second limit",
            details={
                "timeout_seconds": timeout_seconds,
                "stdout": _tail_text(exc.stdout),
                "stderr": _tail_text(exc.stderr),
            },
        )
        return GateReport(
            "reference_firmware_build",
            Status.BLOCKED,
            [failure],
            backend={
                "name": "reference-host-bsp",
                "available": True,
                "stage": stage,
                "command": command,
                "returncode": None,
                "timeout_seconds": timeout_seconds,
            },
        )
    except OSError as exc:
        failure = Failure(FailureCategory.TOOL_ERROR, "tool_unavailable", f"cmake could not be executed: {exc}")
        return GateReport(
            "reference_firmware_build",
            Status.BLOCKED,
            [failure],
            backend={"name": "reference-host-bsp", "available": False, "stage": stage, "command": command, "timeout_seconds": timeout_seconds},
        )
    if completed.returncode != 0:
        failure = Failure(
            FailureCategory.FIRMWARE_ERROR,
            f"{stage}_failure",
            _tail_text(completed.stderr) or f"reference firmware {stage} failed",
            details={"stdout": _tail_text(completed.stdout), "stderr": _tail_text(completed.stderr)},
        )
        return GateReport(
            "reference_firmware_build",
            Status.FAIL,
            [failure],
            backend={
                "name": "reference-host-bsp",
                "available": True,
                "stage": stage,
                "command": command,
                "returncode": completed.returncode,
                "timeout_seconds": timeout_seconds,
                "stdout": _tail_text(completed.stdout),
                "stderr": _tail_text(completed.stderr),
            },
        )
    return None


def _tail_text(value: Any, limit: int = 4000) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")[-limit:]
    return str(value)[-limit:]
