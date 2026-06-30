"""Generated executable semantic schematic for agent review.

This file is intentionally compact and pin-name based. It can be executed
to reconstruct the normalized semantic_schematic model. Native EDA/CAD
artifacts are generated from typed artifacts; edit the spec or graph-producing
blocks when changing a generated design.
"""

from hw_codesign.semantic_schematic import SemanticBoard, pin

board = SemanticBoard(
    project='bench_rp2040_usb_hid_b204035a',
    revision=None,
    purpose='LLM-suited schematic representation derived from typed graph; native EDA files are generated outputs.',
    source_graph='/Users/mrcha033/Documents/hw-cli/projects/bench_rp2040_usb_hid_b204035a/electronics/generated/electrical_graph.json',
    board_width_mm=51.0,
    board_height_mm=21.0,
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
    component_id='grm155_100n',
    mpn='GRM155R71C104KA88D',
    manufacturer='Murata',
    package='0402',
    footprint='Capacitor_SMD:C_0402_1005Metric',
    pins=[
        pin('1', 'VCC', net='V3V3', role='power_in', voltage_domain='V3V3', mcu_pin=None),
        pin('2', 'GND', net='GND', role='ground', voltage_domain='GND', mcu_pin=None),
    ],
)

component(
    'C2',
    role='decoupling',
    value='100nF',
    component_id='grm155_100n',
    mpn='GRM155R71C104KA88D',
    manufacturer='Murata',
    package='0402',
    footprint='Capacitor_SMD:C_0402_1005Metric',
    pins=[
        pin('1', 'VCC', net='V3V3', role='power_in', voltage_domain='V3V3', mcu_pin=None),
        pin('2', 'GND', net='GND', role='ground', voltage_domain='GND', mcu_pin=None),
    ],
)

