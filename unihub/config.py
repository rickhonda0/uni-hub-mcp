"""Paths, settings and secrets."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "corpus"
RAW = ROOT / "raw"
STATE = ROOT / "state"
DB_PATH = STATE / "hub.db"

CANVAS_BASE = os.environ.get("CANVAS_BASE_URL", "https://canvas.sydney.edu.au").rstrip("/")
# AU Ed courses are served from the main host; au.edstem.org redirects away.
ED_BASE = os.environ.get("ED_BASE_URL", "https://edstem.org/api").rstrip("/")
ED_WEB = os.environ.get("ED_WEB_URL", "https://edstem.org/au").rstrip("/")

TIMEZONE = "Australia/Sydney"

# Files larger than this are recorded in the outline but not downloaded.
MAX_DOWNLOAD_BYTES = 150 * 1024 * 1024


def _read_dotenv() -> dict[str, str]:
    path = ROOT / ".env"
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def secret(name: str) -> str:
    """Return a secret from the environment or .env. Callers must never log the value."""
    value = os.environ.get(name) or _read_dotenv().get(name)
    if not value:
        raise RuntimeError(f"{name} is not set in the environment or in {ROOT / '.env'}")
    return value


def preferred_unit_names() -> set[str]:
    """Unit folder names that already exist next to this hub (e.g. DATA2902), used as unit keys."""
    try:
        return {p.name.upper() for p in ROOT.parent.iterdir() if p.is_dir()}
    except OSError:
        return set()
