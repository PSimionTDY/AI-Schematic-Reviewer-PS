# `board_interface.json` — Board Interface Descriptor Schema

This document defines the schema for `board_interface.json` files. These files describe a PCB's
external connectors and their signals, enabling cross-board interface verification between
mating PCBs.

---

## Location

```
datasheets/boards/<PART_NUMBER>/
  board_interface.json   ← one per board design
```

Example:
```
datasheets/boards/PCB_ASSY1019832-1C/
  board_interface.json
```

---

## Purpose

A `board_interface.json` allows the review pipeline to:

1. Verify that two mating connectors (e.g., XK-A on board A → XK-A' on board B) carry
   compatible signals on each pin.
2. Check voltage compatibility across connector boundaries.
3. Detect signal direction mismatches (two outputs connected together, floating inputs, etc.).
4. Provide human-readable connector documentation for design review.

---

## Schema

```jsonc
{
  // ── Board identity ─────────────────────────────────────────────────────────
  "board": {
    "name":           "Teledyne Reson RP3 RX CPU Board",  // string
    "part_number":    "PCB_ASSY1019832-1C",               // string — BOM part number
    "revision":       "2025a",                            // string — schematic revision
    "description":    "CPU/FPGA carrier board for ...",   // string
    "generated_from": "SCH_ID or netlist path",           // string | null
    "extracted_by":   "claude-sonnet-4.6"                 // string — AI model
  },

  // ── Connectors ─────────────────────────────────────────────────────────────
  "connectors": {
    "<REF>": {                        // schematic reference designator, e.g. "XK1", "J500"
      "part_number":    "LSHM-130-02.5-L-DV-A-N-K-TR",  // string — manufacturer part number
      "manufacturer":   "Samtec",                          // string | null
      "description":    "60-pin high-speed mezzanine connector",  // string
      "interface_type": "high_speed_mezzanine",           // string — see Interface Types below
      "interfaces":     ["GTH_TX", "GTH_RX", "power"],   // string[] — logical interface groups
      "mating_ref":     null,                             // string | null — ref on the mating board,
                                                          //   if known (e.g. "XK1" on the other board)
      "pins": {
        "<PIN>": {                    // pin number as string, e.g. "1", "A1", "GND"
          "name":          "GTH_TX0_P",   // string — human-readable signal name
          "net":           "U1_GTH_TX0_P",// string — schematic net name (from netlist)
          "direction":     "output",      // "input" | "output" | "bidir" | "power" | "gnd" | "nc"
          "voltage_level": "1.8V",        // string | null — expected voltage or range
          "interface_group": "GTH_TX",   // string | null — logical grouping within connector
          "description":   "FPGA GTH TX lane 0, positive"  // string
        }
        // ... one entry per pin
      }
    }
  },

  // ── Power rails sourced from this board (outputs to other boards) ──────────
  "power_outputs": {
    "<RAIL_NAME>": {
      "voltage_v":   3.3,          // number — nominal voltage
      "max_current_a": 2.0,        // number | null
      "connectors":  ["J500"],     // string[] — which connector pins carry this rail
      "description": "3.3V supply output to downstream board"
    }
  },

  // ── Power rails consumed by this board (inputs from other boards) ──────────
  "power_inputs": {
    "<RAIL_NAME>": {
      "voltage_v":      12.0,
      "max_current_a":  5.0,
      "connectors":     ["J500"],
      "description":    "12V main input from power supply board"
    }
  }
}
```

---

## Interface Types

| `interface_type` | Description |
|---|---|
| `power` | Power supply connector (DC power in or out) |
| `high_speed_mezzanine` | High-speed board-to-board (Samtec LSHM, ERM8, etc.) |
| `jtag` | JTAG / SWD debug connector |
| `ethernet` | 10/100/1G/2.5G Ethernet (MDI pairs) |
| `usb` | USB 2.0 / 3.x |
| `uart` | RS-232 or LVTTL UART |
| `rs485` | RS-485 / RS-422 differential |
| `lvds` | LVDS differential pairs |
| `gpio` | General-purpose I/O header |
| `i2c` | I2C bus header |
| `spi` | SPI bus header |
| `trigger` | Trigger / sync signals |
| `clock` | Clock distribution connector |
| `test_point` | Test/probe connector (BD020-03 style) |
| `board_to_board` | Generic board-to-board mezzanine |
| `mixed` | Connector carries multiple interface types |

---

## Direction Rules

| Direction | Meaning |
|---|---|
| `output` | This board drives the signal onto the connector |
| `input` | This board receives the signal from the connector |
| `bidir` | Bidirectional (e.g., SDA, data buses) |
| `power` | Power supply pin (not a signal) |
| `gnd` | Ground return |
| `nc` | Not connected (intentionally open) |

---

## Cross-Board Verification

When verifying two mating boards, compare their `board_interface.json` files:

1. Identify mating connector pairs (may require a `board_connections.yaml` that maps
   `BoardA.XK-A` → `BoardB.XK-A'`).
2. For each pin pair, check:
   - Signal names match (or are mapped in a known translation table).
   - Directions are complementary (output↔input, power↔input, bidir↔bidir).
   - Voltage levels are compatible (≤ the receiver's abs-max; ≥ the receiver's VIH min).
   - If one pin is NC, the other should be NC or a non-critical signal.

---

## Notes for Agents

- **Auto-generate** a `board_interface.json` from `schematic.yaml` using `connector_dump.json`
  (produced by the altium netlist parser pipeline).
- Use schematic net names to classify signals; see the net pattern table in the ic-reviewer SKILL.md.
- For pins with auto-generated net names (e.g. `NetC12_1`), trace to the connected component
  to find the actual signal name.
- **Never leave `name` as just the raw net name** — infer a human-readable name from context.
- Store the completed file in `datasheets/boards/<PART_NUMBER>/board_interface.json`.
- Also copy or symlink to `REVIEW/board_interface.json` for use by the review pipeline.
