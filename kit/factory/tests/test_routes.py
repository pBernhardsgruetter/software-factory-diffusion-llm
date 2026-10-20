import pytest

from factorylib.banding import Banded
from factorylib.routes import ROUTES

RISKS = ["auth", "payments", "dependencies", "ci_deploy", "schema_migration",
         "email", "prod_data", "build_scripts"]
ALL4 = ["security", "ux", "ui", "code"]


def yes():
    return Banded("boolean", True, 0.99, True)


def no():
    return Banded("boolean", False, 0.01, True)


def unsure(t="boolean"):
    return Banded(t, None, 0.0, False)


def choice(v, conf=True):
    return Banded("choice", v, 0.95 if conf else 0.4, conf)


def score(v, conf=True):
    return Banded("score", float(v), 0.9 if conf else 0.4, conf)


def triage_banded(complexity=None, ui=None, risks=None, kind=None):
    b = {"kind": kind or choice("bugfix"),
         "complexity": complexity or score(0),
         "ui_change": ui or no()}
    for r in RISKS:
        b[r] = (risks or {}).get(r, no())
    return b


# ---- D1 triage ----

@pytest.mark.parametrize("complexity,ui,risks,tier,reviewers,flagged", [
    (score(0), no(), {}, "fast", ["security", "code"], []),
    (score(1), no(), {}, "fast", ["security", "code"], []),
    (score(1.0), no(), {}, "fast", ["security", "code"], []),
    (score(2), no(), {}, "heavy", ["security", "code"], []),
    (score(3), yes(), {}, "heavy", ALL4, []),
    (score(0, conf=False), no(), {}, "heavy", ["security", "code"], []),
    (unsure("score"), no(), {}, "heavy", ["security", "code"], []),
    (score(0), yes(), {}, "fast", ALL4, []),              # tier independent of reviewers
    (score(0), unsure(), {}, "fast", ALL4, []),
    (score(0), no(), {"auth": yes()}, "heavy", ["security", "code"], ["auth"]),
    (score(0), no(), {"email": unsure(), "auth": yes()}, "heavy", ["security", "code"], ["auth", "email"]),
])
def test_triage_table(complexity, ui, risks, tier, reviewers, flagged):
    out = ROUTES["triage"](triage_banded(complexity, ui, risks), {})
    assert out == {"tier": tier, "reviewers": reviewers, "risks": flagged}


def test_triage_uncertain_risk_forces_heavy():
    out = ROUTES["triage"](triage_banded(risks={"prod_data": unsure()}), {})
    assert out["tier"] == "heavy"
    assert out["risks"] == ["prod_data"]


def test_triage_trivial_no_ui_gets_security_code_only():
    out = ROUTES["triage"](triage_banded(complexity=score(0), ui=no()), {})
    assert out["reviewers"] == ["security", "code"]
    assert out["tier"] == "fast"


def test_triage_empty_banded_is_conservative_and_does_not_raise():
    out = ROUTES["triage"]({}, {})
    assert out["tier"] == "heavy"
    assert out["reviewers"] == ALL4
    assert out["risks"] == RISKS


# ---- D2 clarify ----

@pytest.mark.parametrize("banded,expected", [
    ({"needs_human_clarification": yes()}, True),
    ({"needs_human_clarification": no()}, False),
    ({"needs_human_clarification": unsure()}, True),
    ({}, True),
])
def test_clarify_table(banded, expected):
    assert ROUTES["clarify"](banded, {}) == {"ask_human": expected}


# ---- D3 assumptions ----

@pytest.mark.parametrize("banded,expected", [
    ({"a0": no(), "a1": no()}, []),
    ({"a0": yes(), "a1": no()}, [0]),
    ({"a0": no(), "a1": unsure()}, [1]),
    ({"a0": unsure(), "a1": yes(), "a2": no()}, [0, 1]),
    ({}, []),
])
def test_assumptions_table(banded, expected):
    assert ROUTES["assumptions"](banded, {"assumptions": ["x"] * len(banded)}) == {"ask_human": expected}


def test_assumptions_missing_answer_for_item_is_asked():
    out = ROUTES["assumptions"]({"a0": no()}, {"assumptions": ["x", "y"]})
    assert out == {"ask_human": [1]}


