import json
from pathlib import Path

import pytest

from factorylib.banding import band_answers
from factorylib.routes import ROUTES
from factorylib.templates import expand_questions, load_template

ROOT = Path(__file__).resolve().parent.parent.parent  # kit/: mirrors a target repo root
NAMES = ["triage", "clarify", "assumptions", "failure", "rereview", "finding",
         "promote", "stuck", "escalation", "toolgate", "visual"]
RISKS = ["auth", "payments", "dependencies", "ci_deploy", "schema_migration",
         "email", "prod_data", "build_scripts"]


def test_all_templates_load_and_have_route():
    files = sorted(p.stem for p in (ROOT / "factory" / "decisions").glob("*.json"))
    assert files == sorted(NAMES)
    for name in files:
        tpl = load_template(ROOT, name)
        assert tpl["name"] == name
        assert "fallback" in tpl
        assert name in ROUTES
    assert [load_template(ROOT, n)["id"] for n in NAMES] == [f"D{i}" for i in range(1, 12)]


@pytest.mark.parametrize("name", NAMES)
def test_template_questions_are_wellformed(name):
    tpl = load_template(ROOT, name)
    qs = dict(tpl["questions"])
    if tpl["per_item"]:
        assert set(tpl["per_item"]) == {"state_key", "prefix", "question"}
        qs["_item"] = tpl["per_item"]["question"]
    assert qs
    for q in qs.values():
        assert q["type"] in ("boolean", "choice", "score")
        assert isinstance(q["instructions"], str) and q["instructions"].strip()
        if q["type"] == "choice":
            assert isinstance(q["criteria"], dict) and q["criteria"]
        if q["type"] == "score":
            assert isinstance(q["criteria"], list) and 2 <= len(q["criteria"]) <= 10


def test_triage_template_shape():
    tpl = load_template(ROOT, "triage")
    qs = tpl["questions"]
    assert list(qs["kind"]["criteria"]) == ["docs", "ui", "bugfix", "feature", "refactor", "infra"]
    assert len(qs["complexity"]["criteria"]) == 4
    assert qs["ui_change"]["type"] == "boolean"
    assert [k for k, q in qs.items() if q.get("risk")] == RISKS
    assert tpl["fallback"] == {"tier": "heavy", "reviewers": ["security", "ux", "ui", "code"], "risks": []}


def test_escalation_template_has_eight_risks():
    tpl = load_template(ROOT, "escalation")
    assert list(tpl["questions"]) == RISKS
    assert all(q["risk"] and q["type"] == "boolean" for q in tpl["questions"].values())


def test_risk_instructions_are_distinct():
    qs = load_template(ROOT, "triage")["questions"]
    texts = [qs[r]["instructions"] for r in RISKS]
    assert len(set(texts)) == 8


def test_expand_per_item():
    tpl = load_template(ROOT, "assumptions")
    qs = expand_questions(tpl, {"assumptions": ["use postgres", "no auth"]})
    assert list(qs) == ["a0", "a1"]
    assert qs["a0"]["instructions"].endswith("Item: use postgres")
    assert qs["a1"]["instructions"].endswith("Item: no auth")
    assert qs["a0"]["type"] == "boolean"
    # the template itself is not mutated
    assert "Item:" not in tpl["per_item"]["question"]["instructions"]


def test_expand_per_item_missing_or_empty_state():
    tpl = load_template(ROOT, "assumptions")
    assert expand_questions(tpl, {}) == {}
    assert expand_questions(tpl, {"assumptions": []}) == {}
    assert expand_questions(tpl, {"assumptions": None}) == {}


def test_expand_dict_items_compact_json():
    tpl = load_template(ROOT, "finding")
    item = {"reviewer": "code", "text": "rename var"}
    qs = expand_questions(tpl, {"findings": [item]})
    suffix = qs["f0"]["instructions"].split("Item: ", 1)[1]
    assert json.loads(suffix) == item
    assert " " not in suffix.replace("rename var", "")  # compact separators


