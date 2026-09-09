# Schematic Review Checklist

Work through each section in order. For targeted reviews, jump to the relevant section.
Check off each item as verified. Flag anything that fails with its severity (Critical / Major / Minor / Info).

---

## 1. Power & Decoupling

- [ ] All expected power rails present and correctly named (3V3, 1V8, 1V2, GND, etc.)
- [ ] Every IC has at least one decoupling capacitor on each VCC/VDD pin — typically 100 nF placed close to the pin
- [ ] Bulk capacitors present at power entry points and regulators
- [ ] Capacitor voltage ratings ≥ 1.5× the operating rail voltage
  - e.g. on a 3.3 V rail: rating must be ≥ 5 V; on a 5 V rail: rating must be ≥ 7.5 V
- [ ] Power-good / enable sequencing signals connected if required by the design

---

## 2. I2C Buses

For each I2C bus in the design:

- [ ] **Unique addresses**: list every device on the bus with its 7-bit address. Flag any duplicates — these will cause bus conflicts and system failure.
  - Check A0/A1/A2 address-select pins in the connections report and cross-reference with the device datasheet to determine the resulting address.
- [ ] Pull-up resistors present on both SDA and SCL
  - Typical values: 4.7 kΩ for 100 kHz (standard mode), 2.2 kΩ for 400 kHz (fast mode), 1 kΩ for 1 MHz (fast-mode plus)
  - Verify pull-up voltage matches the bus logic level
- [ ] All devices on the same bus operate at the same voltage level (or level-shifting is present)
- [ ] Address-select pins (A0, A1, A2) tied to a defined logic level — not floating

---

## 3. SPI Buses

For each SPI bus:

- [ ] Each device has its own dedicated chip select (CS/SS) line — not shared
- [ ] SPI mode (CPOL / CPHA) is compatible across all devices that share the bus
- [ ] Maximum clock frequency is within the slowest device's specification
- [ ] MISO line has a pull-up or pull-down if any device can tri-state it

---

## 4. Component Ratings

- [ ] **Capacitor voltage ratings**: every capacitor rated at ≥ 1.5× the voltage of its rail
- [ ] **Resistor power ratings**: every resistor rated at ≥ 2× the estimated dissipation
  - Use package size as a guide: 0402 ≈ 0.063 W, 0603 ≈ 0.1 W, 0805 ≈ 0.125 W, 1206 ≈ 0.25 W
- [ ] No components exceeding their absolute maximum ratings (voltage, current, temperature)

---

## 5. IC Pin Verification (for key ICs)

For each IC being reviewed:

- [ ] All VCC / VDD / AVDD / DVDD pins connected to the correct rail
- [ ] All GND / AGND / PGND / DGND pins connected to ground
- [ ] Enable, shutdown, and reset pins driven to an appropriate level or pulled up/down per datasheet
- [ ] Unused digital inputs not floating — tied to VCC or GND per the datasheet recommendation
- [ ] Open-drain or open-collector outputs have pull-up resistors to the correct voltage
- [ ] Exposed pad / thermal pad connected to GND (or as specified in datasheet)
- [ ] NC (no-connect) pins left unconnected as instructed by the datasheet

---

## 6. Passive Components

- [ ] Pull-up resistors on I2C lines, open-drain signals, reset lines (see Section 2 for I2C specifics)
- [ ] Pull-down resistors on active-low enables that default to off
- [ ] Series termination resistors on high-speed lines where specified (USB, Ethernet, clock outputs)
- [ ] Ferrite beads on analog / RF power supply pins where specified in the datasheet
- [ ] Filter capacitors on ADC reference pins and analog supply pins

---

## 7. Engineering Notes (from PDF)

Read all pages of the schematic PDF and capture:

- [ ] Any written notes, warnings, or cautions on schematic pages
- [ ] **DNP (Do Not Populate)** components — list them and confirm they are intentional
- [ ] Revision history / change log — note any recent changes that may affect the review
- [ ] Assembly or manufacturing notes that affect component selection or placement
- [ ] Any deviations from the standard circuit explicitly called out by the designer

---

## 8. Net Voltage Coverage

Voltages are set by IC/inductor review agents — there is no automatic deduction step.

- [ ] After all tier reviews, check net voltage coverage:
  - Query: `SELECT name, voltage, confidence FROM nets WHERE type='power' ORDER BY confidence`
  - Flag power nets still at `hint` or `unknown` — means no agent has yet confirmed their driver
- [ ] All output rails from regulators and DC-DC converters have `voltage_source_ref` set
- [ ] Voltage spans (`voltage_min`/`voltage_max`) populated for all confirmed power rails

---

## 9. Power Sequencing

- [ ] Enable / power-good chains traced: regulators enable in the correct order
- [ ] ICs with UVLO pins have a resistor divider setting the threshold (not floating, not tied directly to rail)
- [ ] No load powered before its supplying regulator is confirmed on
- [ ] Soft-start components (cap on SS pin) present where specified

