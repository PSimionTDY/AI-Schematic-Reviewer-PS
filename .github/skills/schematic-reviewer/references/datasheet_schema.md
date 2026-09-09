# `datasheet.json` — Canonical Schema Reference

This document defines the canonical structure for `datasheets/<HIGHSTAGE_ID>/datasheet.json`.  
These files are the persistent AI-extraction cache: once written, subsequent reviews skip re-extraction.

> **Note:** Fields extracted from datasheets may not always be present. Agents should only populate fields they found explicitly in the datasheet — never infer or guess values for these fields. Missing fields are treated as `null` in the database.

---

## Overview

Each `datasheet.json` is written by the per-IC review agent (Step 4c) the first time it reads a datasheet.  
The file resides at `datasheets/<HIGHSTAGE_ID>/datasheet.json` in the repository root.

```
datasheets/
  IC1018063/
    LTC3626.pdf
    datasheet.json      ← written on first review, reused on all subsequent reviews
  IC1019510/
    LT8625S.pdf
    datasheet.json
```

### `ic_category` gating

The top-level `ic_category` field determines which optional sections are present:

| `ic_category` | Required optional section(s) |
|---|---|
| `switcher` | `switching` |
| `power_path` | `power_path` |
| `ldo` | `ldo` |
| `sensor` | `sensor` |
| `logic` | *(none beyond universal)* |
| `mcu` | *(none beyond universal)* |
| `fpga` | *(none beyond universal)* |
| `memory` | *(none beyond universal)* |
| `transceiver` | `interfaces` (populated) |
| `other` | *(none beyond universal)* |

Every `ic_category` value may also carry an `interfaces` section when the IC has a digital bus (I²C, SPI, PMBus, UART, etc.).

---

## Universal fields (all IC categories)

These fields MUST be present in every `datasheet.json`, regardless of `ic_category`.

