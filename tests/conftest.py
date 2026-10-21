"""Shared fixture loaders."""

from __future__ import annotations

import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def load_events(name: str) -> list[dict[str, object]]:
    """Load a JSONL Research stream fixture as wire-format event dicts."""
    lines = (FIXTURES / name).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def load_json(name: str) -> dict[str, object]:
    """Load a JSON fixture."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))
