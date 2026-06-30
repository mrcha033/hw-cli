from __future__ import annotations

from collections import defaultdict
from typing import Any


def pin(number: str | int, name: str, net: str, role: str) -> dict[str, Any]:
    return {"number": str(number), "name": name, "net": net, "role": role, "voltage_domain": _domain(net) if role in {"power_in", "power_out", "ground"} else None}


def component(ref: str, category: str, value: str, mpn: str, footprint: str, pins: list[dict[str, Any]], **metadata: Any) -> dict[str, Any]:
    return {
        "ref": ref,
        "category": category,
        "value": value,
        "mpn": mpn,
        "footprint": footprint,
        "pins": pins,
        "lifecycle": metadata.pop("lifecycle", "active"),
        "manufacturer": metadata.pop("manufacturer", "specified"),
        "supplier_sku": metadata.pop("supplier_sku", mpn),
        "substitute_mpn": metadata.pop("substitute_mpn", None),
        **metadata,
    }


def build_controller_graph(spec: dict[str, Any]) -> dict[str, Any]:
    channels = int(spec["actuation"]["motor_channels"])
    signal_nets = ["ESTOP_IN", "CAN_RX", "CAN_TX", "I2C_IMU_SCL", "I2C_IMU_SDA", "IMU_INT", "SWDIO", "SWCLK", "NRST", "USB_DP", "USB_DM"]
    for index in range(1, channels + 1):
        signal_nets.extend((f"MOTOR{index}_PWM", f"MOTOR{index}_CURRENT", f"MOTOR{index}_ENC"))
    mcu_pins = [pin(1, "VDD", "V3V3", "power_in"), pin(2, "VSS", "GND", "ground")]
    for number, net in enumerate(signal_nets, 3):
        role = "analog" if net.endswith("CURRENT") else "bidirectional"
        mcu_pins.append(pin(number, net, net, role))
    components = [
        component("J1", "power_input", "24V INPUT", "Molex-42820-2212", "MicroFit_2Pin", [pin(1, "VBAT", "VBAT_RAW", "power_in"), pin(2, "GND", "GND", "ground")], manufacturer="Molex", substitute_mpn="Molex-43650-0200"),
        component("F1", "fuse", "80A SERVICE FUSE", "Littelfuse-0498080.M", "MIDI_Fuse", [pin(1, "IN", "VBAT_RAW", "passive"), pin(2, "OUT", "VBAT_FUSED", "passive")], manufacturer="Littelfuse", substitute_mpn="Eaton-BK-AMG-80"),
        component("Q1", "reverse_polarity", "IDEAL DIODE", "LM74700QDBVRQ1", "SOT23-6", [pin(1, "ANODE", "VBAT_FUSED", "power_in"), pin(2, "CATHODE", "VBAT", "power_out"), pin(3, "GND", "GND", "ground")], manufacturer="Texas Instruments", substitute_mpn="LTC4359IMS8"),
        component("D1", "tvs", "33V TVS", "SMCJ33A", "SMCJ", [pin(1, "K", "VBAT", "passive"), pin(2, "A", "GND", "ground")], manufacturer="Littelfuse", substitute_mpn="SMCJ33A-E3/57T"),
        component("U3", "efuse", "eFuse", "TPS26630RGET", "VQFN24", [pin(1, "IN", "VBAT", "power_in"), pin(2, "OUT", "VSYS", "power_out"), pin(3, "GND", "GND", "ground"), pin(4, "SHDN", "ESTOP_GATE", "input")], manufacturer="Texas Instruments", substitute_mpn="LTC4368IDD-2"),
        component("U4", "regulator", "5V Buck", "LM76005RNPR", "WQFN30", [pin(1, "VIN", "VSYS", "power_in"), pin(2, "VOUT", "V5", "power_out"), pin(3, "GND", "GND", "ground")], manufacturer="Texas Instruments", substitute_mpn="LT8645SIV"),
        component("U5", "regulator", "3V3 Buck", "TPS62133RGTR", "VQFN16", [pin(1, "VIN", "V5", "power_in"), pin(2, "VOUT", "V3V3", "power_out"), pin(3, "GND", "GND", "ground")], manufacturer="Texas Instruments", substitute_mpn="AP63203WU-7"),
        component("U1", "mcu", "STM32H743", "STM32H743VIT6", "LQFP100", mcu_pins, manufacturer="STMicroelectronics", substitute_mpn="STM32H753VIT6"),
        component("U2", "imu", "ICM-42688-P", "ICM-42688-P", "LGA14", [pin(1, "VDD", "V3V3", "power_in"), pin(2, "GND", "GND", "ground"), pin(3, "SCL", "I2C_IMU_SCL", "open_drain"), pin(4, "SDA", "I2C_IMU_SDA", "open_drain"), pin(5, "INT1", "IMU_INT", "output")], manufacturer="TDK InvenSense", substitute_mpn="BMI088"),
        component("U6", "can", "CAN PHY", "TCAN1042HGVDRQ1", "SOIC8", [pin(1, "VCC", "V5", "power_in"), pin(2, "GND", "GND", "ground"), pin(3, "RXD", "CAN_RX", "output"), pin(4, "TXD", "CAN_TX", "input"), pin(5, "CANH", "CANH", "bidirectional"), pin(6, "CANL", "CANL", "bidirectional")], manufacturer="Texas Instruments", substitute_mpn="TJA1042TK/3"),
        component("J2", "estop", "E-STOP", "Phoenix-1935161", "Terminal_3Pin", [pin(1, "V3V3", "V3V3", "power_out"), pin(2, "ESTOP", "ESTOP_IN", "input"), pin(3, "GND", "GND", "ground")], manufacturer="Phoenix Contact", substitute_mpn="WAGO-250-203"),
        component("Q2", "safety_gate", "E-STOP GATE", "SN74LVC1G97DBVR", "SOT23-6", [pin(1, "VCC", "V3V3", "power_in"), pin(2, "IN", "ESTOP_IN", "input"), pin(3, "OUT", "ESTOP_GATE", "output"), pin(4, "GND", "GND", "ground")], manufacturer="Texas Instruments", substitute_mpn="74LVC1G97GV"),
        component("J3", "can_connector", "CAN", "Molex-43650-0300", "MicroFit_3Pin", [pin(1, "CANH", "CANH", "bidirectional"), pin(2, "CANL", "CANL", "bidirectional"), pin(3, "GND", "GND", "ground")], manufacturer="Molex", substitute_mpn="Phoenix-1935174"),
        component("R1", "termination", "120R", "RC0603FR-07120RL", "R0603", [pin(1, "A", "CANH", "passive"), pin(2, "B", "CANL", "passive")], manufacturer="Yageo", substitute_mpn="CRCW0603120RFKEA"),
        component("J4", "debug", "SWD", "Samtec-FTSH-105-01-L-DV-K", "Cortex_Debug_10Pin", [pin(1, "VREF", "V3V3", "power_out"), pin(2, "SWDIO", "SWDIO", "bidirectional"), pin(3, "GND", "GND", "ground"), pin(4, "SWCLK", "SWCLK", "input"), pin(5, "NRST", "NRST", "bidirectional")], manufacturer="Samtec", substitute_mpn="Harwin-M50-3600542"),
        component("J5", "usb", "USB-C", "USB4105-GF-A", "USB_C_16Pin", [pin(1, "VBUS", "USB_VBUS", "power_in"), pin(2, "GND", "GND", "ground"), pin(3, "D+", "USB_DP_RAW", "bidirectional"), pin(4, "D-", "USB_DM_RAW", "bidirectional")], manufacturer="GCT", substitute_mpn="USB4085-GF-A"),
        component("D2", "usb_esd", "USB ESD", "USBLC6-2SC6", "SOT23-6", [pin(1, "DP_IN", "USB_DP_RAW", "bidirectional"), pin(2, "DP_OUT", "USB_DP", "bidirectional"), pin(3, "DM_IN", "USB_DM_RAW", "bidirectional"), pin(4, "DM_OUT", "USB_DM", "bidirectional"), pin(5, "GND", "GND", "ground")], manufacturer="STMicroelectronics", substitute_mpn="TPD2EUSB30DRTR"),
        component("R4", "discharge", "100K", "RC0603FR-07100KL", "R0603", [pin(1, "VBUS", "USB_VBUS", "passive"), pin(2, "GND", "GND", "ground")], manufacturer="Yageo", substitute_mpn="CRCW0603100KFKEA"),
        component("R2", "pullup", "4K7", "RC0603FR-074K7L", "R0603", [pin(1, "VCC", "V3V3", "passive"), pin(2, "SCL", "I2C_IMU_SCL", "passive")], manufacturer="Yageo", substitute_mpn="CRCW06034K70FKEA"),
        component("R3", "pullup", "4K7", "RC0603FR-074K7L", "R0603", [pin(1, "VCC", "V3V3", "passive"), pin(2, "SDA", "I2C_IMU_SDA", "passive")], manufacturer="Yageo", substitute_mpn="CRCW06034K70FKEA"),
    ]
    for index in range(1, channels + 1):
        components.append(component(f"J{index + 10}", "motor_io", f"MOTOR_IO_{index}", "Molex-43045-0800", "MicroFit_8Pin", [
            pin(1, "V5", "V5", "power_out"), pin(2, "GND", "GND", "ground"), pin(3, "PWM", f"MOTOR{index}_PWM", "output"),
            pin(4, "CURRENT", f"MOTOR{index}_CURRENT", "analog"), pin(5, "ENC", f"MOTOR{index}_ENC", "input"), pin(6, "ESTOP", "ESTOP_GATE", "output"),
        ], manufacturer="Molex", substitute_mpn="Molex-43025-0800"))
    for index in range(1, 9):
        components.append(component(f"C{index}", "decoupling", "100nF", "GRM188R71C104KA01D", "C0603", [pin(1, "VCC", "V3V3", "passive"), pin(2, "GND", "GND", "ground")], manufacturer="Murata", substitute_mpn="CC0603KRX7R9BB104"))
    components.extend((
        component("C9", "bulk_cap", "22uF", "GRM31CR61E226ME15L", "C1206", [pin(1, "VCC", "V5", "passive"), pin(2, "GND", "GND", "ground")], manufacturer="Murata", substitute_mpn="CL31A226KAHNNNE"),
        component("C10", "bulk_cap", "10uF 50V", "GRM32ER71H106KA12L", "C1210", [pin(1, "VCC", "VSYS", "passive"), pin(2, "GND", "GND", "ground")], manufacturer="Murata", substitute_mpn="C3225X7R1H106K250AB"),
    ))
    net_classes = {"GND": "ground", "VBAT_RAW": "power", "VBAT_FUSED": "power", "VBAT": "power", "VSYS": "power", "V5": "power", "V3V3": "power", "USB_VBUS": "power", "CANH": "can", "CANL": "can"}
    endpoints: dict[str, list[str]] = defaultdict(list)
    for item in components:
        for item_pin in item["pins"]:
            endpoints[item_pin["net"]].append(f"{item['ref']}.{item_pin['number']}")
    nets = []
    for name in sorted(endpoints):
        signal_class = net_classes.get(name, "analog" if name.endswith("CURRENT") else "signal")
        nets.append({"name": name, "signal_class": signal_class, "voltage_domain": _domain(name), "connected_pins": endpoints[name], "required_track_width_mm": 2.0 if signal_class == "power" and name.startswith("VBAT") else (0.5 if signal_class == "power" else 0.2)})
    return {"components": components, "nets": nets, "design_basis": {"motor_topology": "external_driver_modules", "board_carries_motor_power": False, "max_simultaneous_peak_channels": min(channels, 10), "architecture": "protected_controller_and_external_driver_io"}}


