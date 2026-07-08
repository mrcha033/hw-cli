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
        elif item.get("category") == "usb_cc_pulldown":
            positions[ref] = _usb_c_rd_seed_position(item, graph, positions, table)
        elif item.get("category") == "xtal_cap":
            positions[ref] = _xtal_cap_seed_position(item, graph, positions, table)
        elif ref in table:
            positions[ref] = table[ref][0]
        else:
            positions[ref] = _grid_fallback(index)
    return positions


def placement_sources(graph: dict[str, Any]) -> dict[str, str]:
    """Provenance tag for each placed reference (mirrors ``component_positions``)."""
    table = _seed_table_for_graph(graph)
    return {
        item["ref"]: (
            "usb_c_rd_connector_seed"
            if item.get("category") == "usb_cc_pulldown" and item["ref"] not in table
            else "crystal_load_cap_seed"
            if item.get("category") == "xtal_cap" and item["ref"] not in table
            else table[item["ref"]][1] if item["ref"] in table else "grid_fallback"
        )
        for item in graph.get("components", [])
    }


def _usb_c_rd_seed_position(
    item: dict[str, Any],
    graph: dict[str, Any],
    positions: dict[str, tuple[float, float]],
    table: dict[str, tuple[tuple[float, float], str]],
) -> tuple[float, float]:
    cc_nets = {pin.get("net") for pin in item.get("pins", []) if str(pin.get("net", "")).startswith("USB_CC")}
    for connector in graph.get("components", []):
        connector_ref = connector.get("ref")
        if not connector_ref or connector_ref == item.get("ref"):
            continue
        connector_nets = {pin.get("net") for pin in connector.get("pins", [])}
        if not (cc_nets & connector_nets):
            continue
        if connector_ref in positions:
            x_mm, y_mm = positions[connector_ref]
        elif connector_ref in table:
            x_mm, y_mm = table[connector_ref][0]
        else:
            continue
        x_offset = 4.0 if "USB_CC1" in cc_nets else 8.0
        return (x_mm + x_offset, y_mm + 3.0)
    return _grid_fallback(len(positions))


def _xtal_cap_seed_position(
    item: dict[str, Any],
    graph: dict[str, Any],
    positions: dict[str, tuple[float, float]],
    table: dict[str, tuple[tuple[float, float], str]],
) -> tuple[float, float]:
    cap_nets = {pin.get("net") for pin in item.get("pins", []) if pin.get("net") and pin.get("net") != "GND"}
    for crystal in graph.get("components", []):
        crystal_ref = crystal.get("ref")
        category = str(crystal.get("category", ""))
        if not crystal_ref or "crystal" not in category:
            continue
        crystal_nets = {pin.get("net") for pin in crystal.get("pins", []) if pin.get("net")}
        if not (cap_nets & crystal_nets):
            continue
        if crystal_ref in positions:
            x_mm, y_mm = positions[crystal_ref]
        elif crystal_ref in table:
            x_mm, y_mm = table[crystal_ref][0]
        else:
            continue
        y_offset = -3.0 if any(str(net).endswith(("XIN", "XIN32")) for net in cap_nets) else 3.0
        return (x_mm + 3.0, y_mm + y_offset)
    return _grid_fallback(len(positions))


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
    "C5": (36.0,  7.0),   # XIN load cap, beside crystal.
    "C6": (36.0, 13.0),   # XOUT load cap, beside crystal.
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


def _usb_hid_controller_seed_table() -> dict[str, tuple[tuple[float, float], str]]:
    anchors = dict(_RP2040_USB_HID_ANCHORS)
    # usb_hid_controller reuses the RP2040 topology but its refs differ:
    # U1 is the RP2040 MCU and U2 is the LDO. Keep the physical roles aligned
    # with the RP2040 seed so oscillator and USB placement stay grounded.
    anchors["U1"] = _RP2040_USB_HID_ANCHORS["U2"]
    anchors["U2"] = _RP2040_USB_HID_ANCHORS["U1"]
    anchors.pop("X1", None)
    anchors.pop("C5", None)
    anchors["Y1"] = (33.0, 10.0)
    anchors["C6"] = (36.0, 7.0)
    anchors["C7"] = (36.0, 13.0)
    return {
        ref: (xy, "usb_hid_controller_anchor")
        for ref, xy in anchors.items()
    }


