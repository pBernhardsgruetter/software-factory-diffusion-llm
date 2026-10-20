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
    assert "reviewers_active" in text and "skipped" in text


def test_escalation_truth_is_the_independent_verdict():
    line = "Independent verdict (ignoring Decide)"
    assert line in (AGENTS / "approver.md").read_text()
    assert line in _skill()
    assert "first sentence after the Independent verdict line" in _skill()
    assert "escalation outcome skipped" in _skill()


def test_every_helper_call_isolates_stderr():
    text = _skill()
    calls = re.findall(r"`(factory/bin/(?:decide|fast-coder) [^`\n]*--job[^`\n]*)`", text)
    calls += [ln for ln in text.splitlines()
              if re.match(r"factory/bin/(?:decide|fast-coder) .*--job", ln)]
    assert len(calls) > 20
    bad = [c for c in calls if not c.endswith(" 2>>factory/jobs/<job-id>/stderr.log")]
    assert bad == []


def test_budget_baseline_and_config_resumes():
    text = _skill()
    assert "budget_base" in text
    assert "max_resumes" in text


def test_builder_knows_promotion_and_failure_decision():
    text = (AGENTS / "builder.md").read_text()
    assert "When promoted" in text
    assert "decide failure" in text


def test_fast_tier_allowlist_block_promotes():
    """Final review I4: a fast-tier allowlist block promotes; safety denials escalate."""
    text = _skill()
    check = text[text.index("### Build check"):text.index("### Review round N")]
    for phrase in ("command not on allowlist", "cd outside own worktree", "**Promote**",
                   "fast tier blocked: <that line>", "policy deny", "guarded path",
                   "job folder is orchestrator-owned", "source `tool-gate`"):
        assert phrase in check, phrase
    # the safety denials are checked before the promote rule
    assert check.index("job folder is orchestrator-owned") < check.index("command not on allowlist")
    # the phrases are exactly what the gate prints
    gate = (KIT / "factory" / "factorylib" / "toolgate.py").read_text()
    for phrase in ("command not on allowlist", "cd outside own worktree", "policy deny",
                   "guarded path", "path outside the worktree",
                   "job folder is orchestrator-owned"):
        assert phrase in gate, phrase
    for role in ("fast-builder", "fast-fixer"):
        assert "decide failure" not in (FAST_AGENTS / f"{role}.md").read_text()
