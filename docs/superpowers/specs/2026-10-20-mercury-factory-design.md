# Mercury Software Factory — Design

> Status: draft for review · 2026-10-20 · author: Patrick Bernhardsgruetter (with Claude)


## Context

The reference factory (`software-factory-diffusion-llm/zazencodes-season-3/src/software-factory-claude-code`) is one `/factory` skill, seven Claude subagents and markdown handoff files. It runs spec → build → 4 parallel reviews → approve → merge, with one git worktree and one `job.json` per job. Every stage uses Opus or Sonnet, so even trivial jobs and trivial decisions pay heavy‑model latency and cost.

**Goal:** a factory for **real daily use** that adds two Inception models:
- **Mercury Decide** (System 1) makes fast, calibrated decisions: triage, routing, which reviewers to run, whether a finding is trivial, whether progress has stalled, and **when a human is needed** (both job escalation and individual tool actions).
- **Mercury 3** (fast coder, in place of Haiku) does simple jobs end‑to‑end, mechanical fix‑ups and repo scouting. It can be promoted to Opus.
- Spec, building complex jobs, all reviews, security and final approval stay on **Opus 5.5 / Sonnet 5.5**.

### What you said vs. what I assumed
- **Said:** daily use. Mercury 3 takes simple jobs, fix‑ups, tier promotion and scouting. Its harness is headless `claude -p` through OpenRouter. Decide uses the `:free` endpoint with a paid fallback. The factory lives at the root of the outer repo `software-factory-diffusion-llm/`. Escalations surface inside the Claude Code session. Images that matter: UI screenshots, spec mockups, diagrams and error screenshots.
- **Researched facts (verify in Phase 0):**
  - Decide is **not** chat‑compatible. It is called at OpenRouter `/v1/systemone` with `{model, state, questions:{name:{type: boolean|choice|score, instructions, criteria}}}` and returns `answers` with probabilities.
  - 1–128 questions per call, about 66K context, about 500 ms median latency.
  - Several sources list it as **text/JSON only (no vision)**.
  - Artificial Analysis measured the **highest calibration error** among 13 decision models, so thresholds must be tuned on our own data.
  - Mercury 3 is OpenAI‑compatible with tools and `response_format`, 1M context, about 1,000 tok/s, $0.10/$0.35 per M tokens at launch.
  - OpenRouter guarantees Claude Code compatibility only for Anthropic models.
- **Assumed:**
  - A Python CLI (stdlib + `httpx`, run with `uv`) is an acceptable bridge, as in the sibling `jev-system-one-model-python-demo`.
  - The sample target app is a small Vite + TypeScript app with tests, so the UI/UX reviewers have something to look at.

## Core invariant (what keeps this safe)

**System 1 may only speed up or escalate. It never overrides System 2.**
- Decide may skip work for clearly simple cases: a smaller builder tier, fewer reviewers, the fast fixer.
- Decide may add a human escalation at any point.
- Decide may **never** turn an Opus/Sonnet `CHANGES`, `BLOCKED` or `ESCALATE` into a pass.
- Every Decide answer gets a band:
  - `confident`: the probability clears the decision's threshold, so the fast path is taken.
  - `uncertain`: the System‑2 default path runs. For risk questions, the uncertain band also escalates.
- If the API fails or is rate‑limited after the fallback, the answer counts as `uncertain`. The factory degrades to the original behaviour.
- **Shadow mode first.** Each decision has `mode: shadow|active` in `factory/config.json`. In shadow mode it is logged but the original path runs. A decision is set to active only after its agreement report clears a target.

## Architecture

```
/factory <feature>  (orchestrator = main session, Opus)
  │
  ├─ D1 triage ............ decide ──► tier (fast|heavy), reviewer set, risk flags
  ├─ scout ................ fast-coder scout (Mercury 3) ──► scout.md (relevant files)
  ├─ D2 clarify? .......... decide ──► uncertain/needs-human → ask in session
  ├─ spec-writer (Opus) ... spec.md        └─ D3 assumptions = product decision? → ask
  ├─ build
  │    fast tier:  fast-coder fast-builder (Mercury 3, resumable claude -p session)
  │    heavy tier: builder subagent (Opus, as today)
  │    D4 failure triage on test/build errors (flaky|env|code|spec-conflict)
  ├─ review round N: only reviewers chosen by D1 (round 1) / D5 (later rounds)
  │    security (Opus) · code (Sonnet) · ux (Sonnet) · ui (Sonnet, vision → observations.json → D11)
  ├─ any CHANGES:
  │    D6 per finding: mechanical → fast-coder fast-fixer │ substantive → resume builder
  │    D7 fast tier failed round → promote to Opus builder
  │    D8 stuck (no progress vs round N-1) → escalate early
  ├─ D9 escalation gate: path rules → Decide risk questions → approver (Opus)
  │    any rule hit / risk ≥ τ / uncertain risk / approver ESCALATE → needs-human
  └─ merge (unchanged)

Every agent tool call ─► PreToolUse hook ─► allowlist/denylist ─► D10 decide ─► allow | deny | ask(human)
```

