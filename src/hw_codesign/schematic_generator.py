from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

import kicad_sch_api as ksa

UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def generate_kicad_schematic(name: str, graph: dict[str, Any], output: Path) -> None:
    schematic = ksa.create_schematic(name)
    components = sorted(graph["components"], key=lambda item: _reference_key(item["ref"]))
    ordinary = [item for item in components if item["ref"] != "U1"]
    positions = {
        item["ref"]: (35.0 + (index // 10) * 52.0, 22.0 + (index % 10) * 30.48)
        for index, item in enumerate(ordinary)
    }
    positions["U1"] = (282.0, 105.0)

    for item in components:
        pins = item["pins"]
        symbol_pin_count = _symbol_pin_count(pins)
        symbol = schematic.components.add(
            f"Connector_Generic:Conn_01x{symbol_pin_count:02d}",
            item["ref"],
            item["value"],
            position=positions[item["ref"]],
            footprint=item["footprint"],
            MPN=item["mpn"],
            Manufacturer=item["manufacturer"],
            Supplier_SKU=item["supplier_sku"],
        )
        local_pins = {pin["number"]: pin["position"] for pin in symbol.list_pins()}
        connected_numbers: set[str] = set()
        for pin in pins:
            local = local_pins.get(str(pin["number"]))
            if local is None:
                raise ValueError(f"Missing generated schematic pin {item['ref']}.{pin['number']}")
            point = (symbol.position.x + local.x, symbol.position.y - local.y)
            label_point = (point[0] - 5.08, point[1])
            schematic.add_wire(start=point, end=label_point)
            schematic.add_label(pin["net"], position=label_point)
            connected_numbers.add(str(pin["number"]))
        for pin_number, local_pos in local_pins.items():
            if pin_number not in connected_numbers:
                point = (symbol.position.x + local_pos.x, symbol.position.y - local_pos.y)
                schematic.no_connects.add(point)

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


def _symbol_pin_count(pins: list[dict[str, Any]]) -> int:
    numeric = [int(str(pin["number"])) for pin in pins if str(pin.get("number", "")).isdigit()]
    return max(numeric, default=len(pins))


def _reference_key(reference: str) -> tuple[str, int]:
    match = re.fullmatch(r"([A-Za-z]+)(\d+)", reference)
    return (match.group(1), int(match.group(2))) if match else (reference, 0)
