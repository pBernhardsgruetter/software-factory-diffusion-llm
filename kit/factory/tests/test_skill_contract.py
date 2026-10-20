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
    missing = [r for r in roles - {"clean-stubs"} if not (FAST_AGENTS / f"{r}.md").is_file()]
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


def test_readme_covers_usage_and_security_model():
    readme = KIT.parent / "README.md"
    if not readme.is_file():  # an installed copy of the kit has no repo README
        return
    text = readme.read_text()
    for phrase in ("scripts/install.sh", "--force", ".factory-backup-", "/factory next",
                   "/factory approve", "/factory rework", "`main` branch", "OPENROUTER_API_KEY",
                   "decide report", "python3 -m http.server 8765", "## Costs",
                   "## Security model: guardrail, not a sandbox", "pi", "## End-to-end results",
                   "unsupported", "risk categories"):
        assert phrase in text, phrase


def test_reviewer_ui_wording():
    """Final review M3: decide runs from the main checkout; a failed call is not fatal."""
    text = (AGENTS / "reviewer-ui.md").read_text()
    head = text.split("---")[1]
    assert "observations.json" in head and "decide visual" in head and "review-ui.md" in head
    assert "your one file" not in text
    assert "run it from the\nmain checkout, without the `cd`" in text
    assert "exits non-zero or prints no record, continue\non your observations alone" in text


def test_m4_review_commit_and_gate_description():
    text = _skill()
    assert "git -C" not in text
    assert "`cd <worktree> && git rev-parse HEAD`" in text
    assert "a PreToolUse hook on the Mercury 3 roles)" not in text
    for phrase in ("main session", "subagents", "Mercury 3 roles"):
        assert phrase in text[text.index("The tool gate"):][:600], phrase


def test_m5_spec_lists_are_flat_dash_bullets():
    text = (AGENTS / "spec-writer.md").read_text()
    for section, item in (("## Acceptance criteria", "criterion"), ("## Assumptions", "assumption")):
        body = text[text.index(section):].split("\n## ", 1)[0]
        assert f"one {item} per bullet, no nested bullets" in body, section
        assert "Numbered list" not in body
    # the template parses as the factory expects
    from factorylib.state import parse_assumptions
    spec = ("## Assumptions\n- Use the existing date helper: it is already imported.\n"
            "- Keep the English copy: no i18n in the repo.\n")
    assert parse_assumptions(spec) == ["Use the existing date helper: it is already imported.",
                                       "Keep the English copy: no i18n in the repo."]
    assert parse_assumptions("## Assumptions\n- None.\n") == []