```jsonc
{
  // ── Identity ──────────────────────────────────────────────────────────────
  "highstage_id":      "IC1018063",          // string — Highstage part number, e.g. IC1018063
  "mfg_part_number":   "LTC3626",            // string — manufacturer part number (base, no suffix)
  "orderable_part_number": "LTC3626EUFD#PBF",// string | null — full orderable MPN
  "manufacturer":      "Analog Devices",     // string | null
  "description":       "18V, 2.5A Synchronous Step-Down Regulator",  // string
  "packages":          ["4mm x 4mm QFN-20"], // string[] — all package options
  "datasheet_rev":     "Rev. C",             // string | null — datasheet revision
  "extracted_by":      "claude-sonnet-4.6",  // string — AI model that wrote this file
  "ic_category":       "switcher",           // string — see table above

  // ── Supply domains ────────────────────────────────────────────────────────
  // Maps each named power domain to its pin list and voltage range.
  "supply_domains": {
    "VIN": {
      "pins":          ["1", "2"],           // string[] — pin numbers for this domain
      "voltage_min_v": 3.6,                  // number | null
      "voltage_max_v": 20.0,                 // number | null
      "abs_max_v":     22.0,                 // number | null
      "typical_v":     null,                 // number | null — only for fixed-voltage outputs
      "description":   "Main power input"    // string
    }
    // ... one entry per named supply rail
  },

  // ── Absolute maximum ratings ───────────────────────────────────────────────
  // Free-form object — include all abs-max entries from the datasheet table.
  // Values are numbers or strings (for voltage-dependent limits such as "VIN + 0.3V").
  "absolute_max": {
    "vin_max_v":        22,
    "tj_operating_max_c": 125,
    "storage_temp_min_c": -65,
    "storage_temp_max_c": 150
    // add all relevant fields from the datasheet
  },

  // ── Logic levels ──────────────────────────────────────────────────────────
  // Voltage thresholds for digital control pins.
  // Express voltage-dependent levels as formula strings (see §Voltage formulas).
  "logic_levels": {
    "VIH_min_v":  1.2,             // number | string — e.g. "0.7 * VDD"
    "VIL_max_v":  0.4,             // number | string
    "VOH_min_v":  null,            // number | string | null
    "VOL_max_v":  null,            // number | string | null
    "applicable_pins": ["EN"],     // string[] | null — pins these thresholds apply to
    "note":       "Absolute thresholds (not VCC-referenced)"  // string | null
  },

  // ── Digital interfaces ─────────────────────────────────────────────────────
  // Set interface values to null when the IC does not implement that bus.
  "interfaces": {
    "i2c": null,    // null | object — see §Interfaces section
    "spi": null,    // null | object
    "pmbus": null,  // null | object
    "uart": null,   // null | object
    // any other bus the IC supports:
    "external_sync": { "pin": "SYNC", "frequency_range_hz": [500000, 3000000] }
  },

  // ── Pin table ─────────────────────────────────────────────────────────────
  // Keyed by pin number string. This is the authoritative source for pin types.
  "pins": {
    "1": {
      "name":         "VIN",          // string — pin name exactly as in datasheet
      "direction":    "power_in",     // string — see §Pin direction values
      "supply_domain": "VIN",         // string | null — maps to supply_domains key
      "description":  "Power input. Decouple with 10µF ceramic to GND."  // string
    }
    // ... one entry per pin
  },

  // ── Required external components ─────────────────────────────────────────
  // Checklist for schematic verification. Scripts use this list to flag
  // missing required passives as major issues.
  "required_external_components": [
    {
      "pin":        "BOOST",            // string — pin name (as in datasheet)
      "component":  "0.1µF cap to SW", // string — human-readable description
      "required":   true,              // boolean — true = flag if missing, false = optional
      "constraint": "Ceramic, X5R/X7R. Voltage rating ≥ VIN + INTVCC."  // string | null
    }
  ],

  // ── Design equations ──────────────────────────────────────────────────────
  // Formulas needed to verify passive component values in the schematic
  // (e.g. switching frequency set by a resistor, output voltage set by a divider).
  // SCHEMATIC-RELEVANT ONLY — do NOT include firmware/software formulas such as
  // raw-ADC-to-physical-unit conversions, CRC polynomials, or communication
  // timing parameters. Those belong in firmware documentation, not here.
  // For sensors: omit raw→engineering-unit conversion equations entirely.
  "design_equations": [
    {
      "parameter": "switching_frequency",       // string — what is being set
      "formula":   "R_RT = 3.2e11 / f_sw",     // string — the equation
      "variables": {                            // object | null — units for each variable
        "R_RT": "Ω",
        "f_sw": "Hz"
      },
      "pins":  ["RT", "SGND"],                 // string[] | null — relevant pins
      "note":  "R_RT range: 105 kΩ–640 kΩ"    // string | null
    }
  ],

  // ── Notes ─────────────────────────────────────────────────────────────────
  // Free-form list of design rules, warnings, and important observations
  // extracted from the datasheet. This is the PRIMARY input to schematic
  // checklist items — reviewers and automated scripts scan this list to
  // identify potential issues. Each note should be self-contained.
  "notes": [
    "CRITICAL: RUN pin must NOT be floated. Drive high (>1.25 V) to enable.",
    "Exposed pad (PGND) MUST be soldered — electrical and thermal requirement.",
    "Bootstrap cap (BOOST→SW) must be 0.1 µF ceramic, rated ≥ VIN + 3.3 V."
  ],

  // ── Temperature ratings ───────────────────────────────────────────────────
  "temp_rating_min_c":  -40,           // number | null — minimum operating temperature in °C
  "temp_rating_max_c":   85,           // number | null — maximum operating temperature in °C (85, 105, or 125 are typical)
  "temp_grade":          "industrial", // string | null — one of: commercial (0–70°C), industrial (-40–85°C), automotive (-40–125°C), military (-55–125°C)

  // ── Power / electrical ────────────────────────────────────────────────────
  "output_voltage_v":         3.3,     // number | null — for regulators/LDOs: nominal output voltage in volts
  "output_voltage_min_v":     3.267,   // number | null — minimum output voltage (from datasheet tolerance)
  "output_voltage_max_v":     3.333,   // number | null — maximum output voltage
  "output_current_max_a":     0.5,     // number | null — maximum output current in amps
  "quiescent_current_ua":     55,      // number | null — quiescent/standby current in microamps
  "supply_current_max_ma":    120,     // number | null — maximum supply current in milliamps (for non-regulators)
  "power_dissipation_max_mw": 625,     // number | null — maximum power dissipation in mW (from absolute max ratings)

  // ── Availability / supply chain ────────────────────────────────────────────
  "lifecycle_status": "active",        // string | null — one of: active, nrnd (not recommended for new designs), last_time_buy, discontinued, unknown
  "rohs_compliant":   true,            // boolean | null
  "reach_compliant":  true             // boolean | null
}
```