---

## 10. Board Interface (Cross-Board Verification)

When a `board_interface.json` exists for this board **and** its mating board(s):

- [ ] Load both `board_interface.json` files and compare mating connector pairs pin-by-pin
- [ ] **Signal name match**: every pin pair carries the same logical signal (or has a documented mapping)
- [ ] **Direction compatibility**: output↔input, power↔input; flag output↔output and input↔input
- [ ] **Voltage compatibility**: driving voltage ≤ receiver's abs-max; signal levels satisfy VIH/VIL
- [ ] **NC pins**: if one board leaves a pin NC, verify the mating board does not drive a critical signal onto it
- [ ] **Power rail consistency**: supply voltages on power pins match between boards (same nominal voltage)
- [ ] **Connector part numbers cross-check**: confirm mating pair (e.g. MH↔MS variant) is physically compatible

> See `references/board_interface_schema.md` for the `board_interface.json` format.  
> Store each board's file at `datasheets/boards/<PART_NUMBER>/board_interface.json`.

## 11. Future Checks

- [ ] Load both `board_interface.json` files and compare mating connector pairs pin-by-pin
- [ ] **Signal name match**: every pin pair carries the same logical signal (or has a documented mapping)
- [ ] **Direction compatibility**: output↔input, power↔input; flag output↔output and input↔input
- [ ] **Voltage compatibility**: driving voltage ≤ receiver's abs-max; signal levels satisfy VIH/VIL
- [ ] **NC pins**: if one board leaves a pin NC, verify the mating board does not drive a critical signal onto it
- [ ] **Power rail consistency**: supply voltages on power pins match between boards (same nominal voltage)
- [ ] **Connector part numbers cross-check**: confirm mating pair (e.g. MH↔MS variant) is physically compatible

> See `references/board_interface_schema.md` for the `board_interface.json` format.  
> Store each board's file at `datasheets/boards/<PART_NUMBER>/board_interface.json`.

## 11. Future Checks

> Note: BOM completeness, temperature ratings, supply chain, and power consumption are now automated — see §12–15.

- [ ] Crystal / oscillator load capacitors match the value specified in the device datasheet
- [ ] Differential pairs (USB D+/D−, Ethernet TX/RX, LVDS) are routed as a pair and noted on the schematic
- [ ] Every clock consumer has a confirmed clock source
- [ ] Clock buffers / fanout drivers used where one source drives many receivers
- [ ] Spread-spectrum clock settings match EMI requirements (if applicable)

---

## 12. BOM Completeness

Run `verify_bom.py` — issues are written to `review.db` automatically.

- [ ] All active (non-DNP) components have a manufacturer part number
- [ ] All ICs and key passives have a Highstage ID for procurement lookup
- [ ] All passives (R, C, L) have a value populated
- [ ] All components have a package/footprint specified
- [ ] DNP components are intentional — confirm with designer if uncertain

---

## 13. Temperature Ratings

Run `verify_temperature_ratings.py` — issues are written to `review.db` automatically.

Set board temperature range in `review.db` meta table if not already set:
```sql
INSERT OR REPLACE INTO meta (key, value) VALUES ('board_temp_min_c', '-40');
INSERT OR REPLACE INTO meta (key, value) VALUES ('board_temp_max_c', '85');
```

- [ ] All ICs have temperature rating data in their datasheet fields
  - Flag components skipped due to missing data — manually verify these
- [ ] No component rated below the board minimum operating temperature
- [ ] No component rated below the board maximum operating temperature
- [ ] Commercial-grade components (0–70°C) not used on industrial/automotive boards
- [ ] Tight margins (< 10°C headroom) reviewed and accepted by designer

---

## 14. Supply Chain & Obsolescence

Run `check_price_availability.py` then `check_obsolescence.py` — issues written to `review.db`.

- [ ] No components with lifecycle status `nrnd` (not recommended for new designs) — or documented exception
- [ ] No components with `last_time_buy` or `discontinued` status — or approved alternate sourcing plan
- [ ] No DIP/through-hole packages on an SMD board (unless intentional)
- [ ] All ICs have a manufacturer part number (prerequisite for obsolescence checking)
- [ ] Check Highstage for suggested replacements for any flagged parts

---

## 15. Power Consumption

Run `estimate_power_consumption.py` after enrichment — issues written to `review.db`.

- [ ] Total estimated board power within the power budget (if specified)
- [ ] No single component dissipating > 500 mW without adequate thermal management
- [ ] LDO dropout power < 250 mW — if higher, consider switching regulator
- [ ] High-power components (> 200 mW) have thermal relief on their pads
- [ ] Power estimate reviewed for unestimated components (those with no current data) — manually verify the high-power ones
