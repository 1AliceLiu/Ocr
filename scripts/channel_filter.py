"""Channel-code blocklist shared by the monitor and HK03 reconciliation."""
from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BLOCKED_FILE = PROJECT_ROOT / "config" / "blocked_channels.txt"


def load_blocked_prefixes(path: Path = DEFAULT_BLOCKED_FILE) -> tuple[str, ...]:
    """One prefix per line; blank lines and # comments are ignored."""
    if not path.exists():
        return ()
    prefixes: list[str] = []
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            value = raw.strip()
            if not value or value.startswith("#"):
                continue
            value = value.rstrip("*$")
            if value and value not in prefixes:
                prefixes.append(value)
    except OSError:
        return ()
    return tuple(prefixes)


def is_blocked(channel_code: str, prefixes: tuple[str, ...]) -> bool:
    return any(channel_code.startswith(prefix) for prefix in prefixes)