### Pin direction values

| Value | Meaning |
|---|---|
| `power_in` | Supply voltage input (VCC, VIN, AVDD, …) |
| `power_out` | Regulated supply output (INTVCC, VOUT, …) |
| `ground` | Ground reference (GND, PGND, AGND, EP) |
| `input` | Digital input |
| `output` | Digital or analog output |
| `open_drain` | Open-drain/open-collector output — needs external pull-up |
| `analog_in` | Analog input (threshold, feedback, reference pin) |
| `analog_out` | Analog output (monitor, DAC output) |
| `bidir` | Bidirectional digital (I²C SDA, etc.) |
| `no_connect` | NC pin — must not be connected |
| `passive` | Passive pin (crystal, filter capacitor, bootstrap) |

---

## Category-specific sections

### `switching` — Buck/boost/flyback regulators (`ic_category: "switcher"`)

```jsonc
"switching": {
  "topology":                   "synchronous buck",   // string
  "synchronous":                true,                 // boolean | null
  "frequency_range_hz":         [300000, 4000000],    // [min, max] | null
  "frequency_pin":              "RT",                 // string | null
  "frequency_formula":          "R_T(kΩ) = 114.8 / f_SW(MHz) − 10.4",  // string | null
  "output_voltage_range_v":     [0.6, 6.0],           // [min, max] | null
  "output_voltage_formula":     "V_OUT = 0.6 * (1 + R1/R2)",  // string | null
  "feedback_reference_v":       0.6,                  // number | null
  "feedback_pins":              ["FB"],               // string[]
  "current_limit_a":            2.5,                  // number | null — fixed limit
  "current_limit_adjustable":   true,                 // boolean
  "enable_pin":                 "RUN",               // string
  "enable_active":              "high",              // "high" | "low"
  "enable_threshold_rising_v":  1.23,                // number | null
  "enable_threshold_falling_v": 1.0,                 // number | null
  "soft_start_pin":             "SS",                // string | null
  "soft_start_formula":         "t_SS = 430000 * C_SS",  // string | null
  "compensation":               "internal",          // "internal" | "external" | "selectable"
  "compensation_pin":           "ITH",              // string | null
  "operating_modes": {                               // object | null
    "burst_mode":               "Float MODE/SYNC or tie to INTVCC",
    "forced_continuous":        "Tie MODE/SYNC to GND or apply clock",
    "pulse_skipping":           "Tie SYNC/MODE to GND"
  },
  "polyphase": {                                    // object | null
    "supported":    true,
    "max_phases":   12,
    "clkout_pin":   "CLKOUT"
  },
  "quiescent_current_shutdown_ua": 13,             // number | null
  "min_on_time_ns":   20,                          // number | null
  "top_switch_rdson_mohm_typ": 115,                // number | null
  "bottom_switch_rdson_mohm_typ": 70               // number | null
}
```

### `power_path` — eFuses, load switches, ideal diodes (`ic_category: "power_path"`)