def test_assumptions_iterates_in_numeric_order():
    banded = {f"a{i}": yes() for i in range(12)}
    out = ROUTES["assumptions"](banded, {"assumptions": list(range(12))})
    assert out["ask_human"] == list(range(12))


# ---- D4 failure ----

@pytest.mark.parametrize("banded,action", [
    ({"cause": choice("flaky")}, "retry"),
    ({"cause": choice("env")}, "blocked"),
    ({"cause": choice("spec_conflict")}, "blocked"),
    ({"cause": choice("code")}, "fix"),
    ({"cause": choice("flaky", conf=False)}, "fix"),
    ({"cause": choice("env", conf=False)}, "fix"),
    ({"cause": unsure("choice")}, "fix"),
    ({"cause": choice("bogus")}, "fix"),
    ({}, "fix"),
])
def test_failure_table(banded, action):
    assert ROUTES["failure"](banded, {}) == {"action": action}


# ---- D5 rereview ----

def rr_state(*verdicts):
    names = ALL4
    return {"reviewers": [{"name": n, "last_verdict": v, "delta_stat": "x"}
                          for n, v in zip(names, verdicts)]}


def test_rereview_changes_always_included():
    state = rr_state("CHANGES", "PASS", "PASS", "CHANGES")
    banded = {f"r{i}": no() for i in range(4)}
    assert ROUTES["rereview"](banded, state) == {"reviewers": ["security", "code"]}


def test_rereview_never_run_reviewer_included_when_uncertain():
    state = rr_state("PASS", None, "skipped", "PASS")
    banded = {"r0": no(), "r1": unsure(), "r2": unsure(), "r3": no()}
    assert ROUTES["rereview"](banded, state) == {"reviewers": ["ux", "ui"]}


def test_rereview_never_run_confident_untouched_excluded():
    state = rr_state(None, "skipped", "PASS", "PASS")
    banded = {f"r{i}": no() for i in range(4)}
    assert ROUTES["rereview"](banded, state) == {"reviewers": []}


def test_rereview_touched_included_even_if_pass():
    state = rr_state("PASS", "PASS", None, "PASS")
    banded = {"r0": no(), "r1": yes(), "r2": yes(), "r3": no()}
    assert ROUTES["rereview"](banded, state) == {"reviewers": ["ux", "ui"]}


def test_rereview_empty_state():
    assert ROUTES["rereview"]({}, {}) == {"reviewers": []}


def test_rereview_missing_answer_is_uncertain():
    state = rr_state("PASS", "PASS")
    assert ROUTES["rereview"]({"r0": no()}, state) == {"reviewers": ["ux"]}


def test_rereview_does_not_depend_on_item_order():
    state = {"reviewers": [{"name": "code", "last_verdict": "CHANGES", "delta_stat": ""},
                           {"name": "security", "last_verdict": "PASS", "delta_stat": ""}]}
    out = ROUTES["rereview"]({"r0": no(), "r1": no()}, state)
    assert out == {"reviewers": ["code"]}


# ---- D6 finding ----

@pytest.mark.parametrize("banded,fast,builder", [
    ({"f0": yes(), "f1": yes()}, [0, 1], []),
    ({"f0": yes(), "f1": no()}, [0], [1]),
    ({"f0": no(), "f1": no()}, [], [0, 1]),
    ({"f0": unsure(), "f1": yes()}, [1], [0]),
])
def test_finding_table(banded, fast, builder):
    state = {"findings": [{"reviewer": "code", "text": "t"}] * len(banded)}
    assert ROUTES["finding"](banded, state) == {"fast_fixer": fast, "builder": builder}


def test_finding_uncertain_goes_to_builder():
    out = ROUTES["finding"]({"f0": Banded("boolean", True, 0.6, False)},
                            {"findings": [{"reviewer": "code", "text": "t"}]})
    assert out == {"fast_fixer": [], "builder": [0]}


def test_finding_missing_answer_goes_to_builder():
    out = ROUTES["finding"]({"f0": yes()}, {"findings": ["a", "b"]})
    assert out == {"fast_fixer": [0], "builder": [1]}


def test_finding_no_findings():
    assert ROUTES["finding"]({}, {}) == {"fast_fixer": [], "builder": []}


# ---- D7 promote ----

