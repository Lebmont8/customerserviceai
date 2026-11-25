"""Small cross-process runtime failure markers used by healthcheck and bots."""

from __future__ import annotations

import os
from pathlib import Path
import re


RUNTIME_DIR = Path(
    os.getenv("CUSTOMERSERVICEAI_RUNTIME_DIR", "/tmp/customerserviceai-runtime")
)


def _safe_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:80] or "unknown"


def record_polling_conflict(role: str, shop_id: str = "") -> Path:
    RUNTIME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(RUNTIME_DIR, 0o700)
    suffix = f"-{_safe_component(shop_id)}" if shop_id else ""
    marker = RUNTIME_DIR / f"{_safe_component(role)}{suffix}.polling_conflict"
    marker.write_text("polling_conflict\n", encoding="utf-8")
    os.chmod(marker, 0o600)
    return marker


def polling_conflict_count() -> int:
    if not RUNTIME_DIR.exists():
        return 0
    return sum(1 for _ in RUNTIME_DIR.glob("*.polling_conflict"))


def clear_polling_conflicts() -> int:
    if not RUNTIME_DIR.exists():
        return 0
    removed = 0
    for marker in RUNTIME_DIR.glob("*.polling_conflict"):
        marker.unlink(missing_ok=True)
        removed += 1
    return removed
