#!/usr/bin/env python3
"""
Canonical enrichment writer for schematic review agents.

This is the ONLY approved way to write net voltages, component roles, and pin
directions into review.db.  Do NOT write YAML files or execute raw SQL UPDATE
statements — always call write_enrichments() or use the CLI.

Normalises field-name aliases before validation so that agent output is always
consistent regardless of which agent wrote it.

## Usage as a Python module (preferred for IC review agents)

    import sys
    sys.path.insert(0, r'C:\\work\\bitbucket\\schematic_reviewer\\.github\\skills\\schematic-reviewer\\scripts')
    from add_enrichment import write_enrichments

    summary = write_enrichments('reviews/SCH25678-1F/review.db', {
        'nets': {
            'VCC_3V3': {'voltage': 3.3, 'confidence': 'confirmed', 'type': 'power'},
            'SDA':     {'type': 'i2c', 'confidence': 'confirmed'},
        },
        'component_roles': {
            'R15': {'role': 'pull_up', 'confidence': 'confirmed',
                    'reason': 'Connected between VCC_3V3 and SDA'},
        },
        'pin_directions': {
            'U15': {'3': 'output', '4': 'power_in'},
        },
    })
    print(summary)
    # {'nets_updated': 2, 'roles_updated': 1, 'pins_updated': 2, 'skipped': 0}

## Usage from the command line

    # Write from a JSON string
    python add_enrichment.py review.db --json '{"nets": {"VCC_3V3": {"voltage": 3.3}}}'

    # Write from a YAML file
    python add_enrichment.py review.db --from-yaml enrichments.yaml

    # Write from stdin (YAML)
    python add_enrichment.py review.db --stdin < enrichments.yaml

## Field-name aliases

For `nets` sub-entries:
    voltage_level, rail_voltage, v, volt  →  voltage
    v_min, vmin                           →  voltage_min
    v_max, vmax                           →  voltage_max
    net_type, signal_type                 →  type
    certainty, conf                       →  confidence

For `component_roles` sub-entries:
    comp_role, function                   →  role
    certainty, conf                       →  confidence

For `pin_directions`: no aliases — keys are pin numbers, values are direction strings.

## Valid values

confidence  : unknown | hint | doubtful | inferred | deduced | calculated |
              confirmed | certain
type (net)  : power | gnd | signal | i2c | spi | uart | can | usb | ethernet |
              clock | reset | interrupt | pwm | analog | differential
role        : pull_up | pull_down | decoupling | ac_coupling | series |
              termination | current_limit | voltage_divider | snubber |
              bypass | filter | esd_protection | led_current_limit |
              power_filter | other
direction   : input | output | power_in | power_out | open_drain |
              bidirectional | nc | tristate_output
voltage     : numeric (int or float)
voltage_min : numeric (int or float)
voltage_max : numeric (int or float)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_CONFIDENCE = {
    "unknown", "hint", "doubtful", "inferred", "deduced",
    "calculated", "confirmed", "certain",
}

VALID_NET_TYPES = {
    "power", "gnd", "signal", "i2c", "spi", "uart", "can", "usb",
    "ethernet", "clock", "reset", "interrupt", "pwm", "analog", "differential",
}

VALID_ROLES = {
    "pull_up", "pull_down", "decoupling", "ac_coupling", "series",
    "termination", "current_limit", "voltage_divider", "snubber",
    "bypass", "filter", "esd_protection", "led_current_limit",
    "power_filter", "other",
}

VALID_DIRECTIONS = {
    "input", "output", "power_in", "power_out", "open_drain",
    "bidirectional", "nc", "tristate_output",
}

# Field-name aliases for nets sub-entries.
_NET_ALIASES: dict[str, str] = {
    "voltage_level": "voltage",
    "rail_voltage":  "voltage",
    "v":             "voltage",
    "volt":          "voltage",
    "v_min":         "voltage_min",
    "v_max":         "voltage_max",
    "vmin":          "voltage_min",
    "vmax":          "voltage_max",
    "net_type":      "type",
    "signal_type":   "type",
    "certainty":     "confidence",
    "conf":          "confidence",
    "source_ref":    "voltage_source_ref",
    "driver":        "voltage_source_ref",
    "source_pin":    "voltage_source_pin",
}

# Field-name aliases for component_roles sub-entries.
_ROLE_ALIASES: dict[str, str] = {
    "comp_role": "role",
    "function":  "role",
    "certainty": "confidence",
    "conf":      "confidence",
}


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _apply_aliases(raw: dict[str, Any], aliases: dict[str, str]) -> dict[str, Any]:
    """Return a copy of *raw* with alias keys replaced by their canonical names.

    When both an alias and its canonical target are present, the canonical name wins.
    """
    out: dict[str, Any] = {}
    for key, value in raw.items():
        canonical = aliases.get(key, key)
        # canonical key wins over alias
        if canonical not in out:
            out[canonical] = value
        elif canonical == key:
            out[canonical] = value
    return out


def _normalise_net(raw: dict[str, Any]) -> dict[str, Any]:
    return _apply_aliases(raw, _NET_ALIASES)


def _normalise_role(raw: dict[str, Any]) -> dict[str, Any]:
    return _apply_aliases(raw, _ROLE_ALIASES)


def _normalise_enrichment(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a normalised copy of the top-level enrichment dict."""
    return {
        "nets":            {k: _normalise_net(v)  for k, v in raw.get("nets", {}).items()},
        "component_roles": {k: _normalise_role(v) for k, v in raw.get("component_roles", {}).items()},
        "pin_directions":  dict(raw.get("pin_directions", {})),
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_net(net_name: str, fields: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if "confidence" in fields and fields["confidence"] not in VALID_CONFIDENCE:
        errors.append(
            f"nets[{net_name!r}]: 'confidence' must be one of "
            f"{sorted(VALID_CONFIDENCE)}, got {fields['confidence']!r}"
        )
    if "type" in fields and fields["type"] not in VALID_NET_TYPES:
        errors.append(
            f"nets[{net_name!r}]: 'type' must be one of "
            f"{sorted(VALID_NET_TYPES)}, got {fields['type']!r}"
        )
    for key in ("voltage", "voltage_min", "voltage_max"):
        if key in fields and not isinstance(fields[key], (int, float)):
            errors.append(
                f"nets[{net_name!r}]: '{key}' must be numeric, "
                f"got {fields[key]!r} ({type(fields[key]).__name__})"
            )
    return errors


def _validate_role(ref: str, fields: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if "role" in fields and fields["role"] not in VALID_ROLES:
        errors.append(
            f"component_roles[{ref!r}]: 'role' must be one of "
            f"{sorted(VALID_ROLES)}, got {fields['role']!r}"
        )
    if "confidence" in fields and fields["confidence"] not in VALID_CONFIDENCE:
        errors.append(
            f"component_roles[{ref!r}]: 'confidence' must be one of "
            f"{sorted(VALID_CONFIDENCE)}, got {fields['confidence']!r}"
        )
    return errors


def _validate_pins(ref: str, pins: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for pin, direction in pins.items():
        if direction not in VALID_DIRECTIONS:
            errors.append(
                f"pin_directions[{ref!r}][{pin!r}]: direction must be one of "
                f"{sorted(VALID_DIRECTIONS)}, got {direction!r}"
            )
    return errors


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def write_enrichments(
    db_path: str | Path,
    enrichment: dict[str, Any],
) -> dict[str, int]:
    """Normalise *enrichment* and write it into the SQLite database at *db_path*.

    Creates the database (and schema) if it does not already exist.

    Invalid individual entries are skipped with a warning to stderr — the rest
    of the batch is still written.

    Args:
        db_path:    Path to ``review.db``. Raises ``ValueError`` if the path
                    ends in ``.yaml`` or ``.yml``.
        enrichment: Top-level dict with optional keys ``nets``,
                    ``component_roles``, and ``pin_directions``.

    Returns:
        Summary dict:
        ``{"nets_updated": N, "roles_updated": N, "pins_updated": N, "skipped": N}``

    Raises:
        ValueError: If *db_path* ends in ``.yaml``/``.yml``.
    """
    db_path = Path(db_path)

    if db_path.suffix.lower() in (".yaml", ".yml"):
        print(
            f"DEPRECATION ERROR: '{db_path}' looks like a YAML path. "
            "Pass a .db path (e.g. review.db) instead.",
            file=sys.stderr,
        )
        raise ValueError(
            f"db_path must not end in .yaml/.yml — got '{db_path}'. "
            "Pass a .db path instead."
        )

    normalised = _normalise_enrichment(enrichment)

    _ensure_db(db_path)

    from db_io import get_connection, update_net, update_component, update_pin

    conn = get_connection(db_path)

    nets_updated = 0
    roles_updated = 0
    pins_updated = 0
    skipped = 0

    with conn:
        # ---- nets --------------------------------------------------------
        for net_name, fields in normalised["nets"].items():
            errors = _validate_net(net_name, fields)
            if errors:
                for err in errors:
                    print(f"WARNING (skipping): {err}", file=sys.stderr)
                skipped += 1
                continue
            update_net(conn, net_name, **fields)
            nets_updated += 1

        # ---- component_roles ---------------------------------------------
        for ref, role_data in normalised["component_roles"].items():
            errors = _validate_role(ref, role_data)
            if errors:
                for err in errors:
                    print(f"WARNING (skipping): {err}", file=sys.stderr)
                skipped += 1
                continue
            # Only `role` maps to a components column; confidence/reason are
            # enrichment metadata and have no column.
            role = role_data.get("role")
            if role is not None:
                update_component(conn, ref, role=role)
                roles_updated += 1

        # ---- pin_directions ----------------------------------------------
        for ref, pins in normalised["pin_directions"].items():
            if not isinstance(pins, dict):
                print(
                    f"WARNING (skipping): pin_directions[{ref!r}] must be a dict "
                    f"of pin→direction, got {type(pins).__name__}",
                    file=sys.stderr,
                )
                skipped += 1
                continue
            errors = _validate_pins(ref, pins)
            if errors:
                for err in errors:
                    print(f"WARNING (skipping): {err}", file=sys.stderr)
                skipped += 1
                continue
            for pin, direction in pins.items():
                update_pin(conn, ref, str(pin), direction=direction)
                pins_updated += 1

    if skipped:
        print(f"Skipped {skipped} invalid enrichment entries (see warnings above).", file=sys.stderr)

    return {
        "nets_updated":  nets_updated,
        "roles_updated": roles_updated,
        "pins_updated":  pins_updated,
        "skipped":       skipped,
    }


def _ensure_db(db_path: Path) -> None:
    """Create *db_path* with the review schema if it does not yet exist."""
    scripts_dir = Path(__file__).parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from db_schema import create_db
    create_db(db_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Write enrichment data (net voltages, roles, pin directions) to review.db.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("output", help="Path to review.db (SQLite database)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--from-yaml", metavar="FILE",
        help="Read enrichment from a YAML file",
    )
    group.add_argument(
        "--json", metavar="JSON",
        help="Enrichment as a JSON string",
    )
    group.add_argument(
        "--stdin", action="store_true",
        help="Read enrichment YAML from stdin",
    )
    args = parser.parse_args()

    output_path = Path(args.output)
    if output_path.suffix.lower() in (".yaml", ".yml"):
        print(
            f"ERROR: output path '{args.output}' ends in .yaml/.yml — "
            "pass a .db path (e.g. review.db) instead.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.from_yaml:
        with open(args.from_yaml, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    elif args.json:
        data = json.loads(args.json)
    else:  # --stdin
        data = yaml.safe_load(sys.stdin)

    if not isinstance(data, dict):
        print("ERROR: input must be a dict with 'nets', 'component_roles', and/or 'pin_directions' keys", file=sys.stderr)
        sys.exit(1)

    summary = write_enrichments(args.output, data)
    print(
        f"Enrichment written → {args.output}: "
        f"nets_updated={summary['nets_updated']}, "
        f"roles_updated={summary['roles_updated']}, "
        f"pins_updated={summary['pins_updated']}, "
        f"skipped={summary['skipped']}"
    )


if __name__ == "__main__":
    _cli()