```jsonc
"power_path": {
  "vin_range":                [2.7, 23.0],           // [min_v, max_v]
  "current_limit_a":          null,                  // number | null — fixed limit
  "current_limit_formula":    "R_ILM(Ω) = 3334 / I_LIM(A)",  // string | null
  "current_limit_pin":        "ILM",                // string | null
  "current_limit_range_a":    [0.5, 6.0],           // [min, max] | null
  "inrush_control":           true,                 // boolean
  "inrush_pin":               "DVDT",              // string | null
  "inrush_formula":           "C_DVDT(pF) = 2000 / SR(V/ms)",  // string | null
  "overcurrent_response":     "active_current_limit",  // string | object
  "fault_recovery":           "auto_retry after 110 ms",  // string | object
  "enable_pin":               "EN/UVLO",           // string
  "enable_active":            "high",              // "high" | "low"
  "power_good_pin":           "PG",               // string | null
  "power_good_active":        "high",             // "high" | "low" | null
  "power_good_type":          "open_drain",       // "open_drain" | "push_pull" | null
  "fault_pin":                "FLT",             // string | null
  "fault_pin_active":         "low",             // "high" | "low" | null
  "fault_pin_type":           "open_drain",      // "open_drain" | "push_pull" | null
  "uvlo_threshold_v": {                          // object | null
    "rising_typ":  1.2,
    "falling_typ": 1.09,
    "description": "At EN/UVLO pin"
  },
  "uvp_threshold_v": {                           // object | null — fixed internal UVP
    "rising_typ":  2.53,
    "description": "Fixed internal threshold on IN pin"
  },
  "ovlo_threshold_v": {                          // object | null
    "rising_typ":  1.2,
    "pin_range":   [0.5, 1.5]
  },
  "thermal_shutdown_c":            154,          // number | null
  "thermal_shutdown_hysteresis_c": 10,           // number | null
  "ron_mohm": {                                  // object | null
    "typ_at_12v_25c": 28.3,
    "max_over_full_range": 45.0
  },
  "quiescent_current_ua": {                      // object | null
    "on_min": 428, "on_max": 610
  }
}
```

### `ldo` — Linear regulators (`ic_category: "ldo"`)

```jsonc
"ldo": {
  "vin_range":              [1.5, 5.5],     // [min_v, max_v]
  "vout_fixed_v":           3.3,            // number | null — null if adjustable
  "vout_range_v":           null,           // [min, max] | null — null if fixed
  "vout_formula":           null,           // string | null — for adjustable LDOs
  "feedback_reference_v":   null,           // number | null
  "dropout_v_typ":          0.3,            // number | null — at rated current
  "current_max_a":          0.5,            // number
  "enable_pin":             "EN",          // string | null
  "enable_active":          "high",        // "high" | "low" | null
  "power_good_pin":         "PG",          // string | null
  "power_good_type":        "open_drain",  // string | null
  "soft_start":             false,         // boolean | null
  "quiescent_current_ua":   55,            // number | null
  "thermal_shutdown_c":     150            // number | null
}
```

### `sensor` — Sensing ICs (temperature, humidity, pressure, current, …) (`ic_category: "sensor"`)

```jsonc
"sensor": {
  "measurement_types":  ["temperature", "humidity"],  // string[]
  "measurement_ranges": {                             // object — one entry per type
    "temperature": { "min": -40, "max": 125, "unit": "°C" },
    "humidity":    { "min": 0,   "max": 100, "unit": "%RH" }
  },
  "accuracy": {                                       // object | null
    "temperature": "±0.2 °C typ (0 °C – 60 °C)",
    "humidity":    "±2 %RH typ (10 %RH – 90 %RH)"
  },
  "resolution_bits": {                               // object | null
    "temperature": 16,
    "humidity":    16
  },
  "supply_voltage_v":    [2.4, 5.5],                 // [min, max]
  "supply_current_ua":   { "active": 600, "sleep": 0.2 },  // object | null
  "output_type":         "digital",                  // "digital" | "analog" | "pwm"
  "communication":       "i2c",                      // string | null
  "i2c_address":         "0x44",                     // string | null — hex
  "i2c_address_options": ["0x44", "0x45"],           // string[] | null
  "i2c_address_pin":     "ADDR",                    // string | null
  "response_time_s":     { "temperature": 8.3, "humidity": 8.3 }  // object | null
}
```

