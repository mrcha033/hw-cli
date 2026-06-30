from __future__ import annotations

from typing import Any

# Hand-tuned anchor coordinates for the robotics-controller reference design.
# These are the authoritative placement seed. Changes should be driven by
# explicit placement or physical-correctness gates so downstream routing and
# mechanical checks keep a traceable reason for coordinate movement.
_ANCHORS: dict[str, tuple[float, float]] = {
    "J1": (20.0, 5.0), "F1": (38.0, 5.0), "Q1": (56.0, 5.0), "D1": (74.0, 5.0),
    "U3": (92.0, 5.0), "U4": (112.0, 18.0), "U5": (132.0, 18.0),
    "J5": (120.0, 95.0), "D2": (120.0, 86.0), "R4": (60.0, 27.0), "U6": (90.0, 27.0),
    "J3": (80.0, 5.0), "R1": (104.0, 20.0), "U2": (22.0, 48.0), "R2": (42.0, 48.0),
    "R3": (58.0, 48.0), "J2": (140.0, 95.0), "Q2": (124.0, 48.0), "J4": (104.0, 69.0),
    "U1": (70.0, 37.0),
}

_SENSOR_DATA_LOGGER_ANCHORS: dict[str, tuple[float, float]] = {
    "J1": (18.0, 4.0),
    "F1": (26.0, 4.0),   # fuse, between USB-C and ideal-diode on power path
    "Q1": (34.0, 4.0),   # reverse-polarity ideal-diode, before regulator
    "D1": (30.0, 10.0),
    "U3": (44.0, 12.0),
    "U1": (44.0, 44.0),
    "U2": (18.0, 28.0),
    "J2": (58.0, 44.0),
    "R1": (18.0, 38.0),
    "R2": (27.0, 38.0),
    "R3": (50.0, 25.0),
    "C1": (35.0, 39.0),
    "C2": (44.0, 39.0),
    "C9": (56.0, 14.0),
}