def test_expand_rereview_items():
    tpl = load_template(ROOT, "rereview")
    items = [{"name": "security", "last_verdict": "PASS", "delta_stat": "2 files"}]
    qs = expand_questions(tpl, {"reviewers": items})
    assert list(qs) == [tpl["per_item"]["prefix"] + "0"]
    assert json.loads(qs[tpl["per_item"]["prefix"] + "0"]["instructions"].split("Item: ", 1)[1]) == items[0]


def test_expand_without_per_item_returns_fixed_questions_copy():
    tpl = load_template(ROOT, "clarify")
    qs = expand_questions(tpl, {"anything": [1]})
    assert qs == tpl["questions"]
    assert qs is not tpl["questions"]


def test_expand_merges_fixed_and_per_item():
    tpl = {"questions": {"x": {"type": "boolean", "instructions": "X?"}},
           "per_item": {"state_key": "items", "prefix": "i",
                        "question": {"type": "boolean", "instructions": "Q."}}}
    qs = expand_questions(tpl, {"items": ["a", "b"]})
    assert list(qs) == ["x", "i0", "i1"]


def test_expanded_questions_band_cleanly():
    tpl = load_template(ROOT, "assumptions")
    qs = expand_questions(tpl, {"assumptions": ["a"]})
    banded = band_answers(qs, {"a0": {"type": "boolean", "probability": 0.99}}, {})
    assert banded["a0"].confident and banded["a0"].value is True


def test_resolve_fallback_is_fresh_and_resolves_all():
    from factorylib.templates import resolve_fallback
    tpl = load_template(ROOT, "finding")
    fb = resolve_fallback(tpl, {"findings": ["a", "b", "c"]})
    assert fb == {"fast_fixer": [], "builder": [0, 1, 2]}
    assert fb is not tpl["fallback"]
    assert resolve_fallback(tpl, {}) == {"fast_fixer": [], "builder": []}
    tri = load_template(ROOT, "triage")
    f1, f2 = resolve_fallback(tri, {}), resolve_fallback(tri, {})
    assert f1 == tri["fallback"] and f1 is not f2 and f1["reviewers"] is not f2["reviewers"]


# ---- load-time validation ----

GOOD = {
    "id": "D99", "name": "demo",
    "questions": {
        "b": {"type": "boolean", "instructions": "Is it?",
              "criteria": {"true": "yes", "false": "no"}, "risk": True},
        "c": {"type": "choice", "instructions": "Which?", "criteria": {"a": "A"}},
        "s": {"type": "score", "instructions": "How much?", "criteria": ["lo", "hi"]},
    },
    "per_item": {"state_key": "items", "prefix": "i",
                 "question": {"type": "boolean", "instructions": "Item ok?"}},
    "fallback": {"x": 1},
}


def write(tmp_path, tpl, name="demo"):
    d = tmp_path / "factory" / "decisions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(tpl))
    return tmp_path


def mutated(**changes):
    import copy
    t = copy.deepcopy(GOOD)
    for k, v in changes.items():
        if v is _DEL:
            t.pop(k, None)
        else:
            t[k] = v
    return t


_DEL = object()


def q_with(**kw):
    import copy
    t = copy.deepcopy(GOOD)
    t["questions"]["x"] = {"type": "boolean", "instructions": "X?", **kw}
    return t


def test_load_template_reads_root_factory_decisions(tmp_path):
    write(tmp_path, GOOD)
    assert load_template(tmp_path, "demo")["id"] == "D99"


def test_load_template_does_not_read_root_decisions(tmp_path):
    (tmp_path / "decisions").mkdir()
    (tmp_path / "decisions" / "demo.json").write_text(json.dumps(GOOD))
    with pytest.raises(FileNotFoundError):
        load_template(tmp_path, "demo")


def test_all_shipped_templates_validate():
    for n in NAMES:
        load_template(ROOT, n)


