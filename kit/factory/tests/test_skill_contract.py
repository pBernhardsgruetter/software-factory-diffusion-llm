"""The /factory skill and agent prompts stay in step with the decisions and fast agents."""

import re
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent.parent  # kit/
SKILL = KIT / ".claude" / "skills" / "factory" / "SKILL.md"
AGENTS = KIT / ".claude" / "agents"
DECISIONS = KIT / "factory" / "decisions"
FAST_AGENTS = KIT / "factory" / "fast-agents"


def _skill() -> str:
    return SKILL.read_text()


def test_every_decision_is_named_except_toolgate():
    text = _skill()
    names = sorted(p.stem for p in DECISIONS.glob("*.json"))
    assert len(names) == 11
    missing = [n for n in names if n != "toolgate" and not re.search(rf"\bdecide {n}\b", text)]
    assert missing == []


def test_orchestrator_never_calls_toolgate():
    assert "decide toolgate" not in _skill()


def test_every_fast_coder_role_has_a_prompt():
    roles = set(re.findall(r"(?:factory/bin/|`)fast-coder ([a-z][a-z-]*)", _skill()))
    assert {"scout", "fast-builder", "fast-fixer"} <= roles
    missing = [r for r in roles if not (FAST_AGENTS / f"{r}.md").is_file()]
    assert missing == []


def test_skill_keeps_the_core_phrases():
    text = _skill()
    for phrase in ("WAIT FOR ALL", "skipped", "decide outcome", "When a decide call fails",
                   "factory/bin/decide", "factory/bin/fast-coder"):
        assert phrase in text, phrase


def test_approver_reads_decide_escalation():
    text = (AGENTS / "approver.md").read_text()
    assert "Decide escalation" in text
    assert "decisions.jsonl" in text


def test_builder_knows_promotion_and_failure_decision():
    text = (AGENTS / "builder.md").read_text()
    assert "When promoted" in text
    assert "decide failure" in text