def _seed_table() -> dict[str, tuple[tuple[float, float], str]]:
    """Provenance-tagged seed coordinates.

    Each entry maps a reference to ``((x_mm, y_mm), source)`` where ``source``
    records how the seed coordinate was derived: a hand-tuned anchor, a
    decoupling-capacitor row, or a connector pushed to a board edge.
    """
    table: dict[str, tuple[tuple[float, float], str]] = {
        ref: (xy, "curated_anchor") for ref, xy in _ANCHORS.items()
    }
    for index in range(10):
        table[f"C{index + 1}"] = ((24.0 + (index % 5) * 22.0, 76.0 + (index // 5) * 13.0), "decoupling_row_seed")
    for index in range(1, 13):
        side_index = (index - 1) % 6
        table[f"J{index + 10}"] = ((9.0 if index <= 6 else 151.0, 7.0 + side_index * 15.0), "connector_edge_seed")
    return table


def _sensor_data_logger_seed_table() -> dict[str, tuple[tuple[float, float], str]]:
    return {
        ref: (xy, "sensor_data_logger_anchor")
        for ref, xy in _SENSOR_DATA_LOGGER_ANCHORS.items()
    }


def _grid_fallback(index: int) -> tuple[float, float]:
    return (10.0 + (index % 7) * 20.0, 10.0 + (index // 7) * 15.0)


def component_positions(graph: dict[str, Any]) -> dict[str, tuple[float, float]]:
    """Coordinate lookup (ref -> (x_mm, y_mm)).

    Prefers ``pcb_position_mm`` written by the placement pipeline so that all
    downstream consumers (reference backend, fabrication export) use the same
    coordinates the placement gate validated.  Falls back to the seed table,
    then to the deterministic grid.
    """
    table = _seed_table_for_graph(graph)
    positions: dict[str, tuple[float, float]] = {}
    for index, item in enumerate(graph.get("components", [])):
        ref = item["ref"]
        if "pcb_position_mm" in item:
            pos = item["pcb_position_mm"]
            positions[ref] = (float(pos[0]), float(pos[1]))
        elif ref in table:
            positions[ref] = table[ref][0]
        else:
            positions[ref] = _grid_fallback(index)
    return positions


def placement_sources(graph: dict[str, Any]) -> dict[str, str]:
    """Provenance tag for each placed reference (mirrors ``component_positions``)."""
    table = _seed_table_for_graph(graph)
    return {
        item["ref"]: table[item["ref"]][1] if item["ref"] in table else "grid_fallback"
        for item in graph.get("components", [])
    }


_BLE_SENSOR_NODE_ANCHORS: dict[str, tuple[float, float]] = {
    # Power path: left edge top to right edge top
    "J1":  (12.0, 4.0),   # USB-C, top-left edge
    "F1":  (17.0, 4.0),   # fuse, next in power path
    "Q1":  (24.0, 4.0),   # reverse-polarity mosfet
    "U2":  (30.0, 4.0),   # LiPo charger, near power path
    "BT1": (43.0, 4.0),   # JST LiPo connector, top-right
    "R4":  (37.0, 4.0),   # Charge-set resistor, beside charger
    "C3":  (44.0, 10.0),  # 10uF VBAT bulk cap, near BT1; avoids the D1-2 USB_DP pad
    # LDO between charger rail and MCU
    "LD1": (32.0, 11.0),  # AP2112K LDO, between charger (top) and MCU (centre)
    # Centralised MCU + close decoupling
    "U1":  (25.0, 28.0),  # nRF52840 MCU, near rear edge for integral antenna clearance
    "C1":  (25.0, 12.0),  # 100nF decoupling, directly above MCU V3V3 pin
    # C2 was at (31,19): C2-2 GND landed at (33,19) = U1-5 I2C_SCL (same cell),
    # blocking freerouting from accessing the I2C_SCL pad. Moved to (26,19) so
    # C2-2 GND lands near the MCU GND cluster without sharing the component center.
    "C2":  (27.0, 19.0),  # 100nF decoupling, beside MCU GND cluster (avoids I2C_SCL pad)
    "C4":  (19.0, 19.0),  # 10uF V3V3 bulk cap, left of MCU
    # Sensors on left, away from nRF52840 RF antenna (top-right)
    "U5":  (10.0, 24.0),  # SHT31 temp/humidity, left of MCU and away from RF area
    "R1":  (10.0, 13.0),  # I2C SCL pull-up, near sensors
    "R2":  (15.0, 13.0),  # I2C SDA pull-up, near sensors
    # Fuel gauge and USB ESD on right; U3 moved UP to (40,6) so that U3-4 GND (index 3,
    # pitch=3 high-current layout) lands at y=15 instead of y=27, clearing the y=24-28
    # signal routing channel between U1 and J2.
    "U3":  (40.0, 6.0),   # BQ27441 fuel gauge; GND pad at y=6+3*3=15, above signal channel
    "D1":  (12.0, 10.0),  # USB ESD TVS, between USB-C and MCU USB pins
    "R3":  (38.0, 25.0),  # LED resistor, lower-right
    # Debug header: placed at the left side of the board (x=6), clear of U1 which starts at x=25.
    # J2 row-0 pads land at x=6..18, row-1 at (6,30). Nearest mounting hole H3(3.5,31.5) is 2.9mm away.
    "J2":  (6.0, 28.0),   # SWD header, pads x=6..18mm row0 and (6,30) row1
}


def _ble_sensor_node_seed_table() -> dict[str, tuple[tuple[float, float], str]]:
    return {
        ref: (xy, "ble_sensor_node_anchor")
        for ref, xy in _BLE_SENSOR_NODE_ANCHORS.items()
    }


_RP2040_USB_HID_ANCHORS: dict[str, tuple[float, float]] = {
    # Board: 51x21 mm, usable area 1..50 x 1..20.
    # U2 (RP2040, 20 pins): 7-col placeholder, pads at (18-30, 4-8).
    # U3 (Flash, 8 pins): 7-col placeholder, pads at (33-45, 2-4).
    # QSPI_D3 path: U2(18,4) to U3(45,2); other QSPI paths are <= 20 mm.
    "J1": (3.0,  10.0),   # USB-C, left edge; pads (3-9, 10)
    "D1": (10.0, 10.0),   # USB ESD, inline on USB data path; pads (10-14, 10)
    "U1": (3.0,  15.0),   # 3V3 LDO, near USB_VBUS; pads (3-7, 15)
    "U2": (18.0,  4.0),   # RP2040 MCU, center; pads (18-30, 4-8)
    "U3": (33.0,  2.0),   # 2 MB QSPI Flash, right of MCU; pads (33-45, 2-4)
    "X1": (33.0, 10.0),   # 12 MHz crystal, near MCU XIN/XOUT; pads (33-35, 10)
    "J2": (18.0, 12.0),   # SWD 10-pin, below MCU; pads (18-30, 12-14)
    "C1": (3.0,   5.0),   # 100 nF decoupling; pads (3-5, 5)
    "C2": (7.0,   5.0),   # 100 nF decoupling; pads (7-9, 5)
    "C3": (11.0,  5.0),   # 10 uF bulk cap; pads (11-13, 5)
    "C4": (9.0,  15.0),   # 10 uF USB_VBUS input bulk cap, near USB/LDO input path.
}


def _rp2040_usb_hid_seed_table() -> dict[str, tuple[tuple[float, float], str]]:
    return {
        ref: (xy, "rp2040_usb_hid_anchor")
        for ref, xy in _RP2040_USB_HID_ANCHORS.items()
    }


def _seed_table_for_graph(graph: dict[str, Any]) -> dict[str, tuple[tuple[float, float], str]]:
    architecture = graph.get("design_basis", {}).get("architecture")
    if architecture == "nrf52840_ble_sensor":
        return _ble_sensor_node_seed_table()
    if architecture == "esp32s3_usb_i2c_sensor_data_logger":
        return _sensor_data_logger_seed_table()
    if architecture in {"rp2040_usb_hid_qspi_flash", "rp2040_qspi_usb_device"}:
        return _rp2040_usb_hid_seed_table()
    return _seed_table()
