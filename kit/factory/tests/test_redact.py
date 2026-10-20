import copy

import pytest

from factorylib.redact import cap_text, redact_obj

PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEA\nabcdef\n"
    "-----END RSA PRIVATE KEY-----"
)
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
SK_OR = "sk-or-v1-" + "ab12" * 16


def test_redacts_planted_secrets():
    state = {
        "a": {"deep": [f"key is {SK_OR} ok", ("x", "GITHUB_TOKEN=abc")]},
        "b": f"cert:\n{PEM}\nend",
        "c": [{"jwt": JWT}],
    }
    out, count = redact_obj(state, [])
    flat = repr(out)
    assert SK_OR not in flat
    assert "GITHUB_TOKEN=abc" not in flat
    assert "PRIVATE KEY" not in flat
    assert "eyJhbGci" not in flat
    assert count == 4
    assert out["a"]["deep"][0] == "key is [REDACTED] ok"
    assert isinstance(out["a"]["deep"][1], tuple)


def test_redact_leaves_code_alone():
    out, count = redact_obj("const tokenCount = 3", [])
    assert out == "const tokenCount = 3"
    assert count == 0


def test_redact_does_not_mutate_input_and_keeps_keys_and_non_strings():
    state = {"sk-abcdefghijklmnopqrstuv": 5, "n": None, "l": [SK_OR], "t": 1.5}
    before = copy.deepcopy(state)
    out, count = redact_obj(state, [])
    assert state == before
    assert count == 1
    assert out["sk-abcdefghijklmnopqrstuv"] == 5
    assert out["n"] is None and out["t"] == 1.5
    assert out["l"] == ["[REDACTED]"]


def test_other_builtin_patterns():
    s = "ghp_" + "a" * 36 + " AKIAABCDEFGHIJKLMNOP github_pat_" + "B" * 22
    out, count = redact_obj(s, [])
    assert out == "[REDACTED] [REDACTED] [REDACTED]"
    assert count == 3


def test_extra_patterns_applied():
    out, count = redact_obj({"x": "customer ACME-1234 here"}, [r"ACME-\d+"])
    assert out == {"x": "customer [REDACTED] here"}
    assert count == 1


def test_invalid_extra_pattern_raises_value_error_naming_pattern():
    with pytest.raises(ValueError, match=r"\(unclosed"):
        redact_obj("x", ["(unclosed"])


def test_cap_text_marker():
    out = cap_text("a" * 300, 100)
    marker = "\n…[truncated 200 chars]…\n"
    assert len(out) <= 100 + len(marker)
    assert "[truncated 200 chars]" in out
    assert out == "a" * 50 + marker + "a" * 50


def test_cap_text_noop_when_short():
    assert cap_text("abc", 3) == "abc"
    assert cap_text("abc", 10) == "abc"


def test_cap_text_odd_cap_head_tail_sum():
    s = "".join(chr(65 + i % 26) for i in range(100))
    out = cap_text(s, 11)
    head, rest = out.split("\n…[truncated", 1)
    tail = rest.split("]…\n", 1)[1]
    assert len(head) == 5 and len(tail) == 6
    assert head == s[:5] and tail == s[-6:]
    assert "[truncated 89 chars]" in out
