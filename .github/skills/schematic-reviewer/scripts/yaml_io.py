#!/usr/bin/env python3
"""
Process-safe YAML read-modify-write for schematic.yaml.

Usage:
    from yaml_io import locked_yaml_update

    with locked_yaml_update(schematic_path) as data:
        data['components']['U1']['verified'] = True
    # schematic.yaml is written back and the lock is released on exit.

The lock file is placed alongside the YAML file with a '.lock' suffix
(e.g. 'schematic.yaml.lock'). Multiple parallel subagents will queue up
and each get exclusive access in turn — no data is lost.
"""
from contextlib import contextmanager
from pathlib import Path

import yaml

try:
    from filelock import FileLock, Timeout as FileLockTimeout
    _HAS_FILELOCK = True
except ImportError:
    _HAS_FILELOCK = False

LOCK_TIMEOUT = 60.0  # seconds to wait before giving up


@contextmanager
def locked_yaml_update(path: Path, timeout: float = LOCK_TIMEOUT):
    """Context manager for exclusive read-modify-write on a YAML file.

    Acquires an exclusive file lock on '<path>.lock', loads the YAML into a
    dict, yields it for modification, then writes it back and releases the
    lock.  All of this is atomic with respect to other processes using the
    same lock file.

    Args:
        path: Path to the YAML file to update.
        timeout: Seconds to wait for the lock before raising TimeoutError.

    Raises:
        TimeoutError: If the lock cannot be acquired within *timeout* seconds.
        FileNotFoundError: If *path* does not exist when the lock is acquired.
    """
    lock_path = Path(str(path) + ".lock")

    if not _HAS_FILELOCK:
        import warnings
        warnings.warn(
            f"'filelock' is not installed — {path.name} is NOT protected against "
            "concurrent writes from parallel subagents. "
            "Fix with: pip install filelock",
            stacklevel=3,
        )
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        yield data
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        return

    lock = FileLock(str(lock_path), timeout=timeout)
    try:
        lock.acquire()
    except FileLockTimeout:
        raise TimeoutError(
            f"Could not acquire lock on {path} within {timeout}s. "
            "Another process may be stuck. "
            f"Delete {lock_path} manually if the problem persists."
        ) from None

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        yield data
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    finally:
        lock.release()
