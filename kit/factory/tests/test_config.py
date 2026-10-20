import json
import shutil
from pathlib import Path

from factorylib.config import decision_config, load_config

REAL = Path(__file__).resolve().parent.parent / "config.json"
NAMES = ["triage", "clarify", "assumptions", "failure", "rereview", "finding",
         "promote", "stuck", "escalation", "toolgate", "visual"]


def test_decision_config_defaults_to_shadow():
    assert decision_config({}, "triage") == {"mode": "shadow", "thresholds": {}}
    cfg = {"decisions": {"triage": {"mode": "live"}}}
    assert decision_config(cfg, "other") == {"mode": "shadow", "thresholds": {}}


def test_decision_config_present_entry():
    cfg = {"decisions": {"a": {"mode": "live"}, "b": {"mode": "shadow", "thresholds": {"x": 1}}}}
    assert decision_config(cfg, "a") == {"mode": "live", "thresholds": {}}
    assert decision_config(cfg, "b") == {"mode": "shadow", "thresholds": {"x": 1}}


def test_load_config_reads_factory_config(tmp_path):
    (tmp_path / "factory").mkdir()
    (tmp_path / "factory" / "config.json").write_text(json.dumps({"max_resumes": 7}))
    assert load_config(tmp_path) == {"max_resumes": 7}


def test_real_config_values(tmp_path):
    (tmp_path / "factory").mkdir()
    shutil.copy(REAL, tmp_path / "factory" / "config.json")
    cfg = load_config(tmp_path)
    assert cfg["decide"] == {
        "endpoint": "https://openrouter.ai/api/v1/systemone",
        "models": ["inception/mercury-decide:free", "inception/mercury-decide"],
        "timeout_s": 5,
        "state_char_cap": 200000,
        "api_key_env": "OPENROUTER_API_KEY",
    }
    assert cfg["fast_coder"] == {
        "model": "inception/mercury-3",
        "timeout_s": 1800,
        "harness": "claude-p",
        "price_per_mtok": {"input": 0.10, "output": 0.35, "cached_input": 0.01},
    }
    assert list(cfg["decisions"]) == NAMES
    assert all(v == {"mode": "shadow"} for v in cfg["decisions"].values())
    assert cfg["redact_patterns"] == []
    assert cfg["max_resumes"] == 2
    assert cfg["job_budget_usd"] == 5.0
