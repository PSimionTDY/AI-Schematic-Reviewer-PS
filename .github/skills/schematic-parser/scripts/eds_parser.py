#!/usr/bin/env python3
"""
EDIF (.eds) parser - Siemens/Mentor Xpedition ("xDX Designer") EDIF netlist export.

Parses EDIF 2.0.0 S-expression schematic netlists and returns the same data
structure as schematic_parser.parse() so it can be used interchangeably
throughout the review pipeline.

Key EDIF structural fact this parser depends on:

  Instance symbols (e.g. &0441I53) are only unique **within the enclosing
  view's own `contents` block** -- NOT globally across the file. The same
  symbol is reused independently inside every schematic page/sub-block
  (each `(cell NAME (view VIEWNAME (contents ...)))` is its own namespace).
  A flat, file-wide instance-id lookup silently merges unrelated components
  that happen to share a numeric suffix on different pages.

  The design hierarchy nests sheets as block instances, e.g. the top-level
  view `SCHEMATIC_VIEW` contains instances like `&0441I532` whose `viewRef`
  points at a sub-view (a page/block, e.g. `T13253802`). Fully-qualified
  references in `instanceBackAnnotate` / `portBackAnnotate` spell out the
  full chain, innermost first:

      (instanceRef &0441I53  (instanceRef &0441I532  (viewRef SCHEMATIC_VIEW)))

  reads as: leaf instance `&0441I53`, which lives inside block instance
  `&0441I532`, which lives inside the view named `SCHEMATIC_VIEW`. To
  resolve it we walk from the named view inward: look up `&0441I532` in
  `SCHEMATIC_VIEW`'s own instances to find which sub-view it references,
  then look up the leaf `&0441I53` within *that* sub-view's instances.

This module builds one namespace ("view") per `(cell ... (view VIEWNAME ...))`
block -- covering both hierarchy sheets and individual component/library
cells (e.g. `CAP`, `RES`) -- then resolves every instanceBackAnnotate /
portBackAnnotate / net connection through that per-view namespace.
"""

import re
from pathlib import Path


def find_eds_file(folder: Path) -> Path | None:
    """Return the first .eds file found directly in folder, or None."""
    files = list(folder.glob('*.eds')) + list(folder.glob('*.EDS'))
    return files[0] if files else None


def is_eds_folder(folder: Path) -> bool:
    """Return True if folder contains an EDIF (.eds) netlist export."""
    return find_eds_file(folder) is not None


def _match_paren(text: str, start: int) -> int:
    """
    Given the index of an opening '(', return the index of its matching ')'.

    Skips over double-quoted string literals while tracking depth, since EDIF
    free-form annotation/comment strings (e.g. inside 'commentGraphics') can
    legitimately contain unbalanced literal '(' / ')' characters (e.g. a note
    reading "1.1*(1+560/(150+56)= 4.1V"). Counting parens inside such a string
    would desync depth tracking for the remainder of the file/block, silently
    truncating or dropping every subsequent instance/net in that page.
    """
    depth = 0
    i = start
    n = len(text)
    in_string = False
    while i < n:
        c = text[i]
        if in_string:
            if c == '\\':
                i += 2
                continue
            if c == '"':
                in_string = False
        elif c == '"':
            in_string = True
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _iter_blocks(text: str, keyword: str):
    """Yield balanced-paren substrings starting with '(<keyword>' followed by whitespace/')'."""
    pattern = re.compile(r'\(' + re.escape(keyword) + r'(?=[\s\)])')
    for m in pattern.finditer(text):
        start = m.start()
        end = _match_paren(text, start)
        if end == -1:
            continue
        yield text[start:end + 1]