_SAMD21_SENSOR_HUB_ANCHORS: dict[str, tuple[float, float]] = {
    # Board: 50x30 mm. USB-C must stay on the front edge; SWD exits the right edge.
    "J1": (12.0, 3.0),   # USB-C, front edge
    "D1": (18.0, 3.0),   # USB ESD, connector-side of the protected USB pair
    "U1": (28.0, 5.0),   # 3V3 LDO, close to VBUS but thermally separated from sensors/MCU
    "C3": (34.0, 5.0),   # V3V3 bulk cap beside the regulator output
    "U2": (24.0, 17.0),  # ATSAMD21G18A, central for USB/SWD/I2C fanout
    "X1": (18.0, 20.0),  # 32.768 kHz crystal, close to XIN32/XOUT32
    "C4": (21.0, 17.0),  # XIN32 load cap near crystal and MCU pins
    "C5": (21.0, 23.0),  # XOUT32 load cap near crystal and MCU pins
    "C6": (18.0, 24.0),  # VDDCORE cap near MCU core rail
    "C1": (20.0, 14.0),  # V3V3 decoupling near MCU power pins
    "C2": (34.0, 16.0),  # V3V3 decoupling near I2C sensors
    "U3": (36.0, 12.0),  # LSM6DSOX IMU on the sensor side of the bus
    "U4": (36.0, 21.0),  # BME280, separated from LDO heat source
    "R1": (29.0, 22.0),  # I2C SCL pull-up near sensors
    "R2": (29.0, 26.0),  # I2C SDA pull-up near sensors
    "J2": (47.0, 21.0),  # SWD 10-pin header, right edge
}


def _samd21_sensor_hub_seed_table() -> dict[str, tuple[tuple[float, float], str]]:
    return {
        ref: (xy, "samd21_sensor_hub_anchor")
        for ref, xy in _SAMD21_SENSOR_HUB_ANCHORS.items()
    }


_AVR_32U4_HID_ANCHORS: dict[str, tuple[float, float]] = {
    # Board: 55x30 mm. USB-C enters from the left edge, ATmega32U4 is central,
    # and the 16 MHz crystal sits beside the XTAL1/XTAL2 pins for USB timing.
    "J1": (5.0, 10.0),
    "R2": (9.0, 13.0),
    "R3": (13.0, 13.0),
    "D1": (12.0, 10.0),
    "U1": (28.0, 14.0),
    "Y1": (36.0, 14.0),
    "C3": (24.0, 9.0),
    "C4": (28.0, 9.0),
    "C5": (18.0, 6.0),
    "C6": (23.0, 19.0),
    "J2": (50.0, 18.0),
    "R1": (34.0, 20.0),
}


def _avr_32u4_hid_seed_table() -> dict[str, tuple[tuple[float, float], str]]:
    return {
        ref: (xy, "avr_32u4_hid_anchor")
        for ref, xy in _AVR_32U4_HID_ANCHORS.items()
    }


def _seed_table_for_graph(graph: dict[str, Any]) -> dict[str, tuple[tuple[float, float], str]]:
    architecture = graph.get("design_basis", {}).get("architecture")
    if architecture == "nrf52840_ble_sensor":
        return _ble_sensor_node_seed_table()
    if architecture == "esp32s3_usb_i2c_sensor_data_logger":
        return _sensor_data_logger_seed_table()
    if architecture == "atsamd21g18a_lsm6dsox_bme280":
        return _samd21_sensor_hub_seed_table()
    if architecture == "rp2040_usb_hid_qspi_flash":
        return _usb_hid_controller_seed_table()
    if architecture == "rp2040_qspi_usb_device":
        return _rp2040_usb_hid_seed_table()
    if architecture == "atmega32u4_native_usb_hid":
        return _avr_32u4_hid_seed_table()
    return _seed_table()
