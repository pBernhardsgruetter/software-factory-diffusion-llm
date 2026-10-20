import pytest

from factorylib.banding import Banded, band_answers

QUESTIONS = {
    "flag": {"type": "boolean", "instructions": "?"},
    "danger": {"type": "boolean", "instructions": "?", "risk": True},
    "cat": {"type": "choice", "instructions": "?", "criteria": {"a": "A", "b": "B"}},
    "urg": {"type": "score", "instructions": "?", "criteria": ["lo", "mid", "hi", "max"]},
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


def _choice(probs, choice="x"):
    qs = {"c": {"type": "choice", "instructions": "?", "criteria": {"x": "X", "y": "Y"}}}
    return band_answers(qs, {"c": {"type": "choice", "choice": choice, "probabilities": probs}}, {})["c"]


def _score(probs, score=1.0):
    ans = {"urg": {"type": "score", "score": score, "probabilities": probs}}
    return band_answers({"urg": QUESTIONS["urg"]}, ans, {})["urg"]


def test_choice_probs_not_summing_to_one_uncertain():
    r = _choice({"x": 0.9, "y": 0.9})
    assert (r.value, r.p, r.confident) == (None, 0.0, False)


def test_choice_missing_option_sum_low_uncertain():
    r = _choice({"x": 0.8})
    assert (r.value, r.p, r.confident) == (None, 0.0, False)
    assert _choice({"x": 0.8, "y": 0.2}).confident is True
    assert _choice({"x": 0.8, "y": 0.18}).confident is True  # within tolerance


def test_score_probs_not_summing_to_one_uncertain():
    r = _score({"0": 0.9, "1": 0.9, "2": 0.0, "3": 0.0})
    assert (r.value, r.p, r.confident) == (None, 0.0, False)
    assert _score({"0": 0.05, "1": 0.05, "2": 0.8, "3": 0.1}).confident is True


def test_score_out_of_range_uncertain():
    probs = {"0": 0.05, "1": 0.05, "2": 0.8, "3": 0.1}
    for bad in (-0.5, 3.5, 99.0):
        r = _score(probs, bad)
        assert (r.value, r.p, r.confident) == (None, 0.0, False)
    assert _score(probs, 3.0).value == 3.0
    assert _score(probs, 0.0).value == 0.0
    assert _score(probs, 3.04).confident is True  # tolerance


def test_score_bad_rung_key_uncertain():
    for probs in (
        {"0": 0.1, "1": 0.1, "2": 0.1, "9": 0.7},      # out of range
        {"0": 0.1, "1": 0.1, "2": 0.1, "high": 0.7},   # not an integer
        {"0": 0.1, "1": 0.1, "2": 0.1, "-1": 0.7},     # negative
        {"0": 0.1, "1": 0.1, "2": 0.1, "1.5": 0.7},    # non-integer string
    ):
        r = _score(probs)
        assert (r.value, r.p, r.confident) == (None, 0.0, False)


@pytest.mark.parametrize("t", [0.8, 0.85, 0.9, 0.95])
@pytest.mark.parametrize("which", ["t", "1-t"])
def test_boundary_exact_threshold_is_confident(t, which):
    qs = {"flag": QUESTIONS["flag"]}
    p = t if which == "t" else 1 - t
    r = band_answers(qs, {"flag": b(p)}, {"flag": t})["flag"]
    assert r.confident is True


def test_boundary_float_unsafe_lower_case():
    qs = {"flag": QUESTIONS["flag"]}
    assert band_answers(qs, {"flag": b(0.1)}, {"flag": 0.9})["flag"].confident is True


def test_boundary_choice_and_score_at_threshold():
    assert _choice({"x": 0.75, "y": 0.25}).confident is True
    assert _score({"0": 0.05, "1": 0.15, "2": 0.75, "3": 0.05}).confident is True
    assert _choice({"x": 0.74, "y": 0.26}).confident is False