def build_sensor_data_logger_graph(spec: dict[str, Any]) -> dict[str, Any]:
    components = [
        component("J1", "power_input", "USB-C POWER", "USB4105-GF-A", "USB_C_16Pin", [
            pin(1, "VBUS", "USB_VBUS", "power_in"),
            pin(2, "GND", "GND", "ground"),
            pin(3, "D+", "USB_DP_RAW", "bidirectional"),
            pin(4, "D-", "USB_DM_RAW", "bidirectional"),
        ], manufacturer="GCT"),
        component("F1", "fuse", "500mA Fuse", "Littelfuse-0498080.M", "MIDI", [
            pin(1, "IN", "USB_VBUS", "passive"),
            pin(2, "OUT", "USB_FUSED", "passive"),
        ], manufacturer="Littelfuse"),
        component("Q1", "reverse_polarity", "Ideal Diode", "LM74700QDBVRQ1", "SOT23-6", [
            pin(1, "ANODE", "USB_FUSED", "power_in"),
            pin(2, "CATHODE", "USB_PROT", "power_out"),
            pin(3, "GND", "GND", "ground"),
        ], manufacturer="Texas Instruments"),
        component("D1", "tvs", "USB ESD", "USBLC6-2SC6", "SOT23-6", [
            pin(1, "DP_IN", "USB_DP_RAW", "bidirectional"),
            pin(2, "DP_OUT", "USB_DP", "bidirectional"),
            pin(3, "DM_IN", "USB_DM_RAW", "bidirectional"),
            pin(4, "DM_OUT", "USB_DM", "bidirectional"),
            pin(5, "GND", "GND", "ground"),
        ], manufacturer="STMicroelectronics"),
        component("U3", "regulator", "3V3 Buck", "TPS62133RGTR", "VQFN16", [
            pin(1, "VIN", "USB_PROT", "power_in"),
            pin(2, "VOUT", "V3V3", "power_out"),
            pin(3, "GND", "GND", "ground"),
        ], manufacturer="Texas Instruments"),
        component("U1", "mcu", "ESP32-S3-WROOM-1", "ESP32-S3-WROOM-1-N8", "RF_Module:ESP32-S3-WROOM-1", [
            pin(1, "GND", "GND", "ground"),
            pin(2, "3V3", "V3V3", "power_in"),
            {**pin(3, "EN", "ESP_EN", "input"), "mcu_pin": "EN"},
            {**pin(12, "IO8", "I2C_IMU_SCL", "open_drain"), "mcu_pin": "GPIO8"},
            {**pin(13, "USB_D-", "USB_DM", "bidirectional"), "mcu_pin": "USB_D-"},
            {**pin(14, "USB_D+", "USB_DP", "bidirectional"), "mcu_pin": "USB_D+"},
            {**pin(15, "IO3", "I2C_IMU_SDA", "open_drain"), "mcu_pin": "GPIO3"},
            {**pin(27, "IO0", "BOOT", "input"), "mcu_pin": "GPIO0"},
            {**pin(36, "RXD0", "UART_RX", "input"), "mcu_pin": "RXD0"},
            {**pin(37, "TXD0", "UART_TX", "output"), "mcu_pin": "TXD0"},
            {**pin(39, "IO1", "IMU_INT", "input"), "mcu_pin": "GPIO1"},
            pin(40, "GND", "GND", "ground"),
            pin(41, "GND", "GND", "ground"),
        ], manufacturer="Espressif Systems"),
        component("U2", "imu", "ICM-42688-P", "ICM-42688-P", "LGA14", [
            pin(1, "VDD", "V3V3", "power_in"),
            pin(2, "GND", "GND", "ground"),
            pin(3, "SCL", "I2C_IMU_SCL", "open_drain"),
            pin(4, "SDA", "I2C_IMU_SDA", "open_drain"),
            pin(5, "INT1", "IMU_INT", "output"),
        ], manufacturer="TDK InvenSense"),
        component("J2", "debug", "UART DEBUG", "Samtec-FTSH-105-01-L-DV-K", "Cortex_Debug_10Pin", [
            pin(1, "VREF", "V3V3", "power_out"),
            pin(2, "TXD", "UART_TX", "output"),
            pin(3, "RXD", "UART_RX", "input"),
            pin(4, "BOOT", "BOOT", "input"),
            pin(5, "GND", "GND", "ground"),
        ], manufacturer="Samtec"),
        component("R1", "pullup", "4K7", "RC0603FR-074K7L", "R0603", [pin(1, "VCC", "V3V3", "passive"), pin(2, "SCL", "I2C_IMU_SCL", "passive")], manufacturer="Yageo"),
        component("R2", "pullup", "4K7", "RC0603FR-074K7L", "R0603", [pin(1, "VCC", "V3V3", "passive"), pin(2, "SDA", "I2C_IMU_SDA", "passive")], manufacturer="Yageo"),
        component("R3", "pullup", "4K7", "RC0603FR-074K7L", "R0603", [pin(1, "VCC", "V3V3", "passive"), pin(2, "EN", "ESP_EN", "passive")], manufacturer="Yageo"),
        component("C1", "decoupling", "100nF", "GRM188R71C104KA01D", "C0603", [pin(1, "VCC", "V3V3", "passive"), pin(2, "GND", "GND", "ground")], manufacturer="Murata", decoupling_target_ref="U1"),
        component("C2", "decoupling", "100nF", "GRM188R71C104KA01D", "C0603", [pin(1, "VCC", "V3V3", "passive"), pin(2, "GND", "GND", "ground")], manufacturer="Murata", decoupling_target_ref="U1"),
        component("C9", "bulk_cap", "22uF", "GRM31CR61E226ME15L", "C1206", [pin(1, "VCC", "V3V3", "passive"), pin(2, "GND", "GND", "ground")], manufacturer="Murata"),
    ]
    net_classes = {"GND": "ground", "USB_VBUS": "power", "USB_FUSED": "power", "USB_PROT": "power", "V3V3": "power", "USB_DP": "usb", "USB_DM": "usb", "USB_DP_RAW": "usb", "USB_DM_RAW": "usb"}
    endpoints: dict[str, list[str]] = defaultdict(list)
    for item in components:
        for item_pin in item["pins"]:
            endpoints[item_pin["net"]].append(f"{item['ref']}.{item_pin['number']}")
    nets = [
        {
            "name": name,
            "signal_class": net_classes.get(name, "i2c" if name.startswith("I2C_") else "signal"),
            "voltage_domain": _domain(name),
            "connected_pins": sorted(pins),
            "required_track_width_mm": 0.5 if net_classes.get(name) == "power" else (0.2 if net_classes.get(name) == "ground" else 0.15),
        }
        for name, pins in sorted(endpoints.items())
    ]
    return {
        "components": components,
        "nets": nets,
        "design_basis": {
            "architecture": "esp32s3_usb_i2c_sensor_data_logger",
            "usb_powered": True,
            "integral_antenna_keepout_required": True,
            "board_carries_motor_power": False,
        },
    }


def build_ble_sensor_node_graph(spec: dict[str, Any]) -> dict[str, Any]:
    # Sequential pin numbers for schematic (Connector_Generic size = len(pins)).
    # mcu_pin field records the physical package pin / GPIO name used in DTS.
    mcu_pins = [
        pin(1,  "VDD",         "V3V3",     "power_in"),
        pin(2,  "VSS",         "GND",      "ground"),
        pin(3,  "VDDPA",       "V3V3",     "power_in"),
        pin(4,  "VDDMAIN",     "V3V3",     "power_in"),
        {**pin(5,  "P0.02",   "I2C_SCL",  "open_drain"),  "mcu_pin": "P0.02"},
        {**pin(6,  "P0.03",   "I2C_SDA",  "open_drain"),  "mcu_pin": "P0.03"},
        {**pin(7,  "P0.04",   "CHG_STAT", "input"),       "mcu_pin": "P0.04"},
        {**pin(8,  "P0.05",   "TEMP_INT", "input"),       "mcu_pin": "P0.05"},
        {**pin(9,  "P0.06",   "FUEL_ALRT","input"),       "mcu_pin": "P0.06"},
        {**pin(10, "P0.08",   "LED_BLE",  "output"),      "mcu_pin": "P0.08"},
        {**pin(11, "NRESET",  "NRST",     "bidirectional"),"mcu_pin": "NRESET"},
        {**pin(12, "P0.28",   "UART_TX",  "output"),      "mcu_pin": "P0.28"},
        {**pin(13, "P0.29",   "UART_RX",  "input"),       "mcu_pin": "P0.29"},
        {**pin(14, "SWDCLK",  "SWDCLK",   "input"),       "mcu_pin": "SWDCLK"},
        {**pin(15, "SWDIO",   "SWDIO",    "bidirectional"),"mcu_pin": "SWDIO"},
        {**pin(16, "SWO",     "SWO",      "output"),      "mcu_pin": "SWO"},
        {**pin(17, "USB_DP",  "USB_DP",   "bidirectional"),"mcu_pin": "D+"},
        {**pin(18, "USB_DM",  "USB_DM",   "bidirectional"),"mcu_pin": "D-"},
    ]
    components = [
        component("J1", "power_input", "USB-C CHARGE", "USB4105-GF-A", "USB_C_16Pin", [
            pin(1, "VBUS", "USB_VBUS", "power_in"),
            pin(2, "GND", "GND", "ground"),
            pin(3, "D+", "USB_DP_RAW", "bidirectional"),
            pin(4, "D-", "USB_DM_RAW", "bidirectional"),
        ], manufacturer="GCT"),
        component("F1", "fuse", "500mA Fuse", "Littelfuse-0498080.M", "MIDI", [
            pin(1, "IN", "USB_VBUS", "passive"),
            pin(2, "OUT", "USB_FUSED", "passive"),
        ], manufacturer="Littelfuse"),
        component("Q1", "reverse_polarity", "Ideal Diode", "LM74700QDBVRQ1", "SOT23-6", [
            pin(1, "ANODE", "USB_FUSED", "power_in"),
            pin(2, "CATHODE", "USB_PROT", "power_out"),
            pin(3, "GND", "GND", "ground"),
        ], manufacturer="Texas Instruments"),
        component("U2", "charger", "LiPo Charger", "BQ24079RGTT", "SOT-23-8", [
            pin(1, "IN", "USB_PROT", "power_in"),
            pin(2, "VSS", "GND", "ground"),
            pin(3, "EN1", "V3V3", "input"),
            pin(4, "EN2", "GND", "input"),
            pin(5, "OUT", "VBAT", "power_out"),
            pin(6, "STAT", "CHG_STAT", "output"),
            pin(7, "TE", "V3V3", "input"),
            pin(8, "ISET", "CHG_ISET", "passive"),
        ], manufacturer="Texas Instruments"),
        component("BT1", "battery", "LiPo 400mAh", "S2B-PH-K-S", "JST_PH_S2B", [
            pin(1, "VBAT", "VBAT", "power_in"),
            pin(2, "GND", "GND", "ground"),
        ], manufacturer="JST"),
        component("U3", "fuel_gauge", "Fuel Gauge", "BQ27441DRZR-G1A", "VSON-9", [
            pin(1, "SDA", "I2C_SDA", "open_drain"),
            pin(2, "SCL", "I2C_SCL", "open_drain"),
            pin(3, "GPO", "FUEL_ALRT", "output"),
            pin(4, "VSS", "GND", "ground"),
            pin(5, "SRN", "VBAT", "passive"),
            pin(6, "SRP", "VBAT", "passive"),
            pin(7, "BAT", "VBAT", "power_in"),
            pin(8, "VCC", "V3V3", "power_in"),
            pin(9, "REGIN", "VBAT", "passive"),
        ], manufacturer="Texas Instruments"),
        component("LD1", "regulator", "3V3 LDO", "AP2112K-3.3TRG1", "SOT-23-5", [
            pin(1, "EN", "V3V3", "input"),
            pin(2, "GND", "GND", "ground"),
            pin(3, "VIN", "VBAT", "power_in"),
            pin(4, "NC", "GND", "ground"),
            pin(5, "VOUT", "V3V3", "power_out"),
        ], manufacturer="Diodes Incorporated"),
        component("U1", "mcu", "nRF52840", "nRF52840-QIAA", "Nordic_nRF52840:nRF52840-QIAA", mcu_pins, manufacturer="Nordic Semiconductor"),
        component("U5", "env_sensor", "Temp/Humidity", "SHT31-DIS-B2.5KS", "DFN-8", [
            pin(1, "SDA", "I2C_SDA", "open_drain"),
            pin(2, "ADDR", "GND", "input"),
            pin(3, "ALERT", "TEMP_INT", "output"),
            pin(4, "SCL", "I2C_SCL", "open_drain"),
            pin(5, "VDD", "V3V3", "power_in"),
            pin(6, "nRESET", "V3V3", "input"),
            pin(7, "R", "GND", "passive"),
            pin(8, "VSS", "GND", "ground"),
        ], manufacturer="Sensirion"),
        component("J2", "debug", "SWD DEBUG", "Samtec-FTSH-105-01-L-DV-K", "Cortex_Debug_10Pin", [
            # GND on pin-1 (leftmost pad, x=33): spanning-tree routes LEFT toward C2 instead of RIGHT
            # through the signal channel. Signals in U1 left-to-right x-order → zero route crossings.
            pin(1, "GND_IN", "GND", "ground"),
            pin(2, "SWDIO", "SWDIO", "bidirectional"),
            pin(3, "SWO", "SWO", "output"),
            pin(4, "NRST", "NRST", "bidirectional"),
            pin(5, "TX", "UART_TX", "output"),
            pin(6, "VREF", "V3V3", "power_out"),
            pin(7, "SWDCLK", "SWDCLK", "input"),
            pin(8, "RX", "UART_RX", "input"),
        ], manufacturer="Samtec"),
        component("R1", "pullup", "4K7", "RC0603FR-074K7L", "R0603", [pin(1, "VCC", "V3V3", "passive"), pin(2, "SCL", "I2C_SCL", "passive")], manufacturer="Yageo"),
        component("R2", "pullup", "4K7", "RC0603FR-074K7L", "R0603", [pin(1, "VCC", "V3V3", "passive"), pin(2, "SDA", "I2C_SDA", "passive")], manufacturer="Yageo"),
        component("R3", "led_resistor", "1K", "RC0603FR-071KL", "R0603", [pin(1, "A", "V3V3", "passive"), pin(2, "K", "LED_BLE", "passive")], manufacturer="Yageo"),
        component("R4", "charge_set", "10K", "RC0603FR-0710KL", "R0603", [pin(1, "A", "CHG_ISET", "passive"), pin(2, "GND", "GND", "passive")], manufacturer="Yageo"),
        component("C1", "decoupling", "100nF", "GRM188R71C104KA01D", "C0603", [pin(1, "VCC", "V3V3", "passive"), pin(2, "GND", "GND", "ground")], manufacturer="Murata"),
        component("C2", "decoupling", "100nF", "GRM188R71C104KA01D", "C0603", [pin(1, "VCC", "V3V3", "passive"), pin(2, "GND", "GND", "ground")], manufacturer="Murata", decoupling_target_ref="U1"),
        component("C3", "bulk_cap", "10uF", "GRM188R60J106ME47D", "C0603", [pin(1, "VCC", "VBAT", "passive"), pin(2, "GND", "GND", "ground")], manufacturer="Murata"),
        component("C4", "bulk_cap", "10uF", "GRM188R60J106ME47D", "C0603", [pin(1, "VCC", "V3V3", "passive"), pin(2, "GND", "GND", "ground")], manufacturer="Murata"),
        component("D1", "tvs", "USB ESD", "USBLC6-2SC6", "SOT23-6", [
            pin(1, "DP_IN", "USB_DP_RAW", "bidirectional"),
            pin(2, "DP_OUT", "USB_DP", "bidirectional"),
            pin(3, "DM_IN", "USB_DM_RAW", "bidirectional"),
            pin(4, "DM_OUT", "USB_DM", "bidirectional"),
            pin(5, "GND", "GND", "ground"),
        ], manufacturer="STMicroelectronics"),
    ]
    net_classes = {
        "GND": "ground", "USB_VBUS": "power", "USB_FUSED": "power", "USB_PROT": "power",
        "VBAT": "power", "V3V3": "power",
        "USB_DP": "usb", "USB_DM": "usb", "USB_DP_RAW": "usb", "USB_DM_RAW": "usb",
        "I2C_SCL": "i2c", "I2C_SDA": "i2c",
    }
    endpoints: dict[str, list[str]] = defaultdict(list)
    for item in components:
        for item_pin in item["pins"]:
            endpoints[item_pin["net"]].append(f"{item['ref']}.{item_pin['number']}")
    nets = [
        {
            "name": name,
            "signal_class": net_classes.get(name, "signal"),
            "voltage_domain": _domain(name),
            "connected_pins": sorted(pins),
            "required_track_width_mm": 0.5 if net_classes.get(name) == "power" else (0.2 if net_classes.get(name) == "ground" else 0.15),
        }
        for name, pins in sorted(endpoints.items())
    ]
    return {
        "components": components,
        "nets": nets,
        "design_basis": {
            "architecture": "nrf52840_ble_sensor",
            "lipo_powered": True,
            "usb_charging": True,
            "integral_pcb_antenna_required": True,
            "board_carries_motor_power": False,
        },
    }


