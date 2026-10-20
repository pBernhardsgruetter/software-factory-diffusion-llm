"""Load ``factory/config.json`` and resolve per-decision settings."""

from __future__ import annotations

import json
from pathlib import Path


def load_config(root: Path) -> dict:
    """Read ``factory/config.json`` under ``root``."""
    return json.loads((Path(root) / "factory" / "config.json").read_text())


def decision_config(cfg: dict, name: str) -> dict:
    """Return ``{"mode", "thresholds"}`` for a decision; missing means shadow."""
    entry = (cfg.get("decisions") or {}).get(name)
    if not entry:
        return {"mode": "shadow", "thresholds": {}}
    return {
        "mode": entry.get("mode", "shadow"),
        "thresholds": dict(entry.get("thresholds") or {}),
    }
