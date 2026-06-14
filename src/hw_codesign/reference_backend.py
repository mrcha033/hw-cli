from __future__ import annotations

import csv
import io
import json
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

from .artifacts import box_stl, deterministic_zip, step_box
from .electronics_design import build_controller_graph
from .io import atomic_write_text, write_json
from .models import Failure, FailureCategory, GateReport, Status
from .pcb_router import route_board
from .schematic_generator import generate_kicad_schematic


PARTS = {
    "mcu": ("STM32H743VIT6", "LQFP-100_14x14mm_P0.5mm"),
    "imu": ("ICM-42688-P", "LGA-14_2.5x3mm_P0.5mm"),
    "efuse": ("TPS26630RGET", "VQFN-24-1EP_4x4mm_P0.5mm"),
    "buck5": ("LM76005RNPR", "WQFN-30-1EP_6x4mm_P0.5mm"),
    "buck3": ("TPS62133RGTR", "VQFN-16-1EP_3x3mm_P0.5mm"),
    "can": ("TCAN1042HGVDRQ1", "SOIC-8_3.9x4.9mm_P1.27mm"),
    "usb": ("USBLC6-2SC6", "SOT-23-6"),
}


def build_graph(spec: dict[str, Any]) -> dict[str, Any]:
    return build_controller_graph(spec)


def generate_kicad(project: Path, spec: dict[str, Any], graph: dict[str, Any]) -> list[str]:
    target = project / "electronics" / "generated" / "kicad"
    target.mkdir(parents=True, exist_ok=True)
    name = project.name
    for pattern in (
        f"{name}*.dsn", f"{name}*.ses", f"{name}.*.kicad_pcb",
        f"{name}*.kicad_prl", f"{name}.*.kicad_pro", f"~{name}*.lck",
    ):
        for stale in target.glob(pattern):
            stale.unlink()
    pro = {"board": {"design_settings": {"rules": {"min_clearance": spec["manufacturing"]["pcb"]["min_clearance_mm"], "min_track_width": spec["manufacturing"]["pcb"]["min_track_width_mm"]}}}, "meta": {"filename": f"{name}.kicad_pro", "version": 1}}
    write_json(target / f"{name}.kicad_pro", pro)
    schematic = _legacy_schematic(graph)
    atomic_write_text(target / f"{name}.sch", schematic)
    generate_kicad_schematic(name, graph, target / f"{name}.kicad_sch")
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


