# Materials Filter Research Appendix

Per-family recommended facets from the 2026-06-08 research run (14 agents, Newegg-first
for IT-hardware families, DigiKey/Mouser/Arrow for component families). Guidance for the
§4.3 canonical-completeness audit — values to ensure exist in `commodity_seeds.json`
`enum_values`. "essential" → `is_primary: true` (expanded); rest fold under "More".

## Storage & Drives (hdd, ssd)
- **Essential:** Capacity (numeric), Form Factor (enum), Interface (enum), RPM (hdd, enum),
  Usage Class / Drive Type (enum: Desktop·NAS·Enterprise/Datacenter·Surveillance), Condition (global).
- **Advanced:** NAND Type (ssd), Cache/Buffer, Endurance (DWPD/TBW), Recording Tech (CMR/SMR), Brand.
- Interface canonical incl. **SCSI, FC, IDE/PATA** (hdd) and **NVMe PCIe 3/4/5, U.2/U.3** (ssd).

## Memory (dram, flash)
- **DRAM essential:** Type/Generation (DDR/DDR2…DDR5, LPDDR4/5), Capacity per Module (numeric),
  Module Form Factor (RDIMM/LRDIMM/UDIMM/SO-DIMM/DIMM), ECC (bool), Speed (numeric).
- **DRAM advanced:** Rank (1Rx8/2Rx4), Voltage, CAS latency, Buffering.
- **Flash essential:** Memory Technology (NAND/NOR/eMMC/SD), Density, Interface (SPI/I2C/Parallel/eMMC), Voltage.

## Connectors, Interconnects & Cables (connectors, cables, sockets)
- **Connector essential:** Family/Type (Rectangular/Circular/D-Sub/Terminal/Card-Edge/RJ45/USB/HDMI/Coax/Fiber/Backplane/FFC-FPC/Power),
  Positions/Pin Count (numeric), Pitch (enum of canonical pitches), Mounting (TH/SMT/Panel/Cable).
- **Cable essential:** Spec/Performance Class (HDMI 2.1/USB4/TB/Cat6/Cat6a/SAS-3/SATA), Length (numeric), End A/End B connectors.
- **Advanced:** Gender, Series (open/typeahead), Shielding.

## Electromechanical (relays, switches, motors)
- **Relay:** Type, Contact Form (SPST/SPDT/DPDT…), Coil Voltage (5/12/24/48VDC, 120/230VAC), Latching (bool).
- **Switch:** Switch Type (Tactile/Pushbutton/Toggle/Rocker/Slide/DIP/Rotary), Contact Form, Current Rating.
- **Motor:** Motor Type (DC Brushed/Brushless/Stepper/Servo/AC/Solenoid/Actuator), Voltage, Frame/Step Angle.

## Passives (capacitors, resistors, inductors, transformers, fuses, oscillators, filters)
- **Essential:** Mounting (SMD/THT), Package/Case (EIA 0402…2512, Radial/Axial), Primary Value
  (Capacitance/Resistance/Inductance/Frequency/Fuse-current — numeric), Voltage Rating, Dielectric (ceramic caps: C0G/X7R/X5R…).
- **Advanced:** Tolerance, Power Rating (resistors), ESR (electrolytic/tantalum), Temp Grade, Brand.

## Semiconductors — Discrete (diodes, transistors, mosfets, thyristors)
- **Essential:** Subtype (MOSFET/BJT/IGBT/Rectifier/Zener/TVS/SCR/TRIAC), Package/Case (TO-220/TO-247/SOT-23…),
  Breakdown/Standoff Voltage (numeric), Continuous Current (numeric), Polarity/Channel (N/P, NPN/PNP, Schottky/Zener/TVS).
- **Advanced:** Rds(on) (MOSFET), Diode speed/technology, AEC-Q101, Mounting, Brand.

## Semiconductors — ICs (analog_ic, logic_ic, power_ic)
- **Essential:** Function (Op-Amp/Comparator/ADC/DAC/Buck/Boost/LDO/Gate-Driver/Logic/Level-Translator),
  Package/Case, Mounting, Channels/Circuits, Supply Voltage (numeric).
- **Advanced:** Output V/I (power), Resolution bits (converters), Interface/Protocol, Temp grade, Brand.

## Processors & Programmable (microcontrollers, cpu, microprocessors, dsp, fpga, asic, gpu)
- **Essential:** Manufacturer/Brand, Series/Family (Core i9/Ryzen/Xeon/RTX/STM32/Artix), Core-Arch or Socket
  (ARM Cortex/RISC-V/x86; LGA1700/AM5/LGA4677), Cores/Clock (numeric), Memory Size (VRAM/Flash/Logic — numeric).
- **Advanced:** Package+Mounting (components), Integrated Graphics (cpu), Packaging (Tape&Reel/Tray), Temp grade.

## Power & Energy (power_supplies, voltage_regulators, batteries)
- **Essential:** Wattage (PSU, numeric), Form Factor/Type (ATX/SFX/1U/2U/CRPS/EPS12V), Battery Chemistry,
  Rechargeable (bool), Regulator Topology/Output V/I (voltage_regulators).
- **Advanced:** 80-PLUS efficiency, Redundancy/Hot-Swap (server PSU), Nominal Voltage/Capacity, Cell Size, Condition.

## Optoelectronics & Display (leds, displays, optoelectronics)
- **Essential:** Subtype/Class (Indicator/Power Emitter/7-Seg/Graphic/Monitor/Optoisolator/Photodetector),
  Color (LEDs), Mounting, Display Tech/Diagonal (displays), Optoisolator Output/Isolation V.
- **Advanced:** Package (LEDs), Luminous Flux/CCT, Resolution/Refresh/Panel (monitors), Interface, Brand.

## Sensors & RF (sensors, rf)
- Sensors: Type, Output (I2C/SPI/Analog/PWM), Supply V, Accuracy. RF: Device Type, Protocol/Standard,
  Frequency, Gain/Output Power, Impedance (50/75Ω).

## Noise-control rules (from synthesis)
- Subtype-gate deep facets (commodity selection drives which facets show).
- Collapse-by-default: ~5–7 essentials expanded, rest under "More/Advanced".
- High-cardinality (Manufacturer, Package, Series) → search + top-N, never a flat dump.
- Bucket continuous values as numeric ranges or canonical-rail enums (coil voltage, DDR gen, memory speed).
- Demote known-noisy specs to display-only (turns_ratio, load_capacitance, insertion_loss, CTR).