## Model roster

| Role | Model | How it is invoked |
|---|---|---|
| Orchestrator, spec-writer, heavy builder, security reviewer, approver | Opus 5.5 | Claude subagents (`model: opus`), unchanged |
| Code, UX and UI reviewers; vision observations | Sonnet 5.5 | Claude subagents (`model: sonnet`) |
| Scout, fast builder, fast fixer | `inception/mercury-3` | `factory/bin/fast-coder` → `claude -p` with OpenRouter env on the **child process only** |
| All D‑decisions, tool gate | `inception/mercury-decide:free` → paid fallback | `factory/bin/decide` CLI |

## Components

### 1. `factory/bin/decide` (Python, `uv run --script`)
- **CLI:** `decide <decision> --job <id> --state-file <f> [--state-json …]` reads a template from `factory/decisions/<decision>.json` and prints JSON to stdout: `{decision, mode, band, route, answers, probabilities, model, latency_ms}`.
- **Templates:** each holds `questions` (typed), `thresholds` per question, a `route` mapping (band/answer → action), `mode` and `fallback_route`.
- **Transport:**
  - Calls OpenRouter systemone with `model: inception/mercury-decide:free`.
  - On 429/5xx, retries once against `inception/mercury-decide`, then returns `band: uncertain` with `error`.
  - Timeout is 5 s.
- **Redaction** runs before sending, because the free tier may log prompts. It strips `.env`-style lines, common key/token patterns (`sk-…`, `ghp_…`, `AKIA…`, JWTs), and anything matching `factory/config.json` → `redact_patterns`. It also caps state at about 50K tokens, keeping the head and tail of each file and marking the truncation.
- **Logging:** appends to `factory/jobs/<id>/decisions.jsonl` (with a state hash, not the state itself). Tool‑gate decisions go to `factory/tool-gate.jsonl`.
- **Images:** accepts `--image <path>`, used only if the Phase‑0 spike proves image input works. Otherwise it errors and tells the caller to use the observations path.
- **`decide report`:** reads all `decisions.jsonl` files plus outcomes (`outcome` records written by the orchestrator on approve/rework/override). Per decision it reports agreement, the confident‑band error rate, coverage (the share of cases that were confident) and a suggested threshold.

### 2. Decision catalog (`factory/decisions/*.json`)
| ID | When | State | Questions | Confident → | Uncertain → |
|---|---|---|---|---|---|
| D1 triage | after the job is claimed | feature text + repo outline | choice `kind` (docs/ui/bugfix/feature/refactor/infra); score `complexity` (trivial…large); boolean `ui_change`; booleans for auth/payments/deps/CI/schema/email/prod‑data | tier=fast if complexity ≤ small and no risk; reviewers = security+code (+ux/ui if `ui_change`) | tier=heavy, all 4 reviewers |
| D2 clarify | after scout | feature + scout.md | boolean `needs_human_clarification` | continue | **ask in session** before spec |
| D3 assumptions | after spec | each spec Assumption | boolean `is_product_decision` per assumption | continue | ask in session (any true or uncertain) |
| D4 failure triage | builder/fixer hits a failing command | command + output tail | choice `flaky/env/code/spec_conflict` | flaky → retry once; env → BLOCKED; code → keep fixing; spec_conflict → BLOCKED | keep fixing (default behaviour) |
| D5 re‑review set | before round N>1 | prior verdicts + fix diff stat | boolean per reviewer `area_touched` | rerun CHANGES reviewers + touched areas | rerun all |
| D6 finding triage | each CHANGES finding | finding text + file excerpt | boolean `mechanical` (lint/typo/rename/missing‑import) | fast-fixer batch | resume builder |
| D7 promotion | fast tier gets CHANGES | round verdicts + findings | boolean `beyond_fast_tier` | promote to Opus builder (fresh, reads history) | promote (conservative) |
| D8 stuck | round ≥ 2 | findings N‑1 vs N | score `progress` | continue | escalate: "not converging" |
| D9 escalation | before approver | diff stat, changed paths, diff (truncated), spec | risk booleans (same set as D1 + "touches deploy/build scripts") | approver runs; any risk ≥ τ → needs-human even if APPROVE | needs-human after approver |
| D10 tool gate | PreToolUse hook | tool name, input, cwd, job worktree | choice `safe/needs_human/destructive` | allow / ask / deny | **ask** |
| D11 visual match | UI review | Sonnet `observations.json` + spec criteria | boolean per visual criterion `met` | feeds reviewer-ui verdict as evidence | reviewer-ui decides itself |