def _iter_direct_children(container_text: str):
    """
    Yield each direct child '(...)' block of a container block (e.g. '(contents ...)'),
    at nesting depth 1 relative to the container's own opening paren.

    Mentor/Xpedition EDIF nests local net-segment display labels *inside* their
    parent global net block, e.g.:

        (net GNDD
          (joined (globalPortRef GNDD) (portRef GND (instanceRef IC1)) ...)
          (net (rename ... "$1N340")
            (joined (portRef GND (instanceRef IC1)) (portRef PWP (instanceRef IC1)))))

    The nested '(net ...)' is not a separate electrical net -- it is only a
    display-label segment of the same net, and its 'joined' connections are
    already a subset of the parent's. A flat/global '(net' regex scan (as used
    by _iter_blocks) would incorrectly treat it as an independent net, splitting
    off a handful of pins from their true (global) net. Only direct children of
    the container should be treated as top-level nets/instances.
    """
    start = container_text.find('(')
    if start == -1:
        return
    i = start + 1
    n = len(container_text)
    depth = 1
    child_start = None
    in_string = False
    while i < n and depth > 0:
        c = container_text[i]
        if in_string:
            if c == '\\':
                i += 2
                continue
            if c == '"':
                in_string = False
        elif c == '"':
            in_string = True
        elif c == '(':
            if depth == 1:
                child_start = i
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 1 and child_start is not None:
                yield container_text[child_start:i + 1]
                child_start = None
        i += 1


def _iter_top_level_blocks(container_text: str, keyword: str):
    """Yield direct-child blocks of *container_text* whose keyword matches (e.g. 'net', 'instance')."""
    prefix = '(' + keyword
    for child in _iter_direct_children(container_text):
        if child.startswith(prefix) and (len(child) == len(prefix) or child[len(prefix)] in ' \t\n\r)'):
            yield child


def _iter_contents_page_blocks(contents_text: str):
    """
    Yield each schematic page's own body text within a '(contents ...)' block.

    Xpedition EDIF nests each sheet's instances/nets one level deeper, inside a
    '(page ...)' block that is itself a direct child of '(contents ...)':

        (contents
          (offPageConnector ...)
          (page (rename &1 "1") (pageSize ...)
            (instance ...) (instance ...) ...
            (net ...) (net ...) ...))

    Single-page cells (e.g. individual component symbols) may have their
    instances/nets directly under '(contents ...)' with no '(page ...)'
    wrapper at all, so both layouts are yielded/handled by the caller.
    """
    found_page = False
    for child in _iter_direct_children(contents_text):
        if child.startswith('(page') and (len(child) == 5 or child[5] in ' \t\n\r)'):
            found_page = True
            yield child
    if not found_page:
        yield contents_text


def _instance_id(block: str) -> str | None:
    """Extract the instance id symbol from an '(instance ...)' block."""
    m = re.match(r'\(instance\s+\(rename\s+(\S+)\s+"[^"]*"\)', block)
    if m:
        return m.group(1)
    m = re.match(r'\(instance\s+(\S+)', block)
    return m.group(1) if m else None


def _view_block_name(block: str) -> str | None:
    """Extract the name of a '(view ...)' block (bare symbol or renamed)."""
    m = re.match(r'\(view\s+\(rename\s+(\S+)\s+"[^"]*"\)', block)
    if m:
        return m.group(1)
    m = re.match(r'\(view\s+(\S+)', block)
    return m.group(1) if m else None


def _view_ref_name(block: str) -> str:
    """Extract the target view/cell name from a (viewRef ...) inside an instance block."""
    m = re.search(r'\(viewRef\s+\(rename\s+(\S+)\s+"[^"]*"\)', block)
    if m:
        return m.group(1)
    m = re.search(r'\(viewRef\s+(\S+)', block)
    return m.group(1) if m else ''


def _cell_ref_name(block: str) -> str:
    """
    Extract the library part identifier from a (viewRef ... (cellRef (name ID ...))) entry
    inside an instance block.

    The (viewRef NAME ...) target is only a *generic schematic-symbol view* (e.g. 'RES',
    'COMP') that many unrelated library parts share -- it is NOT the actual part number.
    The real per-part identity lives one level deeper, in the instance's own cellRef, e.g.:

        (viewRef COMP (cellRef (name &1950257 ...) (libraryRef IC)))

    Different resistor/IC library cells (each with its own distinct part id like
    &1950434, &1950257, ...) commonly reuse the same generic view name, so without this
    the part identity is lost/collapsed and every instance sharing a view looks identical.
    """
    m = re.search(r'\(cellRef\s+\(name\s+&?([^\s()]+)', block)
    return m.group(1) if m else ''


