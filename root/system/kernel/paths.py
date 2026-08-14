"""Filesystem layout for aiOS.

Every piece of state lives under a single root directory so the entire OS can be
copied to a USB stick, a VM disk image or a VPS without changing a line. Nothing
is written outside this root -- that is what makes the OS portable and what makes
"pull the stick, no trace" true.
"""

import os
from pathlib import Path


def home() -> Path:
    """Resolve the aiOS root.

    AIOS_HOME wins so a VM or VPS can mount the payload anywhere; otherwise we
    infer it from this file's location (root/system/kernel/paths.py -> root).
    """
    env = os.environ.get("AIOS_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


HOME = home()
SYSTEM = HOME / "system"
APPS = HOME / "apps"
MEMORY = HOME / "memory"
VAULT = HOME / "vault"
LOGS = HOME / "logs"
DATA = HOME / "data"

CONFIG = HOME / "aios.json"


def ensure() -> None:
    for p in (APPS, MEMORY, VAULT, LOGS, DATA):
        p.mkdir(parents=True, exist_ok=True)


def inside(path) -> bool:
    """True if path resolves to somewhere inside the aiOS root.

    The jail check for every filesystem syscall. Resolving first means symlinks
    pointing out of the root are caught too.
    """
    try:
        Path(path).resolve().relative_to(HOME)
        return True
    except ValueError:
        return False


def rel(path) -> str:
    """Path rendered relative to the root, for display."""
    p = Path(path).resolve()
    try:
        return "/" + str(p.relative_to(HOME))
    except ValueError:
        return str(p)