@pytest.mark.parametrize("tpl,fragment", [
    (mutated(id=_DEL), "id"),
    (mutated(name="other"), "name"),
    (mutated(name=_DEL), "name"),
    (mutated(questions=[]), "questions"),
    (mutated(questions=_DEL), "questions"),
    (mutated(per_item=[]), "per_item"),
    (mutated(per_item={"state_key": "i", "prefix": "p"}), "per_item"),
    (mutated(per_item={"state_key": "i", "prefix": "p", "question": {"type": "boolean",
        "instructions": "q"}, "extra": 1}), "per_item"),
    (mutated(per_item={"state_key": 1, "prefix": "p", "question": {"type": "boolean",
        "instructions": "q"}}), "per_item"),
    (mutated(per_item={"state_key": "i", "prefix": 2, "question": {"type": "boolean",
        "instructions": "q"}}), "per_item"),
    (mutated(per_item={"state_key": "i", "prefix": "p", "question": "q"}), "per_item"),
    (mutated(per_item={"state_key": "i", "prefix": "p", "question": {"type": "text",
        "instructions": "q"}}), "per_item"),
    (mutated(fallback=[]), "fallback"),
    (mutated(fallback=_DEL), "fallback"),
    (q_with(type="text"), "type"),
    (mutated(questions={"x": {"instructions": "X?"}}), "type"),
    (mutated(questions={"x": {"type": "boolean"}}), "instructions"),
    (mutated(questions={"x": {"type": "boolean", "instructions": "  "}}), "instructions"),
    (mutated(questions={"x": {"type": "boolean", "instructions": 5}}), "instructions"),
    (mutated(questions={"x": {"type": "choice", "instructions": "?"}}), "criteria"),
    (mutated(questions={"x": {"type": "choice", "instructions": "?", "criteria": {}}}), "criteria"),
    (mutated(questions={"x": {"type": "choice", "instructions": "?", "criteria": ["a"]}}), "criteria"),
    (mutated(questions={"x": {"type": "score", "instructions": "?", "criteria": ["only"]}}), "criteria"),
    (mutated(questions={"x": {"type": "score", "instructions": "?", "criteria": [str(i) for i in "abcdefghijk"]}}), "criteria"),
    (mutated(questions={"x": {"type": "score", "instructions": "?", "criteria": ["a", ""]}}), "criteria"),
    (mutated(questions={"x": {"type": "score", "instructions": "?", "criteria": ["a", 2]}}), "criteria"),
    (mutated(questions={"x": {"type": "score", "instructions": "?", "criteria": {"a": "b"}}}), "criteria"),
    (q_with(criteria=["x"]), "criteria"),
    (q_with(criteria={"maybe": "x"}), "criteria"),
    (q_with(criteria={"true": "x", "other": "y"}), "criteria"),
    (q_with(risk="yes"), "risk"),
    (q_with(risk=1), "risk"),
])
def test_load_template_rejects_invalid(tmp_path, tpl, fragment):
    write(tmp_path, tpl)
    with pytest.raises(ValueError) as e:
        load_template(tmp_path, "demo")
    assert "demo.json" in str(e.value)
    assert fragment in str(e.value)


def test_load_template_rejects_non_object_and_bad_json(tmp_path):
    write(tmp_path, [1, 2])
    with pytest.raises(ValueError, match="demo.json"):
        load_template(tmp_path, "demo")
    (tmp_path / "factory" / "decisions" / "demo.json").write_text("{nope")
    with pytest.raises(ValueError, match="demo.json"):
        load_template(tmp_path, "demo")


def test_load_template_accepts_null_per_item_and_score_bounds(tmp_path):
    t = mutated(per_item=None)
    t["questions"]["s2"] = {"type": "score", "instructions": "?", "criteria": list("abcdefghij")}
    write(tmp_path, t)
    assert load_template(tmp_path, "demo")["per_item"] is None