def _domain(name: str) -> str | None:
    if name == "GND": return "GND"
    if name.startswith("VBAT") or name == "VSYS": return "VBAT"
    if name == "V5": return "V5"
    if name == "USB_VBUS": return "USB_5V"
    return "V3V3"


_WELL_KNOWN_NET_CLASSES: dict[str, str] = {
    "GND": "ground",
    "V3V3": "power", "V5": "power", "VSYS": "power",
    "VBAT": "power", "VBAT_RAW": "power", "VBAT_FUSED": "power", "USB_VBUS": "power",
    "USB_DP": "usb", "USB_DM": "usb", "USB_DP_RAW": "usb", "USB_DM_RAW": "usb",
    "I2C_SCL": "i2c", "I2C_SDA": "i2c", "I2C_IMU_SCL": "i2c", "I2C_IMU_SDA": "i2c",
    "CANH": "can", "CANL": "can",
    "PHASE_A": "motor", "PHASE_B": "motor", "PHASE_C": "motor",
    "LORA_CLK": "spi", "LORA_MOSI": "spi", "LORA_MISO": "spi",
    "FLASH_CLK": "signal", "FLASH_CS": "signal",
    "ETH_CLK": "spi", "ETH_MOSI": "spi", "ETH_MISO": "spi", "ETH_CS": "signal",
    "ETH_TXDP": "differential", "ETH_TXDM": "differential",
    "ETH_RXDP": "differential", "ETH_RXDM": "differential",
    "XTAL_XIN": "signal", "XTAL_XOUT": "signal",
    "SHUNT_HI": "signal", "SHUNT_LO": "signal",
}


def _block_to_component(block: dict[str, Any]) -> dict[str, Any]:
    """Convert an agent-authored electronics block to a component dict for graph merging."""
    connections = block.get("connections", {})
    pins_list: list[dict[str, Any]] = []
    for pin_num, (pin_name, net_name) in enumerate(connections.items(), 1):
        if net_name in {"V3V3", "V5", "VSYS", "VBAT", "VBAT_RAW", "USB_VBUS"}:
            pin_role = "power_in"
        elif net_name == "GND":
            pin_role = "ground"
        else:
            pin_role = "bidirectional"
        pins_list.append(pin(pin_num, pin_name, net_name, pin_role))
    extra = {
        k: v for k, v in block.items()
        if k not in {"ref", "id", "category", "value", "mpn", "footprint", "connections"}
    }
    return component(
        ref=block["ref"],
        category=block.get("category", "agent_block"),
        value=block.get("value", block.get("ref", "AGENT BLOCK")),
        mpn=block.get("mpn", "UNSPECIFIED"),
        footprint=block.get("footprint", ""),
        pins=pins_list,
        **extra,
    )


def build_usb_hid_controller_graph(spec: dict[str, Any]) -> dict[str, Any]:
    mcu_pins = [
        pin(1,  "VDD",        "V3V3",       "power_in"),
        pin(2,  "VSS",        "GND",        "ground"),
        {**pin(3,  "USB_DM",     "USB_DM",     "bidirectional"),  "mcu_pin": "USB_DM"},
        {**pin(4,  "USB_DP",     "USB_DP",     "bidirectional"),  "mcu_pin": "USB_DP"},
        {**pin(5,  "XIN",        "XTAL_XIN",   "passive"),         "mcu_pin": "XIN"},
        {**pin(6,  "QSPI_SCLK", "FLASH_CLK",  "output"),          "mcu_pin": "QSPI_SCLK"},
        {**pin(7,  "QSPI_CS",   "FLASH_CS",   "output"),          "mcu_pin": "QSPI_CS"},
        {**pin(8,  "QSPI_SD0",  "FLASH_D0",   "bidirectional"),   "mcu_pin": "QSPI_SD0"},
        {**pin(9,  "QSPI_SD1",  "FLASH_D1",   "bidirectional"),   "mcu_pin": "QSPI_SD1"},
        {**pin(10, "QSPI_SD2",  "FLASH_D2",   "bidirectional"),   "mcu_pin": "QSPI_SD2"},
        {**pin(11, "QSPI_SD3",  "FLASH_D3",   "bidirectional"),   "mcu_pin": "QSPI_SD3"},
        {**pin(12, "RUN",        "MCU_RUN",    "input"),           "mcu_pin": "RUN"},
        {**pin(13, "GPIO23",     "BOOTSEL",    "input"),           "mcu_pin": "GPIO23"},
        {**pin(14, "GPIO25",     "STATUS_LED", "output"),          "mcu_pin": "GPIO25"},
        {**pin(15, "SWCLK",      "SWCLK",      "input"),           "mcu_pin": "SWCLK"},
        {**pin(16, "SWDIO",      "SWDIO",      "bidirectional"),   "mcu_pin": "SWDIO"},
        {**pin(17, "GPIO0",      "HID_GPIO0",  "bidirectional"),   "mcu_pin": "GPIO0"},
        {**pin(18, "GPIO1",      "HID_GPIO1",  "bidirectional"),   "mcu_pin": "GPIO1"},
        {**pin(19, "GPIO2",      "HID_GPIO2",  "bidirectional"),   "mcu_pin": "GPIO2"},
        {**pin(20, "GPIO3",      "HID_GPIO3",  "bidirectional"),   "mcu_pin": "GPIO3"},
        {**pin(21, "GPIO4",      "HID_GPIO4",  "bidirectional"),   "mcu_pin": "GPIO4"},
        {**pin(22, "GPIO5",      "HID_GPIO5",  "bidirectional"),   "mcu_pin": "GPIO5"},
    ]
    components = [
        component("J1", "power_input", "USB-C", "USB4105-GF-A", "USB_C_16Pin", [
            pin(1, "VBUS", "USB_VBUS", "power_in"),
            pin(2, "GND", "GND", "ground"),
            pin(3, "D+", "USB_DP_RAW", "bidirectional"),
            pin(4, "D-", "USB_DM_RAW", "bidirectional"),
        ], manufacturer="GCT"),
        component("D1", "tvs", "USB ESD", "USBLC6-2SC6", "SOT23-6", [
            pin(1, "DP_IN", "USB_DP_RAW", "bidirectional"),
            pin(2, "DP_OUT", "USB_DP", "bidirectional"),
            pin(3, "DM_IN", "USB_DM_RAW", "bidirectional"),
            pin(4, "DM_OUT", "USB_DM", "bidirectional"),
            pin(5, "GND", "GND", "ground"),
        ], manufacturer="STMicroelectronics"),
        component("U1", "mcu", "RP2040", "RP2040", "QFN-56", mcu_pins, manufacturer="Raspberry Pi Ltd"),
        component("U2", "regulator", "3V3 LDO", "AP2112K-3.3TRG1", "SOT-23-5", [
            pin(1, "EN", "V3V3", "input"),
            pin(2, "GND", "GND", "ground"),
            pin(3, "VIN", "USB_VBUS", "power_in"),
            pin(4, "NC", "GND", "ground"),
            pin(5, "VOUT", "V3V3", "power_out"),
        ], manufacturer="Diodes Incorporated"),
        component("U3", "flash", "16Mbit QSPI Flash", "W25Q16JVSSIQ", "SOIC-8", [
            pin(1, "CS",   "FLASH_CS",  "input"),
            pin(2, "DO",   "FLASH_D1",  "output"),
            pin(3, "WP",   "FLASH_D2",  "input"),
            pin(4, "GND",  "GND",       "ground"),
            pin(5, "DI",   "FLASH_D0",  "input"),
            pin(6, "CLK",  "FLASH_CLK", "input"),
            pin(7, "HOLD", "FLASH_D3",  "input"),
            pin(8, "VCC",  "V3V3",      "power_in"),
        ], manufacturer="Winbond Electronics"),
        component("Y1", "crystal", "12MHz Crystal", "ABM8-12.000MHZ-B2-T", "HC-49S-SMD", [
            pin(1, "XIN",  "XTAL_XIN",  "passive"),
            pin(2, "XOUT", "XTAL_XOUT", "passive"),
        ], manufacturer="Abracon"),
        component("J2", "debug", "SWD DEBUG", "Samtec-FTSH-105-01-L-DV-K", "Cortex_Debug_10Pin", [
            pin(1, "VREF",  "V3V3",  "power_out"),
            pin(2, "SWDIO", "SWDIO", "bidirectional"),
            pin(3, "GND",   "GND",   "ground"),
            pin(4, "SWCLK", "SWCLK", "input"),
            pin(5, "RUN",   "MCU_RUN", "bidirectional"),
        ], manufacturer="Samtec"),
        component("R1", "boot_resistor", "10K BOOTSEL", "RC0603FR-0710KL", "R0603", [
            pin(1, "VCC", "V3V3",    "passive"),
            pin(2, "B",   "BOOTSEL", "passive"),
        ], manufacturer="Yageo"),
        component("R2", "pullup", "10K RUN", "RC0603FR-0710KL", "R0603", [
            pin(1, "VCC", "V3V3",   "passive"),
            pin(2, "B",   "MCU_RUN", "passive"),
        ], manufacturer="Yageo"),
        component("R3", "led_resistor", "1K LED", "RC0603FR-071KL", "R0603", [
            pin(1, "A", "V3V3",      "passive"),
            pin(2, "K", "STATUS_LED", "passive"),
        ], manufacturer="Yageo"),
        component("C1", "decoupling", "100nF", "GRM188R71C104KA01D", "C0603", [
            pin(1, "VCC", "V3V3", "passive"), pin(2, "GND", "GND", "ground"),
        ], manufacturer="Murata"),
        component("C2", "decoupling", "100nF", "GRM188R71C104KA01D", "C0603", [
            pin(1, "VCC", "V3V3", "passive"), pin(2, "GND", "GND", "ground"),
        ], manufacturer="Murata"),
        component("C3", "decoupling", "100nF", "GRM188R71C104KA01D", "C0603", [
            pin(1, "VCC", "USB_VBUS", "passive"), pin(2, "GND", "GND", "ground"),
        ], manufacturer="Murata"),
        component("C4", "decoupling", "100nF", "GRM188R71C104KA01D", "C0603", [
            pin(1, "VCC", "V3V3", "passive"), pin(2, "GND", "GND", "ground"),
        ], manufacturer="Murata"),
        component("C5", "bulk_cap", "22uF", "GRM31CR61E226ME15L", "C1206", [
            pin(1, "VCC", "V3V3", "passive"), pin(2, "GND", "GND", "ground"),
        ], manufacturer="Murata"),
    ]
    net_classes = {
        "GND": "ground", "USB_VBUS": "power", "V3V3": "power",
        "USB_DP": "usb", "USB_DM": "usb", "USB_DP_RAW": "usb", "USB_DM_RAW": "usb",
        "FLASH_CLK": "signal", "FLASH_CS": "signal",
        "FLASH_D0": "signal", "FLASH_D1": "signal", "FLASH_D2": "signal", "FLASH_D3": "signal",
    }
    endpoints: dict[str, list[str]] = defaultdict(list)
    for item in components:
        for item_pin in item["pins"]:
            endpoints[item_pin["net"]].append(f"{item['ref']}.{item_pin['number']}")
    nets = [
        {
            "name": name,
            "signal_class": net_classes.get(name, "signal"),
            "voltage_domain": _domain(name),
            "connected_pins": sorted(pins),
            "required_track_width_mm": 0.5 if net_classes.get(name) == "power" else 0.15,
        }
        for name, pins in sorted(endpoints.items())
    ]
    return {
        "components": components,
        "nets": nets,
        "design_basis": {
            "architecture": "rp2040_usb_hid_qspi_flash",
            "usb_powered": True,
            "native_usb_phy": True,
            "qspi_flash_required": True,
            "board_carries_motor_power": False,
        },
    }