---

## Interfaces section detail

When the IC has a digital communication bus, populate the relevant sub-object within `interfaces`.  
All interfaces not present on the IC MUST be set to `null`.

### I²C

```jsonc
"i2c": {
  "pins":          { "SDA": "5", "SCL": "6" },   // object — pin name → pin number
  "address":       "0x44",                        // string | null — 7-bit hex
  "address_options": ["0x44", "0x45"],            // string[] | null
  "address_pin":   "ADDR",                        // string | null
  "speed_modes":   ["standard", "fast"],          // string[] — "standard"|"fast"|"fast-plus"|"hs"
  "voltage_levels": "0 to VDD",                   // string | null
  "pull_up_required": true,                        // boolean
  "pull_up_to":    "VDD",                         // string | null
  "note":          null                           // string | null
}
```

### SPI

```jsonc
"spi": {
  "pins": { "MOSI": "1", "MISO": "2", "SCK": "3", "CS": "4" },
  "max_clock_mhz":  10,                           // number | null
  "mode":           0,                            // 0|1|2|3 | null — CPOL/CPHA
  "cs_active":      "low",                        // "low" | "high"
  "word_length_bits": 8,                          // number | null
  "note":           null
}
```

### PMBus / SMBus

```jsonc
"pmbus": {
  "pins":    { "SDA": "3", "SCL": "4", "ALERT": "5" },
  "address": "0x40",                              // string | null
  "address_pin": "ADDR",                          // string | null
  "version": "PMBus 1.3",                         // string | null
  "alert_pin": "ALERT",                           // string | null
  "alert_active": "low",                          // "high" | "low" | null
  "note":    null
}
```

---

## Voltage-dependent logic level formulas

When logic-level thresholds depend on the supply voltage, express them as formula strings rather than fixed numbers.  
Use `VDD`, `VCC`, `VBAT`, or the actual supply domain name from `supply_domains`.

```jsonc
"logic_levels": {
  "VIH_min_v": "0.7 * VDD",     // 70% of supply voltage
  "VIL_max_v": "0.3 * VDD",     // 30% of supply voltage
  "VOH_min_v": "VDD - 0.4",     // rail minus 0.4 V
  "VOL_max_v": 0.4              // absolute threshold — use number
}
```

Supported formula tokens:
- Named supply domains: `VDD`, `VCC`, `VIO`, `VBAT`, `VREF`, or any key from `supply_domains`
- Arithmetic operators: `+`, `-`, `*`, `/`
- Numeric literals

Automated tools evaluate these by substituting the actual net voltage from `schematic.yaml`.

---

## Design equations structure

```jsonc
"design_equations": [
  {
    "parameter": "output_voltage",           // string — what the equation sets
    "formula":   "V_OUT = V_REF * (1 + R1/R2)",  // string — the equation
    "variables": {                           // object | null — each variable with its unit
      "V_OUT": "V",
      "V_REF": "V",
      "R1":    "Ω",
      "R2":    "Ω"
    },
    "pins":  ["FB"],                         // string[] | null — pins involved
    "note":  "V_REF = 0.6 V typ. R1: VOUT→FB, R2: FB→GND."  // string | null
  }
]
```

---

## Notes field — primary checklist input

The `notes` array is the **primary input** to automated schematic checklist items.  
Reviewers and scripts scan this list to generate warnings when schematic connections
appear to violate a constraint.

Guidelines for writing notes:
- Each note must be **self-contained** (no cross-references to other notes)
- Start with `CRITICAL:` for violations that will cause functional failure
- Start with `WARNING:` for violations that may cause reliability problems
- Notes about floating pins MUST name the pin: `"EN pin must NOT be left floating"`
- Notes about required passives MUST state the component and value: `"10 µF ceramic to GND on PVIN"`

The per-IC review agent reads `notes[]` and checks each one against the schematic connections.

---

