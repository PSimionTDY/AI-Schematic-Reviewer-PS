## Allegro Export File Formats

Cadence Allegro produces two generations of export format. Detect which is present before parsing.

---

## Format A: `*View.dat` (newer, preferred)

Files: `pinView.dat`, `netView.dat`, `funcView.dat`, `compView.dat`, `chipsView.dat`

All files share the same structure:
- `A` row: column header (fields separated by `!`)
- `J` row: job metadata (linked PCB path, timestamp, bounds)
- `S` rows: data records

### pinView.dat — connections (primary input)

Columns: `NET_NAME`, `REFDES`, `PIN_NUMBER`, `FUNC_LOGICAL_PATH`, `COMP_DEVICE_TYPE`, `FUNC_SCH_SIZE`, `FUNC_HAS_FIXED_SIZE`, `FUNC_DES`, `PIN_NET_SHORT`, `PIN_NO_SWAP_PIN`

Key fields:
- `NET_NAME`: the electrical net this pin connects to
- `REFDES`: component reference designator (e.g. `U5`, `R10`, `C42`)
- `PIN_NUMBER`: pin number or name
- `FUNC_DES`: schematic block identifier (e.g. `F117`) — use for schematic location cross-reference
- `FUNC_LOGICAL_PATH`: hierarchical path encoding the sheet, e.g. `@\sch25678-1\.top(sch_1):...` — extract page number from `(sch_N)`

### netView.dat — net list

Columns: `NET_NAME`, `NET_LOGICAL_PATH`, `NET_CDS_FSP_UID`, `NET_VOLTAGE_LAYER`, ...

Use for net enumeration. `NET_VOLTAGE_LAYER` may contain voltage info in some exports.

### funcView.dat — component instances with sheet location

Columns: `FUNC_LOGICAL_PATH`, `COMP_DEVICE_TYPE`, `REFDES`, `FUNC_PRIM_FILE`, ..., `FUNC_DES`, ...

Maps each component to its sheet via the hierarchical path and `FUNC_DES` block identifier.

### compView.dat — component list

Columns: `REFDES`, `COMP_VOLTAGE`, `COMP_CDS_FSP_LIB_PART_MODEL`, ...

Often sparse. Used for component enumeration and voltage annotations if present.

### chipsView.dat — component library definitions

**Same format as `pstchip.dat` (Format B below).** Contains `primitive` blocks with `PINUSE` for pin directions.

---

## Format B: `pst*.dat` (older)

Files: `pstxnet.dat`, `pstchip.dat`, `pstxprt.dat`

### pstxnet.dat — netlist

```
NET_NAME
'<net_name>'
 '@<path>': C_SIGNAL='...';
NODE_NAME  <refdes> <pin>
 '@<path>': '<pin>':;
```

### pstchip.dat — component library (also used as chipsView.dat)

```
FILE_TYPE=LIBRARY_PARTS;
primitive '<name>';
  pin '<pin>': PIN_NUMBER='(N)'; PINUSE='<direction>';
  body
    PART_NAME='...'; VALUE='...'; MFG1='...'; MFG1PARTNUMBER='...';
  end_body;
end_primitive;
```

`PINUSE` values → canonical directions:

| PINUSE | Direction |
|--------|-----------|
| IN | input |
| OUT | output |
| BI | bidir |
| OC / OE | open_drain_out |
| PWRIN | power_in |
| PWROUT | power_out |
| UNSPEC / NC | passive |

### pstxprt.dat — part placement with XY

```
PART_NAME
 <refdes> '<primitive>':;
SECTION_NUMBER N
 '@<path>': XY='(<x>,<y>)', ...
```

XY is in mils. Page encoded in path as `(SCH_N)`.

---

## Parser Detection Logic