def _property_string(block: str, prop_name: str) -> str:
    """Extract the string value of a (property NAME ...) entry inside a block."""
    escaped = re.escape(prop_name)
    for pat in (
        r'\(property\s+(?:\(rename\s+\S+\s+"' + escaped + r'"\)|' + escaped + r')\s*'
        r'\(string\s+\(stringDisplay\s+"([^"]*)"',
        r'\(property\s+(?:\(rename\s+\S+\s+"' + escaped + r'"\)|' + escaped + r')\s*'
        r'\(string\s+"([^"]*)"',
    ):
        m = re.search(pat, block)
        if m:
            return m.group(1).strip()
    return ''


def _port_instances(block: str) -> dict:
    """Extract portInstance port-name -> default pin designator (schematic-symbol pin label)."""
    result = {}
    for m in re.finditer(
        r'\(portInstance\s+(\S+)\s+\(designator\s+\(stringDisplay\s+"([^"]*)"', block
    ):
        result[m.group(1)] = m.group(2)
    return result


_PINUSE_MAP = {
    'INPUT':  'input',
    'OUTPUT': 'output',
    'INOUT':  'bidir',
}


def _interface_port_directions(view_block: str) -> dict:
    """Extract port-name -> direction from a (view ... (interface (port ...))) block."""
    result = {}
    iface_m = re.search(r'\(interface(.*?)\)\s*(?:\(page|\(contents|\(symbol|\Z)', view_block, re.DOTALL)
    iface_text = iface_m.group(1) if iface_m else ''
    for block in _iter_blocks('(interface' + iface_text, 'port'):
        name_m = re.match(r'\(port\s+\(rename\s+(\S+)\s+"[^"]*"\)', block)
        if not name_m:
            name_m = re.match(r'\(port\s+(\S+)', block)
        dir_m = re.search(r'\(direction\s+(\w+)\)', block)
        if name_m and dir_m:
            result[name_m.group(1)] = _PINUSE_MAP.get(dir_m.group(1), 'passive')
    return result


def _net_name(block: str) -> str | None:
    """
    Extract the net name from an '(net ...)' block.

    Nets appear in several forms in Xpedition EDIF exports:
      (net GNDD ...)                                  -- bare symbol
      (net (rename &1 "8V4") ...)                     -- renamed, plain string
      (net (rename &1 (stringDisplay "8V4" ...)) ...) -- renamed, display string
      (net (name ICHG (display ...)) ...)             -- named (not renamed!), with display

    The '(name ...)' form is functionally identical to '(rename ...)' for our
    purposes (both just attach a human-readable label), but a missing case here
    means _net_name returns None and the net -- along with every connection in
    its 'joined' block -- is silently dropped from the parse entirely.
    """
    m = re.match(r'\(net\s+\(rename\s+\S+\s+"([^"]*)"\)', block)
    if m:
        return m.group(1)
    m = re.match(r'\(net\s+\(rename\s+\S+\s+\(stringDisplay\s+"([^"]*)"', block)
    if m:
        return m.group(1)
    m = re.match(r'\(net\s+\(name\s+(\S+)\s+"([^"]*)"\)', block)
    if m:
        return m.group(2)
    m = re.match(r'\(net\s+\(name\s+(\S+)\s+\(stringDisplay\s+"([^"]*)"', block)
    if m:
        return m.group(2)
    m = re.match(r'\(net\s+\(name\s+([\w$&]+)', block)
    if m:
        return m.group(1)
    m = re.match(r'\(net\s+([\w$&]+)', block)
    return m.group(1) if m else None