## Minimal examples

### Switcher example (LTC3626-class)

```json
{
  "highstage_id": "IC1018063",
  "mfg_part_number": "LTC3626",
  "description": "18V, 2.5A Synchronous Step-Down Regulator",
  "packages": ["4mm × 4mm QFN-20"],
  "extracted_by": "claude-sonnet-4.6",
  "ic_category": "switcher",
  "supply_domains": {
    "PVIN": { "pins": ["15", "16"], "voltage_min_v": 3.6, "voltage_max_v": 20.0, "abs_max_v": 22.0, "description": "Power input" },
    "PGND": { "pins": ["21"], "voltage_min_v": 0, "voltage_max_v": 0, "description": "Power ground — exposed pad, MUST be soldered" }
  },
  "absolute_max": { "pvin_max_v": 22, "tj_operating_max_c": 125 },
  "switching": {
    "topology": "synchronous buck",
    "frequency_range_hz": [500000, 3000000],
    "frequency_pin": "RT",
    "frequency_formula": "R_RT(Ω) = 3.2e11 / f_sw(Hz)",
    "output_voltage_formula": "V_OUT = 0.6 * (1 + R1/R2)",
    "feedback_reference_v": 0.6,
    "feedback_pins": ["FB"],
    "enable_pin": "RUN",
    "enable_active": "high"
  },
  "logic_levels": { "VIH_min_v": 1.25, "VIL_max_v": 1.0, "note": "Absolute thresholds" },
  "interfaces": { "i2c": null, "spi": null, "pmbus": null },
  "pins": {
    "1":  { "name": "BOOST", "direction": "passive",   "supply_domain": null, "description": "Bootstrap cap (+). Connect 0.1 µF ceramic from BOOST to SW." },
    "21": { "name": "PGND",  "direction": "ground",    "supply_domain": "PGND", "description": "Exposed pad. MUST be soldered to PCB." }
  },
  "required_external_components": [
    { "pin": "BOOST",    "component": "0.1 µF cap to SW", "required": true, "constraint": "Ceramic, rated ≥ VIN + 3.3 V" },
    { "pin": "RT",       "component": "105 kΩ–640 kΩ resistor to GND", "required": true, "constraint": "Sets switching frequency" },
    { "pin": "PGND/EP",  "component": "Solder exposed pad to GND plane", "required": true, "constraint": "Thermal and electrical" }
  ],
  "design_equations": [
    { "parameter": "switching_frequency", "formula": "R_RT(Ω) = 3.2e11 / f_sw(Hz)", "variables": { "R_RT": "Ω", "f_sw": "Hz" }, "pins": ["RT"] }
  ],
  "notes": [
    "CRITICAL: RUN pin must NOT be floated — drive high to enable.",
    "CRITICAL: Exposed pad (PGND) MUST be soldered — thermal and electrical connection.",
    "Bootstrap cap 0.1 µF (BOOST→SW) required. Voltage rating ≥ VIN + INTVCC."
  ]
}
```

### Power-path example (TPS25947-class)

