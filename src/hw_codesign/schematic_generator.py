from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

import kicad_sch_api as ksa

UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# Minimum vertical gap (mm) between the bottom of one symbol and the top of the next.
_SYMBOL_GAP_MM = 5.08


def generate_kicad_schematic(name: str, graph: dict[str, Any], output: Path) -> None:
    schematic = ksa.create_schematic(name)
    components = sorted(graph["components"], key=lambda item: _reference_key(item["ref"]))
    ordinary = [item for item in components if item["ref"] != "U1"]

    # Compute y positions adaptively so symbols never overlap.
    # kicad_sch_api places Conn_01xN with pin ceil(N/2) at the passed y-coordinate.
    # Pin 1 is above that center by (ceil(N/2) - 1) * 2.54 mm.
    positions: dict[str, tuple[float, float]] = {}
    y_cursor = 22.0  # nominal y of pin-1 of the first symbol
    for index, item in enumerate(ordinary):
        n = len(item["pins"])
        center_pin = (n + 1) // 2          # ceil(N/2) — the pin placed at passed y
        above_mm = (center_pin - 1) * 2.54  # distance from pin-1 to center
        col = index // 10
        x = 35.0 + col * 52.0
        positions[item["ref"]] = (x, y_cursor + above_mm)
        # Advance cursor past the last pin of this symbol plus the gap.
        y_cursor += (n - 1) * 2.54 + _SYMBOL_GAP_MM
    positions["U1"] = (282.0, 105.0)

    for item in components:
        pins = item["pins"]
        n = len(pins)
        symbol = schematic.components.add(
            f"Connector_Generic:Conn_01x{n:02d}",
            item["ref"],
            item["value"],
            position=positions[item["ref"]],
            footprint=item["footprint"],
            MPN=item["mpn"],
            Manufacturer=item["manufacturer"],
            Supplier_SKU=item["supplier_sku"],
        )
        local_pins = {pin["number"]: pin["position"] for pin in symbol.list_pins()}
        for seq_idx, pin in enumerate(pins, 1):
            local = local_pins.get(str(seq_idx))
            if local is None:
                raise ValueError(f"Missing generated schematic pin {item['ref']}.{pin['number']} (seq {seq_idx})")
            point = (symbol.position.x + local.x, symbol.position.y - local.y)
            label_point = (point[0] - 5.08, point[1])
            schematic.add_wire(start=point, end=label_point)
            schematic.add_label(pin["net"], position=label_point)

    schematic.add_text(
        f"{name}: generated typed electrical graph ({len(components)} components, {len(graph['nets'])} nets)",
        position=(20.0, 8.0),
        size=1.27,
    )
    schematic.save(str(output))
    _normalize_uuids(output, name)


def _normalize_uuids(path: Path, namespace: str) -> None:
    text = path.read_text(encoding="utf-8")
    replacements: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        source = match.group(0)
        if source not in replacements:
            replacements[source] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"hw-codesign:{namespace}:{len(replacements)}"))
        return replacements[source]

    path.write_text(UUID_PATTERN.sub(replace, text), encoding="utf-8")


def _reference_key(reference: str) -> tuple[str, int]:
    match = re.fullmatch(r"([A-Za-z]+)(\d+)", reference)
    return (match.group(1), int(match.group(2))) if match else (reference, 0)