def _build_views(text: str) -> dict:
    """
    Build a per-view namespace covering every '(cell ... (view VIEWNAME ...))' block
    in the file -- both hierarchy sheets/blocks and individual component cells.

    Returns: {view_name: {'instances': {local_id: {device_type, value, ports}},
                           'nets': [(net_name, [(port_name, local_id), ...]), ...],
                           'directions': {port_name: direction}}}
    """
    views: dict = {}

    for lib_block in _iter_blocks(text, 'library'):
        for cell_block in _iter_blocks(lib_block, 'cell'):
            for view_block in _iter_blocks(cell_block, 'view'):
                view_name = _view_block_name(view_block)
                if view_name is None:
                    continue

                contents_start = view_block.find('(contents')
                if contents_start != -1:
                    contents_end = _match_paren(view_block, contents_start)
                    contents_text = (
                        view_block[contents_start:contents_end + 1]
                        if contents_end != -1 else view_block[contents_start:]
                    )
                else:
                    contents_text = '(contents)'

                local_instances = {}
                local_nets = []
                for page_text in _iter_contents_page_blocks(contents_text):
                    for inst_block in _iter_top_level_blocks(page_text, 'instance'):
                        local_id = _instance_id(inst_block)
                        if local_id is None:
                            continue
                        value = _property_string(inst_block, 'Value')
                        ports = _port_instances(inst_block)
                        device_type = _view_ref_name(inst_block)
                        part_id = _cell_ref_name(inst_block)
                        if local_id not in local_instances or value or ports:
                            local_instances[local_id] = {
                                'device_type': device_type, 'value': value, 'ports': ports,
                                'part_id': part_id,
                            }

                    for net_block in _iter_top_level_blocks(page_text, 'net'):
                        name = _net_name(net_block)
                        if not name:
                            continue
                        # Only take portRefs from this net's own direct 'joined' block(s) --
                        # NOT from nested '(net ...)' display-label segments, whose portRefs
                        # are already a subset of this net's own joined list. Using a naive
                        # scan across the whole net_block text would double-count/misattribute
                        # those nested segments, so restrict to direct (depth-1) children.
                        conns: list[tuple[str, str]] = []
                        for joined_block in _iter_top_level_blocks(net_block, 'joined'):
                            conns.extend(re.findall(r'\(portRef\s+(\S+)\s+\(instanceRef\s+(\S+)\)\)', joined_block))
                        local_nets.append((name, conns))

                directions = _interface_port_directions(view_block)

                entry = views.setdefault(view_name, {'instances': {}, 'nets': [], 'directions': {}})
                entry['instances'].update(local_instances)
                entry['nets'].extend(local_nets)
                entry['directions'].update(directions)

    return views


def _resolve_ref_chain(ref_text: str, views: dict) -> tuple[str, str] | None:
    """
    Resolve a fully-qualified '(instanceRef ID1 (instanceRef ID2 (... (viewRef VIEWNAME))))'
    reference chain to the (view_name, local_id) of the leaf instance.
    """
    ids = re.findall(r'\(instanceRef\s+([^\s()]+)', ref_text)
    view_m = re.search(r'\(viewRef\s+([^\s()]+)\)', ref_text)
    if not ids or not view_m:
        return None

    current_view = view_m.group(1)
    leaf_id = ids[0]
    block_ids = ids[1:]  # outer-to-inner text order; walk from outermost (last) to innermost (first)

    for block_id in reversed(block_ids):
        inst = views.get(current_view, {}).get('instances', {}).get(block_id)
        if inst is None:
            return None
        current_view = inst['device_type']

    return current_view, leaf_id