```json
{
  "highstage_id": "IC1017820",
  "mfg_part_number": "TPS25947",
  "description": "2.7–23 V, 5.5 A eFuse with Reverse Polarity Protection",
  "packages": ["QFN-10 2 mm × 2 mm"],
  "extracted_by": "claude-sonnet-4.6",
  "ic_category": "power_path",
  "supply_domains": {
    "VIN":  { "pins": [5], "voltage_min_v": 2.7, "voltage_max_v": 23.0, "abs_max_v": 28.0, "description": "Power input" },
    "VOUT": { "pins": [6], "voltage_min_v": 0,   "voltage_max_v": 23.0, "description": "Protected output" }
  },
  "absolute_max": { "vin_max_v": 28.0, "vin_min_v": -15.0 },
  "power_path": {
    "vin_range": [2.7, 23.0],
    "current_limit_formula": "R_ILM(Ω) = 3334 / I_LIM(A)",
    "current_limit_pin": "ILM",
    "current_limit_range_a": [0.5, 6.0],
    "enable_pin": "EN/UVLO",
    "enable_active": "high",
    "power_good_pin": "PG",
    "power_good_type": "open_drain",
    "thermal_shutdown_c": 154,
    "ron_mohm": { "typ_at_12v_25c": 28.3, "max_over_full_range": 45.0 }
  },
  "logic_levels": { "VIH_min_v": 1.183, "VIL_max_v": 1.116, "note": "Thresholds at EN/UVLO pin" },
  "interfaces": { "i2c": null, "spi": null, "pmbus": null },
  "pins": {
    "9": { "name": "ILM",     "direction": "analog_out", "supply_domain": null, "description": "Resistor to GND sets current limit. Do NOT leave floating." },
    "1": { "name": "EN/UVLO", "direction": "analog_in",  "supply_domain": null, "description": "Enable. Resistor divider from VIN. Do NOT leave floating." }
  },
  "required_external_components": [
    { "pin": "ILM",     "component": "R_ILM resistor to GND", "required": true, "constraint": "549–6650 Ω. Floating = near-zero limit, shorted = latched fault." },
    { "pin": "EN/UVLO", "component": "Pull-up or resistor divider from VIN", "required": true, "constraint": "Do not float." }
  ],
  "design_equations": [
    { "parameter": "current_limit", "formula": "R_ILM(Ω) = 3334 / I_LIM(A)", "variables": { "R_ILM": "Ω", "I_LIM": "A" }, "pins": ["ILM"] }
  ],
  "notes": [
    "ILM resistor MUST be present — floating = near-zero limit, shorted = latched fault shutdown.",
    "EN/UVLO must NOT be left floating. Tie to logic HIGH or use resistor divider.",
    "PG, AUXOFF, FLT are open-drain — all need external pull-up resistors."
  ]
}
```

### Sensor example (SHT30-class)