def build_lora_sensor_node_graph(spec: dict[str, Any]) -> dict[str, Any]:
    mcu_pins = [
        pin(1,  "VDD",             "V3V3",       "power_in"),
        pin(2,  "VSS",             "GND",         "ground"),
        {**pin(3,  "PA0_ADC",         "ADC_VBAT",   "analog"),        "mcu_pin": "PA0"},
        {**pin(4,  "PA4_SPI1_NSS",    "LORA_NSS",   "output"),         "mcu_pin": "PA4"},
        {**pin(5,  "PA5_SPI1_SCK",    "LORA_CLK",   "output"),         "mcu_pin": "PA5"},
        {**pin(6,  "PA6_SPI1_MISO",   "LORA_MISO",  "input"),          "mcu_pin": "PA6"},
        {**pin(7,  "PA7_SPI1_MOSI",   "LORA_MOSI",  "output"),         "mcu_pin": "PA7"},
        {**pin(8,  "PA8_LORA_RST",    "LORA_NRESET","output"),         "mcu_pin": "PA8"},
        {**pin(9,  "PA9_USART1_TX",   "UART_TX",    "output"),         "mcu_pin": "PA9"},
        {**pin(10, "PA10_USART1_RX",  "UART_RX",    "input"),          "mcu_pin": "PA10"},
        {**pin(11, "PA11_LORA_DIO0",  "LORA_DIO0",  "input"),          "mcu_pin": "PA11"},
        {**pin(12, "PA12_LORA_DIO1",  "LORA_DIO1",  "input"),          "mcu_pin": "PA12"},
        {**pin(13, "SWDIO",           "SWDIO",       "bidirectional"),  "mcu_pin": "SWDIO"},
        {**pin(14, "SWCLK",           "SWCLK",       "input"),          "mcu_pin": "SWCLK"},
        {**pin(15, "PB6_I2C1_SCL",    "I2C_SCL",    "open_drain"),     "mcu_pin": "PB6"},
        {**pin(16, "PB7_I2C1_SDA",    "I2C_SDA",    "open_drain"),     "mcu_pin": "PB7"},
        {**pin(17, "NRST",            "NRST",        "bidirectional"),  "mcu_pin": "NRESET"},
        pin(18, "VDDA",            "V3V3",       "power_in"),
        pin(19, "VSSA",            "GND",         "ground"),
        {**pin(20, "PC13_WKUP",       "WKUP",       "input"),           "mcu_pin": "PC13"},
        {**pin(21, "BOOT0",           "BOOT0",       "input"),          "mcu_pin": "BOOT0"},
    ]
    components = [
        component("J1", "power_input", "LiSOCl2 Battery", "S2B-PH-K-S", "JST_PH_S2B", [
            pin(1, "VBAT", "VBAT", "power_in"),
            pin(2, "GND",  "GND",  "ground"),
        ], manufacturer="JST"),
        component("U1", "mcu", "STM32L071CZ", "STM32L071CZT6", "LQFP-48", mcu_pins, manufacturer="STMicroelectronics"),
        component("U2", "regulator", "3V3 LDO", "AP2112K-3.3TRG1", "SOT-23-5", [
            pin(1, "EN",   "V3V3", "input"),
            pin(2, "GND",  "GND",  "ground"),
            pin(3, "VIN",  "VBAT", "power_in"),
            pin(4, "NC",   "GND",  "ground"),
            pin(5, "VOUT", "V3V3", "power_out"),
        ], manufacturer="Diodes Incorporated"),
        component("U3", "lora_radio", "SX1276 LoRa", "SX1276IMLTRT", "QFN-28", [
            pin(1,  "VDD",     "V3V3",       "power_in"),
            pin(2,  "VSS",     "GND",         "ground"),
            pin(3,  "NSS",     "LORA_NSS",   "input"),
            pin(4,  "MOSI",    "LORA_MOSI",  "input"),
            pin(5,  "MISO",    "LORA_MISO",  "output"),
            pin(6,  "SCK",     "LORA_CLK",   "input"),
            pin(7,  "NRESET",  "LORA_NRESET","input"),
            pin(8,  "DIO0",    "LORA_DIO0",  "output"),
            pin(9,  "DIO1",    "LORA_DIO1",  "output"),
            pin(10, "DIO2",    "LORA_DIO2",  "output"),
            pin(11, "RF_ANT",  "RF_ANT",     "passive"),
            pin(12, "RFIO",    "RF_ANT",     "passive"),
        ], manufacturer="Semtech"),
        component("U4", "env_sensor", "BME280 Env Sensor", "BME280", "LGA-8", [
            pin(1, "VDD",      "V3V3",    "power_in"),
            pin(2, "VDDIO",    "V3V3",    "power_in"),
            pin(3, "GND",      "GND",     "ground"),
            pin(4, "SDI_SDA",  "I2C_SDA", "open_drain"),
            pin(5, "SCK_SCL",  "I2C_SCL", "open_drain"),
            pin(6, "SDO_ADDR", "GND",     "input"),
            pin(7, "CSB",      "V3V3",    "input"),
            pin(8, "GND2",     "GND",     "ground"),
        ], manufacturer="Bosch Sensortec"),
        component("J2", "rf_connector", "RF U.FL", "U.FL-R-SMT-1(01)", "U.FL-SMT", [
            pin(1, "RF",  "RF_ANT", "passive"),
            pin(2, "GND", "GND",    "ground"),
        ], manufacturer="Hirose Electric"),
        component("J3", "debug", "SWD DEBUG", "Samtec-FTSH-105-01-L-DV-K", "Cortex_Debug_10Pin", [
            pin(1, "VREF",  "V3V3",  "power_out"),
            pin(2, "SWDIO", "SWDIO", "bidirectional"),
            pin(3, "GND",   "GND",   "ground"),
            pin(4, "SWCLK", "SWCLK", "input"),
            pin(5, "NRST",  "NRST",  "bidirectional"),
        ], manufacturer="Samtec"),
        component("R1", "resistor_4k7", "4K7 I2C SCL", "RC0603FR-074K7L", "R0603", [
            pin(1, "VCC", "V3V3",    "passive"),
            pin(2, "SCL", "I2C_SCL", "passive"),
        ], manufacturer="Yageo"),
        component("R2", "resistor_4k7", "4K7 I2C SDA", "RC0603FR-074K7L", "R0603", [
            pin(1, "VCC", "V3V3",    "passive"),
            pin(2, "SDA", "I2C_SDA", "passive"),
        ], manufacturer="Yageo"),
        component("R3", "resistor_100k", "100K VBAT_H", "RC0603FR-07100KL", "R0603", [
            pin(1, "A", "VBAT",     "passive"),
            pin(2, "B", "VBAT_DIV", "passive"),
        ], manufacturer="Yageo"),
        component("R4", "resistor_100k", "100K VBAT_L", "RC0603FR-07100KL", "R0603", [
            pin(1, "A", "VBAT_DIV", "passive"),
            pin(2, "B", "GND",       "passive"),
        ], manufacturer="Yageo"),
        component("C1", "decoupling", "100nF", "GRM188R71C104KA01D", "C0603", [
            pin(1, "VCC", "V3V3", "passive"), pin(2, "GND", "GND", "ground"),
        ], manufacturer="Murata"),
        component("C2", "decoupling", "100nF", "GRM188R71C104KA01D", "C0603", [
            pin(1, "VCC", "V3V3", "passive"), pin(2, "GND", "GND", "ground"),
        ], manufacturer="Murata"),
        component("C3", "decoupling", "100nF", "GRM188R71C104KA01D", "C0603", [
            pin(1, "VCC", "V3V3", "passive"), pin(2, "GND", "GND", "ground"),
        ], manufacturer="Murata"),
        component("C4", "decoupling", "100nF", "GRM188R71C104KA01D", "C0603", [
            pin(1, "VCC", "VBAT", "passive"), pin(2, "GND", "GND", "ground"),
        ], manufacturer="Murata"),
        component("C5", "capacitor_10u_50v", "10uF VBAT", "GRM32ER71H106KA12L", "C1210", [
            pin(1, "VCC", "VBAT", "passive"), pin(2, "GND", "GND", "ground"),
        ], manufacturer="Murata"),
    ]
    net_classes = {
        "GND": "ground", "VBAT": "power", "V3V3": "power",
        "I2C_SCL": "i2c", "I2C_SDA": "i2c",
        "LORA_CLK": "spi", "LORA_MOSI": "spi", "LORA_MISO": "spi", "LORA_NSS": "spi",
    }
    endpoints: dict[str, list[str]] = defaultdict(list)
    for item in components:
        for item_pin in item["pins"]:
            endpoints[item_pin["net"]].append(f"{item['ref']}.{item_pin['number']}")
    nets = [
        {
            "name": name,
            "signal_class": net_classes.get(name, "signal"),
            "voltage_domain": _domain(name),
            "connected_pins": sorted(pins),
            "required_track_width_mm": 0.5 if net_classes.get(name) == "power" else 0.15,
        }
        for name, pins in sorted(endpoints.items())
    ]
    return {
        "components": components,
        "nets": nets,
        "design_basis": {
            "architecture": "stm32l071_lora_sx1276_bme280",
            "lipo_powered": True,
            "lorawan_capable": True,
            "ultra_low_power": True,
            "board_carries_motor_power": False,
        },
    }