def _legacy_schematic(graph: dict[str, Any]) -> str:
    lines = ["EESchema Schematic File Version 4", "LIBS:power", "EELAYER 29 0", "EELAYER END", "$Descr A4 11693 8268", "Sheet 1 1", "Title \"Robot Controller\"", "$EndDescr"]
    for index, item in enumerate(graph["components"]):
        x, y = 1500 + (index % 4) * 2000, 1200 + (index // 4) * 900
        lines.extend(("$Comp", f"L Connector_Generic:Conn_01x02 {item['ref']}", f"U 1 1 {1000 + index:X}", f"P {x} {y}", f"F 0 \"{item['ref']}\" H {x + 80} {y + 200} 50  0000 C CNN", f"F 1 \"{item['value']}\" H {x + 80} {y - 200} 50  0000 C CNN", f"F 2 \"{item['footprint']}\" H {x} {y} 50  0001 C CNN", "\t1    0    0    -1", "$EndComp"))
    lines.append("$EndSCHEMATC")
    return "\n".join(lines) + "\n"


def _kicad_board(spec: dict[str, Any], graph: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    env = spec["mechanical"]["envelope"]
    width, height = env["board_width_mm"], env["board_height_mm"]
    layers = '\n'.join(('    (0 "F.Cu" signal)', '    (2 "In1.Cu" power)', '    (4 "In2.Cu" power)', '    (31 "B.Cu" signal)', '    (36 "B.SilkS" user "b.silkscreen")', '    (37 "F.SilkS" user "f.silkscreen")', '    (38 "B.Mask" user)', '    (39 "F.Mask" user)', '    (44 "Edge.Cuts" user)'))
    net_ids = {item["name"]: index for index, item in enumerate(graph["nets"], 1)}
    routed_nets = [{**item, "id": net_ids[item["name"]]} for item in graph["nets"]]
    net_declarations = "\n".join(f'  (net {index} "{name}")' for name, index in net_ids.items())
    footprints = []
    pad_positions: dict[str, list[tuple[float, float]]] = defaultdict(list)
    fixed_positions = {
        "J1": (20, 5), "F1": (38, 5), "Q1": (56, 5), "D1": (74, 5), "U3": (92, 5), "U4": (112, 5), "U5": (132, 5),
        "J5": (22, 27), "D2": (42, 27), "R4": (60, 27), "U6": (90, 27), "J3": (118, 27), "R1": (136, 27),
        "U2": (22, 48), "R2": (42, 48), "R3": (58, 48), "J2": (104, 48), "Q2": (124, 48), "J4": (104, 69),
        "U1": (70, 37),
    }
    capacitor_refs = [f"C{index}" for index in range(1, 11)]
    fixed_positions.update({ref: (24 + (index % 5) * 22, 76 + (index // 5) * 13) for index, ref in enumerate(capacitor_refs)})
    for index in range(1, 13):
        side_index = (index - 1) % 6
        fixed_positions[f"J{index + 10}"] = (3 if index <= 6 else 155, 7 + side_index * 15)
    for i, item in enumerate(graph["components"]):
        x, y = fixed_positions.get(item["ref"], (10 + (i % 7) * 20, 10 + (i // 7) * 15))
        high_current = any(item_pin["net"].startswith("VBAT") or item_pin["net"] == "VSYS" for item_pin in item["pins"])
        pitch = 3.0 if high_current else 2.0
        columns = 1 if high_current else min(7, max(2, len(item["pins"])))
        pads = []
        mirror_x = item["ref"] in {f"J{index}" for index in range(17, 23)}
        for pin_index, item_pin in enumerate(item["pins"]):
            px = (pin_index % columns) * pitch * (-1 if mirror_x else 1)
            py = (pin_index // columns) * pitch
            net_id = net_ids[item_pin["net"]]
            pad_positions[item_pin["net"]].append((x + px, y + py))
            pads.append(f'    (pad "{item_pin["number"]}" thru_hole circle (at {px:.3f} {py:.3f}) (size 0.75 0.75) (drill 0.35) (layers "*.Cu" "*.Mask") (net {net_id} "{item_pin["net"]}"))')
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
{chr(10).join(pads)})''')
    segments, vias, routing_failures = route_board(routed_nets, pad_positions, width, height, route_signals=False)
    plane_layers = {"GND": "In1.Cu", "V5": "In2.Cu", "V3V3": "B.Cu"}
    zones = []
    for net_name, layer in plane_layers.items():
        zones.append(f'''  (zone (net {net_ids[net_name]}) (net_name "{net_name}") (layer "{layer}") (hatch edge 0.5)
    (connect_pads (clearance 0.25))
    (min_thickness 0.25)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
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
)\n''', routing_failures


def internal_erc(graph: dict[str, Any]) -> GateReport:
    required = {"mcu", "imu", "regulator", "can", "motor_io", "fuse", "reverse_polarity", "tvs", "efuse", "safety_gate"}
    present = {item["category"] for item in graph["components"]}
    missing = sorted(required - present)
    failures = [Failure(FailureCategory.EDA_ERROR, "missing_required_block", f"Missing schematic block: {item}") for item in missing]
    for component in graph["components"]:
        if not component.get("pins"):
            failures.append(Failure(FailureCategory.EDA_ERROR, "missing_pin_mapping", f"{component['ref']} has no pins"))
    for net in graph["nets"]:
        if len(net.get("connected_pins", [])) < 2:
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
    return GateReport("ir_pcb_sanity", Status.FAIL if failures else Status.PASS, failures, metrics={"footprints": len(graph["components"]), "pads": expected_pads, "segments": segment_count, "layers": spec["manufacturing"]["pcb"]["layers"]}, artifacts=[str(pcb)], backend={"name": "reference-ir-drc", "deterministic": True})


def export_fabrication(project: Path, spec: dict[str, Any], graph: dict[str, Any], release: Path) -> list[str]:
    fab = release / "fabrication"; fab.mkdir(parents=True, exist_ok=True)
    width = spec["mechanical"]["envelope"]["board_width_mm"]; height = spec["mechanical"]["envelope"]["board_height_mm"]
    gerber = fab / f"{project.name}-Edge_Cuts.gm1"
    atomic_write_text(gerber, f"G04 HW co-design deterministic Gerber*\n%FSLAX46Y46*%\n%MOMM*%\n%ADD10C,0.500000*%\nD10*\nX0Y0D02*\nX{int(width*1e6)}Y0D01*\nX{int(width*1e6)}Y{int(height*1e6)}D01*\nX0Y{int(height*1e6)}D01*\nX0Y0D01*\nM02*\n")
    drill = fab / f"{project.name}.drl"
    atomic_write_text(drill, "M48\nMETRIC,TZ\nT01C1.000\n%\nT01\nX010000Y010000\nX150000Y010000\nX010000Y090000\nX150000Y090000\nM30\n")
    deterministic_zip(fab / "gerbers.zip", [(gerber, gerber.name)])
    deterministic_zip(fab / "drill.zip", [(drill, drill.name)])
    output = io.StringIO(); writer = csv.writer(output); writer.writerow(("Reference", "Value", "MPN", "Footprint", "Quantity"))
    for item in graph["components"]: writer.writerow((item["ref"], item["value"], item["mpn"], item["footprint"], 1))
    atomic_write_text(fab / "bom.csv", output.getvalue())
    output = io.StringIO(); writer = csv.writer(output); writer.writerow(("Ref", "PosX", "PosY", "Rotation", "Side"))
    for i, item in enumerate(graph["components"]): writer.writerow((item["ref"], 12 + (i % 6) * 24, 12 + (i // 6) * 18, 0, "Top"))
    atomic_write_text(fab / "pick_and_place.csv", output.getvalue())
    atomic_write_text(fab / "assembly_drawing.svg", f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}mm" height="{height}mm" viewBox="0 0 {width} {height}"><rect width="{width}" height="{height}" fill="white" stroke="black"/><text x="5" y="8" font-size="4">{project.name} assembly</text></svg>\n')
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


def build_firmware_reference(project: Path) -> GateReport:
    app = project / "firmware" / "reference"
    build = app / "build"; build.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(["cmake", "-S", str(app), "-B", str(build), "-G", "Ninja"], capture_output=True, text=True, check=False)
    if completed.returncode == 0:
        completed = subprocess.run(["cmake", "--build", str(build)], capture_output=True, text=True, check=False)
    failures = [] if completed.returncode == 0 else [Failure(FailureCategory.FIRMWARE_ERROR, "build_failure", completed.stderr[-4000:])]
    return GateReport("reference_firmware_build", Status.PASS if not failures else Status.FAIL, failures, artifacts=[str(build / "bringup_tests")], backend={"name": "reference-host-bsp", "returncode": completed.returncode, "stderr": completed.stderr[-4000:]})
