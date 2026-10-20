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


# ---- fix round 1: broadened patterns ----

@pytest.mark.parametrize(
    "line",
    [
        "export OPENAI_API_KEY=abcd1234efgh",
        "  API_KEY=abc12345",
        "password: hunter2hunter2",
        "PASSWORD=hunter2",
        "KEY=abc",
        "TOKEN=abc",
        "SECRET=abc",
        "db_credential = 'abcdefghij'",
        "DB_PASSWD=short",
    ],
)
def test_assignment_lines_redacted(line):
    out, count = redact_obj(f"before\n{line}\nafter", [])
    secret = line.strip().split("=")[-1].split(":")[-1].strip(" '")
    assert secret not in out
    assert "[REDACTED]" in out
    assert "before" in out and "after" in out
    assert count == 1


@pytest.mark.parametrize(
    "text",
    ["const tokenCount = 3", "  tokenCount = 3", "const apiKey = getKey();", "monkey = 1",
     "key: 12", "if (token == null) return"],
)
def test_assignment_pattern_leaves_code_alone(text):
    assert redact_obj(text, []) == (text, 0)


@pytest.mark.parametrize(
    "text, secret",
    [
        ('{"api_key": "sk_abcdefgh12345", "x": 1}', "sk_abcdefgh12345"),
        ("{'auth_token': 'abcdefgh1234'}", "abcdefgh1234"),
        ('  "client_secret":   "Zxcvbnm98765"', "Zxcvbnm98765"),
    ],
)
def test_quoted_key_values_redacted(text, secret):
    out, count = redact_obj(text, [])
    assert secret not in out
    assert "[REDACTED]" in out
    assert count == 1


def test_quoted_key_short_value_kept():
    s = '{"api_key": "short", "name": "abcdefghijkl"}'
    assert redact_obj(s, []) == (s, 0)


@pytest.mark.parametrize(
    "secret",
    [
        "sk_live_" + "aB3dE5gH7jK9mN1pQ",
        "sk_test_" + "aB3dE5gH7jK9mN1pQ",
        "gho_" + "a" * 36,
        "ghs_" + "A1" * 20,
        "ghu_" + "z" * 40,
        "ghr_" + "z" * 36,
        "xoxb-1234567890-abcdefghij",
        "xoxp-1234567890-1234567890-abc",
    ],
)
def test_new_token_formats(secret):
    out, count = redact_obj(f"use {secret} now", [])
    assert out == "use [REDACTED] now"
    assert count == 1


def test_bearer_token_redacted():
    out, count = redact_obj("Authorization: Bearer abcdEFGH1234.ijkl_5678~+/=", [])
    assert "abcdEFGH1234" not in out
    assert count == 1
    out, count = redact_obj("authorization: bearer " + JWT, [])
    assert "eyJ" not in out and count == 1


def test_url_userinfo_password_redacted():
    out, count = redact_obj("clone https://alice:s3cretpw@github.com/org/repo.git", [])
    assert "s3cretpw" not in out
    assert "github.com/org/repo.git" in out
    assert count == 1
    out, count = redact_obj("postgres://user:pa55@db:5432/app", [])
    assert "pa55" not in out and count == 1


def test_url_without_credentials_untouched():
    s = "see http://localhost:3000/a@b and https://example.com/x"
    assert redact_obj(s, []) == (s, 0)


def test_sk_left_boundary():
    s = "disk-usage-monitoring-report-x"
    assert redact_obj(s, []) == (s, 0)
    out, count = redact_obj("key " + SK_OR + " and (sk-abcdefghijklmnopqrst)", [])
    assert SK_OR not in out and "sk-abcdefghij" not in out
    assert count == 2


def test_bytes_and_bytearray_leaves_decoded_and_redacted():
    out, count = redact_obj({"a": SK_OR.encode(), "b": [bytearray(b"x " + SK_OR.encode())]}, [])
    assert out == {"a": "[REDACTED]", "b": ["x [REDACTED]"]}
    assert count == 2
    out, count = redact_obj(b"\xff\xfeplain", [])
    assert isinstance(out, str) and out.endswith("plain") and count == 0


def test_set_and_frozenset_walked_preserving_type():
    out, count = redact_obj({"s": {SK_OR, "ok"}, "f": frozenset({SK_OR})}, [])
    assert out["s"] == {"[REDACTED]", "ok"} and isinstance(out["s"], set)
    assert out["f"] == frozenset({"[REDACTED]"}) and isinstance(out["f"], frozenset)
    assert count == 2