def build_bldc_esc_graph(spec: dict[str, Any]) -> dict[str, Any]:
    mcu_pins = [
        pin(1,  "VDD",           "V3V3",    "power_in"),
        pin(2,  "VSS",           "GND",      "ground"),
        {**pin(3,  "PA8_PWM_AH",    "PWM_AH",  "output"),  "mcu_pin": "PA8"},
        {**pin(4,  "PB13_PWM_AL",   "PWM_AL",  "output"),  "mcu_pin": "PB13"},
        {**pin(5,  "PA9_PWM_BH",    "PWM_BH",  "output"),  "mcu_pin": "PA9"},
        {**pin(6,  "PB14_PWM_BL",   "PWM_BL",  "output"),  "mcu_pin": "PB14"},
        {**pin(7,  "PA10_PWM_CH",   "PWM_CH",  "output"),  "mcu_pin": "PA10"},
        {**pin(8,  "PB15_PWM_CL",   "PWM_CL",  "output"),  "mcu_pin": "PB15"},
        {**pin(9,  "PA3_CURR_A",    "CURR_A",  "analog"),  "mcu_pin": "PA3"},
        {**pin(10, "PA4_CURR_B",    "CURR_B",  "analog"),  "mcu_pin": "PA4"},
        {**pin(11, "PA5_VBUS_ADC",  "VBUS_ADC","analog"),  "mcu_pin": "PA5"},
        {**pin(12, "PC10_SPI_CLK",  "SPI_CLK", "output"),  "mcu_pin": "PC10"},
        {**pin(13, "PC11_SPI_MISO", "SPI_MISO","input"),   "mcu_pin": "PC11"},
        {**pin(14, "PC12_SPI_MOSI", "SPI_MOSI","output"),  "mcu_pin": "PC12"},
        {**pin(15, "PA15_DRV_CS",   "DRV_CS",  "output"),  "mcu_pin": "PA15"},
        {**pin(16, "PA11_CAN_RX",   "CAN_RX",  "input"),   "mcu_pin": "PA11"},
        {**pin(17, "PA12_CAN_TX",   "CAN_TX",  "output"),  "mcu_pin": "PA12"},
        {**pin(18, "PB8_NFAULT",    "NFAULT",  "input"),   "mcu_pin": "PB8"},
        {**pin(19, "PB9_NSLEEP",    "NSLEEP",  "output"),  "mcu_pin": "PB9"},
        {**pin(20, "SWDIO",         "SWDIO",   "bidirectional"), "mcu_pin": "SWDIO"},
        {**pin(21, "SWCLK",         "SWCLK",   "input"),   "mcu_pin": "SWCLK"},
        {**pin(22, "NRST",          "NRST",    "bidirectional"), "mcu_pin": "NRESET"},
        pin(23, "VDDA",          "V3V3",    "power_in"),
        pin(24, "VSSA",          "GND",      "ground"),
        {**pin(25, "VREF",          "V3V3",    "power_in"), "mcu_pin": "VREF+"},
        {**pin(26, "PB0_FAULT_LED", "FAULT_LED","output"), "mcu_pin": "PB0"},
    ]
    components = [
        component("J1", "power_input", "VBAT INPUT", "Molex-42820-2212", "MicroFit_2Pin", [
            pin(1, "VBAT", "VBAT", "power_in"),
            pin(2, "GND",  "GND",  "ground"),
        ], manufacturer="Molex"),
        component("D1", "tvs", "33V TVS", "SMCJ33A", "SMCJ", [
            pin(1, "K", "VBAT", "passive"),
            pin(2, "A", "GND",  "ground"),
        ], manufacturer="Littelfuse"),
        component("U1", "mcu", "STM32G474", "STM32G474RET6", "LQFP-64", mcu_pins, manufacturer="STMicroelectronics"),
        component("U2", "gate_driver", "DRV8323RS", "DRV8323RSRGZT", "WSON-24", [
            pin(1,  "VM",     "VBAT",    "power_in"),
            pin(2,  "PVDD",   "VBAT",    "power_in"),
            pin(3,  "AGND",   "GND",     "ground"),
            pin(4,  "PGND",   "GND",     "ground"),
            pin(5,  "GH_A",   "GATE_AH", "output"),
            pin(6,  "SH_A",   "PHASE_A", "passive"),
            pin(7,  "GL_A",   "GATE_AL", "output"),
            pin(8,  "GH_B",   "GATE_BH", "output"),
            pin(9,  "SH_B",   "PHASE_B", "passive"),
            pin(10, "GL_B",   "GATE_BL", "output"),
            pin(11, "GH_C",   "GATE_CH", "output"),
            pin(12, "SH_C",   "PHASE_C", "passive"),
            pin(13, "GL_C",   "GATE_CL", "output"),
            pin(14, "nFAULT", "NFAULT",  "output"),
            pin(15, "nSLEEP", "NSLEEP",  "input"),
            pin(16, "CLK",    "SPI_CLK", "input"),
            pin(17, "SDI",    "SPI_MOSI","input"),
            pin(18, "SDO",    "SPI_MISO","output"),
            pin(19, "nSCS",   "DRV_CS",  "input"),
            pin(20, "ISEN_A", "CURR_A",  "output"),
            pin(21, "ISEN_B", "CURR_B",  "output"),
            pin(22, "DVDD",   "V3V3",    "power_in"),
        ], manufacturer="Texas Instruments"),
        component("Q1", "half_bridge", "Phase A FETs", "CSD18540Q5B", "VSON-8", [
            pin(1, "S1", "GND",      "ground"),
            pin(2, "G1", "GATE_AL",  "input"),
            pin(3, "DS", "PHASE_A",  "bidirectional"),
            pin(4, "G2", "GATE_AH",  "input"),
            pin(5, "D2", "VBAT",     "power_in"),
        ], manufacturer="Texas Instruments"),
        component("Q2", "half_bridge", "Phase B FETs", "CSD18540Q5B", "VSON-8", [
            pin(1, "S1", "GND",      "ground"),
            pin(2, "G1", "GATE_BL",  "input"),
            pin(3, "DS", "PHASE_B",  "bidirectional"),
            pin(4, "G2", "GATE_BH",  "input"),
            pin(5, "D2", "VBAT",     "power_in"),
        ], manufacturer="Texas Instruments"),
        component("Q3", "half_bridge", "Phase C FETs", "CSD18540Q5B", "VSON-8", [
            pin(1, "S1", "GND",      "ground"),
            pin(2, "G1", "GATE_CL",  "input"),
            pin(3, "DS", "PHASE_C",  "bidirectional"),
            pin(4, "G2", "GATE_CH",  "input"),
            pin(5, "D2", "VBAT",     "power_in"),
        ], manufacturer="Texas Instruments"),
        component("U4", "regulator", "5V Buck", "LM22678TJ-5.0", "SOP-8", [
            pin(1, "VIN",  "VBAT", "power_in"),
            pin(2, "VOUT", "V5",   "power_out"),
            pin(3, "GND",  "GND",  "ground"),
        ], manufacturer="Texas Instruments"),
        component("U5", "regulator", "3V3 LDO", "AP2112K-3.3TRG1", "SOT-23-5", [
            pin(1, "EN",   "V3V3", "input"),
            pin(2, "GND",  "GND",  "ground"),
            pin(3, "VIN",  "V5",   "power_in"),
            pin(4, "NC",   "GND",  "ground"),
            pin(5, "VOUT", "V3V3", "power_out"),
        ], manufacturer="Diodes Incorporated"),
        component("U6", "can", "CAN PHY", "TCAN1042HGVDRQ1", "SOIC8", [
            pin(1, "VCC",  "V5",    "power_in"),
            pin(2, "GND",  "GND",   "ground"),
            pin(3, "RXD",  "CAN_RX","output"),
            pin(4, "TXD",  "CAN_TX","input"),
            pin(5, "CANH", "CANH",  "bidirectional"),
            pin(6, "CANL", "CANL",  "bidirectional"),
        ], manufacturer="Texas Instruments"),
        component("J2", "motor_connector", "MOTOR PHASES", "Phoenix-1935174", "Terminal_3Pin", [
            pin(1, "A", "PHASE_A", "passive"),
            pin(2, "B", "PHASE_B", "passive"),
            pin(3, "C", "PHASE_C", "passive"),
        ], manufacturer="Phoenix Contact"),
        component("J3", "can_connector", "CAN", "Molex-43650-0300", "MicroFit_3Pin", [
            pin(1, "CANH", "CANH", "bidirectional"),
            pin(2, "CANL", "CANL", "bidirectional"),
            pin(3, "GND",  "GND",  "ground"),
        ], manufacturer="Molex"),
        component("J4", "debug", "SWD DEBUG", "Samtec-FTSH-105-01-L-DV-K", "Cortex_Debug_10Pin", [
            pin(1, "VREF",  "V3V3",  "power_out"),
            pin(2, "SWDIO", "SWDIO", "bidirectional"),
            pin(3, "GND",   "GND",   "ground"),
            pin(4, "SWCLK", "SWCLK", "input"),
            pin(5, "NRST",  "NRST",  "bidirectional"),
        ], manufacturer="Samtec"),
        component("R1", "led_resistor", "1K FAULT", "RC0603FR-071KL", "R0603", [
            pin(1, "A", "V3V3",     "passive"),
            pin(2, "K", "FAULT_LED","passive"),
        ], manufacturer="Yageo"),
        component("R2", "pullup", "10K nFAULT PU", "RC0603FR-0710KL", "R0603", [
            pin(1, "VCC", "V3V3",  "passive"),
            pin(2, "B",   "NFAULT","passive"),
        ], manufacturer="Yageo"),
        component("C1", "decoupling", "100nF", "GRM188R71C104KA01D", "C0603", [
            pin(1, "VCC", "V3V3", "passive"), pin(2, "GND", "GND", "ground"),
        ], manufacturer="Murata"),
        component("C2", "decoupling", "100nF", "GRM188R71C104KA01D", "C0603", [
            pin(1, "VCC", "V3V3", "passive"), pin(2, "GND", "GND", "ground"),
        ], manufacturer="Murata"),
        component("C3", "decoupling", "100nF", "GRM188R71C104KA01D", "C0603", [
            pin(1, "VCC", "V5",   "passive"), pin(2, "GND", "GND", "ground"),
        ], manufacturer="Murata"),
        component("C4", "decoupling", "100nF", "GRM188R71C104KA01D", "C0603", [
            pin(1, "VCC", "VBAT", "passive"), pin(2, "GND", "GND", "ground"),
        ], manufacturer="Murata"),
        component("C5", "bulk_cap", "22uF 5V", "GRM31CR61E226ME15L", "C1206", [
            pin(1, "VCC", "V5",   "passive"), pin(2, "GND", "GND", "ground"),
        ], manufacturer="Murata"),
        component("C6", "capacitor_10u_50v", "10uF VBAT", "GRM32ER71H106KA12L", "C1210", [
            pin(1, "VCC", "VBAT", "passive"), pin(2, "GND", "GND", "ground"),
        ], manufacturer="Murata"),
        component("C7", "capacitor_10u_50v", "10uF VBAT", "GRM32ER71H106KA12L", "C1210", [
            pin(1, "VCC", "VBAT", "passive"), pin(2, "GND", "GND", "ground"),
        ], manufacturer="Murata"),
    ]
    net_classes = {
        "GND": "ground", "VBAT": "power", "V5": "power", "V3V3": "power",
        "CANH": "can", "CANL": "can",
        "PHASE_A": "motor", "PHASE_B": "motor", "PHASE_C": "motor",
        "SPI_CLK": "signal", "SPI_MOSI": "signal", "SPI_MISO": "signal", "DRV_CS": "signal",
    }
    endpoints: dict[str, list[str]] = defaultdict(list)
    for item in components:
        for item_pin in item["pins"]:
            endpoints[item_pin["net"]].append(f"{item['ref']}.{item_pin['number']}")
    nets = []
    for name in sorted(endpoints):
        signal_class = net_classes.get(name, "analog" if name.endswith("CURR") else "signal")
        nets.append({
            "name": name,
            "signal_class": signal_class,
            "voltage_domain": _domain(name),
            "connected_pins": sorted(endpoints[name]),
            "required_track_width_mm": (
                2.0 if signal_class == "motor"
                else 1.0 if signal_class == "power" and name.startswith("VBAT")
                else 0.5 if signal_class == "power"
                else 0.2
            ),
        })
    return {
        "components": components,
        "nets": nets,
        "design_basis": {
            "architecture": "stm32g474_drv8323_3phase_foc_esc",
            "motor_type": "bldc_3phase",
            "gate_driver_spi_configurable": True,
            "integrated_current_sense": True,
            "can_telemetry": True,
            "board_carries_motor_power": True,
        },
    }


def build_esp32_wifi_gateway_graph(spec: dict[str, Any]) -> dict[str, Any]:
    mcu_pins = [
        pin(1,  "EN",            "ESP_EN",   "input"),
        pin(2,  "IO0",           "ESP_IO0",  "bidirectional"),
        pin(3,  "TXD0",          "UART_TX",  "output"),
        pin(4,  "RXD0",          "UART_RX",  "input"),
        pin(5,  "IO18",          "ETH_CLK",  "output"),
        pin(6,  "IO19",          "ETH_MISO", "input"),
        pin(7,  "IO23",          "ETH_MOSI", "output"),
        pin(8,  "IO5",           "ETH_CS",   "output"),
        pin(9,  "IO26",          "ETH_INT",  "input"),
        pin(10, "IO14",          "ETH_RST",  "output"),
        pin(11, "3V3",           "V3V3",     "power_in"),
        pin(12, "GND",           "GND",      "ground"),
    ]
    eth_pins = [
        pin(1,  "MOSI",  "ETH_MOSI",  "input"),
        pin(2,  "MISO",  "ETH_MISO",  "output"),
        pin(3,  "SCLK",  "ETH_CLK",   "input"),
        pin(4,  "SCSn",  "ETH_CS",    "input"),
        pin(5,  "INTn",  "ETH_INT",   "output"),
        pin(6,  "RSTn",  "ETH_RST",   "input"),
        pin(7,  "RXDP",  "ETH_RXDP",  "bidirectional"),
        pin(8,  "RXDM",  "ETH_RXDM",  "bidirectional"),
        pin(9,  "TXDP",  "ETH_TXDP",  "bidirectional"),
        pin(10, "TXDM",  "ETH_TXDM",  "bidirectional"),
        pin(11, "VCC",   "V3V3",      "power_in"),
        pin(12, "GND",   "GND",       "ground"),
    ]
    rj45_pins = [
        pin(1,  "TRD0P", "ETH_TXDP",  "bidirectional"),
        pin(2,  "TRD0M", "ETH_TXDM",  "bidirectional"),
        pin(3,  "TRD1P", "ETH_RXDP",  "bidirectional"),
        pin(6,  "TRD1M", "ETH_RXDM",  "bidirectional"),
    ]
    uart_bridge_pins = [
        pin(1, "UD_PLUS",  "USB_DP",    "bidirectional"),
        pin(2, "UD_MINUS", "USB_DM",    "bidirectional"),
        pin(5, "VCC",      "USB_VBUS",  "power_in"),
        pin(6, "VCCIO",    "V3V3",      "power_in"),
        pin(7, "TXD",      "UART_RX",   "output"),
        pin(8, "RXD",      "UART_TX",   "input"),
        pin(4, "GND",      "GND",       "ground"),
    ]
    usbc_pins = [
        pin(1, "VBUS", "USB_VBUS", "power_in"),
        pin(2, "DP",   "USB_DP",   "bidirectional"),
        pin(3, "DM",   "USB_DM",   "bidirectional"),
        pin(4, "GND",  "GND",      "ground"),
    ]
    tvs_pins = [
        pin(1, "A",  "USB_DP",  "bidirectional"),
        pin(2, "K",  "GND",     "ground"),
        pin(3, "A2", "USB_DM",  "bidirectional"),
    ]
    ldo_pins = [
        pin(1, "VIN",  "USB_VBUS", "power_in"),
        pin(2, "GND",  "GND",      "ground"),
        pin(3, "VOUT", "V3V3",     "power_out"),
    ]
    decap_pins_a = [pin(1, "VCC", "V3V3", "power_in"), pin(2, "GND", "GND", "ground")]
    bulk_pins = [pin(1, "VCC", "V3V3", "power_in"), pin(2, "GND", "GND", "ground")]

    components = [
        component("J1", "power_input",     "USB-C POWER",    "USB4105-GF-A",       "USB_C_16Pin",        usbc_pins),
        component("D1", "tvs",             "USB ESD",        "USBLC6-2SC6",        "SOT23-6",            tvs_pins),
        component("U1", "regulator_3v3",   "3V3 LDO",        "AP2112K-3.3TRG1",    "SOT23-5",            ldo_pins),
        component("U2", "mcu",             "ESP32-WROOM-32E","ESP32-WROOM-32E",     "SMD-38-Module",      mcu_pins),
        component("U3", "eth_controller",  "W5500 ETH",      "W5500",              "LQFP-48",            eth_pins),
        component("U4", "usb_uart_bridge", "CH340N UART",    "CH340N",             "SOP-8",              uart_bridge_pins),
        component("J2", "eth_connector",   "RJ45",           "HR911105A",          "RJ45-Integrated",    rj45_pins),
        component("C1", "decoupling",      "100nF",          "GRM155R71C104KA88D", "0402",               decap_pins_a),
        component("C2", "decoupling",      "100nF",          "GRM155R71C104KA88D", "0402",               decap_pins_a),
        component("C3", "bulk_cap",        "10uF",           "GRM188R60J106ME47D", "0603",               bulk_pins),
    ]
    net_classes = {
        "GND": "ground", "USB_VBUS": "power", "V3V3": "power",
        "USB_DP": "usb", "USB_DM": "usb",
        "ETH_CLK": "spi", "ETH_MOSI": "spi", "ETH_MISO": "spi", "ETH_CS": "signal",
        "ETH_TXDP": "differential", "ETH_TXDM": "differential",
        "ETH_RXDP": "differential", "ETH_RXDM": "differential",
        "UART_TX": "signal", "UART_RX": "signal",
    }
    endpoints: dict[str, list[str]] = defaultdict(list)
    for item in components:
        for item_pin in item["pins"]:
            endpoints[item_pin["net"]].append(f"{item['ref']}.{item_pin['number']}")
    nets = [
        {
            "name": name,
            "signal_class": net_classes.get(name, "signal"),
            "voltage_domain": _domain(name),
            "connected_pins": sorted(pins),
            "required_track_width_mm": 0.5 if net_classes.get(name) == "power" else 0.15,
        }
        for name, pins in sorted(endpoints.items())
    ]
    return {
        "components": components,
        "nets": nets,
        "design_basis": {
            "architecture": "esp32_wroom_w5500_ch340n",
            "mcu_family": "ESP32",
            "wifi_capable": True,
            "ethernet_spi": True,
            "usb_uart_bridge": "CH340N",
            "board_carries_motor_power": False,
        },
    }


