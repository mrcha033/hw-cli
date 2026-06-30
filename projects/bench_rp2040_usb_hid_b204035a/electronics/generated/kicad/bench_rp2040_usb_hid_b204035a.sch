EESchema Schematic File Version 4
LIBS:power
EELAYER 29 0
EELAYER END
$Descr A4 11693 8268
Sheet 1 1
Title "Robot Controller"
$EndDescr
$Comp
L Connector_Generic:Conn_01x02 J1
U 1 1 3E8
P 1500 1200
F 0 "J1" H 1580 1400 50  0000 C CNN
F 1 "USB-C POWER" H 1580 1000 50  0000 C CNN
F 2 "Connector_USB:USB_C_GCT_USB4105" H 1500 1200 50  0001 C CNN
	1    0    0    -1
$EndComp
$Comp
L Connector_Generic:Conn_01x02 D1
U 1 1 3E9
P 3500 1200
F 0 "D1" H 3580 1400 50  0000 C CNN
F 1 "USB ESD" H 3580 1000 50  0000 C CNN
F 2 "Package_TO_SOT_SMD:SOT-23-6" H 3500 1200 50  0001 C CNN
	1    0    0    -1
$EndComp
$Comp
L Connector_Generic:Conn_01x02 U1
U 1 1 3EA
P 5500 1200
F 0 "U1" H 5580 1400 50  0000 C CNN
F 1 "3V3 LDO" H 5580 1000 50  0000 C CNN
F 2 "Package_TO_SOT_SMD:SOT-23-5" H 5500 1200 50  0001 C CNN
	1    0    0    -1
$EndComp
$Comp
L Connector_Generic:Conn_01x02 U2
U 1 1 3EB
P 7500 1200
F 0 "U2" H 7580 1400 50  0000 C CNN
F 1 "RP2040" H 7580 1000 50  0000 C CNN
F 2 "Package_DFN_QFN:QFN-56-1EP_7x7mm_P0.4mm_EP3.2x3.2mm" H 7500 1200 50  0001 C CNN
	1    0    0    -1
$EndComp
$Comp
L Connector_Generic:Conn_01x02 U3
U 1 1 3EC
P 1500 2100
F 0 "U3" H 1580 2300 50  0000 C CNN
F 1 "2MB QSPI" H 1580 1900 50  0000 C CNN
F 2 "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm" H 1500 2100 50  0001 C CNN
	1    0    0    -1
$EndComp
$Comp
L Connector_Generic:Conn_01x02 X1
U 1 1 3ED
P 3500 2100
F 0 "X1" H 3580 2300 50  0000 C CNN
F 1 "12MHz XTAL" H 3580 1900 50  0000 C CNN
F 2 "Crystal:Crystal_SMD_HC-49S" H 3500 2100 50  0001 C CNN
	1    0    0    -1
$EndComp
$Comp
L Connector_Generic:Conn_01x02 J2
U 1 1 3EE
P 5500 2100
F 0 "J2" H 5580 2300 50  0000 C CNN
F 1 "SWD 10-pin" H 5580 1900 50  0000 C CNN
F 2 "Connector_PinHeader_2.54mm:PinHeader_2x05_P2.54mm_Vertical" H 5500 2100 50  0001 C CNN
	1    0    0    -1
$EndComp
$Comp
L Connector_Generic:Conn_01x02 C1
U 1 1 3EF
P 7500 2100
F 0 "C1" H 7580 2300 50  0000 C CNN
F 1 "100nF" H 7580 1900 50  0000 C CNN
F 2 "Capacitor_SMD:C_0402_1005Metric" H 7500 2100 50  0001 C CNN
	1    0    0    -1
$EndComp
$Comp
L Connector_Generic:Conn_01x02 C2
U 1 1 3F0
P 1500 3000
F 0 "C2" H 1580 3200 50  0000 C CNN
F 1 "100nF" H 1580 2800 50  0000 C CNN
F 2 "Capacitor_SMD:C_0402_1005Metric" H 1500 3000 50  0001 C CNN
	1    0    0    -1
$EndComp
$Comp
L Connector_Generic:Conn_01x02 C3
U 1 1 3F1
P 3500 3000
F 0 "C3" H 3580 3200 50  0000 C CNN
F 1 "10uF" H 3580 2800 50  0000 C CNN
F 2 "Capacitor_SMD:C_0603_1608Metric" H 3500 3000 50  0001 C CNN
	1    0    0    -1
$EndComp
$Comp
L Connector_Generic:Conn_01x02 C4
U 1 1 3F2
P 5500 3000
F 0 "C4" H 5580 3200 50  0000 C CNN
F 1 "10uF USB_IN" H 5580 2800 50  0000 C CNN
F 2 "Capacitor_SMD:C_0603_1608Metric" H 5500 3000 50  0001 C CNN
	1    0    0    -1
$EndComp
$EndSCHEMATC