def parse(folder: Path) -> dict:
    """Parse an EDIF (.eds) netlist export folder. Returns the schematic_parser-compatible structure."""
    eds_file = find_eds_file(folder)
    if eds_file is None:
        raise FileNotFoundError(f"No .eds file found in {folder}")

    text = eds_file.read_text(encoding='utf-8', errors='replace')

    views = _build_views(text)

    # --- instanceBackAnnotate -> authoritative refdes + package, keyed by (view, local_id) ---
    inst_meta: dict[tuple[str, str], dict] = {}
    for block in _iter_blocks(text, 'instanceBackAnnotate'):
        ref_m = re.search(r'\(instanceRef\s+.*?\(viewRef\s+\S+\)\)?\)', block, re.DOTALL)
        des_m = re.search(r'\(designator\s+"([^"]*)"\)', block)
        if not (ref_m and des_m):
            continue
        resolved = _resolve_ref_chain(ref_m.group(0), views)
        if resolved is None:
            continue
        inst_meta[resolved] = {
            'designator': des_m.group(1),
            'cell_name': _property_string(block, 'Cell_Name') or _property_string(block, 'Cell Name'),
        }

    # --- portBackAnnotate -> authoritative physical pin number, keyed by (view, local_id, port) ---
    pin_overrides: dict[tuple[str, str, str], str] = {}
    for block in _iter_blocks(text, 'portBackAnnotate'):
        ref_m = re.search(r'\(portRef\s+(\S+)\s+(\(instanceRef.*?\(viewRef\s+\S+\)\)?\))', block, re.DOTALL)
        des_m = re.search(r'\(designator\s+"([^"]*)"\)', block)
        if not (ref_m and des_m):
            continue
        resolved = _resolve_ref_chain(ref_m.group(2), views)
        if resolved is None:
            continue
        view_name, local_id = resolved
        pin_overrides[(view_name, local_id, ref_m.group(1))] = des_m.group(1)

    def resolve_pin(view_name: str, local_id: str, port_name: str) -> str:
        override = pin_overrides.get((view_name, local_id, port_name))
        if override:
            return override
        label = views.get(view_name, {}).get('instances', {}).get(local_id, {}).get('ports', {}).get(port_name)
        if label:
            return label
        # Fall back to the raw port symbol, stripping the leading '&' used for
        # symbols that would otherwise start with a digit.
        return port_name.lstrip('&')

    def resolve_direction(view_name: str, local_id: str, port_name: str) -> str:
        device_type = views.get(view_name, {}).get('instances', {}).get(local_id, {}).get('device_type', '')
        return views.get(device_type, {}).get('directions', {}).get(port_name, 'passive')

    def resolve_device_type(view_name: str, local_id: str) -> str:
        return views.get(view_name, {}).get('instances', {}).get(local_id, {}).get('device_type', '')

    def resolve_value(view_name: str, local_id: str) -> str:
        return views.get(view_name, {}).get('instances', {}).get(local_id, {}).get('value', '')

    def resolve_part_id(view_name: str, local_id: str) -> str:
        return views.get(view_name, {}).get('instances', {}).get(local_id, {}).get('part_id', '')

    nets: dict = {}
    components: dict = {}
    seen_conns: set = set()  # (net_name, ref, pin) -- same global net can repeat across sheets

    for view_name, view_data in views.items():
        for net_name, conns in view_data['nets']:
            for port_name, local_id in conns:
                meta = inst_meta.get((view_name, local_id))
                if not meta:
                    # No back-annotated refdes -> global/border/frame symbol, not a real component
                    continue
                refdes = meta['designator']
                pin = resolve_pin(view_name, local_id, port_name)
                direction = resolve_direction(view_name, local_id, port_name)

                key = (net_name, refdes, pin)
                if key in seen_conns:
                    continue
                seen_conns.add(key)

                nets.setdefault(net_name, {'connections': []})['connections'].append({
                    'ref': refdes, 'pin': pin, 'direction': direction, 'func_des': '',
                })

                if refdes not in components:
                    components[refdes] = {
                        'device_type': resolve_device_type(view_name, local_id),
                        'part_name':   resolve_part_id(view_name, local_id),
                        'value':       resolve_value(view_name, local_id),
                        'mfg':         '',
                        'mpn':         '',
                        'package':     meta.get('cell_name', ''),
                        'func_des':    '',
                        'sheet':       'sch_1',
                        'pins':        {},
                    }
                components[refdes]['pins'][pin] = {'net': net_name, 'direction': direction}

    return {'format': 'eds', 'nets': nets, 'components': components}