def build_avr_32u4_hid_graph(spec: dict[str, Any]) -> dict[str, Any]:
    mcu_pins = [
        pin(1,  "VCC",     "USB_VBUS",  "power_in"),
        pin(2,  "GND",     "GND",       "ground"),
        pin(3,  "AVCC",    "USB_VBUS",  "power_in"),
        pin(4,  "UVCC",    "USB_VBUS",  "power_in"),
        pin(5,  "UGND",    "GND",       "ground"),
        pin(6,  "UCap",    "UCAP",      "passive"),
        pin(7,  "D_MINUS", "USB_DM",    "bidirectional"),
        pin(8,  "D_PLUS",  "USB_DP",    "bidirectional"),
        pin(9,  "XTAL1",   "XTAL_XIN",  "passive"),
        pin(10, "XTAL2",   "XTAL_XOUT", "passive"),
        pin(11, "RESET",   "MCU_RESET", "input"),
        pin(12, "PB0",     "MOSI",      "bidirectional"),
        pin(13, "PB1",     "MISO",      "bidirectional"),
        pin(14, "PB2",     "SCK",       "bidirectional"),
        pin(15, "PB3",     "HWB",       "input"),
        pin(16, "PA0",     "GPIO_PA0",  "bidirectional"),
        pin(17, "PA1",     "GPIO_PA1",  "bidirectional"),
        pin(18, "PA2",     "GPIO_PA2",  "bidirectional"),
        pin(19, "PA3",     "GPIO_PA3",  "bidirectional"),
        pin(20, "PA4",     "GPIO_PA4",  "bidirectional"),
    ]
    usbc_pins = [
        pin(1, "VBUS", "USB_VBUS", "power_in"),
        pin(2, "DP",   "USB_DP_RAW", "bidirectional"),
        pin(3, "DM",   "USB_DM_RAW", "bidirectional"),
        pin(4, "GND",  "GND",        "ground"),
    ]
    tvs_pins = [
        pin(1, "A",  "USB_DP_RAW", "bidirectional"),
        pin(2, "K",  "GND",         "ground"),
        pin(3, "A2", "USB_DM_RAW", "bidirectional"),
    ]
    _tvs_out_pins = [
        pin(1, "A",  "USB_DP",   "bidirectional"),
        pin(2, "K",  "GND",      "ground"),
        pin(3, "A2", "USB_DM",   "bidirectional"),
    ]
    xtal_pins = [
        pin(1, "XIN",  "XTAL_XIN",  "passive"),
        pin(2, "XOUT", "XTAL_XOUT", "passive"),
    ]
    xtal_cap_pins_a = [pin(1, "A", "XTAL_XIN",  "passive"), pin(2, "B", "GND", "ground")]
    xtal_cap_pins_b = [pin(1, "A", "XTAL_XOUT", "passive"), pin(2, "B", "GND", "ground")]
    icsp_pins = [
        pin(1, "P1", "MISO",      "passive"),
        pin(2, "P2", "USB_VBUS",  "power_in"),
        pin(3, "P3", "SCK",       "passive"),
        pin(4, "P4", "MOSI",      "passive"),
        pin(5, "P5", "MCU_RESET", "passive"),
        pin(6, "P6", "GND",       "ground"),
    ]
    hwb_pulldown_pins = [pin(1, "A", "HWB", "passive"), pin(2, "B", "GND", "ground")]
    decap_pins = [pin(1, "VCC", "USB_VBUS", "power_in"), pin(2, "GND", "GND", "ground")]
    bulk_pins   = [pin(1, "VCC", "USB_VBUS", "power_in"), pin(2, "GND", "GND", "ground")]
    ucap_pins   = [pin(1, "VCC", "UCAP",     "passive"), pin(2, "GND", "GND", "ground")]

    components = [
        component("J1", "power_input",  "USB-C POWER",      "USB4105-GF-A",        "USB_C_16Pin",            usbc_pins),
        component("D1", "tvs",          "USB ESD",           "USBLC6-2SC6",         "SOT23-6",                tvs_pins),
        component("U1", "mcu",          "ATmega32U4",        "ATMEGA32U4-AU",        "TQFP-44",                mcu_pins),
        component("Y1", "crystal_16m",  "16MHz XTAL",        "ABM8-16.000MHZ-B2-T", "HC-49S-SMD",             xtal_pins),
        component("C1", "xtal_cap",     "22pF XTAL",         "GRM2165C1H220JA01D",  "0805",                   xtal_cap_pins_a),
        component("C2", "xtal_cap",     "22pF XTAL",         "GRM2165C1H220JA01D",  "0805",                   xtal_cap_pins_b),
        component("C3", "decoupling",   "100nF",             "GRM155R71C104KA88D",  "0402",                   decap_pins),
        component("C4", "decoupling",   "100nF",             "GRM155R71C104KA88D",  "0402",                   decap_pins),
        component("C5", "bulk_cap",     "10uF",              "GRM188R60J106ME47D",  "0603",                   bulk_pins),
        component("C6", "ucap",         "1uF UCAP",          "GRM188R61C105KA93D",  "0603",                   ucap_pins),
        component("J2", "icsp_header",  "ICSP 6-pin",        "WE-61300611021",       "PinHeader-1x6-2.54mm",   icsp_pins),
        component("R1", "hwb_pulldown", "10K HWB",           "RC0603FR-0710KL",      "0603",                   hwb_pulldown_pins),
    ]
    net_classes = {
        "GND": "ground", "USB_VBUS": "power",
        "USB_DP": "usb", "USB_DM": "usb", "USB_DP_RAW": "usb", "USB_DM_RAW": "usb",
        "XTAL_XIN": "signal", "XTAL_XOUT": "signal",
        "MOSI": "spi", "MISO": "spi", "SCK": "spi",
    }
    endpoints: dict[str, list[str]] = defaultdict(list)
    for item in components:
        for item_pin in item["pins"]:
            endpoints[item_pin["net"]].append(f"{item['ref']}.{item_pin['number']}")
    nets = [
        {
            "name": name,
            "signal_class": net_classes.get(name, "signal"),
            "voltage_domain": _domain(name),
            "connected_pins": sorted(pins),
            "required_track_width_mm": 0.5 if net_classes.get(name) == "power" else 0.15,
        }
        for name, pins in sorted(endpoints.items())
    ]
    return {
        "components": components,
        "nets": nets,
        "design_basis": {
            "architecture": "atmega32u4_native_usb_hid",
            "mcu_family": "AVR",
            "native_usb_hid": True,
            "usb_fullspeed": True,
            "icsp_programmable": True,
            "board_carries_motor_power": False,
        },
    }


def build_stm32g0_power_monitor_graph(spec: dict[str, Any]) -> dict[str, Any]:
    mcu_pins = [
        pin(1,  "VDD",    "V3V3",      "power_in"),
        pin(2,  "VSS",    "GND",       "ground"),
        pin(3,  "VDDA",   "V3V3",      "power_in"),
        pin(4,  "PA13",   "SWDIO",     "bidirectional"),
        pin(5,  "PA14",   "SWCLK",     "input"),
        pin(6,  "NRST",   "MCU_NRST",  "input"),
        pin(7,  "PB6",    "I2C_SCL",   "bidirectional"),
        pin(8,  "PB7",    "I2C_SDA",   "bidirectional"),
        pin(9,  "PA9",    "UART_TX",   "output"),
        pin(10, "PA10",   "UART_RX",   "input"),
        pin(11, "PA2",    "GPIO_PA2",  "bidirectional"),
        pin(12, "BOOT0",  "BOOT0_GND", "input"),
    ]
    ina226_pins = [
        pin(1, "IN_PLUS",  "SHUNT_HI",  "input"),
        pin(2, "IN_MINUS", "SHUNT_LO",  "input"),
        pin(3, "A0",       "GND",       "ground"),
        pin(4, "A1",       "GND",       "ground"),
        pin(5, "SDA",      "I2C_SDA",   "bidirectional"),
        pin(6, "SCL",      "I2C_SCL",   "input"),
        pin(7, "ALERT",    "INA_ALERT", "output"),
        pin(8, "VS",       "V3V3",      "power_in"),
    ]
    shunt_pins = [
        pin(1, "A", "SHUNT_HI", "passive"),
        pin(2, "B", "SHUNT_LO", "passive"),
    ]
    oled_pins = [
        pin(1, "GND", "GND",      "ground"),
        pin(2, "VCC", "V3V3",     "power_in"),
        pin(3, "SCL", "I2C_SCL",  "input"),
        pin(4, "SDA", "I2C_SDA",  "bidirectional"),
    ]
    uart_bridge_pins = [
        pin(1, "UD_PLUS",  "USB_DP",   "bidirectional"),
        pin(2, "UD_MINUS", "USB_DM",   "bidirectional"),
        pin(5, "VCC",      "USB_VBUS", "power_in"),
        pin(6, "VCCIO",    "V3V3",     "power_in"),
        pin(7, "TXD",      "UART_RX",  "output"),
        pin(8, "RXD",      "UART_TX",  "input"),
        pin(4, "GND",      "GND",      "ground"),
    ]
    usbc_pins = [
        pin(1, "VBUS", "USB_VBUS", "power_in"),
        pin(2, "DP",   "USB_DP",   "bidirectional"),
        pin(3, "DM",   "USB_DM",   "bidirectional"),
        pin(4, "GND",  "GND",      "ground"),
    ]
    tvs_pins = [
        pin(1, "A",  "USB_DP",  "bidirectional"),
        pin(2, "K",  "GND",     "ground"),
        pin(3, "A2", "USB_DM",  "bidirectional"),
    ]
    ldo_pins = [
        pin(1, "VIN",  "USB_VBUS", "power_in"),
        pin(2, "GND",  "GND",      "ground"),
        pin(3, "VOUT", "V3V3",     "power_out"),
    ]
    swd_pins = [
        pin(1,  "P1",  "USB_VBUS",  "power_in"),
        pin(2,  "P2",  "SWDIO",     "passive"),
        pin(3,  "P3",  "GND",       "ground"),
        pin(4,  "P4",  "SWCLK",     "passive"),
        pin(5,  "P5",  "GND",       "ground"),
        pin(6,  "P6",  "MCU_NRST",  "passive"),
        pin(7,  "P7",  "GND",       "ground"),
        pin(8,  "P8",  "GND",       "ground"),
        pin(9,  "P9",  "GND",       "ground"),
        pin(10, "P10", "GND",       "ground"),
    ]
    i2c_pullup_scl_pins = [pin(1, "A", "V3V3", "power_in"), pin(2, "B", "I2C_SCL", "passive")]
    i2c_pullup_sda_pins = [pin(1, "A", "V3V3", "power_in"), pin(2, "B", "I2C_SDA", "passive")]
    decap_pins = [pin(1, "VCC", "V3V3", "power_in"), pin(2, "GND", "GND", "ground")]
    bulk_pins  = [pin(1, "VCC", "V3V3", "power_in"), pin(2, "GND", "GND", "ground")]

    components = [
        component("J1",  "power_input",       "USB-C POWER",     "USB4105-GF-A",        "USB_C_16Pin",         usbc_pins),
        component("D1",  "tvs",               "USB ESD",         "USBLC6-2SC6",         "SOT23-6",             tvs_pins),
        component("U1",  "regulator_3v3",     "3V3 LDO",         "AP2112K-3.3TRG1",     "SOT23-5",             ldo_pins),
        component("U2",  "mcu",               "STM32G030C8T6",   "STM32G030C8T6",        "LQFP-48",             mcu_pins),
        component("U3",  "power_monitor_ic",  "INA226",          "INA226AIDT",           "SOT-23-8",            ina226_pins),
        component("R1",  "shunt_resistor",    "10m SHUNT",       "RL1206FR-010R01L",     "1206",                shunt_pins),
        component("J2",  "display_ic",        "SSD1306 OLED",    "UG-2864HSWEG01",       "OLED-I2C-4pin",       oled_pins),
        component("U4",  "usb_uart_bridge",   "CH340N UART",     "CH340N",               "SOP-8",               uart_bridge_pins),
        component("J3",  "debug_header",      "SWD 10-pin",      "Samtec-FTSH-105",      "SWD_10Pin_1.27mm",    swd_pins),
        component("R2",  "i2c_pullup",        "4K7 SCL PU",      "RC0603FR-074K7L",      "0603",                i2c_pullup_scl_pins),
        component("R3",  "i2c_pullup",        "4K7 SDA PU",      "RC0603FR-074K7L",      "0603",                i2c_pullup_sda_pins),
        component("C1",  "decoupling",        "100nF",           "GRM155R71C104KA88D",   "0402",                decap_pins),
        component("C2",  "decoupling",        "100nF",           "GRM155R71C104KA88D",   "0402",                decap_pins),
        component("C3",  "bulk_cap",          "10uF",            "GRM188R60J106ME47D",   "0603",                bulk_pins),
    ]
    net_classes = {
        "GND": "ground", "USB_VBUS": "power", "V3V3": "power",
        "USB_DP": "usb", "USB_DM": "usb",
        "I2C_SCL": "i2c", "I2C_SDA": "i2c",
        "UART_TX": "signal", "UART_RX": "signal",
        "SHUNT_HI": "signal", "SHUNT_LO": "signal",
        "SWDIO": "signal", "SWCLK": "signal",
    }
    endpoints: dict[str, list[str]] = defaultdict(list)
    for item in components:
        for item_pin in item["pins"]:
            endpoints[item_pin["net"]].append(f"{item['ref']}.{item_pin['number']}")
    nets = [
        {
            "name": name,
            "signal_class": net_classes.get(name, "signal"),
            "voltage_domain": _domain(name),
            "connected_pins": sorted(pins),
            "required_track_width_mm": 0.5 if net_classes.get(name) == "power" else 0.15,
        }
        for name, pins in sorted(endpoints.items())
    ]
    return {
        "components": components,
        "nets": nets,
        "design_basis": {
            "architecture": "stm32g030_ina226_ssd1306",
            "mcu_family": "STM32G0",
            "power_monitoring": True,
            "display": "SSD1306",
            "i2c_bus_shared": True,
            "board_carries_motor_power": False,
        },
    }