```python
import os
def detect_format(allegro_dir):
    if os.path.exists(f"{allegro_dir}/pinView.dat"):
        return "view"   # newer *View.dat format
    if os.path.exists(f"{allegro_dir}/pstxnet.dat"):
        return "pst"    # older pst*.dat format
    raise ValueError("No recognised Allegro export files found")
```

---

## pstxnet.dat — Expanded Netlist

Contains all net-to-pin connections. This is the primary input for connection extraction.

### Format

```
FILE_TYPE = EXPANDEDNETLIST
{ Comment line }

NET_NAME
'<net_name>'
 '@<hierarchical_path>':
 C_SIGNAL='<signal_path>';
NODE_NAME	<refdes> <pin>
 '@<hierarchical_path>':
 '<pin>':;
```

### Parsing Rules

- `NET_NAME` introduces a new net block.
- The net name is the single-quoted string on the following line (e.g. `'3V3'`, `'SDA'`).
- Each `NODE_NAME` line gives a `<refdes> <pin>` pair — this is the component and pin connected to the current net.
- Multiple `NODE_NAME` entries under one `NET_NAME` mean those pins are all on the same net.
- Ignore lines starting with `'@` (hierarchical path metadata) and `C_SIGNAL=` (internal signal name).

### Example

```
NET_NAME
'3V3'
 '@SCH26782-1.SCHEMATIC1(SCH_1):3V3':
 C_SIGNAL='...';
NODE_NAME	C1 1
 '@...': '1':;
NODE_NAME	U5 VCC
 '@...': 'VCC':;
```

→ Net `3V3` connects pin 1 of C1 and pin VCC of U5.

---

## pstchip.dat — Component Library Definitions

Defines each component's pins and metadata. Used to look up part type, manufacturer, and value.

### Format

```
FILE_TYPE=LIBRARY_PARTS;

primitive '<primitive_name>';
  pin '<pin_name>': PIN_NUMBER='(<num>)'; PINUSE='<use>';
  ...
  body
    PART_NAME='<type>';
    JEDEC_TYPE='<package>';
    MFG1='<manufacturer>';
    MFG1PARTNUMBER='<mpn>';
    VALUE='<value>';
  end_body;
end_primitive;
```

### Parsing Rules

- Each `primitive` block defines one component variant.
- `PART_NAME` gives the generic type (e.g. `Gen_C`, `Gen_R`, `74LVC1G`).
- `MFG1PARTNUMBER` gives the manufacturer part number.
- `VALUE` gives the component value (e.g. `10uF`, `4.7k`, `100n`).
- `JEDEC_TYPE` gives the package code.
- Match this data to the refdes instances in `pstxprt.dat`.

---

## pstxprt.dat — Expanded Part List

Maps refdes instances (U1, C1, R12…) to their primitive definitions.

### Format

```
FILE_TYPE = EXPANDEDPARTLIST;

DIRECTIVES
 ROOT_DRAWING='<schematic_name>';
 POST_TIME='<timestamp>';
END_DIRECTIVES;

PART_NAME
 <refdes> '<primitive_name>':;
SECTION_NUMBER <n>
 '@<path>':
 XY='(<x>,<y>)',
 ...
```

### Parsing Rules

- `PART_NAME` introduces a new instance.
- The refdes (e.g. `C1`, `U400`) is on the same line before the primitive name.
- Cross-reference the primitive name with `pstchip.dat` to get the component's value and part number.

---

## Key Tips for the Parser

1. **Strip quotes**: Net names and values are wrapped in single quotes — strip them when storing.
2. **Case sensitivity**: Refdes names are case-sensitive (`U400` ≠ `u400`).
3. **Multi-section components**: Some ICs appear as multiple `SECTION_NUMBER` entries (e.g. quad op-amps). All sections share the same refdes.
4. **Power symbols**: Nets like `GND`, `3V3`, `VCC` appear as regular nets connected to power symbol primitives — treat them normally.
5. **Hierarchy**: The `@...` path strings encode schematic hierarchy but can be ignored for flat connection extraction.
