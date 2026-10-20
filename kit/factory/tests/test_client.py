import json

import httpx
import pytest
import respx

from factorylib.client import DecideError, RawResult, post_decision, to_wire_questions

ENDPOINT = "https://openrouter.ai/api/v1/systemone"
CFG = {"decide": {"endpoint": ENDPOINT,
                  "models": ["inception/mercury-decide:free", "inception/mercury-decide"],
                  "timeout_s": 5, "api_key_env": "OPENROUTER_API_KEY"}}

# Verbatim from docs/superpowers/spikes/2026-10-20-phase0.md (S1, paid model).
OK_BODY = {
    "model": "inception/mercury-decide-20260930",
    "answers": {
        "is_billing_issue": {"type": "noul", "noul": 0.9953904278889971},
        "category": {
            "type": "choice", "choice": "billing",
            "probabilities": {"billing": 0.6206702125302279, "bug": 0.37645551346993955,
                              "other": 0.0028742739998325354},
            "confidence": 0.4310053187953419,
        },
        "urgency": {
            "type": "score", "score": 1.9460001684711743,
            "legend": {"0": "not urgent", "1": "somewhat urgent", "2": "urgent", "3": "critical"},
            "probabilities": {"0": 0.0055364472159568886, "1": 0.08135749793708866,
                              "2": 0.8746754940067777, "3": 0.038430560840176785},
            "confidence": 0.8691390467908208,
        },
    },
    "usage": {"input_tokens": 258, "output_tokens": 3, "cost": 5.16e-06},
    "id": "gen-dec-1792515070-Tw8V3yvsd4POqeNWnRNd",
    "provider": "Inception",
}

QUESTIONS = {
    "is_billing_issue": {"type": "boolean", "instructions": "Billing problem?", "risk": True},
    "category": {"type": "choice", "instructions": "Which?",
                 "criteria": {"billing": "b", "bug": "x", "other": "o"}},
    "urgency": {"type": "score", "instructions": "How urgent?",
                "criteria": ["not urgent", "somewhat urgent", "urgent", "critical"]},
}
KEY = "sk-or-SECRET-123"


def call(**kw):
    return post_decision({"ticket": "t"}, QUESTIONS, cfg=CFG, api_key=KEY, **kw)


@respx.mock
def test_success_normalises_and_sends_wire_format():
    route = respx.post(ENDPOINT).respond(200, json=OK_BODY)
    r = call()
    assert isinstance(r, RawResult)
    assert r.model == "inception/mercury-decide:free" and r.usage == OK_BODY["usage"]
    assert isinstance(r.latency_ms, int) and r.latency_ms >= 0
    assert r.answers["is_billing_issue"] == {"type": "boolean", "probability": 0.9953904278889971}
    assert r.answers["category"]["choice"] == "billing"
    assert r.answers["urgency"]["score"] == 1.9460001684711743
    req = route.calls.last.request
    body = json.loads(req.content)
    assert body["model"] == "inception/mercury-decide:free"
    assert body["state"] == {"ticket": "t"}
    assert body["questions"]["is_billing_issue"] == {"type": "noul", "instructions": "Billing problem?"}
    assert body["questions"]["category"]["criteria"] == QUESTIONS["category"]["criteria"]
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert req.headers["x-title"] == "mercury-factory"
    assert req.headers["http-referer"] == "https://github.com/pBernhardsgruetter/software-factory-diffusion-llm"


def test_to_wire_questions_maps_boolean_only_and_does_not_mutate():
    wire = to_wire_questions(QUESTIONS)
    assert wire["is_billing_issue"]["type"] == "noul"
    assert "risk" not in wire["is_billing_issue"]
    assert wire["category"]["type"] == "choice" and wire["urgency"]["type"] == "score"
    assert QUESTIONS["is_billing_issue"]["type"] == "boolean"
    assert QUESTIONS["is_billing_issue"]["risk"] is True