def build_rp2040_usb_device_graph(spec: dict[str, Any]) -> dict[str, Any]:
    usbc_pins = [
        pin(1, "VBUS", "USB_VBUS", "power_in"),
        pin(2, "DP",   "USB_DP_RAW", "bidirectional"),
        pin(3, "DM",   "USB_DM_RAW", "bidirectional"),
        pin(4, "GND",  "GND",      "ground"),
    ]
    tvs_pins = [
        pin(1, "DP_IN",  "USB_DP_RAW", "bidirectional"),
        pin(2, "DP_OUT", "USB_DP",     "bidirectional"),
        pin(3, "DM_IN",  "USB_DM_RAW", "bidirectional"),
        pin(4, "DM_OUT", "USB_DM",     "bidirectional"),
        pin(5, "GND",    "GND",        "ground"),
    ]
    ldo_pins = [
        pin(1, "VIN",  "USB_VBUS", "power_in"),
        pin(2, "GND",  "GND",      "ground"),
        pin(3, "VOUT", "V3V3",     "power_out"),
    ]
    mcu_pins = [
        pin(1,  "QSPI_SD3",  "QSPI_D3",   "bidirectional"),
        pin(2,  "QSPI_SD2",  "QSPI_D2",   "bidirectional"),
        pin(3,  "QSPI_SD1",  "QSPI_MISO", "bidirectional"),
        pin(4,  "QSPI_SD0",  "QSPI_MOSI", "bidirectional"),
        pin(5,  "QSPI_SCK",  "QSPI_CLK",  "output"),
        pin(6,  "QSPI_SS_N","QSPI_CS",   "output"),
        pin(11, "USB_DM",    "USB_DM",    "bidirectional"),
        pin(12, "USB_DP",    "USB_DP",    "bidirectional"),
        pin(16, "TESTEN",    "GND",       "ground"),
        pin(17, "XIN",       "XIN",       "passive"),
        pin(18, "XOUT",      "XOUT",      "passive"),
        pin(33, "GND",       "GND",       "ground"),
        pin(34, "GND",       "GND",       "ground"),
        pin(40, "SWCLK",     "SWCLK",     "input"),
        pin(41, "SWDIO",     "SWDIO",     "bidirectional"),
        pin(44, "DVDD",      "V3V3",      "power_in"),
        pin(45, "VDD",       "V3V3",      "power_in"),
        pin(46, "VDD",       "V3V3",      "power_in"),
        pin(47, "VDD",       "V3V3",      "power_in"),
        pin(48, "VDD",       "V3V3",      "power_in"),
    ]
    flash_pins = [
        pin(1, "~CS",   "QSPI_CS",   "input"),
        pin(2, "IO1",   "QSPI_MISO", "bidirectional"),
        pin(3, "~WP",   "QSPI_D2",   "bidirectional"),
        pin(4, "GND",   "GND",       "ground"),
        pin(5, "IO0",   "QSPI_MOSI", "bidirectional"),
        pin(6, "CLK",   "QSPI_CLK",  "input"),
        pin(7, "~HOLD", "QSPI_D3",   "bidirectional"),
        pin(8, "VCC",   "V3V3",      "power_in"),
    ]
    xtal_pins = [
        pin(1, "XIN",  "XIN",  "passive"),
        pin(2, "XOUT", "XOUT", "passive"),
    ]
    swd_pins = [
        pin(1,  "P1",  "V3V3",  "power_in"),
        pin(2,  "P2",  "SWDIO", "passive"),
        pin(3,  "P3",  "GND",   "ground"),
        pin(4,  "P4",  "SWCLK", "passive"),
        pin(5,  "P5",  "GND",   "ground"),
        pin(6,  "P6",  "GND",   "ground"),
        pin(7,  "P7",  "GND",   "ground"),
        pin(8,  "P8",  "GND",   "ground"),
        pin(9,  "P9",  "GND",   "ground"),
        pin(10, "P10", "GND",   "ground"),
    ]
    decap_pins = [pin(1, "VCC", "V3V3", "power_in"), pin(2, "GND", "GND", "ground")]
    bulk_pins  = [pin(1, "VCC", "V3V3", "power_in"), pin(2, "GND", "GND", "ground")]
    input_bulk_pins = [pin(1, "VCC", "USB_VBUS", "power_in"), pin(2, "GND", "GND", "ground")]

    components = [
        component("J1",  "power_input",  "USB-C POWER",  "USB4105-GF-A",        "USB_C_16Pin",       usbc_pins),
        component("D1",  "tvs",          "USB ESD",      "USBLC6-2SC6",         "SOT23-6",           tvs_pins),
        component("U1",  "regulator_3v3","3V3 LDO",      "AP2112K-3.3TRG1",     "SOT23-5",           ldo_pins),
        component("U2",  "mcu",          "RP2040",        "RP2040",              "QFN-56",            mcu_pins),
        component("U3",  "flash",        "2MB QSPI",     "W25Q16JVSIQ",         "SOIC-8",            flash_pins),
        component("X1",  "crystal_12m",  "12MHz XTAL",   "ABM8-12.000MHZ-B2-T", "HC-49S-SMD",        xtal_pins),
        component("J2",  "debug_header", "SWD 10-pin",   "61201021621",          "PinHeader-2x5",     swd_pins),
        component("C1",  "decoupling",   "100nF",        "GRM155R71C104KA88D",  "0402",              decap_pins),
        component("C2",  "decoupling",   "100nF",        "GRM155R71C104KA88D",  "0402",              decap_pins),
        component("C3",  "bulk_cap",     "10uF",         "GRM188R60J106ME47D",  "0603",              bulk_pins),
        component("C4",  "bulk_cap",     "10uF USB_IN",  "GRM188R60J106ME47D",  "0603",              input_bulk_pins),
    ]
    net_classes = {
        "GND": "ground", "USB_VBUS": "power", "V3V3": "power",
        "USB_DP": "usb", "USB_DM": "usb", "USB_DP_RAW": "usb", "USB_DM_RAW": "usb",
        "QSPI_CLK": "signal", "QSPI_MOSI": "signal", "QSPI_MISO": "signal",
        "QSPI_CS": "signal", "QSPI_D2": "signal", "QSPI_D3": "signal",
        "SWDIO": "signal", "SWCLK": "signal",
        "XIN": "signal", "XOUT": "signal",
    }
    endpoints: dict[str, list[str]] = defaultdict(list)
    for item in components:
        for item_pin in item["pins"]:
            endpoints[item_pin["net"]].append(f"{item['ref']}.{item_pin['number']}")
    nets = [
        {
            "name": name,
            "signal_class": net_classes.get(name, "signal"),
            "voltage_domain": _domain(name),
            "connected_pins": sorted(pins),
            "required_track_width_mm": 0.5 if net_classes.get(name) == "power" else (0.2 if net_classes.get(name) == "ground" else 0.15),
        }
        for name, pins in sorted(endpoints.items())
    ]
    return {
        "components": components,
        "nets": nets,
        "design_basis": {
            "architecture": "rp2040_qspi_usb_device",
            "mcu_family": "RP2040",
            "usb_native": True,
            "external_flash_required": True,
            "board_carries_motor_power": False,
        },
    }


