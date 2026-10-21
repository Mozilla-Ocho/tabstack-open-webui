"""Shared fixture loaders."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_events(name: str) -> list[dict[str, object]]:
    """Load a JSONL Research stream fixture as wire-format event dicts."""
    lines = (FIXTURES / name).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def load_json(name: str) -> dict[str, object]:
    """Load a JSON fixture."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that tries to open a network connection."""

    def refuse(*_: object, **__: object) -> None:
        raise RuntimeError("tests must not make network calls")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
