# Signal Tracing Rules for IC Reviewer

This document defines mandatory tracing rules for the IC reviewer when analysing
passive component networks. **Always trace actual connections in the graph.
Never guess based on component values, reference designators, or order of
appearance.**

---

## 1. Voltage Divider — Rtop vs Rbot Assignment

A resistor divider consists of two resistors sharing a midpoint node. One resistor
(Rtop) connects the midpoint to a **higher-voltage net** (e.g. regulated output,
supply rail). The other (Rbot) connects the midpoint to **GND (0 V)**.

### Mandatory method — trace the graph

For each resistor in a suspected divider:

1. Look up both pins of the resistor in `schematic.yaml`.
2. For each pin, read the connected net's `type` and `voltage`.
3. Assign roles:
   - Pin connected to a net with `type: gnd` or `voltage: 0` → this pin is the
     **GND side** → this resistor is **Rbot**.
   - Pin connected to a net with higher voltage (output rail, supply) → this pin
     is the **Vtop side** → this resistor is **Rtop**.
4. The shared pin (same net on both resistors) is the **divider midpoint**
   (feedback node, PGFB node, etc.).

### Example

```
Vout ──── R2604 (10k) ──┬── R2606 (11k) ──── GND
                        │
                      PGFB (midpoint, to IC pin)
```

Trace:
- R2604 pin 1 → `N-U2600-OUTS` (power, 1.0V) → Rtop
- R2604 pin 2 → `NetC2603_1` (midpoint)
- R2606 pin 1 → `NetC2603_1` (midpoint)
- R2606 pin 2 → `VSS` (gnd, 0V) → **Rbot**

Result: Rtop = R2604 (10k), Rbot = R2606 (11k). Do NOT guess based on value.

### ❌ Forbidden heuristics

- **Do NOT assume "larger R = Rtop"** — this is not a reliable rule.
- **Do NOT assume "smaller R = Rbot"**.
- **Do NOT use reference designator order** (e.g. R2604 < R2606) to infer position.

If the GND-side pin cannot be determined (e.g. the net has no `type` or
`voltage` and no GND-named connections), flag the ambiguity as a `question`
severity issue — but **first exhaust all tracing paths** (follow through 0Ω
jumpers, ferrites, and short nets before giving up).

---

## 2. Pull-up vs Pull-down — Identifying the Termination Side

For a resistor on a signal pin:

1. Trace **both pins** of the resistor.
2. Pin connected to a supply rail (`type: power`, voltage > 0) → **pull-up**.
3. Pin connected to GND (`type: gnd`, voltage = 0) → **pull-down**.
4. Pin connected to the IC signal pin → the near side.

Always verify: is the resistor value appropriate for the pull-up/pull-down
(e.g. 1k–100kΩ for I²C, 1k–10kΩ for open-drain outputs)?

---

## 3. Series Resistors — Finding the True Far-End Net

When an IC pin connects to a net that contains only one other component (a
resistor), follow through it:

1. Find the resistor's other pin net.
2. Check if that net has a meaningful `type`/`voltage`.
3. If the far net is still ambiguous, follow through another 0Ω jumper if present.
4. Report the **far-end net** as the effective voltage/signal seen by the pin.

---

## 4. Multi-Resistor Feedback Networks (3+ resistors)

When a feedback pin (FB, PGFB, ILIM, etc.) has three or more resistors:

1. Identify the midpoint net (the one the IC pin is on).
2. Trace **every resistor** from the midpoint: record both pins and their connected nets.
3. **Group resistors by their far-end net** (the net that is NOT the midpoint):
   - Resistors whose far-end net is `VSS`/GND all form a **parallel Rbot group**.
   - Resistors whose far-end net is `Vout`/supply all form a **parallel Rtop group**.
   - Any resistor whose far-end net is neither GND nor Vout → may be margining/trim.

### Parallel resistor detection — MANDATORY

If two or more resistors share the **same midpoint net** AND the **same far-end net**,
they are in **parallel**. Compute the combined value:

```
Rparallel = 1 / (1/R1 + 1/R2 + ...)
```

Then use the combined value in the divider formula. **Do NOT treat them as separate
or series resistors.**

### Example: three resistors, two in parallel as Rbot

```
Vout(1.8V) ─── R39(12k) ─┬─ R40(11k) ─── GND
                           │
                           ├─ R41(10k) ─── GND
                           │
                          FB (U16 pin 2)
```

Trace:
- R39 pin 1 → QC_1V8 (Vout), pin 2 → NetR39_2 (midpoint) → **Rtop = 12k**
- R40 pin 1 → NetR39_2 (midpoint), pin 2 → VSS → **Rbot candidate**
- R41 pin 1 → NetR39_2 (midpoint), pin 2 → VSS → **Rbot candidate**
- R40 and R41 share the same far-end (VSS) → they are in parallel:
  **Rbot = 11k ‖ 10k = 5.24k**
- Vout = 0.55 × (1 + 12k/5.24k) = **1.81V ≈ 1.8V** ✓

### Example: margining resistor (far-end is neither GND nor Vout)

If R3(21k) has far-end → U4 pin 1 (an analog switch), it is NOT part of the basic
divider — it is a margining/adjustment element. Flag as a question confirming intent.

4. Only raise `fb_three_resistor` if after grouping by far-end net, the topology
   cannot be explained as a simple (possibly parallel-combined) divider.

---

## 5. Determining Power Net vs Signal Net

A net is a **power net** only when it is directly sourced by a regulator output,
connector supply pin, or power flag with no other logic function. Check:

- Does the net connect exclusively to passives (caps, resistors, ferrites) and
  IC supply/bypass pins? → power net.
- Does the net connect to an IC's **logic output**, **transistor collector/drain**,
  or **FPGA IO pin** as the primary driver? → **signal net**, even if the net
  name includes a voltage suffix (e.g. `PDS_SYNC_CLK.U_PDS_1V8`).
- Net names containing `_1V8`, `_3V3`, `_1V2` etc. do **not** imply the net is a
  power rail — the suffix often refers to the domain or target device, not the
  signal voltage. Always trace the actual driver.