def build_samd21_sensor_hub_graph(spec: dict[str, Any]) -> dict[str, Any]:
    usbc_pins = [
        pin(1, "VBUS", "USB_VBUS", "power_in"),
        pin(2, "DP",   "USB_DP",   "bidirectional"),
        pin(3, "DM",   "USB_DM",   "bidirectional"),
        pin(4, "GND",  "GND",      "ground"),
    ]
    tvs_pins = [
        pin(1, "A",  "USB_DP", "bidirectional"),
        pin(2, "K",  "GND",    "ground"),
        pin(3, "A2", "USB_DM", "bidirectional"),
    ]
    ldo_pins = [
        pin(1, "VIN",  "USB_VBUS", "power_in"),
        pin(2, "GND",  "GND",      "ground"),
        pin(3, "VOUT", "V3V3",     "power_out"),
    ]
    mcu_pins = [
        pin(1,  "PA00_XIN32",  "XIN32",    "passive"),
        pin(2,  "PA01_XOUT32", "XOUT32",   "passive"),
        pin(9,  "VDDANA",      "V3V3",     "power_in"),
        pin(10, "GND",         "GND",      "ground"),
        pin(17, "VDDIO",       "V3V3",     "power_in"),
        pin(18, "GND",         "GND",      "ground"),
        pin(31, "PA22_SDA",    "I2C_SDA",  "bidirectional"),
        pin(32, "PA23_SCL",    "I2C_SCL",  "input"),
        pin(33, "PA24_USBDM",  "USB_DM",   "bidirectional"),
        pin(34, "PA25_USBDP",  "USB_DP",   "bidirectional"),
        pin(35, "VDDIO",       "V3V3",     "power_in"),
        pin(36, "GND",         "GND",      "ground"),
        pin(40, "~RESETN",     "MCU_NRST", "input"),
        pin(42, "VDDCORE",     "VDDCORE",  "power_out"),
        pin(43, "VDDIN",       "V3V3",     "power_in"),
        pin(46, "PA30_SWCLK",  "SWCLK",    "bidirectional"),
        pin(47, "PA31_SWDIO",  "SWDIO",    "bidirectional"),
    ]
    imu_pins = [
        pin(1,  "SDO_SA0", "GND",      "input"),
        pin(2,  "SDX",     "I2C_SDA",  "bidirectional"),
        pin(3,  "SCX",     "I2C_SCL",  "input"),
        pin(4,  "INT1",    "IMU_INT1", "output"),
        pin(5,  "INT2",    "IMU_INT2", "output"),
        pin(6,  "VDD_IO",  "V3V3",     "power_in"),
        pin(7,  "GND",     "GND",      "ground"),
        pin(8,  "GND",     "GND",      "ground"),
        pin(9,  "VDD",     "V3V3",     "power_in"),
        pin(14, "CS",      "V3V3",     "input"),
    ]
    env_sensor_pins = [
        pin(1, "VCC",  "V3V3",     "power_in"),
        pin(2, "GND",  "GND",      "ground"),
        pin(3, "SCK",  "I2C_SCL",  "input"),
        pin(4, "SDI",  "I2C_SDA",  "bidirectional"),
        pin(5, "SDO",  "GND",      "passive"),
        pin(6, "CSB",  "V3V3",     "input"),
    ]
    rtc_xtal_pins = [
        pin(1, "XIN32",  "XIN32",  "passive"),
        pin(2, "XOUT32", "XOUT32", "passive"),
    ]
    swd_pins = [
        pin(1,  "P1",  "V3V3",     "power_in"),
        pin(2,  "P2",  "SWDIO",    "passive"),
        pin(3,  "P3",  "GND",      "ground"),
        pin(4,  "P4",  "SWCLK",    "passive"),
        pin(5,  "P5",  "GND",      "ground"),
        pin(6,  "P6",  "MCU_NRST", "passive"),
        pin(7,  "P7",  "GND",      "ground"),
        pin(8,  "P8",  "GND",      "ground"),
        pin(9,  "P9",  "GND",      "ground"),
        pin(10, "P10", "GND",      "ground"),
    ]
    i2c_pullup_scl_pins = [pin(1, "A", "V3V3", "power_in"), pin(2, "B", "I2C_SCL", "passive")]
    i2c_pullup_sda_pins = [pin(1, "A", "V3V3", "power_in"), pin(2, "B", "I2C_SDA", "passive")]
    decap_pins = [pin(1, "VCC", "V3V3", "power_in"), pin(2, "GND", "GND", "ground")]
    bulk_pins  = [pin(1, "VCC", "V3V3", "power_in"), pin(2, "GND", "GND", "ground")]

    components = [
        component("J1", "power_input",  "USB-C POWER",    "USB4105-GF-A",       "USB_C_16Pin",      usbc_pins),
        component("D1", "tvs",          "USB ESD",         "USBLC6-2SC6",        "SOT23-6",          tvs_pins),
        component("U1", "regulator_3v3","3V3 LDO",         "AP2112K-3.3TRG1",    "SOT23-5",          ldo_pins),
        component("U2", "mcu",          "SAMD21G18A",      "ATSAMD21G18A-AU",    "TQFP-48",          mcu_pins),
        component("U3", "imu",          "LSM6DSOX",        "LSM6DSOXTR",         "LGA-14L",          imu_pins),
        component("U4", "env_sensor",   "BME280",          "BME280",             "LGA-8L-2x2.5mm",   env_sensor_pins),
        component("X1", "rtc_crystal",  "32.768kHz XTAL",  "MC-306 32.768KHZ",   "SMD-2Pin-Crystal", rtc_xtal_pins),
        component("J2", "debug_header", "SWD 10-pin",      "61201021621",         "PinHeader-2x5",    swd_pins),
        component("R1", "resistor_4k7", "4K7 SCL PU",      "RC0603FR-074K7L",    "0603",             i2c_pullup_scl_pins),
        component("R2", "resistor_4k7", "4K7 SDA PU",      "RC0603FR-074K7L",    "0603",             i2c_pullup_sda_pins),
        component("C1", "decoupling",   "100nF",           "GRM155R71C104KA88D", "0402",             decap_pins),
        component("C2", "decoupling",   "100nF",           "GRM155R71C104KA88D", "0402",             decap_pins),
        component("C3", "bulk_cap",     "10uF",            "GRM188R60J106ME47D", "0603",             bulk_pins),
    ]
    net_classes = {
        "GND": "ground", "USB_VBUS": "power", "V3V3": "power",
        "USB_DP": "usb", "USB_DM": "usb",
        "I2C_SCL": "i2c", "I2C_SDA": "i2c",
        "SWDIO": "signal", "SWCLK": "signal",
        "XIN32": "signal", "XOUT32": "signal",
        "IMU_INT1": "signal", "IMU_INT2": "signal",
    }
    endpoints: dict[str, list[str]] = defaultdict(list)
    for item in components:
        for item_pin in item["pins"]:
            endpoints[item_pin["net"]].append(f"{item['ref']}.{item_pin['number']}")
    nets = [
        {
            "name": name,
            "signal_class": net_classes.get(name, "signal"),
            "voltage_domain": _domain(name),
            "connected_pins": sorted(pins),
            "required_track_width_mm": 0.5 if net_classes.get(name) == "power" else 0.15,
        }
        for name, pins in sorted(endpoints.items())
    ]
    return {
        "components": components,
        "nets": nets,
        "design_basis": {
            "architecture": "atsamd21g18a_lsm6dsox_bme280",
            "mcu_family": "SAMD21",
            "usb_native": True,
            "i2c_sensor_bus": True,
            "board_carries_motor_power": False,
        },
    }


def build_nrf52840_dongle_graph(spec: dict[str, Any]) -> dict[str, Any]:
    usbc_pins = [
        pin(1, "VBUS", "USB_VBUS", "power_in"),
        pin(2, "DP",   "USB_DP",   "bidirectional"),
        pin(3, "DM",   "USB_DM",   "bidirectional"),
        pin(4, "GND",  "GND",      "ground"),
    ]
    tvs_pins = [
        pin(1, "A",  "USB_DP", "bidirectional"),
        pin(2, "K",  "GND",    "ground"),
        pin(3, "A2", "USB_DM", "bidirectional"),
    ]
    ldo_pins = [
        pin(1, "VIN",  "USB_VBUS", "power_in"),
        pin(2, "GND",  "GND",      "ground"),
        pin(3, "VOUT", "V3V3",     "power_out"),
    ]
    mcu_pins = [
        pin(3,  "VDD",     "V3V3",     "power_in"),
        pin(4,  "VSS",     "GND",      "ground"),
        pin(62, "DECUSB",  "DECUSB",   "passive"),
        pin(63, "D-",      "USB_DM",   "bidirectional"),
        pin(64, "D+",      "USB_DP",   "bidirectional"),
        pin(65, "VBUS",    "USB_VBUS", "power_in"),
        pin(53, "ANT",     "RF_ANT",   "passive"),
        pin(54, "VDDPA",   "V3V3",     "power_in"),
        pin(55, "VSS",     "GND",      "ground"),
        pin(56, "VSS",     "GND",      "ground"),
        pin(57, "VDD",     "V3V3",     "power_in"),
        pin(58, "VDDMAIN", "V3V3",     "power_in"),
        pin(59, "VSS",     "GND",      "ground"),
        pin(60, "VSS",     "GND",      "ground"),
        pin(61, "VDD",     "V3V3",     "power_in"),
        pin(37, "P1.00",   "SWCLK",   "input"),
        pin(47, "PA31",    "SWDIO",   "bidirectional"),
    ]
    antenna_pins = [
        pin(1, "CENTER", "RF_ANT", "passive"),
        pin(2, "GND",    "GND",    "ground"),
    ]
    swd_pins = [
        pin(1,  "VCC",   "V3V3",  "power_in"),
        pin(2,  "SWDIO", "SWDIO", "passive"),
        pin(3,  "GND",   "GND",   "ground"),
        pin(4,  "SWDCLK","SWCLK", "passive"),
        pin(5,  "GND",   "GND",   "ground"),
    ]
    decap_pins = [pin(1, "VCC", "V3V3", "power_in"), pin(2, "GND", "GND", "ground")]
    bulk_pins  = [pin(1, "VCC", "V3V3", "power_in"), pin(2, "GND", "GND", "ground")]
    pullup_pins = [pin(1, "A", "V3V3", "power_in"), pin(2, "B", "GND", "passive")]

    components = [
        component("J1", "power_input",    "USB-C POWER",  "USB4105-GF-A",       "USB_C_16Pin",    usbc_pins),
        component("D1", "tvs",            "USB ESD",      "USBLC6-2SC6",        "SOT23-6",        tvs_pins),
        component("U1", "regulator_3v3",  "3V3 LDO",      "AP2112K-3.3TRG1",    "SOT23-5",        ldo_pins),
        component("U2", "mcu",            "nRF52840-QIAA","nRF52840-QIAA",      "aQFN-73",        mcu_pins),
        component("J2", "antenna_connector","U.FL ANT",   "U.FL-R-SMT-1(01)",   "UFL_SMD",        antenna_pins),
        component("J3", "debug_header",   "SWD 5-pin",    "FTSH-105-01-L-DV-K", "SWD_5Pin_1.27mm",swd_pins),
        component("R1", "resistor_4k7",   "4K7 PU",       "RC0603FR-074K7L",    "0603",           pullup_pins),
        component("C1", "decoupling",     "100nF",        "GRM155R71C104KA88D", "0402",           decap_pins),
        component("C2", "decoupling",     "100nF",        "GRM155R71C104KA88D", "0402",           decap_pins),
        component("C3", "bulk_cap",       "10uF",         "GRM188R60J106ME47D", "0603",           bulk_pins),
    ]
    net_classes = {
        "GND": "ground", "USB_VBUS": "power", "V3V3": "power",
        "USB_DP": "usb", "USB_DM": "usb",
        "RF_ANT": "rf", "SWDIO": "signal", "SWCLK": "signal",
    }
    endpoints: dict[str, list[str]] = defaultdict(list)
    for item in components:
        for item_pin in item["pins"]:
            endpoints[item_pin["net"]].append(f"{item['ref']}.{item_pin['number']}")
    nets = [
        {
            "name": name,
            "signal_class": net_classes.get(name, "signal"),
            "voltage_domain": _domain(name),
            "connected_pins": sorted(pins),
            "required_track_width_mm": 0.5 if net_classes.get(name) == "power" else 0.15,
        }
        for name, pins in sorted(endpoints.items())
    ]
    return {
        "components": components,
        "nets": nets,
        "design_basis": {
            "architecture": "nrf52840_ble_ieee802154_usb_dongle",
            "mcu_family": "nRF52840",
            "ble_version": "5.3",
            "ieee_802154": True,
            "usb_native": True,
            "board_carries_motor_power": False,
        },
    }


def build_graph_from_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Build the electronics graph, merging agent-authored blocks from spec.

    Agent blocks are read from ``spec["agent_electronics"]["blocks"]``.
    They are appended to the base topology chosen by role_set/template.
    Nets are re-derived from all components so new nets appear automatically.
    """
    role_set = spec.get("electronics", {}).get("role_set", "")
    template = spec.get("project", {}).get("template", "")
    if role_set == "ble_sensor_node" or template == "ble_sensor_node":
        base_graph = build_ble_sensor_node_graph(spec)
    elif role_set == "sensor_data_logger" or template == "sensor_data_logger":
        base_graph = build_sensor_data_logger_graph(spec)
    elif role_set == "usb_hid_controller" or template == "usb_hid_controller":
        base_graph = build_usb_hid_controller_graph(spec)
    elif role_set == "lora_sensor_node" or template == "lora_sensor_node":
        base_graph = build_lora_sensor_node_graph(spec)
    elif role_set == "bldc_esc" or template == "bldc_esc":
        base_graph = build_bldc_esc_graph(spec)
    elif role_set == "esp32_wifi_gateway" or template == "esp32_wifi_gateway":
        base_graph = build_esp32_wifi_gateway_graph(spec)
    elif role_set == "avr_32u4_hid" or template == "avr_32u4_hid":
        base_graph = build_avr_32u4_hid_graph(spec)
    elif role_set == "stm32g0_power_monitor" or template == "stm32g0_power_monitor":
        base_graph = build_stm32g0_power_monitor_graph(spec)
    elif role_set == "rp2040_usb_device" or template == "rp2040_usb_device":
        base_graph = build_rp2040_usb_device_graph(spec)
    elif role_set == "samd21_sensor_hub" or template == "samd21_sensor_hub":
        base_graph = build_samd21_sensor_hub_graph(spec)
    elif role_set == "nrf52840_dongle" or template == "nrf52840_dongle":
        base_graph = build_nrf52840_dongle_graph(spec)
    else:
        base_graph = build_controller_graph(spec)

    agent_blocks = (
        spec.get("agent_electronics", {}).get("blocks", [])
        + spec.get("electronics", {}).get("blocks", [])
    )
    if not agent_blocks:
        return base_graph

    base_refs = {c["ref"] for c in base_graph["components"]}
    extra_components: list[dict[str, Any]] = []
    for block in agent_blocks:
        ref = block.get("ref") or block.get("id")
        if not ref or ref in base_refs:
            continue
        extra_components.append(_block_to_component({**block, "ref": ref}))

    if not extra_components:
        return base_graph

    all_components = base_graph["components"] + extra_components

    net_classes: dict[str, str] = dict(_WELL_KNOWN_NET_CLASSES)
    for item in extra_components:
        for item_pin in item["pins"]:
            net = item_pin["net"]
            if net not in net_classes and (net.endswith("H") or net.endswith("L")):
                net_classes[net] = "can"

    endpoints: dict[str, list[str]] = defaultdict(list)
    for item in all_components:
        for item_pin in item["pins"]:
            endpoints[item_pin["net"]].append(f"{item['ref']}.{item_pin['number']}")

    nets = []
    for name in sorted(endpoints):
        signal_class = net_classes.get(name, "analog" if name.endswith("CURRENT") else "signal")
        nets.append({
            "name": name,
            "signal_class": signal_class,
            "voltage_domain": _domain(name),
            "connected_pins": sorted(endpoints[name]),
            "required_track_width_mm": (
                2.0 if signal_class == "power" and name.startswith("VBAT")
                else 0.5 if signal_class == "power"
                else 0.2
            ),
        })

    return {**base_graph, "components": all_components, "nets": nets}