Deterministic rules always run before Decide, are cheaper, and can never be overridden. They live in `factory/policy.json`:
- `escalate_paths`: e.g. `package.json` deps, lockfiles, `.github/**`, `**/migrations/**`, auth dirs, firestore/storage rules, deploy configs.
- `tool_allow`: e.g. `git status/diff/log`, `npm test`, read‑only commands.
- `tool_deny`: e.g. `git push`, `rm -rf` outside the worktree, `curl | sh`, writes outside the worktree or job folder.

### 3. `factory/bin/fast-coder` (bash)
- **Command:** `fast-coder <role> --job <id> --worktree <p> [--port N] [--resume]`, where role is `scout`, `fast-builder` or `fast-fixer`.
- **Environment (child only):** `ANTHROPIC_BASE_URL=https://openrouter.ai/api`, `ANTHROPIC_AUTH_TOKEN=$OPENROUTER_API_KEY`, empty `ANTHROPIC_API_KEY`. It runs `claude -p --model inception/mercury-3`.
- **Prompt and permissions:**
  - The role prompt comes from `factory/fast-agents/<role>.md` through `--append-system-prompt`.
  - `--allowedTools` is restricted (scout: Read/Grep/Glob only).
  - `--max-turns` is capped, and `--output-format json`.
  - The child session id is stored in `job.json` so later rounds resume with `--resume <id>` (the fast‑tier equivalent of `SendMessage` to `builder-<id>`).
- **Transcript and output:** the transcript goes to `jobs/<id>/fast-<role>-round-N.json`. Each role writes the same handoff file as its Opus counterpart: scout writes `scout.md`, fast-builder writes `build.md` with the same `STATUS:` contract, and fast-fixer appends to the Log in `build.md`.
- **Headless gating:** the D10 tool gate also runs in the child. There is no human attached, so `ask` becomes `deny`, and the role writes `STATUS: BLOCKED` with the reason. The orchestrator then surfaces that in the session.
- **Swappable harness:** if the Phase‑0 spike fails, only this script changes (to the pi coding agent, `pi -p --provider openrouter --model inception/mercury-3`). The handoff files stay the same.

### 4. Tool gate hook (`factory/bin/tool-gate`, a Python hook)
- Registered as a `PreToolUse` hook for `Bash|Edit|Write` in the factory's `.claude/settings.json`, so it applies to the session and to all subagents.
- Runs deterministic policy first, then D10 only for unknown commands, which keeps the median at 0 ms and unknown commands at about 0.5 s.
- Returns `permissionDecision` `allow`, `deny` (with reason) or `ask`. `ask` is the in‑session human escalation you chose.
- Fails closed to `ask`.

### 5. Images (UI screenshots, mockups, diagrams, error screenshots)
- The Phase‑0 spike sends an image content part to systemone.
- **If rejected (expected):** Sonnet does the vision.
  - reviewer-ui gets an extra step: screenshot desktop and phone views with agent-browser, compare them against any mockups referenced in spec.md, and write `round-N/observations.json` with one structured observation per visual criterion.
  - D11 then scores each criterion against those observations.
  - Error screenshots or diagrams pasted by a human are transcribed by the orchestrator into `jobs/<id>/attachments.md` before any decision uses them.
- **If accepted:** `decide --image` passes images directly, and Sonnet observations become the uncertain‑band fallback.

### 6. Orchestrator and agents (edits to the copied kit)
- **`.claude/skills/factory/SKILL.md`:** add the triage, scout, clarify and assumption gates; the tier‑routed build; selective reviewers; the finding‑triage, promotion and stuck steps; the D9 gate; and `outcome` logging on merge, approve, rework and every human answer. Keep the WAIT FOR ALL rule, but over the *active* reviewer set. Reviewers skipped in a round count as `skipped`, not `PASS`.
- **Final approval:** the final round before approval always runs **all reviewers whose area the whole branch diff touches**. D5 only trims intermediate rounds.
- **`.claude/agents/reviewer-ui.md`:** add the observations.json step.
- **`.claude/agents/approver.md`:** read `decisions.jsonl` risk flags, and escalate if D9 flagged a risk, even when everything else would APPROVE.
- **`.claude/agents/builder.md`:** on promotion, read the fast‑tier transcript summary and the round history.
- **New files:**
  - `factory/fast-agents/{scout,fast-builder,fast-fixer}.md`, prompts derived from `builder.md` with tighter scope.
  - `factory/config.json`: model ids, the free→paid order, per‑decision mode and thresholds, `redact_patterns`, max rounds, a per‑job budget.
  - `factory/policy.json`.

