"""Generated executable semantic schematic for agent review.

This file is intentionally compact and pin-name based. It can be executed
to reconstruct the normalized semantic_schematic model. Native EDA/CAD
artifacts are generated from typed artifacts; edit the spec or graph-producing
blocks when changing a generated design.
"""

from hw_codesign.semantic_schematic import SemanticBoard, pin

board = SemanticBoard(
    project='sensor_data_logger',
    revision='r1',
    purpose='LLM-suited schematic representation derived from typed graph; native EDA files are generated outputs.',
    source_graph='/Users/mrcha033/Documents/hw-cli/projects/sensor_data_logger/electronics/generated/electrical_graph.json',
    board_width_mm=70.0,
    board_height_mm=50.0,
)
component = board.component
net = board.net
connect = board.connect
place = board.place
constraint = board.constraint

component(
    'C1',
    role='decoupling',
    value='100nF',
    component_id='grm188_100n',
    mpn='GRM188R71C104KA01D',
    manufacturer='Murata',
    package='0603',
    footprint='Capacitor_SMD:C_0603_1608Metric',
    pin_contracts={'1': {'number': '1', 'name': 'VCC', 'electrical_type': 'passive'}, '2': {'number': '2', 'name': 'GND', 'electrical_type': 'ground', 'voltage_domain': 'GND'}},
    pins=[
        pin('1', 'VCC', net='V3V3', role='passive', voltage_domain=None, mcu_pin=None),
        pin('2', 'GND', net='GND', role='ground', voltage_domain='GND', mcu_pin=None),
    ],
)

component(
    'C2',
    role='decoupling',
    value='100nF',
    component_id='grm188_100n',
    mpn='GRM188R71C104KA01D',
    manufacturer='Murata',
    package='0603',
    footprint='Capacitor_SMD:C_0603_1608Metric',
    pin_contracts={'1': {'number': '1', 'name': 'VCC', 'electrical_type': 'passive'}, '2': {'number': '2', 'name': 'GND', 'electrical_type': 'ground', 'voltage_domain': 'GND'}},
    pins=[
        pin('1', 'VCC', net='V3V3', role='passive', voltage_domain=None, mcu_pin=None),
        pin('2', 'GND', net='GND', role='ground', voltage_domain='GND', mcu_pin=None),
    ],
)

component(
    'C9',
    role='bulk_cap',
    value='22uF',
    component_id='grm31_22u',
    mpn='GRM31CR61E226ME15L',
    manufacturer='Murata',
    package='1206',
    footprint='Capacitor_SMD:C_1206_3216Metric',
    pin_contracts={'1': {'number': '1', 'name': 'VCC', 'electrical_type': 'passive'}, '2': {'number': '2', 'name': 'GND', 'electrical_type': 'ground', 'voltage_domain': 'GND'}},
    pins=[
        pin('1', 'VCC', net='V3V3', role='passive', voltage_domain=None, mcu_pin=None),
        pin('2', 'GND', net='GND', role='ground', voltage_domain='GND', mcu_pin=None),
    ],
)

