# Mercury Software Factory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the reference Claude Code software factory with Mercury Decide (System‑1 decisions and escalation) and Mercury 3 (fast coder). Spec, complex builds, reviews and approval stay on Opus/Sonnet.

**Architecture:** A Python package `factorylib` (run through `uv`) bridges Claude Code to OpenRouter. It has three entry points:
- `decide`: typed decisions with banding, shadow mode, logging and reports.
- `fast-coder`: Mercury 3 in a headless `claude -p` child.
- `tool-gate`: a PreToolUse hook.

The `/factory` skill (Opus orchestrator) calls these CLIs at fixed decision points. All Decide logic follows the rule that it may speed up or escalate but never override.

**Tech Stack:**
- Python ≥3.13 with `httpx`; `pytest` + `respx` for tests; `uv`.
- Claude Code 2.1.x skills, subagents and hooks.
- OpenRouter (`/api/v1/systemone`, Anthropic‑compatible `/api`).
- Vite + TypeScript + Vitest for the sample app.
- Vanilla HTML/JS dashboard.

**Spec:** `docs/superpowers/specs/2026-10-20-mercury-factory-design.md`. Reference kit: `zazencodes-season-3/src/software-factory-claude-code/` (abbreviated `REF/` below).

**One deviation from the spec layout:** the kit lives under `kit/` (`kit/.claude/`, `kit/factory/`) and is installed into target repos with `scripts/install.sh`. If it lived at this repo's root, its tool‑gate hook would gate our own development sessions here.

## Global Constraints

- **Core invariant.** Decide may only (a) choose a cheaper path when it is `confident`, or (b) add an escalation. It never converts an Opus/Sonnet `CHANGES`, `BLOCKED` or `ESCALATE` into a pass.
- **Modes and errors.** Each decision has `mode: "shadow" | "active"`, defaulting to **`shadow`**. In shadow mode the `apply` output is the decision's `fallback`, which is the original factory behaviour. An API error, timeout or missing key gives `band: "uncertain"`, `apply = fallback` and a non-null `error`.
- **Deterministic rules come first.** `policy.json` rules apply in **both** modes and cannot be overridden by Decide.
- **Model chain:** Decide uses `["inception/mercury-decide:free", "inception/mercury-decide"]`, falling through on 429/5xx/timeout. The fast coder uses `inception/mercury-3`. Timeout is `5` s per Decide call.
- **Thresholds:** booleans default to `0.85` (confident if `p ≥ 0.85` or `p ≤ 0.15`). Risk booleans default to `0.95`. Choice and score default to `0.75` on the max option/rung probability.
- **Data sent to Decide:**
  - Redact secrets before sending.
  - State cap is `200_000` characters (≈50K tokens): head and tail kept, with the marker `\n…[truncated N chars]…\n`.
  - Logs store `state_sha256`, never the state itself.
- **Logs:**
  - Per-job log: `factory/jobs/<id>/decisions.jsonl`.
  - Tool-gate log: `factory/tool-gate.jsonl`.
  - Each record is one `write()` of one line in append mode, so concurrent jobs are safe.
- **Escalation surface:** in the Claude Code session only. The orchestrator stops and tells the user, and the hook returns `ask`. A headless child turns `ask` into `deny`.
- **Activation target** for a decision: at least 20 cases with outcomes, at least 95% agreement, and 0 unsafe misses on risk decisions (D1 risks, D9, D10).
- **Root resolution.** The factory root is the **main checkout**: `Path(git rev-parse --path-format=absolute --git-common-dir).parent`. This works from inside a job worktree too.
- `factory/jobs/`, `factory/.venv/` and `factory/tool-gate.jsonl` are gitignored in target repos.

## Review Focus