### 7. `job.json` and dashboard
- **`job.json` adds:** `tier` (`fast|heavy|promoted`), `fast_session_id`, `reviewers_active`, `skipped`, `escalations[]` (`{source: rule|decide|approver|tool-gate|stuck, reason}`), `cost` (per model, from the claude `-p` JSON and the decide usage).
- **`factory/dashboard.html` adds:**
  - a tier badge on cards;
  - decision chips per job showing D‑id, band and top probability (hover for detail);
  - skipped reviewer chips;
  - an escalation reason on needs‑human cards;
  - a cost line;
  - a "Decisions" drawer tab that renders `decisions.jsonl`.
- `STAGES`, `REVIEWERS` and the model labels update accordingly.

## Layout (outer repo `software-factory-diffusion-llm/`)
```
.claude/{agents/*.md, skills/factory/SKILL.md, settings.json}
factory/{bin/{decide,fast-coder,tool-gate}, decisions/*.json, fast-agents/*.md,
         config.json, policy.json, dashboard.html, backlog.md, jobs/ (gitignored)}
factory/tests/            # pytest for decide + tool-gate (recorded HTTP fixtures)
sample-app/               # small Vite+TS app with AGENTS.md (tests, lint, dev server on --port)
docs/superpowers/specs/2026-10-20-mercury-factory-design.md   # this design, committed first
README.md                 # setup: OPENROUTER_API_KEY, uv, agent-browser, copy-into-repo steps
```
- **Reuse:** copy all seven agents, SKILL.md, dashboard.html and backlog.md from the reference factory, then edit them.
- **Patterns from the reference:** the Jev demo's typed‑question client shape (`src/jev-system-one-model-python-demo/demo.py`) and the pi invocation pattern in `src/pi-agent-fleet/AGENTS.md` (`--session`, `--tools`, `--no-skills`, `-p`), for the fallback harness.

## Phases
0. **Spikes (throwaway, go/no‑go):**
   - S1: confirm the systemone endpoint path and schema on OpenRouter, measure latency, test an image part, observe `:free` 429 behaviour.
   - S2: run `claude -p --model inception/mercury-3` through OpenRouter on a sample‑app task that needs Read/Edit/Bash/commit, over 5 runs. Go if at least 4/5 runs finish with correct tool use; otherwise switch to the pi agent.
1. **Foundation:** commit the spec doc, the kit copy, sample-app with AGENTS.md, `decide` + tests, and config/policy files.
2. **Fast coder:** `fast-coder`, the three fast-agent prompts, and tier routing (D1) in SKILL.md.
3. **Decision points:** D2–D8 and D11 in **shadow** mode, plus outcome logging.
4. **Escalation:** the D9 gate, approver changes, and the tool-gate hook (D10).
5. **Images:** the observations pipeline (or direct images, depending on S1).
6. **Dashboard and calibration:** `decide report`, dashboard updates, and switching decisions to `active` as each reaches its agreement target (proposed: ≥95% agreement and 0 unsafe misses on risk decisions over ≥20 cases).

## Verification
- **Unit tests:** `uv run pytest factory/tests` covers decide (template loading, banding, free→paid fallback on mocked 429, timeout → uncertain, redaction of planted secrets, truncation) and tool-gate (allowlist, denylist, outside‑worktree write denied, Decide‑uncertain → ask, headless ask → deny).
- **Live smoke test:** `factory/bin/decide triage --job smoke --state-json '{"feature":"fix typo in footer"}'` returns `tier: fast` with a confident band.
- **End‑to‑end on sample-app** with three backlog items:
  1. A copy fix should be triaged fast, built by Mercury 3, reviewed by security + code only, then APPROVE and auto‑merge.
  2. A real feature should be triaged heavy, built by the Opus builder, run all four reviewers, have mechanical findings routed to fast-fixer, then merge.
  3. "Add a dependency + login check" should hit the rule and the D9 risk flag, end needs‑human with reasons in the session, and merge on `/factory approve`.
- **Also check:** `decisions.jsonl`, `job.json` and the dashboard chips. `decide report` should show all three jobs. A planted `rm -rf ~` or `git push` in a fast-builder run is denied by the tool gate, and an unknown network command triggers an in‑session ask.
- **Safety regression:** forcing Decide to return a confident "no risk" on a diff that edits `.github/workflows` must still escalate, because rules beat Decide.