component(
    'D1',
    role='tvs',
    value='USB ESD',
    component_id='usblc6_2sc6',
    mpn='USBLC6-2SC6',
    manufacturer='STMicroelectronics',
    package='SOT-23-6',
    footprint='Package_TO_SOT_SMD:SOT-23-6',
    pin_contracts={'1': {'number': '1', 'name': 'DP_IN', 'electrical_type': 'bidirectional'}, '2': {'number': '2', 'name': 'DP_OUT', 'electrical_type': 'bidirectional'}, '3': {'number': '3', 'name': 'DM_IN', 'electrical_type': 'bidirectional'}, '4': {'number': '4', 'name': 'DM_OUT', 'electrical_type': 'bidirectional'}, '5': {'number': '5', 'name': 'GND', 'electrical_type': 'ground', 'voltage_domain': 'GND'}},
    pins=[
        pin('1', 'DP_IN', net='USB_DP_RAW', role='bidirectional', voltage_domain=None, mcu_pin=None),
        pin('2', 'DP_OUT', net='USB_DP', role='bidirectional', voltage_domain=None, mcu_pin=None),
        pin('3', 'DM_IN', net='USB_DM_RAW', role='bidirectional', voltage_domain=None, mcu_pin=None),
        pin('4', 'DM_OUT', net='USB_DM', role='bidirectional', voltage_domain=None, mcu_pin=None),
        pin('5', 'GND', net='GND', role='ground', voltage_domain='GND', mcu_pin=None),
        pin('6', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
    ],
)

component(
    'F1',
    role='fuse',
    value='500mA Fuse',
    component_id='littelfuse_0498080',
    mpn='Littelfuse-0498080.M',
    manufacturer='Littelfuse',
    package='MIDI',
    footprint='HW_Curated:MIDI_Fuse',
    pin_contracts={'1': {'number': '1', 'name': 'IN', 'electrical_type': 'passive'}, '2': {'number': '2', 'name': 'OUT', 'electrical_type': 'passive'}},
    pins=[
        pin('1', 'IN', net='USB_VBUS', role='passive', voltage_domain=None, mcu_pin=None),
        pin('2', 'OUT', net='USB_FUSED', role='passive', voltage_domain=None, mcu_pin=None),
    ],
)

component(
    'J1',
    role='power_input',
    value='USB-C POWER',
    component_id='usb4105_gf_a',
    mpn='USB4105-GF-A',
    manufacturer='GCT',
    package='USB-C-16',
    footprint='Connector_USB:USB_C_GCT_USB4105',
    pin_contracts={'1': {'number': '1', 'name': 'VBUS', 'electrical_type': 'power_in', 'voltage_domain': 'USB_5V'}, '2': {'number': '2', 'name': 'GND', 'electrical_type': 'ground', 'voltage_domain': 'GND'}, '3': {'number': '3', 'name': 'D+', 'electrical_type': 'bidirectional'}, '4': {'number': '4', 'name': 'D-', 'electrical_type': 'bidirectional'}, '5': {'number': '5', 'name': 'CC1', 'electrical_type': 'passive'}, '6': {'number': '6', 'name': 'CC2', 'electrical_type': 'passive'}},
    pins=[
        pin('1', 'VBUS', net='USB_VBUS', role='power_in', voltage_domain='USB_5V', mcu_pin=None),
        pin('2', 'GND', net='GND', role='ground', voltage_domain='GND', mcu_pin=None),
        pin('3', 'D+', net='USB_DP_RAW', role='bidirectional', voltage_domain=None, mcu_pin=None),
        pin('4', 'D-', net='USB_DM_RAW', role='bidirectional', voltage_domain=None, mcu_pin=None),
        pin('5', 'CC1', net='USB_CC1', role='passive', voltage_domain=None, mcu_pin=None),
        pin('6', 'CC2', net='USB_CC2', role='passive', voltage_domain=None, mcu_pin=None),
    ],
)

component(
    'J2',
    role='debug',
    value='UART DEBUG',
    component_id='samtec_ftsh_105_uart',
    mpn='Samtec-FTSH-105-01-L-DV-K',
    manufacturer='Samtec',
    package='FTSH-10',
    footprint='Connector_Samtec:FTSH-105',
    pin_contracts={'1': {'number': '1', 'name': 'VREF', 'electrical_type': 'power_out', 'voltage_domain': 'V3V3'}, '2': {'number': '2', 'name': 'TXD', 'electrical_type': 'output'}, '3': {'number': '3', 'name': 'RXD', 'electrical_type': 'input'}, '4': {'number': '4', 'name': 'BOOT', 'electrical_type': 'input'}, '5': {'number': '5', 'name': 'GND', 'electrical_type': 'ground', 'voltage_domain': 'GND'}},
    pins=[
        pin('1', 'VREF', net='V3V3', role='power_out', voltage_domain='V3V3', mcu_pin=None),
        pin('2', 'TXD', net='UART_TX', role='output', voltage_domain=None, mcu_pin=None),
        pin('3', 'RXD', net='UART_RX', role='input', voltage_domain=None, mcu_pin=None),
        pin('4', 'BOOT', net='BOOT', role='input', voltage_domain=None, mcu_pin=None),
        pin('5', 'GND', net='GND', role='ground', voltage_domain='GND', mcu_pin=None),
        pin('6', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('7', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('8', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('9', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('10', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
    ],
)

component(
    'Q1',
    role='reverse_polarity',
    value='Ideal Diode',
    component_id='lm74700qdbvrq1',
    mpn='LM74700QDBVRQ1',
    manufacturer='Texas Instruments',
    package='SOT-23-6',
    footprint='Package_TO_SOT_SMD:SOT-23-6',
    pin_contracts={'1': {'number': '1', 'name': 'ANODE', 'electrical_type': 'power_in'}, '2': {'number': '2', 'name': 'CATHODE', 'electrical_type': 'power_out'}, '3': {'number': '3', 'name': 'GND', 'electrical_type': 'ground', 'voltage_domain': 'GND'}},
    pins=[
        pin('1', 'ANODE', net='USB_FUSED', role='power_in', voltage_domain='V3V3', mcu_pin=None),
        pin('2', 'CATHODE', net='USB_PROT', role='power_out', voltage_domain='V3V3', mcu_pin=None),
        pin('3', 'GND', net='GND', role='ground', voltage_domain='GND', mcu_pin=None),
        pin('4', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('5', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('6', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
    ],
)

component(
    'R1',
    role='pullup',
    value='4K7',
    component_id='rc0603_4k7',
    mpn='RC0603FR-074K7L',
    manufacturer='Yageo',
    package='0603',
    footprint='Resistor_SMD:R_0603_1608Metric',
    pin_contracts={'1': {'number': '1', 'name': 'VCC', 'electrical_type': 'passive'}, '2': {'number': '2', 'name': 'SIGNAL', 'electrical_type': 'passive'}},
    pins=[
        pin('1', 'VCC', net='V3V3', role='passive', voltage_domain=None, mcu_pin=None),
        pin('2', 'SCL', net='I2C_IMU_SCL', role='passive', voltage_domain=None, mcu_pin=None),
    ],
)

component(
    'R2',
    role='pullup',
    value='4K7',
    component_id='rc0603_4k7',
    mpn='RC0603FR-074K7L',
    manufacturer='Yageo',
    package='0603',
    footprint='Resistor_SMD:R_0603_1608Metric',
    pin_contracts={'1': {'number': '1', 'name': 'VCC', 'electrical_type': 'passive'}, '2': {'number': '2', 'name': 'SIGNAL', 'electrical_type': 'passive'}},
    pins=[
        pin('1', 'VCC', net='V3V3', role='passive', voltage_domain=None, mcu_pin=None),
        pin('2', 'SDA', net='I2C_IMU_SDA', role='passive', voltage_domain=None, mcu_pin=None),
    ],
)

component(
    'R3',
    role='pullup',
    value='4K7',
    component_id='rc0603_4k7',
    mpn='RC0603FR-074K7L',
    manufacturer='Yageo',
    package='0603',
    footprint='Resistor_SMD:R_0603_1608Metric',
    pin_contracts={'1': {'number': '1', 'name': 'VCC', 'electrical_type': 'passive'}, '2': {'number': '2', 'name': 'SIGNAL', 'electrical_type': 'passive'}},
    pins=[
        pin('1', 'VCC', net='V3V3', role='passive', voltage_domain=None, mcu_pin=None),
        pin('2', 'EN', net='ESP_EN', role='passive', voltage_domain=None, mcu_pin=None),
    ],
)

component(
    'R5',
    role='usb_cc_pulldown',
    value='5K1 USB-C Rd',
    component_id='rc0603_5k1',
    mpn='RC0603FR-075K1L',
    manufacturer='Yageo',
    package='0603',
    footprint='Resistor_SMD:R_0603_1608Metric',
    pin_contracts={'1': {'number': '1', 'name': 'A', 'electrical_type': 'passive'}, '2': {'number': '2', 'name': 'B', 'electrical_type': 'passive'}},
    pins=[
        pin('1', 'A', net='USB_CC1', role='passive', voltage_domain=None, mcu_pin=None),
        pin('2', 'B', net='GND', role='ground', voltage_domain='GND', mcu_pin=None),
    ],
)

component(
    'R6',
    role='usb_cc_pulldown',
    value='5K1 USB-C Rd',
    component_id='rc0603_5k1',
    mpn='RC0603FR-075K1L',
    manufacturer='Yageo',
    package='0603',
    footprint='Resistor_SMD:R_0603_1608Metric',
    pin_contracts={'1': {'number': '1', 'name': 'A', 'electrical_type': 'passive'}, '2': {'number': '2', 'name': 'B', 'electrical_type': 'passive'}},
    pins=[
        pin('1', 'A', net='USB_CC2', role='passive', voltage_domain=None, mcu_pin=None),
        pin('2', 'B', net='GND', role='ground', voltage_domain='GND', mcu_pin=None),
    ],
)

component(
    'U1',
    role='mcu',
    value='ESP32-S3-WROOM-1',
    component_id='esp32s3_wroom_1',
    mpn='ESP32-S3-WROOM-1-N8',
    manufacturer='Espressif Systems',
    package='LCC-41',
    footprint='RF_Module:ESP32-S3-WROOM-1',
    pin_contracts={'1': {'number': '1', 'name': 'GND', 'electrical_type': 'ground', 'voltage_domain': 'GND'}, '2': {'number': '2', 'name': '3V3', 'electrical_type': 'power_in', 'voltage_domain': 'V3V3'}, '3': {'number': '3', 'name': 'EN', 'electrical_type': 'input'}, '12': {'number': '12', 'name': 'IO8', 'electrical_type': 'bidirectional'}, '13': {'number': '13', 'name': 'USB_D-', 'electrical_type': 'bidirectional'}, '14': {'number': '14', 'name': 'USB_D+', 'electrical_type': 'bidirectional'}, '15': {'number': '15', 'name': 'IO3', 'electrical_type': 'bidirectional'}, '27': {'number': '27', 'name': 'IO0', 'electrical_type': 'bidirectional'}, '36': {'number': '36', 'name': 'RXD0', 'electrical_type': 'bidirectional'}, '37': {'number': '37', 'name': 'TXD0', 'electrical_type': 'bidirectional'}, '39': {'number': '39', 'name': 'IO1', 'electrical_type': 'bidirectional'}, '40': {'number': '40', 'name': 'GND', 'electrical_type': 'ground', 'voltage_domain': 'GND'}, '41': {'number': '41', 'name': 'GND', 'electrical_type': 'ground', 'voltage_domain': 'GND'}},
    pins=[
        pin('1', 'GND', net='GND', role='ground', voltage_domain='GND', mcu_pin=None),
        pin('2', '3V3', net='V3V3', role='power_in', voltage_domain='V3V3', mcu_pin=None),
        pin('3', 'EN', net='ESP_EN', role='input', voltage_domain=None, mcu_pin='EN'),
        pin('12', 'IO8', net='I2C_IMU_SCL', role='open_drain', voltage_domain=None, mcu_pin='GPIO8'),
        pin('13', 'USB_D-', net='USB_DM', role='bidirectional', voltage_domain=None, mcu_pin='USB_D-'),
        pin('14', 'USB_D+', net='USB_DP', role='bidirectional', voltage_domain=None, mcu_pin='USB_D+'),
        pin('15', 'IO3', net='I2C_IMU_SDA', role='open_drain', voltage_domain=None, mcu_pin='GPIO3'),
        pin('27', 'IO0', net='BOOT', role='input', voltage_domain=None, mcu_pin='GPIO0'),
        pin('36', 'RXD0', net='UART_RX', role='input', voltage_domain=None, mcu_pin='RXD0'),
        pin('37', 'TXD0', net='UART_TX', role='output', voltage_domain=None, mcu_pin='TXD0'),
        pin('39', 'IO1', net='IMU_INT', role='input', voltage_domain=None, mcu_pin='GPIO1'),
        pin('40', 'GND', net='GND', role='ground', voltage_domain='GND', mcu_pin=None),
        pin('41', 'GND', net='GND', role='ground', voltage_domain='GND', mcu_pin=None),
        pin('4', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('5', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('6', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('7', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('8', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('9', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('10', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('11', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('16', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('17', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('18', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('19', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('20', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('21', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('22', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('23', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('24', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('25', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('26', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('28', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('29', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('30', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('31', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('32', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('33', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('34', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('35', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
        pin('38', 'NC', net=None, role='no_connect', voltage_domain=None, mcu_pin=None),
    ],
)

component(
    'U2',
    role='imu',
    value='ICM-42688-P',
    component_id='icm_42688_p',
    mpn='ICM-42688-P',
    manufacturer='TDK InvenSense',
    package='LGA-14',
    footprint='Package_LGA:LGA-14',
    pin_contracts={'1': {'number': '1', 'name': 'VDD', 'electrical_type': 'power_in', 'voltage_domain': 'V3V3'}, '2': {'number': '2', 'name': 'GND', 'electrical_type': 'ground', 'voltage_domain': 'GND'}, '3': {'number': '3', 'name': 'SCL', 'electrical_type': 'open_drain'}, '4': {'number': '4', 'name': 'SDA', 'electrical_type': 'open_drain'}, '5': {'number': '5', 'name': 'INT1', 'electrical_type': 'output'}},
    pins=[
        pin('1', 'VDD', net='V3V3', role='power_in', voltage_domain='V3V3', mcu_pin=None),
        pin('2', 'GND', net='GND', role='ground', voltage_domain='GND', mcu_pin=None),
        pin('3', 'SCL', net='I2C_IMU_SCL', role='open_drain', voltage_domain=None, mcu_pin=None),
        pin('4', 'SDA', net='I2C_IMU_SDA', role='open_drain', voltage_domain=None, mcu_pin=None),
        pin('5', 'INT1', net='IMU_INT', role='output', voltage_domain=None, mcu_pin=None),
    ],
)

component(
    'U3',
    role='regulator',
    value='3V3 Buck',
    component_id='tps62133rgtr',
    mpn='TPS62133RGTR',
    manufacturer='Texas Instruments',
    package='VQFN-16',
    footprint='Package_DFN_QFN:VQFN-16',
    pin_contracts={'1': {'number': '1', 'name': 'VIN', 'electrical_type': 'power_in'}, '2': {'number': '2', 'name': 'VOUT', 'electrical_type': 'power_out'}, '3': {'number': '3', 'name': 'GND', 'electrical_type': 'ground', 'voltage_domain': 'GND'}},
    pins=[
        pin('1', 'VIN', net='USB_PROT', role='power_in', voltage_domain='V3V3', mcu_pin=None),
        pin('2', 'VOUT', net='V3V3', role='power_out', voltage_domain='V3V3', mcu_pin=None),
        pin('3', 'GND', net='GND', role='ground', voltage_domain='GND', mcu_pin=None),
    ],
)


net('BOOT', signal_class='signal', voltage_domain='V3V3', required_track_width_mm=0.15)
connect('J2', pin='BOOT', number='4', net='BOOT', role='input', mcu_pin=None)
connect('U1', pin='IO0', number='27', net='BOOT', role='input', mcu_pin='GPIO0')

net('ESP_EN', signal_class='signal', voltage_domain='V3V3', required_track_width_mm=0.15)
connect('R3', pin='EN', number='2', net='ESP_EN', role='passive', mcu_pin=None)
connect('U1', pin='EN', number='3', net='ESP_EN', role='input', mcu_pin='EN')

net('GND', signal_class='ground', voltage_domain='GND', required_track_width_mm=0.2)
connect('C1', pin='GND', number='2', net='GND', role='ground', mcu_pin=None)
connect('C2', pin='GND', number='2', net='GND', role='ground', mcu_pin=None)
connect('C9', pin='GND', number='2', net='GND', role='ground', mcu_pin=None)
connect('D1', pin='GND', number='5', net='GND', role='ground', mcu_pin=None)
connect('J1', pin='GND', number='2', net='GND', role='ground', mcu_pin=None)
connect('J2', pin='GND', number='5', net='GND', role='ground', mcu_pin=None)
connect('Q1', pin='GND', number='3', net='GND', role='ground', mcu_pin=None)
connect('R5', pin='B', number='2', net='GND', role='ground', mcu_pin=None)
connect('R6', pin='B', number='2', net='GND', role='ground', mcu_pin=None)
connect('U1', pin='GND', number='1', net='GND', role='ground', mcu_pin=None)
connect('U1', pin='GND', number='40', net='GND', role='ground', mcu_pin=None)
connect('U1', pin='GND', number='41', net='GND', role='ground', mcu_pin=None)
connect('U2', pin='GND', number='2', net='GND', role='ground', mcu_pin=None)
connect('U3', pin='GND', number='3', net='GND', role='ground', mcu_pin=None)

net('I2C_IMU_SCL', signal_class='i2c', voltage_domain='V3V3', required_track_width_mm=0.15)
connect('R1', pin='SCL', number='2', net='I2C_IMU_SCL', role='passive', mcu_pin=None)
connect('U1', pin='IO8', number='12', net='I2C_IMU_SCL', role='open_drain', mcu_pin='GPIO8')
connect('U2', pin='SCL', number='3', net='I2C_IMU_SCL', role='open_drain', mcu_pin=None)

net('I2C_IMU_SDA', signal_class='i2c', voltage_domain='V3V3', required_track_width_mm=0.15)
connect('R2', pin='SDA', number='2', net='I2C_IMU_SDA', role='passive', mcu_pin=None)
connect('U1', pin='IO3', number='15', net='I2C_IMU_SDA', role='open_drain', mcu_pin='GPIO3')
connect('U2', pin='SDA', number='4', net='I2C_IMU_SDA', role='open_drain', mcu_pin=None)

net('IMU_INT', signal_class='signal', voltage_domain='V3V3', required_track_width_mm=0.15)
connect('U1', pin='IO1', number='39', net='IMU_INT', role='input', mcu_pin='GPIO1')
connect('U2', pin='INT1', number='5', net='IMU_INT', role='output', mcu_pin=None)

net('UART_RX', signal_class='signal', voltage_domain='V3V3', required_track_width_mm=0.15)
connect('J2', pin='RXD', number='3', net='UART_RX', role='input', mcu_pin=None)
connect('U1', pin='RXD0', number='36', net='UART_RX', role='input', mcu_pin='RXD0')

net('UART_TX', signal_class='signal', voltage_domain='V3V3', required_track_width_mm=0.15)
connect('J2', pin='TXD', number='2', net='UART_TX', role='output', mcu_pin=None)
connect('U1', pin='TXD0', number='37', net='UART_TX', role='output', mcu_pin='TXD0')

net('USB_CC1', signal_class='usb', voltage_domain='V3V3', required_track_width_mm=0.15)
connect('J1', pin='CC1', number='5', net='USB_CC1', role='passive', mcu_pin=None)
connect('R5', pin='A', number='1', net='USB_CC1', role='passive', mcu_pin=None)

net('USB_CC2', signal_class='usb', voltage_domain='V3V3', required_track_width_mm=0.15)
connect('J1', pin='CC2', number='6', net='USB_CC2', role='passive', mcu_pin=None)
connect('R6', pin='A', number='1', net='USB_CC2', role='passive', mcu_pin=None)

net('USB_DM', signal_class='usb', voltage_domain='V3V3', required_track_width_mm=0.15)
connect('D1', pin='DM_OUT', number='4', net='USB_DM', role='bidirectional', mcu_pin=None)
connect('U1', pin='USB_D-', number='13', net='USB_DM', role='bidirectional', mcu_pin='USB_D-')

net('USB_DM_RAW', signal_class='usb', voltage_domain='V3V3', required_track_width_mm=0.15)
connect('D1', pin='DM_IN', number='3', net='USB_DM_RAW', role='bidirectional', mcu_pin=None)
connect('J1', pin='D-', number='4', net='USB_DM_RAW', role='bidirectional', mcu_pin=None)

net('USB_DP', signal_class='usb', voltage_domain='V3V3', required_track_width_mm=0.15)
connect('D1', pin='DP_OUT', number='2', net='USB_DP', role='bidirectional', mcu_pin=None)
connect('U1', pin='USB_D+', number='14', net='USB_DP', role='bidirectional', mcu_pin='USB_D+')

net('USB_DP_RAW', signal_class='usb', voltage_domain='V3V3', required_track_width_mm=0.15)
connect('D1', pin='DP_IN', number='1', net='USB_DP_RAW', role='bidirectional', mcu_pin=None)
connect('J1', pin='D+', number='3', net='USB_DP_RAW', role='bidirectional', mcu_pin=None)

net('USB_FUSED', signal_class='power', voltage_domain='V3V3', required_track_width_mm=0.5)
connect('F1', pin='OUT', number='2', net='USB_FUSED', role='passive', mcu_pin=None)
connect('Q1', pin='ANODE', number='1', net='USB_FUSED', role='power_in', mcu_pin=None)

net('USB_PROT', signal_class='power', voltage_domain='V3V3', required_track_width_mm=0.5)
connect('Q1', pin='CATHODE', number='2', net='USB_PROT', role='power_out', mcu_pin=None)
connect('U3', pin='VIN', number='1', net='USB_PROT', role='power_in', mcu_pin=None)

net('USB_VBUS', signal_class='power', voltage_domain='USB_5V', required_track_width_mm=0.5)
connect('F1', pin='IN', number='1', net='USB_VBUS', role='passive', mcu_pin=None)
connect('J1', pin='VBUS', number='1', net='USB_VBUS', role='power_in', mcu_pin=None)

net('V3V3', signal_class='power', voltage_domain='V3V3', required_track_width_mm=0.5)
connect('C1', pin='VCC', number='1', net='V3V3', role='passive', mcu_pin=None)
connect('C2', pin='VCC', number='1', net='V3V3', role='passive', mcu_pin=None)
connect('C9', pin='VCC', number='1', net='V3V3', role='passive', mcu_pin=None)
connect('J2', pin='VREF', number='1', net='V3V3', role='power_out', mcu_pin=None)
connect('R1', pin='VCC', number='1', net='V3V3', role='passive', mcu_pin=None)
connect('R2', pin='VCC', number='1', net='V3V3', role='passive', mcu_pin=None)
connect('R3', pin='VCC', number='1', net='V3V3', role='passive', mcu_pin=None)
connect('U1', pin='3V3', number='2', net='V3V3', role='power_in', mcu_pin=None)
connect('U2', pin='VDD', number='1', net='V3V3', role='power_in', mcu_pin=None)
connect('U3', pin='VOUT', number='2', net='V3V3', role='power_out', mcu_pin=None)


place('C1', data={'ref': 'C1', 'x_mm': 35.0, 'y_mm': 39.0, 'rotation_deg': 0.0, 'side': 'top', 'courtyard_w_mm': 4.243, 'courtyard_h_mm': 4.243, 'source': 'sensor_data_logger_anchor', 'rationale': ''})
place('C2', data={'ref': 'C2', 'x_mm': 44.0, 'y_mm': 39.0, 'rotation_deg': 0.0, 'side': 'top', 'courtyard_w_mm': 4.243, 'courtyard_h_mm': 4.243, 'source': 'sensor_data_logger_anchor', 'rationale': ''})
place('C9', data={'ref': 'C9', 'x_mm': 56.0, 'y_mm': 14.0, 'rotation_deg': 0.0, 'side': 'top', 'courtyard_w_mm': 4.243, 'courtyard_h_mm': 4.243, 'source': 'sensor_data_logger_anchor', 'rationale': ''})
place('D1', data={'ref': 'D1', 'x_mm': 30.0, 'y_mm': 10.0, 'rotation_deg': 0.0, 'side': 'top', 'courtyard_w_mm': 7.348, 'courtyard_h_mm': 7.348, 'source': 'sensor_data_logger_anchor', 'rationale': ''})
place('F1', data={'ref': 'F1', 'x_mm': 26.0, 'y_mm': 10.0, 'rotation_deg': 0.0, 'side': 'top', 'courtyard_w_mm': 4.243, 'courtyard_h_mm': 4.243, 'source': 'sensor_data_logger_anchor', 'rationale': ''})
place('J1', data={'ref': 'J1', 'x_mm': 18.0, 'y_mm': 4.0, 'rotation_deg': 0.0, 'side': 'top', 'courtyard_w_mm': 7.348, 'courtyard_h_mm': 7.348, 'source': 'sensor_data_logger_anchor', 'rationale': ''})
place('J2', data={'ref': 'J2', 'x_mm': 54.0, 'y_mm': 39.0, 'rotation_deg': 0.0, 'side': 'top', 'courtyard_w_mm': 9.487, 'courtyard_h_mm': 9.487, 'source': 'sensor_data_logger_anchor', 'rationale': ''})
place('Q1', data={'ref': 'Q1', 'x_mm': 34.0, 'y_mm': 4.0, 'rotation_deg': 0.0, 'side': 'top', 'courtyard_w_mm': 7.348, 'courtyard_h_mm': 7.348, 'source': 'sensor_data_logger_anchor', 'rationale': ''})
place('R1', data={'ref': 'R1', 'x_mm': 18.0, 'y_mm': 38.0, 'rotation_deg': 0.0, 'side': 'top', 'courtyard_w_mm': 4.243, 'courtyard_h_mm': 4.243, 'source': 'sensor_data_logger_anchor', 'rationale': ''})
place('R2', data={'ref': 'R2', 'x_mm': 27.0, 'y_mm': 38.0, 'rotation_deg': 0.0, 'side': 'top', 'courtyard_w_mm': 4.243, 'courtyard_h_mm': 4.243, 'source': 'sensor_data_logger_anchor', 'rationale': ''})
place('R3', data={'ref': 'R3', 'x_mm': 50.0, 'y_mm': 25.0, 'rotation_deg': 0.0, 'side': 'top', 'courtyard_w_mm': 4.243, 'courtyard_h_mm': 4.243, 'source': 'sensor_data_logger_anchor', 'rationale': ''})
place('R5', data={'ref': 'R5', 'x_mm': 22.0, 'y_mm': 7.0, 'rotation_deg': 0.0, 'side': 'top', 'courtyard_w_mm': 4.243, 'courtyard_h_mm': 4.243, 'source': 'usb_c_rd_connector_seed', 'rationale': 'USB-C Rd resistor position derived from the connector CC pins.'})
place('R6', data={'ref': 'R6', 'x_mm': 26.0, 'y_mm': 7.0, 'rotation_deg': 0.0, 'side': 'top', 'courtyard_w_mm': 4.243, 'courtyard_h_mm': 4.243, 'source': 'usb_c_rd_connector_seed', 'rationale': 'USB-C Rd resistor position derived from the connector CC pins.'})
place('U1', data={'ref': 'U1', 'x_mm': 44.0, 'y_mm': 43.0, 'rotation_deg': 0.0, 'side': 'top', 'courtyard_w_mm': 19.209, 'courtyard_h_mm': 19.209, 'source': 'sensor_data_logger_anchor', 'rationale': ''})
place('U2', data={'ref': 'U2', 'x_mm': 18.0, 'y_mm': 28.0, 'rotation_deg': 0.0, 'side': 'top', 'courtyard_w_mm': 6.708, 'courtyard_h_mm': 6.708, 'source': 'sensor_data_logger_anchor', 'rationale': ''})
place('U3', data={'ref': 'U3', 'x_mm': 44.0, 'y_mm': 12.0, 'rotation_deg': 0.0, 'side': 'top', 'courtyard_w_mm': 5.196, 'courtyard_h_mm': 5.196, 'source': 'sensor_data_logger_anchor', 'rationale': ''})

constraint(data={'kind': 'board_keepout', 'target_ref': None, 'params': {'width_mm': 70.0, 'height_mm': 50.0, 'edge_margin_mm': 0.15}, 'derived_from': 'mechanical.envelope + manufacturing.pcb.min_clearance_mm', 'enforced': True, 'rationale': ''})
constraint(data={'kind': 'mounting_hole_keepout', 'target_ref': None, 'params': {'x_mm': 4.0, 'y_mm': 4.0, 'keepout_radius_mm': 2.4}, 'derived_from': 'mechanical.mounting_holes[0] + mechanical.assembly_clearance_mm', 'enforced': True, 'rationale': ''})
constraint(data={'kind': 'mounting_hole_keepout', 'target_ref': None, 'params': {'x_mm': 66.0, 'y_mm': 4.0, 'keepout_radius_mm': 2.4}, 'derived_from': 'mechanical.mounting_holes[1] + mechanical.assembly_clearance_mm', 'enforced': True, 'rationale': ''})
constraint(data={'kind': 'mounting_hole_keepout', 'target_ref': None, 'params': {'x_mm': 4.0, 'y_mm': 46.0, 'keepout_radius_mm': 2.4}, 'derived_from': 'mechanical.mounting_holes[2] + mechanical.assembly_clearance_mm', 'enforced': True, 'rationale': ''})
constraint(data={'kind': 'mounting_hole_keepout', 'target_ref': None, 'params': {'x_mm': 66.0, 'y_mm': 46.0, 'keepout_radius_mm': 2.4}, 'derived_from': 'mechanical.mounting_holes[3] + mechanical.assembly_clearance_mm', 'enforced': True, 'rationale': ''})
constraint(data={'kind': 'connector_edge', 'target_ref': 'J1', 'params': {'side': 'front', 'max_edge_distance_mm': 5.0}, 'derived_from': 'mechanical.connector_interfaces + mechanical.max_connector_edge_distance_mm', 'enforced': True, 'rationale': ''})
constraint(data={'kind': 'decoupling_proximity', 'target_ref': 'C1', 'params': {'power_nets': ['GND', 'V3V3'], 'target_ref': 'U1', 'target_source': 'explicit_decoupling_target_ref', 'max_distance_mm': 12.0}, 'derived_from': 'graph decoupling component pins + decoupling target inference', 'enforced': True, 'rationale': ''})
constraint(data={'kind': 'decoupling_proximity', 'target_ref': 'C2', 'params': {'power_nets': ['GND', 'V3V3'], 'target_ref': 'U1', 'target_source': 'explicit_decoupling_target_ref', 'max_distance_mm': 12.0}, 'derived_from': 'graph decoupling component pins + decoupling target inference', 'enforced': True, 'rationale': ''})
constraint(data={'kind': 'thermal_spacing', 'target_ref': 'Q1', 'params': {'min_spacing_mm': 8.0}, 'derived_from': 'graph power-category component', 'enforced': False, 'rationale': 'Advisory spacing only; thermal qualification requires load testing.'})
constraint(data={'kind': 'thermal_spacing', 'target_ref': 'U3', 'params': {'min_spacing_mm': 8.0}, 'derived_from': 'graph power-category component', 'enforced': False, 'rationale': 'Advisory spacing only; thermal qualification requires load testing.'})

semantic_schematic = board.to_dict()