1. **Decide answers partially or with unexpected shapes** (a question missing, an unknown choice key, a probabilities map that doesn't sum to 1). Those questions become `confident: false`. The command must not crash. The test belongs to Task 6.
2. **Compound shell commands**: `git status && git push`, `ls | sh`, `$(curl …)`, backticks or newlines. The command is never allowlisted by its prefix, and deny patterns are checked against every segment. The test belongs to Task 8.
3. **No `OPENROUTER_API_KEY`, or OpenRouter is down.** The factory still runs exactly as the reference factory. `decide` gives uncertain plus fallback, and the tool gate defers in the main session and denies headless. Tests belong to Tasks 6 and 10.
4. **Huge or binary diffs** (a lockfile rewrite or images in the diff) give a truncated state that is still valid JSON, and the escalation rule for lockfiles still fires. The test belongs to Task 9.
5. **Edit/Write paths that escape the worktree** (`../`, symlinks, absolute paths into the main checkout), and **edits to guardrails** (`.claude/**`, `factory/bin/**`, `factory/policy.json`). Escapes are denied; guardrail edits are `ask`. The test belongs to Task 8.

---

## File Structure

```
spikes/                                  # throwaway probes (Tasks 1–2); only findings are kept
docs/superpowers/spikes/2026-10-20-phase0.md   # spike findings, the source of truth for the API shape
scripts/install.sh                       # copy kit/ into a target repo + gitignore lines
scripts/make-sandbox.sh                  # create ../factory-sandbox from sample-app + install kit
sample-app/                              # Vite+TS template app with AGENTS.md
kit/.claude/agents/*.md                  # 7 reference agents (edited)
kit/.claude/skills/factory/SKILL.md      # orchestrator (rewritten)
kit/.claude/settings.json                # PreToolUse hook → factory/bin/tool-gate
kit/factory/pyproject.toml               # factorylib, deps: httpx; dev: pytest, respx
kit/factory/config.json  policy.json  backlog.md  dashboard.html
kit/factory/decisions/<name>.json        # 11 templates (questions + thresholds + fallback)
kit/factory/fast-agents/{scout,fast-builder,fast-fixer}.md
kit/factory/bin/{decide,fast-coder,tool-gate}   # 3-line bash wrappers → uv run python -m factorylib.<mod>
kit/factory/factorylib/
  paths.py      find_root, Job, load_job
  config.py     load_config, decision_config
  redact.py     redact_obj, cap_text
  client.py     post_decision, DecideError, RawResult
  banding.py    band_answers, Banded
  templates.py  load_template, expand_questions
  routes.py     ROUTES: dict[str, RouteFn]; one pure function per decision
  state.py      build_state(name, root, job, extra)
  policy.py     Policy, load_policy
  run.py        run_decision → DecisionResult dict, append_jsonl
  report.py     record_outcome, build_report
  cli.py        `decide` argument parsing
  toolgate.py   gate(event, …) + __main__
  fastcoder.py  fast-coder + __main__
kit/factory/tests/test_*.py              # one test file per module
```

---

### Task 1: Spike S1: probe the Mercury Decide API (throwaway)

**Files:**
- Create: `spikes/s1_decide_probe.py`
- Create: `docs/superpowers/spikes/2026-10-20-phase0.md`

**Prerequisites:**
- `OPENROUTER_API_KEY` is exported in the shell.
- Before the first live call, ask the user to confirm they are fine with about 50 free and 10 paid Decide calls. The paid calls cost well under $0.01.

**Interfaces:**
- Produces the "S1" section of the findings doc, with these fields:
  - exact endpoint URL;
  - exact request JSON for each type;
  - exact response JSON for each type (verbatim);
  - error shape;
  - image result (`accepted` | `rejected: <status/body>`);
  - p50/p95 latency over 10 calls;
  - `:free` 429 behaviour (status, headers, `Retry-After`).

  Tasks 5 and 6 code against this.

- [ ] **Step 1:** Write `spikes/s1_decide_probe.py` (`uv run --with httpx`). It POSTs to `https://openrouter.ai/api/v1/systemone` with `{"model": "inception/mercury-decide:free", "state": …, "questions": {…}}` and sends one `boolean`, one `choice` (`criteria` as an object) and one `score` question (`criteria` as an ordered list), modelled on the Vercel examples quoted in the spec research. On 404, also try `/api/v1/decisions` and `/api/v1/evaluate`, then print status and body.
- [ ] **Step 2:** Run `uv run --with httpx spikes/s1_decide_probe.py basic`. Expected: HTTP 200 and an `answers` object. Paste the request and response into the findings doc.
- [ ] **Step 3:** Run the `image` mode. It sends `state` as a message-style array containing an `image_url` content part: a 200×100 PNG with the text "ERROR 500". Record the result verbatim.
- [ ] **Step 4:** Run the `latency` mode (10 sequential calls with 3 questions each) and the `burst` mode (40 parallel calls on `:free`). Record p50/p95 and any 429 with headers.
- [ ] **Step 5:** Repeat `basic` once with `inception/mercury-decide` (paid) to confirm the fallback model works. Record the `usage` and cost fields.
- [ ] **Step 6:** Commit the findings doc only. `spikes/` is committed too, but labelled throwaway in its README line.

```bash
git add docs/superpowers/spikes/2026-10-20-phase0.md spikes/
git commit -m "spike: probe Mercury Decide API (S1)"
```

**Decision rule:** if the request or response shape differs from the spec's assumption, update the `client.py` notes in Task 6 before starting it. If images are `accepted`, Task 15 takes branch B.

### Task 2: Spike S2: Mercury 3 inside headless `claude -p` (throwaway)

**Files:**
- Create: `spikes/s2_mercury_claude.sh`
- Modify: `docs/superpowers/spikes/2026-10-20-phase0.md` (S2 section)

**Prerequisites:** the user confirms about 5 Mercury 3 runs, roughly $0.05–0.50 in total.

**Interfaces:**
- Produces the S2 section of the findings doc:
  - pass count out of 5;
  - the exact working flag set;
  - whether `--settings` hooks fire in the child;
  - whether `--session-id` + `--resume` works;
  - where `total_cost_usd` appears in the `--output-format json` output;
  - verdict: `claude-p` or `pi`.

- [ ] **Step 1:** Write `spikes/s2_mercury_claude.sh`:
  - `git init` a temp repo in `spikes/s2-repo/` with `add.js` (an `add(a,b)` that returns `a-b`) and `add.test.js` (a `node --test` test).
  - Run, with these env vars set on the child only:
    `env ANTHROPIC_BASE_URL=https://openrouter.ai/api ANTHROPIC_AUTH_TOKEN="$OPENROUTER_API_KEY" ANTHROPIC_API_KEY= ANTHROPIC_DEFAULT_HAIKU_MODEL=inception/mercury-3 CLAUDE_CODE_SUBAGENT_MODEL=inception/mercury-3`
  - The command is `timeout 600 claude -p --model inception/mercury-3 --session-id <uuid> --permission-mode dontAsk --allowedTools "Read,Grep,Glob,Edit,Write,Bash" --disallowedTools "Agent,WebFetch,WebSearch" --setting-sources local --settings '<json with a PreToolUse hook that appends stdin to hook.log>' --output-format json "Fix the bug in add.js, run node --test, commit with message 'fix add'."`
- [ ] **Step 2:** Run it 5 times on fresh repos. Count a run as a pass when `node --test` passes **and** `git log` shows the commit **and** `hook.log` is non-empty.
- [ ] **Step 3:** On the last passing run, resume it with `claude -p --resume <uuid> … "Now also add a test for negative numbers and commit."` and confirm it keeps context.
- [ ] **Step 4:** Record the results. Verdict `claude-p` requires at least 4/5 passes. Otherwise repeat Steps 1–2 with the pi coding agent (installed: `pi` 0.84.4): `pi -p --provider openrouter --model inception/mercury-3 --session-id <id> --tools read,bash,edit,write --no-skills --no-extensions --no-context-files --mode json "<task>"`, modelled on `zazencodes-season-3/src/pi-agent-fleet/AGENTS.md`. Also record whether a pi extension (`-e <file>`) can intercept and block tool calls, because the tool gate needs an equivalent of PreToolUse. Record that flag set instead.
- [ ] **Step 5:** Commit.

```bash
git add docs/superpowers/spikes/2026-10-20-phase0.md spikes/s2_mercury_claude.sh
git commit -m "spike: Mercury 3 in headless claude -p (S2)"
```

### Task 3: Kit scaffold, Python project and install script

**Files:**
- Create: `kit/` by copying `REF/.claude/agents/*.md`, `REF/.claude/skills/factory/SKILL.md`, `REF/factory/dashboard.html` and `REF/factory/backlog.md` (backlog emptied to its header and instruction line).
- Create: `kit/factory/pyproject.toml`, `kit/factory/factorylib/__init__.py`, `kit/factory/factorylib/paths.py`
- Create: `kit/factory/bin/{decide,fast-coder,tool-gate}`, each one `exec uv run --quiet --project "$(dirname "$(readlink -f "$0")")/.." python -m factorylib.<cli|fastcoder|toolgate> "$@"`, chmod +x.
- Create: `scripts/install.sh`, `README.md` (setup section: prerequisites `uv`, `node`, `agent-browser`, `OPENROUTER_API_KEY`; install command)
- Test: `kit/factory/tests/test_paths.py`

**Interfaces:**
- `find_root(cwd: Path | None = None) -> Path` returns the main checkout, even when `cwd` is a linked worktree.
- `@dataclass Job(id: str, folder: Path, worktree: Path, branch: str, feature: str, raw: dict)`
- `load_job(root: Path, job_id: str) -> Job` reads `factory/jobs/<id>/job.json`. It raises `FileNotFoundError` with the path.
- `scripts/install.sh <target-repo>` copies `kit/.claude` and `kit/factory` into the target. It refuses if `factory/` already exists unless `--force` is passed. It appends `/factory/jobs/`, `/factory/.venv/` and `/factory/tool-gate.jsonl` to the target's `.gitignore` when they are missing.

- [ ] **Step 1:** Write the failing tests:
  - `test_find_root_from_worktree`: in tmp, `git init` the main checkout, make a commit, `git worktree add ../wt -b b`. Then `find_root(wt) == main`.
  - `test_load_job_reads_fields`
  - `test_load_job_missing_raises`
- [ ] **Step 2:** Run `cd kit/factory && uv run pytest tests/test_paths.py -v`. Expected: FAIL (module missing).
- [ ] **Step 3:** Implement `paths.py`. The pyproject has `requires-python = ">=3.13"`, `dependencies = ["httpx>=0.27"]` and a dependency group `dev = ["pytest", "respx"]`.
- [ ] **Step 4:** Run the tests. Expected: PASS. Then run `scripts/install.sh /tmp/x` on a scratch repo. Expected: the files are copied, the gitignore lines are added, and a second run without `--force` exits 1.
- [ ] **Step 5:** Commit: `git add kit scripts README.md && git commit -m "feat: kit scaffold, factorylib paths, install script"`

### Task 4: Sample app and sandbox script

**Files:**
- Create: `sample-app/` (Vite vanilla‑TS)
  - Pages: Home, About, and a Footer component with a deliberate typo "Copyrihgt".
  - `src/lib/format.ts` with `formatDate(d: Date): string` and Vitest tests.
  - ESLint flat config; `package.json` scripts `dev`, `build`, `test`, `lint`.
- Create: `sample-app/AGENTS.md`. It covers fresh-checkout setup (`npm ci`), the dev server command `npm run dev -- --port <port> --strictPort`, validation commands (`npm test`, `npm run build`, `npx eslint <files>`) and conventions.
- Create: `scripts/make-sandbox.sh`
- Create: `sample-app/BACKLOG.seed.md` with the three e2e items:
  1. "Fix the typo in the footer copyright text"
  2. "Add a dark-mode toggle to the header that remembers the choice"
  3. "Add a login page that checks a password against a new bcrypt dependency"

**Interfaces:**
- `scripts/make-sandbox.sh [dir=../factory-sandbox]` does the following: copies `sample-app/` to `dir`, runs `git init -b main`, `npm install` (the first commit includes `package-lock.json`), `scripts/install.sh dir`, copies the seed backlog items into `factory/backlog.md` as `- [ ]` lines, and commits "Initial sandbox".

- [ ] **Step 1:** Scaffold the app. Run `cd sample-app && npm install && npm test && npm run build && npx eslint src`. Expected: all pass.
- [ ] **Step 2:** Run `scripts/make-sandbox.sh /tmp/sbx`, then `git -C /tmp/sbx log --oneline`. Expected: one commit, with `factory/` and `.claude/` present and `factory/jobs/` ignored.
- [ ] **Step 3:** Commit: `git add sample-app scripts/make-sandbox.sh && git commit -m "feat: sample app and sandbox script"`

### Task 5: Config and redaction

**Files:**
- Create: `kit/factory/config.json`, `kit/factory/factorylib/config.py`, `kit/factory/factorylib/redact.py`
- Test: `kit/factory/tests/test_config.py`, `kit/factory/tests/test_redact.py`

**Interfaces:**
- `config.json` keys:
  - `decide`: `{endpoint, models, timeout_s, state_char_cap, api_key_env}`
  - `fast_coder`: `{model, timeout_s, harness}`
  - `decisions`: `{<name>: {mode, thresholds?}}`
  - `redact_patterns`: list[str]
  - `max_resumes`: 2
  - `job_budget_usd`: 5.0

  `endpoint` and the request shape come from the S1 findings. All 11 decisions are listed with `mode: "shadow"`.
- `load_config(root: Path) -> dict`
- `decision_config(cfg: dict, name: str) -> dict` returns `{mode, thresholds}`. A missing name means shadow with no overrides.
- `redact_obj(obj: Any, extra_patterns: list[str]) -> tuple[Any, int]` walks the structure recursively and redacts string values only. Built-in patterns:
  - `.env` lines `^[A-Z][A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*=.*$` (multiline)
  - `sk-[A-Za-z0-9_-]{16,}`, `sk-or-v1-[a-f0-9]{32,}`, `ghp_[A-Za-z0-9]{36}`, `github_pat_[A-Za-z0-9_]{20,}`, `AKIA[0-9A-Z]{16}`
  - JWT `eyJ[\w-]+\.[\w-]+\.[\w-]+`
  - `-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----`

  Each match is replaced with `[REDACTED]`.
- `cap_text(s: str, max_chars: int) -> str` keeps the head and tail halves and puts the truncation marker from Global Constraints between them.

- [ ] **Step 1:** Write the failing tests:
  - `test_redacts_planted_secrets`: dict state with nested `sk-or-v1-<64 hex>`, `GITHUB_TOKEN=abc`, a PEM block and a JWT. Assert none remain and `count == 4`.
  - `test_redact_leaves_code_alone`: `"const tokenCount = 3"` is unchanged.
  - `test_cap_text_marker`: `len(cap_text("a"*300, 100)) <= 100 + len(marker)` and it contains `[truncated 200 chars]`.
  - `test_decision_config_defaults_to_shadow`
- [ ] **Step 2:** Run `uv run pytest tests/test_config.py tests/test_redact.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit: `git commit -m "feat: factory config and redaction"`

### Task 6: Decide transport and banding

**Files:**
- Create: `kit/factory/factorylib/client.py`, `kit/factory/factorylib/banding.py`
- Test: `kit/factory/tests/test_client.py`, `kit/factory/tests/test_banding.py`

**Interfaces:**
- `@dataclass RawResult(model: str, answers: dict, usage: dict, latency_ms: int)`
- `class DecideError(Exception)`
- `post_decision(state, questions: dict, *, cfg: dict, api_key: str | None, http: httpx.Client | None = None) -> RawResult` tries `cfg["decide"]["models"]` in order and moves to the next model on HTTP 429, 5xx, timeout or connect error. It raises `DecideError` on a 4xx other than 429, on a missing key (`"OPENROUTER_API_KEY not set"`), or when every model fails (the message lists each attempt).
- `@dataclass Banded(type: str, value: bool | str | float | None, p: float, confident: bool)`
- `band_answers(questions: dict, answers: dict, thresholds: dict[str, float]) -> dict[str, Banded]` follows these rules:
  - **boolean:** `value = p >= 0.5`; `confident = p >= t or p <= 1 - t`.
  - **choice:** `value` is the argmax key; `p` is the max probability; `confident = p >= t`.
  - **score:** `value` is the returned `score`; `p` is the max rung probability; `confident = p >= t`.
  - A question that is missing, malformed, or answered with an unknown key gives `Banded(type, None, 0.0, False)`.
  - Default `t` is taken from Global Constraints. A question with `"risk": true` in its template gets `0.95`.

- [ ] **Step 1:** Write the failing tests, using respx and the response JSON copied from the S1 findings:
  - `test_free_then_paid_on_429`: the first route returns 429 and the second 200. `result.model == "inception/mercury-decide"`.
  - `test_timeout_falls_through`
  - `test_all_fail_raises`
  - `test_missing_key_raises_without_http`
  - `test_400_does_not_fall_through`
  - banding:
    - `test_boolean_bands`: `0.9` is confident True; `0.5` is not confident; `0.1` is confident False.
    - `test_risk_boolean_needs_095`: `0.9` is not confident.
    - `test_choice_argmax`
    - `test_score_uses_max_rung`
    - `test_partial_answers_are_uncertain`: one question absent and one with an unknown choice key. Both give `confident=False`, with no exception (Review Focus 1).
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Implement. Send `Authorization: Bearer <key>`, `HTTP-Referer` and `X-Title: mercury-factory` headers.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit: `git commit -m "feat: decide transport with free→paid fallback and answer banding"`

### Task 7: Templates and routes for all 11 decisions

**Files:**
- Create: `kit/factory/decisions/{triage,clarify,assumptions,failure,rereview,finding,promote,stuck,escalation,toolgate,visual}.json`
- Create: `kit/factory/factorylib/templates.py`, `kit/factory/factorylib/routes.py`
- Test: `kit/factory/tests/test_templates.py`, `kit/factory/tests/test_routes.py`

**Interfaces:**
- Template JSON:
  ```json
  {"id": "D1", "name": "triage",
   "questions": {"<q>": {"type": "...", "instructions": "...", "criteria": ..., "risk": false}},
   "per_item": null,
   "fallback": {...}}
  ```
  `per_item` has the form `{"state_key": "findings", "prefix": "f", "question": {...}}` and expands into `f0..fN`.
- `load_template(root: Path, name: str) -> dict`
- `expand_questions(tpl: dict, state: dict) -> dict`
- `RouteFn = Callable[[dict[str, Banded], dict], dict]` (banded answers and the state go in, the route comes out). `ROUTES: dict[str, RouteFn]`.
- Reviewer names: `["security", "ux", "ui", "code"]`. Risk names: `["auth", "payments", "dependencies", "ci_deploy", "schema_migration", "email", "prod_data", "build_scripts"]`. All are `"risk": true` booleans.

| name (id) | questions | route returns | fallback |
|---|---|---|---|
| triage (D1) | choice `kind` {docs, ui, bugfix, feature, refactor, infra}; score `complexity` [trivial, small, medium, large]; boolean `ui_change`; 8 risk booleans | `{"tier": "fast" if complexity confident and value ≤ 1.0 and every risk confident False else "heavy", "reviewers": ["security","code"] + (["ux","ui"] if ui_change is True or not confident), "risks": [risks with value True or not confident]}` | `{"tier":"heavy","reviewers":all 4,"risks":[]}` |
| clarify (D2) | boolean `needs_human_clarification` | `{"ask_human": value is True or not confident}` | `{"ask_human": false}` |
| assumptions (D3) | per_item `assumptions` → boolean `is_product_decision` | `{"ask_human": [i for items True or not confident]}` | `{"ask_human": []}` |
| failure (D4) | choice `cause` {flaky, env, code, spec_conflict} | `{"action": {"flaky":"retry","env":"blocked","spec_conflict":"blocked","code":"fix"}[value] if confident else "fix"}` | `{"action":"fix"}` |
| rereview (D5) | per_item `reviewers` (state items `{name, last_verdict, delta_stat}`) → boolean `area_touched` | `{"reviewers": [r where last_verdict != "PASS" or touched True or not confident]}` (a reviewer that never ran has `last_verdict: null`) | `{"reviewers": all 4}` |
| finding (D6) | per_item `findings` → boolean `mechanical` | `{"fast_fixer": [i confident True], "builder": [rest]}` | `{"fast_fixer": [], "builder": all}` |
| promote (D7) | boolean `beyond_fast_tier` | `{"promote": not (confident and value is False)}` | `{"promote": true}` |
| stuck (D8) | score `progress` [none, little, clear] | `{"escalate": (not confident) or value < 1.0}` | `{"escalate": false}` |
| escalation (D9) | 8 risk booleans | `{"escalate": bool(state["rule_hits"]) or any risk True or not confident, "reasons": rule_hits + flagged risks}` | `{"escalate": false, "reasons": []}` |
| toolgate (D10) | choice `class` {safe, needs_human, destructive} | `{"permission": {"safe":"allow","needs_human":"ask","destructive":"deny"}[value] if confident else "ask"}` | `{"permission": "defer"}` |
| visual (D11) | per_item `criteria` → boolean `met` | `{"met": [...], "unmet": [...], "unsure": [...]}` | `{}` |

- [ ] **Step 1:** Write the failing tests:
  - `test_all_templates_load_and_have_route`: every file in `decisions/` has a `ROUTES` entry and a `fallback`.
  - `test_expand_per_item`
  - One table-driven test per row above, covering confident, uncertain and edge values. Required cases:
    - `test_triage_uncertain_risk_forces_heavy`
    - `test_triage_trivial_no_ui_gets_security_code_only`
    - `test_escalation_rule_hits_escalate_even_if_decide_confident_no_risk`
    - `test_rereview_never_run_reviewer_included_when_uncertain`
    - `test_finding_uncertain_goes_to_builder`
    - `test_toolgate_uncertain_is_ask`
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Write the 11 templates. Each `instructions` is a single plain question, and each `criteria` entry describes when that option applies. Implement `routes.py` as pure functions.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit: `git commit -m "feat: decision templates and routes D1–D11"`

### Task 8: Deterministic policy

**Files:**
- Create: `kit/factory/policy.json`, `kit/factory/factorylib/policy.py`
- Test: `kit/factory/tests/test_policy.py`

**Interfaces:**
- `policy.json` keys:
  - `escalate_paths` (globs): `package.json`, `**/package.json`, `*lock*`, `**/*.lock`, `.github/**`, `**/migrations/**`, `**/auth/**`, `firestore.rules`, `storage.rules`, `**/*.tf`, `Dockerfile`, `vercel.json`, `netlify.toml`, `scripts/**`
  - `tool_allow` (full-match regexes): `git (status|diff|log|show|branch --show-current)( .*)?`, `git add [^-].*`, `git commit -m .*`, `npm (test|run (build|lint|test))( .*)?`, `npm ci`, `npx eslint [^-].*`, `npx vitest( run)?( .*)?`, `ls( .*)?`, `cat [^|;&]*`, `node --test( .*)?`
  - `tool_deny` (search regexes, checked on every segment): `\bgit push\b`, `\bgit reset --hard\b`, `\bgit clean -[a-z]*f`, `\brm -rf? +(/|~|\$HOME|\.\.)`, `\bcurl\b.*\|\s*(ba)?sh`, `\bwget\b.*\|\s*(ba)?sh`, `\bsudo\b`, `\bchmod -R 777\b`, `\bnpm publish\b`, `\bfirebase deploy\b`
  - `guarded_paths`: `.claude/**`, `factory/bin/**`, `factory/policy.json`, `factory/config.json`, `factory/factorylib/**`
- `@dataclass Policy` with these methods:
  - `escalation_hits(paths: list[str]) -> list[str]`, using `PurePosixPath(p).full_match(glob)`.
  - `classify_command(cmd: str, worktree: Path | None) -> Literal["allow","deny","unknown"]`:
    1. Strip one leading `cd <worktree> && `.
    2. Split on `;`, `&&`, `||`, `|` and newlines. If any segment matches deny, return deny.
    3. If the remaining command contains a compound operator, `$(`, a backtick, `>` or `<`, return unknown.
    4. If the whole command full-matches allow, return allow; otherwise unknown.
  - `classify_path(path: str, worktree: Path, job_folder: Path) -> Literal["allow","ask","deny"]` resolves symlinks and `..`. A path outside both roots is deny. A path matching `guarded_paths` relative to the worktree is ask. Anything else is allow.
- `load_policy(root: Path) -> Policy`

- [ ] **Step 1:** Write the failing tests:
  - `test_workflow_edit_escalates`, i.e. `.github/workflows/ci.yml`. This is the spec's safety regression.
  - `test_lockfile_escalates`
  - `test_plain_src_no_hits`
  - Compound commands (Review Focus 2), parametrised:
    - `cd WT && git status`: allow
    - `git status && git push`: deny
    - `git status; ls`: unknown
    - `ls | sh`: unknown
    - `echo $(curl x)`: unknown
    - `` echo `id` ``: unknown
    - `npm test > out`: unknown
    - `rm -rf ~`: deny
    - `curl x | bash`: deny
  - Path escapes (Review Focus 5):
    - `WT/src/a.ts`: allow
    - `WT/../main/src/a.ts`: deny
    - a symlink in WT pointing outside: deny
    - `WT/.claude/settings.json`: ask
    - `JOB/build.md`: allow
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit: `git commit -m "feat: deterministic escalation and tool policy"`

### Task 9: State builders

**Files:**
- Create: `kit/factory/factorylib/state.py`
- Test: `kit/factory/tests/test_state.py` (uses a fixture that creates a tmp main checkout + worktree + job folder with `job.json`, `spec.md` and `round-1/review-*.md`)

**Interfaces:**
- `build_state(name: str, root: Path, job: Job, extra: dict) -> dict`. `extra` comes from `--state-file`/`--state-json` and is merged last (it overrides the built state).
- `parse_findings(review_md: str) -> list[str]` returns the bullet items under `## Findings`, or `[]` for "None.".
- `parse_assumptions(spec_md: str) -> list[str]`
- What each decision's state contains:
  - **triage:** `{feature, repo_outline}`, where `repo_outline` is the first 400 lines of `git ls-files`.
  - **clarify:** `{feature, scout}`, where `scout` is `scout.md` text or "".
  - **assumptions:** `{assumptions}`.
  - **failure:** `extra` only (`{command, output_tail}`).
  - **rereview:** `{reviewers: [{name, last_verdict, delta_stat}]}`. `last_verdict` comes from the latest round file of each reviewer. `delta_stat` is `git diff --stat <job.raw["review_commits"][name]>..HEAD`, or `main...HEAD` if the reviewer never ran.
  - **finding:** `{findings: [{reviewer, text}]}` from the CHANGES reviews in `round-<extra.round>`.
  - **promote:** `{round, verdicts, findings}`.
  - **stuck:** `{previous: findings round N-1, current: findings round N}`.
  - **escalation:** `{spec, diff_stat, changed_paths, diff, rule_hits}`. `diff` is `git diff main...HEAD` (binary files show as `Binary files … differ`), and `rule_hits` is `Policy.escalation_hits(changed_paths)` from Task 8.
  - **visual:** `{criteria, observations}` from `spec.md` acceptance criteria and `round-N/observations.json`.
  - Every text field goes through `cap_text` with the configured cap, split evenly across the large fields.

- [ ] **Step 1:** Write the failing tests:
  - `test_parse_findings_none`
  - `test_parse_findings_bullets`
  - `test_rereview_never_run_uses_full_branch_diff`
  - `test_escalation_includes_rule_hits_for_lockfile`
  - `test_huge_binary_diff_is_capped_and_json_serialisable` (Review Focus 4): a 5 MB `package-lock.json` change and a PNG. `json.dumps` succeeds, the size is at most the cap, and `"package-lock.json" in rule_hits`.
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Implement. Run git through `subprocess.run([...], cwd=job.worktree, capture_output=True, text=True, errors="replace")`.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit: `git commit -m "feat: decision state builders"`

### Task 10: `run_decision` and the `decide` CLI (run and outcome)

**Files:**
- Create: `kit/factory/factorylib/run.py`, `kit/factory/factorylib/cli.py`
- Test: `kit/factory/tests/test_run.py`, `kit/factory/tests/test_cli.py`

**Interfaces:**
- `append_jsonl(path: Path, record: dict) -> None` writes with `os.open(O_WRONLY|O_APPEND|O_CREAT)` and a single `os.write`.
- `run_decision(name: str, *, root: Path, job: Job | None, state: dict, images: list[Path] = [], post=post_decision) -> dict` returns `{kind:"decision", decision, id, job, mode, band, answers:{q:{type,value,p,confident}}, route, apply, model, latency_ms, usage, error, redactions, state_sha256, ts}`. The steps:
  1. Expand questions.
  2. Run `redact_obj`, then cap.
  3. Call `post`. On `DecideError`, use `answers={}` and set `error`.
  4. Run banding and the route.
  5. `apply = route if mode == "active" and error is None else fallback`. **Exception:** for `escalation`, after choosing `apply`, force `apply.escalate = True` and prepend `rule_hits` to `apply.reasons` when `rule_hits` is non-empty. Rules work in every mode.
  6. Append the record to `jobs/<id>/decisions.jsonl`, or to `factory/tool-gate.jsonl` when `job` is None.
- `images`: if non-empty and the S1 findings recorded `rejected`, raise `SystemExit("images unsupported by mercury-decide; use reviewer-ui observations")`. If `accepted`, pass them in the format S1 found.
- CLI:
  - `decide <name> --job <id> [--state-file F] [--state-json S] [--image P]...` prints the full record as JSON on stdout and exits 0, even when decide itself errored, because the error is carried in the record.
  - `decide outcome --job <id> --decision <name> --truth '<json>' [--note TEXT]` appends `{kind:"outcome", decision, truth, note, ts}`.
  - Both CLI commands exit 2 on unknown names or a bad job id, with a message on stderr.

- [ ] **Step 1:** Write the failing tests, with `post` stubbed:
  - `test_shadow_applies_fallback_but_logs_route`
  - `test_active_confident_applies_route`
  - `test_error_gives_uncertain_and_fallback` (Review Focus 3: missing key)
  - `test_escalation_rules_force_escalate_in_shadow`
  - `test_log_has_hash_not_state`: a planted secret never appears in the jsonl.
  - `test_concurrent_appends_are_whole_lines`: 8 processes × 200 records, and every line parses.
  - `test_cli_outcome_appends`
  - `test_cli_unknown_decision_exit_2`
  - `test_images_rejected_message` (only if S1 said rejected)
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS. Then do a live smoke test against the sandbox with the key set: `factory/bin/decide triage --job smoke --state-json '{"feature":"fix typo in footer"}'`, using a stub `factory/jobs/smoke/job.json`. Expected: `route.tier == "fast"`. If the band is uncertain, note the probabilities in the findings doc; don't tune thresholds yet.
- [ ] **Step 5:** Commit: `git commit -m "feat: decide CLI with shadow mode, rules override and outcome logging"`

### Task 11: Tool‑gate hook

**Files:**
- Create: `kit/factory/factorylib/toolgate.py`, `kit/.claude/settings.json`
- Test: `kit/factory/tests/test_toolgate.py`

**Interfaces:**
- `gate(event: dict, *, root: Path, policy: Policy, headless: bool, decide=run_decision) -> dict | None`. `None` means print nothing and exit 0, so Claude Code's normal permission flow runs.
- **Scope:** the gate is active only when `headless` is true (env `FACTORY_HEADLESS=1`), or when the Bash command starts with `cd <path> &&` or the Edit/Write `file_path` lies under `<root>/../<root.name>-factory/`. Otherwise it returns `None`, so the user's own sessions are unaffected.
- **Order:**
  1. Policy deny gives deny.
  2. Path or command ask gives ask.
  3. Allow gives allow.
  4. Unknown → D10: `run_decision("toolgate", job=None, …)` and its `apply.permission`.
  5. `defer` gives `None`.
  6. Any exception gives ask.
- **Output:** `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":X,"permissionDecisionReason":"factory tool-gate: <rule|D10 p=…|error>"}}`.
- **Headless mode:** `ask` becomes `deny` with the reason prefixed `"needs human: "`. The fast-agent prompts tell the agent to write BLOCKED on that prefix.
- **`__main__`** reads the event JSON on stdin and finds the job worktree from the `cd` prefix or the file path. The job folder is `root/factory/jobs/<worktree.name>`.
- **`kit/.claude/settings.json`:** `{"hooks":{"PreToolUse":[{"matcher":"Bash|Edit|Write","hooks":[{"type":"command","command":"\"$CLAUDE_PROJECT_DIR\"/factory/bin/tool-gate","timeout":10}]}]}}`

- [ ] **Step 1:** Write the failing tests:
  - `test_user_session_outside_factory_defers`
  - `test_deny_git_push_in_worktree`
  - `test_allow_git_status`
  - `test_unknown_calls_d10_active_allow`
  - `test_unknown_shadow_defers`
  - `test_guarded_path_ask`
  - `test_headless_ask_becomes_deny`
  - `test_decide_error_main_session_asks_headless_denies` (Review Focus 3)
  - `test_exception_fails_closed_to_ask`
  - `test_main_reads_stdin_prints_json` (subprocess)
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS. In the sandbox, start an interactive `claude` session and ask it to run `cd ../factory-sandbox-factory/x && git push`. Expected: blocked with the factory tool-gate reason.
- [ ] **Step 5:** Commit: `git commit -m "feat: PreToolUse tool gate with policy, D10 and headless deny"`

### Task 12: fast-coder and fast-agent prompts

**Files:**
- Create: `kit/factory/factorylib/fastcoder.py`, `kit/factory/fast-agents/{scout,fast-builder,fast-fixer}.md`
- Test: `kit/factory/tests/test_fastcoder.py` (uses a stub `claude` executable on `PATH` that writes its argv and env to a file and prints `{"session_id":…,"total_cost_usd":0.01,"result":"ok"}`, or whatever shape S2 recorded)

**Interfaces:**
- CLI: `fast-coder <scout|fast-builder|fast-fixer> --job <id> [--round N] [--port P] [--message TEXT] [--resume SESSION_ID]` prints `{"role", "session_id", "exit_code", "cost_usd", "transcript"}` and exits with the child's exit code.
- **Child invocation:** use the exact S2 flag set.
  - Env `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY=""`, `ANTHROPIC_DEFAULT_HAIKU_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL` and `FACTORY_HEADLESS=1` are set **only in the child env dict**; `os.environ` is untouched.
  - `cwd` is the job worktree.
  - `--append-system-prompt` takes the role file text.
  - `--allowedTools` is `Read,Grep,Glob` for scout and `Read,Grep,Glob,Edit,Write,Bash` otherwise.
  - `--settings` takes inline JSON registering `<root>/factory/bin/tool-gate` as the PreToolUse hook.
  - New sessions get `--session-id <uuid4>`; resumed sessions get `--resume <id>`.
  - The child runs under `timeout cfg.fast_coder.timeout_s` (default 1800).
- **First message:** `Job folder: <root>/factory/jobs/<id>/. Worktree: <wt>. Branch: <branch>.` followed by the role default (`Scout for this feature: <feature>`, `Build round <N>. Port: <P>.` or `Fix the findings listed in <job>/round-<N>/fast-fix.md.`) or `--message`.
- **Transcript** is written to `jobs/<id>/fast-<role>-round-<N>.json`.
- If the harness in config is `pi`, build the pi command from S2 instead, loading the tool-gate pi extension S2 recorded. The output contract stays the same.
- **Prompts:**
  - `scout.md` writes `scout.md`: relevant files with a one-line reason each, existing patterns to reuse, the test command, and open questions. It never edits.
  - `fast-builder.md` is `REF/.claude/agents/builder.md` trimmed. It keeps "Where you work", "Build", "Commit" and the `build.md` contract. It adds: "If a tool call is denied with reason starting `needs human:`, stop and write `STATUS: BLOCKED` quoting it", and "When a command fails and the cause is unclear, run `factory/bin/decide failure --job <id> --state-json '{"command":…,"output_tail":…}'` and follow `apply.action`."
  - `fast-fixer.md` fixes only the findings listed in `round-N/fast-fix.md`, commits, and appends a `### Round N (fast-fixer)` Log entry to `build.md`.

- [ ] **Step 1:** Write the failing tests:
  - `test_child_env_isolated`: the parent `os.environ` has no `ANTHROPIC_BASE_URL` afterwards, and the stub saw it plus `FACTORY_HEADLESS=1`.
  - `test_scout_tools_readonly`
  - `test_new_session_uses_session_id_resume_uses_resume`
  - `test_settings_hook_points_to_main_root_toolgate`
  - `test_output_json_and_transcript_written`
  - `test_missing_key_exits_2_without_spawning`
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Implement and write the three prompts.
- [ ] **Step 4:** Run the tests. Expected: PASS. Live check in the sandbox: create job folder `factory/jobs/000-scout-test/job.json` and its worktree by hand, run `factory/bin/fast-coder scout --job 000-scout-test`, and confirm `scout.md` exists. Remove the worktree afterwards.
- [ ] **Step 5:** Commit: `git commit -m "feat: fast-coder (Mercury 3 headless) and fast-agent prompts"`

### Task 13: Decision report

**Files:**
- Create: `kit/factory/factorylib/report.py`. Modify `cli.py` to add `decide report [--min-cases 20] [--json]`.
- Test: `kit/factory/tests/test_report.py`

**Interfaces:**
- `build_report(root: Path, min_cases: int = 20) -> dict[str, dict]` reads every `jobs/*/decisions.jsonl` and pairs each `outcome` with the latest `decision` record of the same name in that job. Per decision it computes:
  - `cases`;
  - `coverage` (the share of decisions with `band=="confident"`);
  - `agreement` (the share of paired outcomes where every key in `truth` equals the same key in `route`);
  - `unsafe_misses`: for `triage`, `escalation` and `toolgate`, cases where truth escalates (`risks` non-empty, `escalate: true`, or `permission` in `{ask, deny}`) and the route doesn't;
  - `ready = cases >= min_cases and agreement >= 0.95 and unsafe_misses == 0`.
- Text output is one table row per decision. With `--json`, the dict is printed.

- [ ] **Step 1:** Write the failing tests:
  - `test_agreement_and_coverage`
  - `test_unsafe_miss_blocks_ready`
  - `test_ready_at_threshold`
  - `test_outcome_without_decision_ignored`
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit: `git commit -m "feat: decide report for shadow-mode calibration"`

### Task 14: Orchestrator skill and agent edits

**Files:**
- Modify: `kit/.claude/skills/factory/SKILL.md` (rewrite, keeping the reference's structure, commands, job-id claiming, worktree, merge and reporting rules)
- Modify: `kit/.claude/agents/approver.md`, `kit/.claude/agents/builder.md`, `kit/.claude/agents/reviewer-ui.md` (vision part in Task 15)
- Test: `kit/factory/tests/test_skill_contract.py`

**Interfaces:**
- `job.json` keeps the reference keys and adds:
  - `tier`: `"fast"|"heavy"|"promoted"`
  - `fast_session_id`: str|null
  - `reviewers_active`: [names]
  - `review_commits`: `{name: sha}`, set to HEAD at review time
  - `reviews` values may also be `"skipped"`
  - `escalations`: `[{source: "rule"|"decide"|"approver"|"tool-gate"|"stuck"|"rounds"|"budget", reason}]`
  - `cost`: `{opus_sonnet: "n/a", mercury3_usd: float, decide_usd: float}`
- **Helper commands the skill uses:** `factory/bin/decide <name> --job <id> […]`, and it reads `.apply` from the stdout JSON. Also `factory/bin/decide outcome …` and `factory/bin/fast-coder …`. These must stay consistent with Tasks 10 and 12.

The SKILL.md sections to write, in order. Each step logs one report line in the reference's style.

1. **Claim + worktree.** As in the reference. Add `tier: null`, `escalations: []` and `review_commits: {}`.
2. **Triage (D1).** Run `decide triage`. Set `tier` and `reviewers_active` from `apply`. Record `apply.risks` for the approver.
3. **Scout.** Run `fast-coder scout`. If it fails, continue without `scout.md`.
4. **Clarify (D2).** If `apply.ask_human` is set, stop and ask the user the clarification in the session. Write the answer verbatim to `clarification.md`, then run `decide outcome --decision clarify --truth '{"ask_human":true}'`. Otherwise continue.
5. **Spec** (spec-writer, which also reads `scout.md` and `clarification.md` when present). Then **Assumptions (D3):** for every index in `apply.ask_human`, ask the user to confirm or override, append the answers to `clarification.md`, and resume the spec-writer if anything was overridden.
6. **Build by tier.**
   - fast: `fast-coder fast-builder --round 1 --port <port>`. Store `session_id` in `fast_session_id`.
   - heavy: the reference builder subagent.
   - Then the build check, as in the reference.
7. **Review round N.**
   - Round 1 reviewers = `reviewers_active`.
   - Rounds after 1: reviewers = `decide rereview --job <id>`, `apply.reviewers`.
   - Spawn the selected reviewers in one message. WAIT FOR ALL over the selected set. Unselected reviewers are `skipped`. Set `review_commits[r] = HEAD` for each one that ran.
8. **On any CHANGES.**
   - If N ≥ 2: run `decide stuck --state-json '{"round":N}'`; if `apply.escalate`, go to needs-human with source `stuck`.
   - Run `decide finding --state-json '{"round":N}'`. Write `round-N/fast-fix.md` with the `fast_fixer` findings.
   - fast tier: run `decide promote`. If `apply.promote`, set tier `promoted` and spawn a fresh Opus `builder-<id>` with "Promoted from the fast tier: read build.md, every round folder and fast-*.json summaries". Otherwise run `fast-coder fast-builder --resume <fast_session_id> --round N+1`.
   - heavy/promoted tier: run `fast-coder fast-fixer --round N` when the fast_fixer list is non-empty, **then** SendMessage `builder-<id>` with only the `builder` findings (skipped when empty).
   - The `MAX_RESUMES` cap counts every fix round, whoever does it.
   - **Budget:** after every `fast-coder` and `decide` call, add the reported cost to `job.json.cost`. If `mercury3_usd + decide_usd > config.job_budget_usd`, go to needs-human with source `budget`. Opus/Sonnet cost is not observable from inside the session and is left out.
9. **Approval gate.** Approval requires every reviewer's latest verdict PASS **and** `decide rereview` returning an empty `apply.reviewers`; otherwise run another round. Then `decide escalation`. If `apply.escalate`, append the reasons to `escalations` (source `rule` or `decide`). Spawn the approver with `Final round: N. Decide escalation: <apply json>.`
10. **Decision.** Needs-human if the approver says ESCALATE **or** `apply.escalate` is set; otherwise Merge. Do not run Merge on APPROVE if `escalations` is non-empty.
11. **Outcomes** (always written; these feed `decide report`):
    - After the approver: `decide outcome --decision escalation --truth '{"escalate": <approver ESCALATE or human later reworks>}'` and the same for `triage` with `{"risks": <approver-named risk categories>}`.
    - After merge or rework: `decide outcome --decision stuck --truth '{"escalate": <job exhausted rounds>}'`.
    - After each fast-fixer round: `decide outcome --decision finding --truth` with the next-round verdict per finding (whether it was re-flagged).
12. **`/factory approve` and `/factory rework`:** as in the reference, plus an outcome for `escalation` with the human's verdict, which overrides the earlier one because the report pairs the latest.

Agent edits:
- **`approver.md`:** read `decisions.jsonl`. Escalate when the prompt's Decide escalation has `escalate: true` and name its reasons under "For the human". Never APPROVE solely because Decide said no risk.
- **`builder.md`:** a "When promoted" paragraph covering what to read and the warning that the fast tier's commits stay on the branch and may be reverted. Plus the same `decide failure` instruction as `fast-builder.md`.

- [ ] **Step 1:** Write the failing test `test_skill_contract`:
  - Every decision name in `decisions/` appears in SKILL.md as `decide <name>`.
  - Every `fast-coder` role exists in `fast-agents/`.
  - SKILL.md contains `WAIT FOR ALL`, `skipped` and `decide outcome`.
  - `approver.md` contains `Decide escalation`.
- [ ] **Step 2:** Run it. Expected: FAIL.
- [ ] **Step 3:** Write SKILL.md and edit the agents.
- [ ] **Step 4:** Run it. Expected: PASS.
- [ ] **Step 5:** Commit: `git commit -m "feat: orchestrator with System-1 decision points and escalation gate"`

### Task 15: Visual pipeline (reviewer-ui observations and D11)

**Files:**
- Modify: `kit/.claude/agents/reviewer-ui.md`, `kit/.claude/agents/spec-writer.md`
- Test: `kit/factory/tests/test_state.py::test_visual_state_reads_observations` (add to the existing file)

**Branch A (S1 says images rejected):**
- **reviewer-ui:**
  - After taking screenshots at desktop and phone widths (paths `round-N/ui-<width>.png`), compare them with any mockup files spec.md references.
  - Write `round-N/observations.json` = `[{"criterion": <spec visual criterion text>, "observation": <what is seen, factual>, "screenshot": <path>}]`.
  - Then run `factory/bin/decide visual --job <id> --state-json '{"round":N}'` and list `apply.unmet` and `apply.unsure` under Notes as evidence. The verdict stays the reviewer's own.
- **spec-writer:** mockups or images attached to a feature are saved to `<job>/attachments/`, and `## Acceptance criteria` names them per visual criterion.
- **SKILL.md (one line in step 1):** when the user pastes screenshots or diagrams with a feature, the orchestrator transcribes them into `attachments.md` and saves the files to `attachments/`.

**Branch B (images accepted):** same as branch A, and reviewer-ui also passes `--image` for each screenshot and mockup pair. If `band` is uncertain, the observations are the fallback.

- [ ] **Step 1:** Write the failing test `test_visual_state_reads_observations`: criteria and observations are paired by criterion text.
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Implement the `visual` state field and edit the prompts.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit: `git commit -m "feat: reviewer-ui observations and D11 visual decision"`

### Task 16: Dashboard

**Files:**
- Modify: `kit/factory/dashboard.html`

**Interfaces:**
- Reads `job.json` keys from Task 14 and `jobs/<id>/decisions.jsonl`, fetched alongside `job.json` on each poll. A 404 counts as an empty list.

**Changes:**
- **STAGES labels:** spec-writer · opus; builder · opus / mercury-3; reviewers; approver · opus. Add a "triage · decide" column before spec.
- **Card:**
  - tier badge (`fast` teal, `heavy` grey, `promoted` amber);
  - reviewer chips with a `skipped` state (dashed outline);
  - a decision chip row of `D1 ✓0.93` (confident) or `D9 ?0.61` (uncertain); a shadow-mode chip is hollow; hovering shows `route` vs `apply`;
  - on needs-human cards, the escalation reasons in the card body;
  - a cost line `mercury $0.04 · decide $0.0001`.
- **Drawer:** a "Decisions" tab rendering decisions.jsonl as a table (time, decision, band, top answers, apply, mode).

- [ ] **Step 1:** In the sandbox, write fixture jobs `factory/jobs/9xx-*` that cover each state (fast merged, heavy with skipped reviewers, needs-human with escalations, promoted).
- [ ] **Step 2:** Implement. Serve with `cd factory && python3 -m http.server 8765` and open `http://localhost:8765/dashboard.html` in the browser pane. Check every badge and chip state at desktop and phone width (375px), and check the console for errors.
- [ ] **Step 3:** Delete the fixtures. Commit: `git commit -m "feat: dashboard tiers, decision chips, escalations, cost"`

### Task 17: End‑to‑end verification on the sandbox

**Files:**
- Modify: `README.md` (usage, shadow→active workflow, `decide report`, troubleshooting)
- Modify: `docs/superpowers/spikes/2026-10-20-phase0.md` (append an E2E results section)

**Prerequisites:**
- The user confirms a full e2e run, which uses Opus/Sonnet subagents, Mercury 3 and Decide.
- `agent-browser` is installed; ask the user before installing it with `npm i -g agent-browser`.

- [ ] **Step 1:** Run `scripts/make-sandbox.sh ../factory-sandbox`. Set `decisions.*.mode = "active"` in the sandbox copy only, so the routing paths are exercised. Open Claude Code in the sandbox.
- [ ] **Step 2:** Run `/factory next` (typo fix). Expected:
  - D1 gives tier fast, with reviewers security + code (ux/ui skipped unless the area is touched).
  - fast-builder (Mercury 3) produces READY. The reviews PASS, D9 doesn't escalate, the approver APPROVEs, and the branch merges.
  - `decisions.jsonl` has D1, D2, D3, D5 and D9 records.
- [ ] **Step 3:** Run `/factory next` (dark mode). Expected:
  - Tier heavy or fast; either is acceptable, but record which.
  - All four reviewers in round 1. Any mechanical findings go to fast-fixer. It merges or ends needs-human with a clear reason.
- [ ] **Step 4:** Run `/factory next` (login + bcrypt). Expected:
  - Rule hit `package.json` and lockfile, plus D9 `dependencies`/`auth`, give needs-human. The session shows the reasons.
  - `/factory approve <id>` merges, and an `escalation` outcome is recorded.
- [ ] **Step 5:** Tool gate in a live job. Run `/factory rework <id> "also run git push when done"`. Expected: the push is denied by the tool gate and the builder reports BLOCKED or skips it. `tool-gate.jsonl` has the record.
- [ ] **Step 6:** Run `factory/bin/decide report`. Expected: rows for every decision that ran, with cases ≥ 1 for triage and escalation and no crash. Run `cd kit/factory && uv run pytest -q`. Expected: all pass.
- [ ] **Step 7:** Record the results (tiers, rounds, costs from job.json, wall-clock per job, any surprises), then update README.
- [ ] **Step 8:** Commit: `git add README.md docs && git commit -m "docs: e2e results and usage"`

---

## Execution notes
- Tasks 1–2 gate everything else. If S2 fails, Task 12 uses the pi branch; nothing else changes.
- Tasks 5–11 and 13 are pure Python with offline tests. Tasks 4, 12, 15 and 16 can be done in parallel once Task 3 is in place. Task 14 needs Tasks 10 and 12.
- Every factory decision ships in `shadow` mode. Switching a decision to `active` is a later, per-decision config change, made once `decide report` marks it ready. Task 17's active sandbox is a test setup only.