```json
{
  "highstage_id": "IC1012118",
  "mfg_part_number": "SHT30",
  "description": "Humidity and Temperature Sensor, I2C, ±2%RH, ±0.2°C",
  "packages": ["DFN-8 2.5 mm × 2.5 mm"],
  "extracted_by": "claude-sonnet-4.6",
  "ic_category": "sensor",
  "supply_domains": {
    "VDD": { "pins": ["1"], "voltage_min_v": 2.4, "voltage_max_v": 5.5, "description": "Supply voltage" },
    "VSS": { "pins": ["4"], "voltage_min_v": 0,   "voltage_max_v": 0,   "description": "Ground" }
  },
  "absolute_max": { "vdd_max_v": 6.0, "tj_max_c": 125 },
  "sensor": {
    "measurement_types": ["temperature", "humidity"],
    "measurement_ranges": {
      "temperature": { "min": -40, "max": 125, "unit": "°C" },
      "humidity":    { "min": 0,   "max": 100, "unit": "%RH" }
    },
    "accuracy": {
      "temperature": "±0.2 °C typ (0 °C – 60 °C)",
      "humidity":    "±2 %RH typ (10 %RH – 90 %RH)"
    },
    "supply_voltage_v": [2.4, 5.5],
    "supply_current_ua": { "active": 600, "sleep": 0.2 },
    "communication": "i2c",
    "i2c_address": "0x44",
    "i2c_address_options": ["0x44", "0x45"],
    "i2c_address_pin": "ADDR"
  },
  "logic_levels": {
    "VIH_min_v": "0.7 * VDD",
    "VIL_max_v": "0.3 * VDD",
    "note": "VDD-referenced thresholds"
  },
  "interfaces": {
    "i2c": {
      "pins": { "SDA": "4", "SCL": "3" },
      "address": "0x44",
      "address_options": ["0x44", "0x45"],
      "address_pin": "ADDR",
      "speed_modes": ["standard", "fast"],
      "pull_up_required": true,
      "pull_up_to": "VDD"
    },
    "spi": null,
    "pmbus": null
  },
  "pins": {
    "1": { "name": "VDD",  "direction": "power_in",  "supply_domain": "VDD", "description": "Supply 2.4–5.5 V. Decouple with 100 nF." },
    "2": { "name": "ADDR", "direction": "input",     "supply_domain": null,  "description": "I2C address select. GND = 0x44, VDD = 0x45. Do not float." },
    "3": { "name": "SCL",  "direction": "input",     "supply_domain": null,  "description": "I2C clock. Requires external pull-up to VDD." },
    "4": { "name": "SDA",  "direction": "bidir",     "supply_domain": null,  "description": "I2C data. Open-drain. Requires external pull-up to VDD." },
    "5": { "name": "ALERT","direction": "open_drain","supply_domain": null,  "description": "Alert/interrupt output. Open-drain. Requires external pull-up if used." },
    "6": { "name": "nRESET","direction": "input",    "supply_domain": null,  "description": "Active-low reset. Pull up to VDD; do not float." },
    "7": { "name": "R",    "direction": "no_connect","supply_domain": null,  "description": "Reserved. Leave unconnected." },
    "8": { "name": "VSS",  "direction": "ground",    "supply_domain": "VSS", "description": "Ground reference." }
  },
  "required_external_components": [
    { "pin": "VDD",    "component": "100 nF bypass capacitor to VSS", "required": true, "constraint": "Place close to pin. X5R/X7R ceramic." },
    { "pin": "SCL",    "component": "Pull-up resistor to VDD",        "required": true, "constraint": "Typically 4.7 kΩ for standard-mode I2C." },
    { "pin": "SDA",    "component": "Pull-up resistor to VDD",        "required": true, "constraint": "Typically 4.7 kΩ for standard-mode I2C." },
    { "pin": "ADDR",   "component": "Tie to GND or VDD",              "required": true, "constraint": "Do not float — selects I2C address." },
    { "pin": "nRESET", "component": "Pull-up resistor to VDD",        "required": false, "constraint": "10 kΩ recommended if pin is used. May be left unconnected if reset not needed." }
  ],
  "design_equations": [],
  "notes": [
    "ADDR pin must NOT be left floating — sets I2C address (GND=0x44, VDD=0x45).",
    "SCL and SDA require external pull-up resistors to VDD (typically 4.7 kΩ).",
    "100 nF bypass capacitor required on VDD, placed as close to pin as possible.",
    "nRESET is active-low; pull up to VDD with 10 kΩ if pin is routed to a connector or left undriven."
  ]
}
```

---

## Backward compatibility

Older reviews may have `datasheets/<ID>/pins.json` files written by the heuristic PDF extractor.  
Scripts that read `datasheet.json` MUST fall back to `pins.json` (using its `pins` section) when `datasheet.json` is absent.  
The `pins.json` format is a subset: `{ "highstage_id", "mfg_part_number", "source_pdf", "extracted_at", "pins": { "<num>": { "name", "type" } } }`.

When reading from `pins.json`, treat all data as `"extraction_method": "heuristic"` and low-confidence.

---

## Derived / computed fields

These fields are **not** extracted from datasheets. They are computed by review scripts and stored
alongside the extracted fields in the database. Agents must never populate these from a datasheet —
they are always calculated at review time.

```yaml
# Computed by verify_temperature_ratings.py
temp_margin_min_c: 15   # board_temp_min - temp_rating_min (positive = margin, negative = violation)
temp_margin_max_c: 40   # temp_rating_max - board_temp_max (positive = margin, negative = violation)

# Computed by estimate_power_consumption.py
estimated_power_mw: 165 # Estimated power draw at typical operating point
```

| Field | Computed by | Formula |
|---|---|---|
| `temp_margin_min_c` | `verify_temperature_ratings.py` | `temp_rating_min_c − board_temp_min_c` (positive = margin) |
| `temp_margin_max_c` | `verify_temperature_ratings.py` | `temp_rating_max_c − board_temp_max_c` (positive = margin) |
| `estimated_power_mw` | `estimate_power_consumption.py` | Typical operating-point estimate |