@respx.mock
def test_boolean_criteria_not_sent_but_folded_into_instructions():
    route = respx.post(ENDPOINT).respond(200, json=OK_BODY)
    qs = {"b": {"type": "boolean", "instructions": "Is it?",
                "criteria": {"true": "it is X", "false": "it is Y"}},
          "t": {"type": "boolean", "instructions": "T?", "criteria": {"true": "only t"}},
          "f": {"type": "boolean", "instructions": "F?", "criteria": {"false": "only f"}}}
    post_decision({"s": 1}, qs, cfg=CFG, api_key=KEY)
    sent = json.loads(route.calls.last.request.content)["questions"]
    assert all("criteria" not in v and v["type"] == "noul" for v in sent.values())
    assert sent["b"]["instructions"] == "Is it? Answer true if: it is X. Answer false if: it is Y."
    assert sent["t"]["instructions"] == "T? Answer true if: only t."
    assert sent["f"]["instructions"] == "F? Answer false if: only f."
    assert qs["b"]["instructions"] == "Is it?" and "criteria" in qs["b"]


def test_boolean_without_criteria_keeps_instructions_unchanged():
    wire = to_wire_questions({"b": {"type": "boolean", "instructions": "Is it?"}})
    assert wire["b"] == {"type": "noul", "instructions": "Is it?"}


@respx.mock
def test_free_then_paid_on_429():
    route = respx.post(ENDPOINT)
    route.side_effect = [httpx.Response(429, json={"error": {"message": "slow", "code": 429}}),
                         httpx.Response(200, json=OK_BODY)]
    r = call()
    assert r.model == "inception/mercury-decide"
    assert route.call_count == 2


@respx.mock
def test_5xx_non_json_falls_through():
    route = respx.post(ENDPOINT)
    route.side_effect = [httpx.Response(502, text="<html>bad gateway</html>"),
                         httpx.Response(200, json=OK_BODY)]
    assert call().model == "inception/mercury-decide"


@respx.mock
def test_timeout_falls_through():
    route = respx.post(ENDPOINT)
    route.side_effect = [httpx.ReadTimeout("slow"), httpx.Response(200, json=OK_BODY)]
    assert call().model == "inception/mercury-decide"


@respx.mock
def test_connect_error_falls_through():
    route = respx.post(ENDPOINT)
    route.side_effect = [httpx.ConnectError("down"), httpx.Response(200, json=OK_BODY)]
    assert call().model == "inception/mercury-decide"


@respx.mock
def test_all_fail_raises():
    route = respx.post(ENDPOINT)
    route.side_effect = [httpx.Response(429, json={}), httpx.ConnectError("down")]
    with pytest.raises(DecideError) as ei:
        call()
    msg = str(ei.value)
    assert "inception/mercury-decide:free" in msg and "429" in msg
    assert "ConnectError" in msg
    assert KEY not in msg
    assert route.call_count == 2  # no same-model retry


def test_missing_key_raises_without_http():
    with respx.mock(assert_all_called=False) as m:
        route = m.post(ENDPOINT).respond(200, json=OK_BODY)
        for key in (None, ""):
            with pytest.raises(DecideError, match="OPENROUTER_API_KEY not set"):
                post_decision({}, QUESTIONS, cfg=CFG, api_key=key)
        assert route.call_count == 0


@respx.mock
@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_4xx_does_not_fall_through(status):
    route = respx.post(ENDPOINT).respond(
        status, json={"error": {"message": "nope", "code": status}})
    with pytest.raises(DecideError) as ei:
        call()
    assert str(status) in str(ei.value) and "nope" in str(ei.value)
    assert KEY not in str(ei.value)
    assert route.call_count == 1


@respx.mock
def test_malformed_200_falls_through():
    route = respx.post(ENDPOINT)
    route.side_effect = [httpx.Response(200, text="not json"), httpx.Response(200, json=OK_BODY)]
    assert call().model == "inception/mercury-decide"


@respx.mock
def test_missing_usage_is_empty_dict_and_injected_client_used():
    body = {k: v for k, v in OK_BODY.items() if k != "usage"}
    respx.post(ENDPOINT).respond(200, json=body)
    with httpx.Client() as c:
        r = call(http=c)
        assert not c.is_closed
    assert r.usage == {}