@pytest.mark.parametrize("banded,promote", [
    ({"beyond_fast_tier": yes()}, True),
    ({"beyond_fast_tier": no()}, False),
    ({"beyond_fast_tier": unsure()}, True),
    ({}, True),
])
def test_promote_table(banded, promote):
    assert ROUTES["promote"](banded, {}) == {"promote": promote}


# ---- D8 stuck ----

@pytest.mark.parametrize("banded,escalate", [
    ({"progress": score(0)}, True),
    ({"progress": score(0.9)}, True),
    ({"progress": score(1)}, False),
    ({"progress": score(2)}, False),
    ({"progress": score(2, conf=False)}, True),
    ({"progress": unsure("score")}, True),
    ({}, True),
])
def test_stuck_table(banded, escalate):
    assert ROUTES["stuck"](banded, {}) == {"escalate": escalate}


# ---- D9 escalation ----

def esc_banded(**overrides):
    b = {r: no() for r in RISKS}
    b.update(overrides)
    return b


def test_escalation_rule_hits_escalate_even_if_decide_confident_no_risk():
    out = ROUTES["escalation"](esc_banded(), {"rule_hits": ["touches .github/workflows"]})
    assert out == {"escalate": True, "reasons": ["touches .github/workflows"]}


@pytest.mark.parametrize("banded,state,escalate,reasons", [
    (esc_banded(), {}, False, []),
    (esc_banded(), {"rule_hits": []}, False, []),
    (esc_banded(), {"rule_hits": None}, False, []),
    (esc_banded(auth=yes()), {}, True, ["auth"]),
    (esc_banded(email=unsure()), {}, True, ["email"]),
    (esc_banded(payments=yes(), auth=unsure()), {"rule_hits": ["r1", "r2"]}, True,
     ["r1", "r2", "auth", "payments"]),
])
def test_escalation_table(banded, state, escalate, reasons):
    assert ROUTES["escalation"](banded, state) == {"escalate": escalate, "reasons": reasons}


def test_escalation_empty_banded_escalates_for_all_risks():
    out = ROUTES["escalation"]({}, {})
    assert out == {"escalate": True, "reasons": RISKS}


# ---- D10 toolgate ----

@pytest.mark.parametrize("banded,perm", [
    ({"class": choice("safe")}, "allow"),
    ({"class": choice("needs_human")}, "ask"),
    ({"class": choice("destructive")}, "deny"),
    ({"class": choice("safe", conf=False)}, "ask"),
    ({"class": choice("destructive", conf=False)}, "ask"),
    ({"class": unsure("choice")}, "ask"),
    ({"class": choice("bogus")}, "ask"),
    ({}, "ask"),
])
def test_toolgate_table(banded, perm):
    assert ROUTES["toolgate"](banded, {}) == {"permission": perm}


def test_toolgate_uncertain_is_ask():
    assert ROUTES["toolgate"]({"class": choice("safe", conf=False)}, {}) == {"permission": "ask"}


# ---- D11 visual ----

@pytest.mark.parametrize("banded,met,unmet,unsure_", [
    ({"c0": yes(), "c1": no(), "c2": unsure()}, [0], [1], [2]),
    ({"c0": yes(), "c1": yes()}, [0, 1], [], []),
    ({"c0": no()}, [], [0], []),
    ({"c0": unsure(), "c1": unsure()}, [], [], [0, 1]),
])
def test_visual_table(banded, met, unmet, unsure_):
    state = {"criteria": ["x"] * len(banded)}
    assert ROUTES["visual"](banded, state) == {"met": met, "unmet": unmet, "unsure": unsure_}


def test_visual_missing_answer_is_unsure():
    out = ROUTES["visual"]({"c0": yes()}, {"criteria": ["a", "b"]})
    assert out == {"met": [0], "unmet": [], "unsure": [1]}


def test_visual_no_criteria():
    assert ROUTES["visual"]({}, {}) == {"met": [], "unmet": [], "unsure": []}


# ---- cross-cutting ----

def test_every_route_returns_fresh_dict_and_never_raises_on_empty():
    for name, fn in ROUTES.items():
        a = fn({}, {})
        b = fn({}, {})
        assert isinstance(a, dict)
        assert a == b and a is not b