component(
    'C3',
    role='bulk_cap',
    value='10uF',
    component_id='grm188_10u',
    mpn='GRM188R60J106ME47D',
    manufacturer='Murata',
    package='0603',
    footprint='Capacitor_SMD:C_0603_1608Metric',
    pins=[
        pin('1', 'VCC', net='V3V3', role='power_in', voltage_domain='V3V3', mcu_pin=None),
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
    pins=[
        pin('1', 'DP_IN', net='USB_DP_RAW', role='bidirectional', voltage_domain=None, mcu_pin=None),
        pin('2', 'DP_OUT', net='USB_DP', role='bidirectional', voltage_domain=None, mcu_pin=None),
        pin('3', 'DM_IN', net='USB_DM_RAW', role='bidirectional', voltage_domain=None, mcu_pin=None),
        pin('4', 'DM_OUT', net='USB_DM', role='bidirectional', voltage_domain=None, mcu_pin=None),
        pin('5', 'GND', net='GND', role='ground', voltage_domain='GND', mcu_pin=None),
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
    pins=[
        pin('1', 'VBUS', net='USB_VBUS', role='power_in', voltage_domain='USB_5V', mcu_pin=None),
        pin('2', 'DP', net='USB_DP_RAW', role='bidirectional', voltage_domain=None, mcu_pin=None),
        pin('3', 'DM', net='USB_DM_RAW', role='bidirectional', voltage_domain=None, mcu_pin=None),
        pin('4', 'GND', net='GND', role='ground', voltage_domain='GND', mcu_pin=None),
    ],
)

component(
    'J2',
    role='debug_header',
    value='SWD 10-pin',
    component_id='conn_2x05_2p54',
    mpn='61201021621',
    manufacturer='Würth Elektronik',
    package='PinHeader-2x5-2.54mm',
    footprint='Connector_PinHeader_2.54mm:PinHeader_2x05_P2.54mm_Vertical',
    pins=[
        pin('1', 'P1', net='V3V3', role='power_in', voltage_domain='V3V3', mcu_pin=None),
        pin('2', 'P2', net='SWDIO', role='passive', voltage_domain=None, mcu_pin=None),
        pin('3', 'P3', net='GND', role='ground', voltage_domain='GND', mcu_pin=None),
        pin('4', 'P4', net='SWCLK', role='passive', voltage_domain=None, mcu_pin=None),
        pin('5', 'P5', net='GND', role='ground', voltage_domain='GND', mcu_pin=None),
        pin('6', 'P6', net='GND', role='ground', voltage_domain='GND', mcu_pin=None),
        pin('7', 'P7', net='GND', role='ground', voltage_domain='GND', mcu_pin=None),
        pin('8', 'P8', net='GND', role='ground', voltage_domain='GND', mcu_pin=None),
        pin('9', 'P9', net='GND', role='ground', voltage_domain='GND', mcu_pin=None),
        pin('10', 'P10', net='GND', role='ground', voltage_domain='GND', mcu_pin=None),
    ],
)

component(
    'U1',
    role='regulator_3v3',
    value='3V3 LDO',
    component_id='ap2112k_33trg1',
    mpn='AP2112K-3.3TRG1',
    manufacturer='Diodes Incorporated',
    package='SOT-23-5',
    footprint='Package_TO_SOT_SMD:SOT-23-5',
    pins=[
        pin('1', 'VIN', net='USB_VBUS', role='power_in', voltage_domain='USB_5V', mcu_pin=None),
        pin('2', 'GND', net='GND', role='ground', voltage_domain='GND', mcu_pin=None),
        pin('3', 'VOUT', net='V3V3', role='power_out', voltage_domain='V3V3', mcu_pin=None),
    ],
)

component(
    'U2',
    role='mcu',
    value='RP2040',
    component_id='rp2040',
    mpn='RP2040',
    manufacturer='Raspberry Pi Ltd',
    package='QFN-56',
    footprint='Package_DFN_QFN:QFN-56-1EP_7x7mm_P0.4mm_EP3.2x3.2mm',
    pins=[
        pin('1', 'QSPI_SD3', net='QSPI_D3', role='bidirectional', voltage_domain=None, mcu_pin=None),
        pin('2', 'QSPI_SD2', net='QSPI_D2', role='bidirectional', voltage_domain=None, mcu_pin=None),
        pin('3', 'QSPI_SD1', net='QSPI_MISO', role='bidirectional', voltage_domain=None, mcu_pin=None),
        pin('4', 'QSPI_SD0', net='QSPI_MOSI', role='bidirectional', voltage_domain=None, mcu_pin=None),
        pin('5', 'QSPI_SCK', net='QSPI_CLK', role='output', voltage_domain=None, mcu_pin=None),
        pin('6', 'QSPI_SS_N', net='QSPI_CS', role='output', voltage_domain=None, mcu_pin=None),
        pin('11', 'USB_DM', net='USB_DM', role='bidirectional', voltage_domain=None, mcu_pin=None),
        pin('12', 'USB_DP', net='USB_DP', role='bidirectional', voltage_domain=None, mcu_pin=None),
        pin('16', 'TESTEN', net='GND', role='ground', voltage_domain='GND', mcu_pin=None),
        pin('17', 'XIN', net='XIN', role='passive', voltage_domain=None, mcu_pin=None),
        pin('18', 'XOUT', net='XOUT', role='passive', voltage_domain=None, mcu_pin=None),
        pin('33', 'GND', net='GND', role='ground', voltage_domain='GND', mcu_pin=None),
        pin('34', 'GND', net='GND', role='ground', voltage_domain='GND', mcu_pin=None),
        pin('40', 'SWCLK', net='SWCLK', role='input', voltage_domain=None, mcu_pin=None),
        pin('41', 'SWDIO', net='SWDIO', role='bidirectional', voltage_domain=None, mcu_pin=None),
        pin('44', 'DVDD', net='V3V3', role='power_in', voltage_domain='V3V3', mcu_pin=None),
        pin('45', 'VDD', net='V3V3', role='power_in', voltage_domain='V3V3', mcu_pin=None),
        pin('46', 'VDD', net='V3V3', role='power_in', voltage_domain='V3V3', mcu_pin=None),
        pin('47', 'VDD', net='V3V3', role='power_in', voltage_domain='V3V3', mcu_pin=None),
        pin('48', 'VDD', net='V3V3', role='power_in', voltage_domain='V3V3', mcu_pin=None),
    ],
)

component(
    'U3',
    role='flash',
    value='2MB QSPI',
    component_id='w25q16jv',
    mpn='W25Q16JVSSIQ',
    manufacturer='Winbond Electronics',
    package='SOIC-8',
    footprint='Package_SO:SOIC-8_3.9x4.9mm_P1.27mm',
    pins=[
        pin('1', '~CS', net='QSPI_CS', role='input', voltage_domain=None, mcu_pin=None),
        pin('2', 'IO1', net='QSPI_MISO', role='bidirectional', voltage_domain=None, mcu_pin=None),
        pin('3', '~WP', net='QSPI_D2', role='bidirectional', voltage_domain=None, mcu_pin=None),
        pin('4', 'GND', net='GND', role='ground', voltage_domain='GND', mcu_pin=None),
        pin('5', 'IO0', net='QSPI_MOSI', role='bidirectional', voltage_domain=None, mcu_pin=None),
        pin('6', 'CLK', net='QSPI_CLK', role='input', voltage_domain=None, mcu_pin=None),
        pin('7', '~HOLD', net='QSPI_D3', role='bidirectional', voltage_domain=None, mcu_pin=None),
        pin('8', 'VCC', net='V3V3', role='power_in', voltage_domain='V3V3', mcu_pin=None),
    ],
)

component(
    'X1',
    role='crystal_12m',
    value='12MHz XTAL',
    component_id='abm8_12m',
    mpn='ABM8-12.000MHZ-B2-T',
    manufacturer='Abracon',
    package='HC-49S-SMD',
    footprint='Crystal:Crystal_SMD_HC-49S',
    pins=[
        pin('1', 'XIN', net='XIN', role='passive', voltage_domain=None, mcu_pin=None),
        pin('2', 'XOUT', net='XOUT', role='passive', voltage_domain=None, mcu_pin=None),
    ],
)


net('GND', signal_class='ground', voltage_domain='GND', required_track_width_mm=0.15)
connect('C1', pin='GND', number='2', net='GND', role='ground', mcu_pin=None)
connect('C2', pin='GND', number='2', net='GND', role='ground', mcu_pin=None)
connect('C3', pin='GND', number='2', net='GND', role='ground', mcu_pin=None)
connect('D1', pin='GND', number='5', net='GND', role='ground', mcu_pin=None)
connect('J1', pin='GND', number='4', net='GND', role='ground', mcu_pin=None)
connect('J2', pin='P10', number='10', net='GND', role='ground', mcu_pin=None)
connect('J2', pin='P3', number='3', net='GND', role='ground', mcu_pin=None)
connect('J2', pin='P5', number='5', net='GND', role='ground', mcu_pin=None)
connect('J2', pin='P6', number='6', net='GND', role='ground', mcu_pin=None)
connect('J2', pin='P7', number='7', net='GND', role='ground', mcu_pin=None)
connect('J2', pin='P8', number='8', net='GND', role='ground', mcu_pin=None)
connect('J2', pin='P9', number='9', net='GND', role='ground', mcu_pin=None)
connect('U1', pin='GND', number='2', net='GND', role='ground', mcu_pin=None)
connect('U2', pin='TESTEN', number='16', net='GND', role='ground', mcu_pin=None)
connect('U2', pin='GND', number='33', net='GND', role='ground', mcu_pin=None)
connect('U2', pin='GND', number='34', net='GND', role='ground', mcu_pin=None)
connect('U3', pin='GND', number='4', net='GND', role='ground', mcu_pin=None)

net('QSPI_CLK', signal_class='signal', voltage_domain='V3V3', required_track_width_mm=0.15)
connect('U2', pin='QSPI_SCK', number='5', net='QSPI_CLK', role='output', mcu_pin=None)
connect('U3', pin='CLK', number='6', net='QSPI_CLK', role='input', mcu_pin=None)

net('QSPI_CS', signal_class='signal', voltage_domain='V3V3', required_track_width_mm=0.15)
connect('U2', pin='QSPI_SS_N', number='6', net='QSPI_CS', role='output', mcu_pin=None)
connect('U3', pin='~CS', number='1', net='QSPI_CS', role='input', mcu_pin=None)

net('QSPI_D2', signal_class='signal', voltage_domain='V3V3', required_track_width_mm=0.15)
connect('U2', pin='QSPI_SD2', number='2', net='QSPI_D2', role='bidirectional', mcu_pin=None)
connect('U3', pin='~WP', number='3', net='QSPI_D2', role='bidirectional', mcu_pin=None)

net('QSPI_D3', signal_class='signal', voltage_domain='V3V3', required_track_width_mm=0.15)
connect('U2', pin='QSPI_SD3', number='1', net='QSPI_D3', role='bidirectional', mcu_pin=None)
connect('U3', pin='~HOLD', number='7', net='QSPI_D3', role='bidirectional', mcu_pin=None)

net('QSPI_MISO', signal_class='signal', voltage_domain='V3V3', required_track_width_mm=0.15)
connect('U2', pin='QSPI_SD1', number='3', net='QSPI_MISO', role='bidirectional', mcu_pin=None)
connect('U3', pin='IO1', number='2', net='QSPI_MISO', role='bidirectional', mcu_pin=None)

net('QSPI_MOSI', signal_class='signal', voltage_domain='V3V3', required_track_width_mm=0.15)
connect('U2', pin='QSPI_SD0', number='4', net='QSPI_MOSI', role='bidirectional', mcu_pin=None)
connect('U3', pin='IO0', number='5', net='QSPI_MOSI', role='bidirectional', mcu_pin=None)

net('SWCLK', signal_class='signal', voltage_domain='V3V3', required_track_width_mm=0.15)
connect('J2', pin='P4', number='4', net='SWCLK', role='passive', mcu_pin=None)
connect('U2', pin='SWCLK', number='40', net='SWCLK', role='input', mcu_pin=None)

net('SWDIO', signal_class='signal', voltage_domain='V3V3', required_track_width_mm=0.15)
connect('J2', pin='P2', number='2', net='SWDIO', role='passive', mcu_pin=None)
connect('U2', pin='SWDIO', number='41', net='SWDIO', role='bidirectional', mcu_pin=None)

net('USB_DM', signal_class='usb', voltage_domain='V3V3', required_track_width_mm=0.15)
connect('D1', pin='DM_OUT', number='4', net='USB_DM', role='bidirectional', mcu_pin=None)
connect('U2', pin='USB_DM', number='11', net='USB_DM', role='bidirectional', mcu_pin=None)

net('USB_DM_RAW', signal_class='usb', voltage_domain='V3V3', required_track_width_mm=0.15)
connect('D1', pin='DM_IN', number='3', net='USB_DM_RAW', role='bidirectional', mcu_pin=None)
connect('J1', pin='DM', number='3', net='USB_DM_RAW', role='bidirectional', mcu_pin=None)

net('USB_DP', signal_class='usb', voltage_domain='V3V3', required_track_width_mm=0.15)
connect('D1', pin='DP_OUT', number='2', net='USB_DP', role='bidirectional', mcu_pin=None)
connect('U2', pin='USB_DP', number='12', net='USB_DP', role='bidirectional', mcu_pin=None)

net('USB_DP_RAW', signal_class='usb', voltage_domain='V3V3', required_track_width_mm=0.15)
connect('D1', pin='DP_IN', number='1', net='USB_DP_RAW', role='bidirectional', mcu_pin=None)
connect('J1', pin='DP', number='2', net='USB_DP_RAW', role='bidirectional', mcu_pin=None)

net('USB_VBUS', signal_class='power', voltage_domain='USB_5V', required_track_width_mm=0.5)
connect('J1', pin='VBUS', number='1', net='USB_VBUS', role='power_in', mcu_pin=None)
connect('U1', pin='VIN', number='1', net='USB_VBUS', role='power_in', mcu_pin=None)

net('V3V3', signal_class='power', voltage_domain='V3V3', required_track_width_mm=0.5)
connect('C1', pin='VCC', number='1', net='V3V3', role='power_in', mcu_pin=None)
connect('C2', pin='VCC', number='1', net='V3V3', role='power_in', mcu_pin=None)
connect('C3', pin='VCC', number='1', net='V3V3', role='power_in', mcu_pin=None)
connect('J2', pin='P1', number='1', net='V3V3', role='power_in', mcu_pin=None)
connect('U1', pin='VOUT', number='3', net='V3V3', role='power_out', mcu_pin=None)
connect('U2', pin='DVDD', number='44', net='V3V3', role='power_in', mcu_pin=None)
connect('U2', pin='VDD', number='45', net='V3V3', role='power_in', mcu_pin=None)
connect('U2', pin='VDD', number='46', net='V3V3', role='power_in', mcu_pin=None)
connect('U2', pin='VDD', number='47', net='V3V3', role='power_in', mcu_pin=None)
connect('U2', pin='VDD', number='48', net='V3V3', role='power_in', mcu_pin=None)
connect('U3', pin='VCC', number='8', net='V3V3', role='power_in', mcu_pin=None)

net('XIN', signal_class='signal', voltage_domain='V3V3', required_track_width_mm=0.15)
connect('U2', pin='XIN', number='17', net='XIN', role='passive', mcu_pin=None)
connect('X1', pin='XIN', number='1', net='XIN', role='passive', mcu_pin=None)

net('XOUT', signal_class='signal', voltage_domain='V3V3', required_track_width_mm=0.15)
connect('U2', pin='XOUT', number='18', net='XOUT', role='passive', mcu_pin=None)
connect('X1', pin='XOUT', number='2', net='XOUT', role='passive', mcu_pin=None)


place('C1', data={'ref': 'C1', 'x_mm': 3.0, 'y_mm': 5.0, 'rotation_deg': 0.0, 'side': 'top', 'courtyard_w_mm': 4.243, 'courtyard_h_mm': 4.243, 'source': 'rp2040_usb_hid_anchor', 'rationale': ''})
place('C2', data={'ref': 'C2', 'x_mm': 7.0, 'y_mm': 5.0, 'rotation_deg': 0.0, 'side': 'top', 'courtyard_w_mm': 4.243, 'courtyard_h_mm': 4.243, 'source': 'rp2040_usb_hid_anchor', 'rationale': ''})
place('C3', data={'ref': 'C3', 'x_mm': 11.0, 'y_mm': 5.0, 'rotation_deg': 0.0, 'side': 'top', 'courtyard_w_mm': 4.243, 'courtyard_h_mm': 4.243, 'source': 'rp2040_usb_hid_anchor', 'rationale': ''})
place('D1', data={'ref': 'D1', 'x_mm': 10.0, 'y_mm': 10.0, 'rotation_deg': 0.0, 'side': 'top', 'courtyard_w_mm': 6.708, 'courtyard_h_mm': 6.708, 'source': 'rp2040_usb_hid_anchor', 'rationale': ''})
place('J1', data={'ref': 'J1', 'x_mm': 3.0, 'y_mm': 10.0, 'rotation_deg': 0.0, 'side': 'top', 'courtyard_w_mm': 6.0, 'courtyard_h_mm': 6.0, 'source': 'rp2040_usb_hid_anchor', 'rationale': ''})
place('J2', data={'ref': 'J2', 'x_mm': 18.0, 'y_mm': 12.0, 'rotation_deg': 0.0, 'side': 'top', 'courtyard_w_mm': 9.487, 'courtyard_h_mm': 9.487, 'source': 'rp2040_usb_hid_anchor', 'rationale': ''})
place('U1', data={'ref': 'U1', 'x_mm': 3.0, 'y_mm': 15.0, 'rotation_deg': 0.0, 'side': 'top', 'courtyard_w_mm': 5.196, 'courtyard_h_mm': 5.196, 'source': 'rp2040_usb_hid_anchor', 'rationale': ''})
place('U2', data={'ref': 'U2', 'x_mm': 18.0, 'y_mm': 4.0, 'rotation_deg': 0.0, 'side': 'top', 'courtyard_w_mm': 13.416, 'courtyard_h_mm': 13.416, 'source': 'rp2040_usb_hid_anchor', 'rationale': ''})
place('U3', data={'ref': 'U3', 'x_mm': 33.0, 'y_mm': 2.0, 'rotation_deg': 0.0, 'side': 'top', 'courtyard_w_mm': 8.485, 'courtyard_h_mm': 8.485, 'source': 'rp2040_usb_hid_anchor', 'rationale': ''})
place('X1', data={'ref': 'X1', 'x_mm': 33.0, 'y_mm': 10.0, 'rotation_deg': 0.0, 'side': 'top', 'courtyard_w_mm': 4.243, 'courtyard_h_mm': 4.243, 'source': 'rp2040_usb_hid_anchor', 'rationale': ''})

constraint(data={'kind': 'board_keepout', 'target_ref': None, 'params': {'width_mm': 51.0, 'height_mm': 21.0, 'edge_margin_mm': 0.127}, 'derived_from': 'mechanical.envelope + manufacturing.pcb.min_clearance_mm', 'enforced': True, 'rationale': ''})
constraint(data={'kind': 'mounting_hole_keepout', 'target_ref': None, 'params': {'x_mm': 2.5, 'y_mm': 2.5, 'keepout_radius_mm': 1.6}, 'derived_from': 'mechanical.mounting_holes[0] + mechanical.assembly_clearance_mm', 'enforced': True, 'rationale': ''})
constraint(data={'kind': 'mounting_hole_keepout', 'target_ref': None, 'params': {'x_mm': 48.5, 'y_mm': 2.5, 'keepout_radius_mm': 1.6}, 'derived_from': 'mechanical.mounting_holes[1] + mechanical.assembly_clearance_mm', 'enforced': True, 'rationale': ''})
constraint(data={'kind': 'mounting_hole_keepout', 'target_ref': None, 'params': {'x_mm': 2.5, 'y_mm': 18.5, 'keepout_radius_mm': 1.6}, 'derived_from': 'mechanical.mounting_holes[2] + mechanical.assembly_clearance_mm', 'enforced': True, 'rationale': ''})
constraint(data={'kind': 'mounting_hole_keepout', 'target_ref': None, 'params': {'x_mm': 48.5, 'y_mm': 18.5, 'keepout_radius_mm': 1.6}, 'derived_from': 'mechanical.mounting_holes[3] + mechanical.assembly_clearance_mm', 'enforced': True, 'rationale': ''})
constraint(data={'kind': 'decoupling_proximity', 'target_ref': 'C1', 'params': {'power_nets': ['GND', 'V3V3'], 'target_ref': None, 'max_distance_mm': 12.0}, 'derived_from': 'graph decoupling component pins + decoupling_target_ref', 'enforced': False, 'rationale': 'Cap-to-IC association is not modelled in the netlist; proximity enforcement is deferred.'})
constraint(data={'kind': 'decoupling_proximity', 'target_ref': 'C2', 'params': {'power_nets': ['GND', 'V3V3'], 'target_ref': None, 'max_distance_mm': 12.0}, 'derived_from': 'graph decoupling component pins + decoupling_target_ref', 'enforced': False, 'rationale': 'Cap-to-IC association is not modelled in the netlist; proximity enforcement is deferred.'})

semantic_schematic = board.to_dict()
