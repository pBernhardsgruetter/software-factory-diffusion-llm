"""Load decision templates and expand per-item questions from job state."""

from __future__ import annotations

import copy
import json
from pathlib import Path


_TYPES = ("boolean", "choice", "score")


def _check_question(q, where: str) -> str | None:
    """Return a problem description for question ``q``, or None when it is valid."""
    if not isinstance(q, dict):
        return f"{where}: question must be an object"
    if q.get("type") not in _TYPES:
        return f"{where}: type must be one of {list(_TYPES)}"
    ins = q.get("instructions")
    if not isinstance(ins, str) or not ins.strip():
        return f"{where}: instructions must be a non-empty string"
    crit = q.get("criteria")
    if q["type"] == "choice":
        if not isinstance(crit, dict) or not crit:
            return f"{where}: choice criteria must be a non-empty object"
    elif q["type"] == "score":
        if (not isinstance(crit, list) or not 2 <= len(crit) <= 10
                or not all(isinstance(c, str) and c.strip() for c in crit)):
            return f"{where}: score criteria must be a list of 2-10 non-empty strings"
    elif "criteria" in q:
        if not isinstance(crit, dict) or not set(crit) <= {"true", "false"}:
            return f"{where}: boolean criteria must be an object with only true/false keys"
    if "risk" in q and not isinstance(q["risk"], bool):
        return f"{where}: risk must be a boolean"
    return None


def _validate(tpl, stem: str) -> str | None:
    if not isinstance(tpl, dict):
        return "template must be a JSON object"
    if "id" not in tpl:
        return "id is missing"
    if tpl.get("name") != stem:
        return f"name must equal the file name {stem!r}"
    questions = tpl.get("questions")
    if not isinstance(questions, dict):
        return "questions must be an object"
    for qname, q in questions.items():
        if (problem := _check_question(q, f"question {qname!r}")):
            return problem
    per_item = tpl.get("per_item")
    if per_item is not None:
        if (not isinstance(per_item, dict)
                or set(per_item) != {"state_key", "prefix", "question"}
                or not isinstance(per_item["state_key"], str)
                or not isinstance(per_item["prefix"], str)
                or not isinstance(per_item["question"], dict)):
            return "per_item must be null or exactly {state_key: str, prefix: str, question: object}"
        if (problem := _check_question(per_item["question"], "per_item.question")):
            return f"per_item: {problem}"
    if not isinstance(tpl.get("fallback"), dict):
        return "fallback must be an object"
    return None


def load_template(root: Path, name: str) -> dict:
    """Load and validate ``<root>/factory/decisions/<name>.json``.

    ``root`` is the main checkout (like ``load_config`` / ``load_job``). Raises
    ``ValueError`` naming the file and the problem when the template is invalid.
    """
    path = Path(root) / "factory" / "decisions" / f"{name}.json"
    try:
        tpl = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name}: invalid JSON ({exc})") from exc
    if (problem := _validate(tpl, path.stem)):
        raise ValueError(f"{path.name}: {problem}")
    return tpl


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
