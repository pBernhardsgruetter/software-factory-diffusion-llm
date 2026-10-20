"""Suite-wide guard: tests write runtime state only under tmp_path."""

from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parent.parent  # kit/factory
RUNTIME = ("tool-gate.jsonl", "jobs")


@pytest.fixture(scope="session", autouse=True)
def kit_tree_stays_clean():
    before = {n for n in RUNTIME if (KIT / n).exists()}
    yield
    stray = [n for n in RUNTIME if (KIT / n).exists() and n not in before]
    assert stray == [], f"tests wrote runtime state into the kit source tree: {stray}"
