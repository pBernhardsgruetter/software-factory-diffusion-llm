"""Load decision templates and expand per-item questions from job state."""

from __future__ import annotations

import copy
import json
from pathlib import Path


def load_template(root: Path, name: str) -> dict:
    """Load ``<root>/decisions/<name>.json`` (``root`` is the ``kit/factory`` directory)."""
    return json.loads((Path(root) / "decisions" / f"{name}.json").read_text())


def _item_text(item) -> str:
    if isinstance(item, str):
        return item
    return json.dumps(item, separators=(",", ":"), ensure_ascii=False)


def state_items(tpl: dict, state: dict) -> list:
    """The list of items a per-item template iterates (``[]`` when missing or malformed)."""
    per_item = tpl.get("per_item")
    if not per_item:
        return []
    items = (state or {}).get(per_item["state_key"])
    return items if isinstance(items, list) else []


def expand_questions(tpl: dict, state: dict) -> dict:
    """Fixed questions plus one question per item for a ``per_item`` template.

    Item ``i`` becomes ``<prefix><i>``: a copy of ``per_item["question"]`` with
    ``" Item: <text>"`` appended to its instructions (dict items as compact JSON).
    """
    questions = copy.deepcopy(tpl.get("questions") or {})
    per_item = tpl.get("per_item")
    if per_item:
        for i, item in enumerate(state_items(tpl, state)):
            q = copy.deepcopy(per_item["question"])
            q["instructions"] = f"{q['instructions']} Item: {_item_text(item)}"
            questions[f"{per_item['prefix']}{i}"] = q
    return questions


def resolve_fallback(tpl: dict, state: dict) -> dict:
    """A fresh copy of the template fallback, with the ``"all"`` placeholder resolved.

    Only ``finding`` uses the placeholder (``builder: "all"`` means every finding index).
    """
    fb = copy.deepcopy(tpl["fallback"])
    if fb.get("builder") == "all":
        fb["builder"] = list(range(len(state_items(tpl, state))))
    return fb
