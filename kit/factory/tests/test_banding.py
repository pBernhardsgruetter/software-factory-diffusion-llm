from factorylib.banding import Banded, band_answers

QUESTIONS = {
    "flag": {"type": "boolean", "instructions": "?"},
    "danger": {"type": "boolean", "instructions": "?", "risk": True},
    "cat": {"type": "choice", "instructions": "?", "criteria": {"a": "A", "b": "B"}},
    "urg": {"type": "score", "instructions": "?", "criteria": ["lo", "mid", "hi"]},
}


def b(p):
    return {"type": "boolean", "probability": p}


def test_boolean_bands():
    qs = {"flag": QUESTIONS["flag"]}
    hi = band_answers(qs, {"flag": b(0.9)}, {})["flag"]
    assert (hi.value, hi.p, hi.confident) == (True, 0.9, True)
    mid = band_answers(qs, {"flag": b(0.5)}, {})["flag"]
    assert mid.value is True and mid.confident is False
    lo = band_answers(qs, {"flag": b(0.1)}, {})["flag"]
    assert lo.value is False and lo.confident is True


def test_risk_boolean_needs_095():
    qs = {"danger": QUESTIONS["danger"]}
    assert band_answers(qs, {"danger": b(0.9)}, {})["danger"].confident is False
    assert band_answers(qs, {"danger": b(0.96)}, {})["danger"].confident is True
    assert band_answers(qs, {"danger": b(0.04)}, {})["danger"].confident is True


def test_threshold_override():
    qs = {"flag": QUESTIONS["flag"]}
    assert band_answers(qs, {"flag": b(0.9)}, {"flag": 0.95})["flag"].confident is False
    assert band_answers(qs, {"flag": b(0.7)}, {"flag": 0.6})["flag"].confident is True


def test_choice_argmax():
    ans = {"cat": {"type": "choice", "choice": "a",
                   "probabilities": {"a": 0.6, "b": 0.4}, "confidence": 0.2}}
    r = band_answers(QUESTIONS, ans, {})["cat"]
    assert (r.type, r.value, r.p, r.confident) == ("choice", "a", 0.6, False)
    ans["cat"]["probabilities"] = {"a": 0.1, "b": 0.9}
    ans["cat"]["choice"] = "b"
    r = band_answers(QUESTIONS, ans, {})["cat"]
    assert (r.value, r.p, r.confident) == ("b", 0.9, True)


def test_score_uses_max_rung():
    ans = {"urg": {"type": "score", "score": 1.946,
                   "probabilities": {"0": 0.005, "1": 0.08, "2": 0.875, "3": 0.04},
                   "confidence": 0.87, "legend": {}}}
    r = band_answers(QUESTIONS, ans, {})["urg"]
    assert r.type == "score" and r.value == 1.946
    assert r.p == 0.875 and r.confident is True


def test_partial_answers_are_uncertain():
    ans = {"cat": {"type": "choice", "choice": "zzz",
                   "probabilities": {"a": 0.5, "zzz": 0.5}}}
    out = band_answers(QUESTIONS, ans, {})
    assert out["flag"] == Banded("boolean", None, 0.0, False)
    assert out["cat"] == Banded("choice", None, 0.0, False)
    assert out["urg"] == Banded("score", None, 0.0, False)
    assert all(not x.confident for x in out.values())


def test_malformed_answers_never_raise():
    ans = {
        "flag": {"type": "boolean", "probability": "high"},
        "danger": "garbage",
        "cat": {"type": "boolean", "probability": 0.9},      # wrong type
        "urg": {"type": "score", "score": 1.0, "probabilities": {"0": None}},
    }
    out = band_answers(QUESTIONS, ans, {})
    for name in QUESTIONS:
        assert out[name].value is None and out[name].confident is False
        assert out[name].p == 0.0
    # more nasty shapes
    nasty = {
        "flag": {"type": "boolean", "probability": float("nan")},
        "danger": {"type": "boolean"},
        "cat": {"type": "choice", "choice": "a", "probabilities": []},
        "urg": {"type": "score", "probabilities": {"0": 0.9}},  # missing score
    }
    out = band_answers(QUESTIONS, nasty, {})
    assert all(x.value is None and not x.confident for x in out.values())
    assert band_answers(QUESTIONS, None, {}).keys() == QUESTIONS.keys()


def test_boolean_out_of_range_is_uncertain():
    qs = {"flag": QUESTIONS["flag"]}
    assert band_answers(qs, {"flag": b(1.5)}, {})["flag"].confident is False
    assert band_answers(qs, {"flag": {"type": "boolean", "probability": True}}, {})["flag"].value is None
