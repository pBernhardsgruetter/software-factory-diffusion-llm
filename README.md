# Mercury software factory

A Claude Code software factory (one `/factory` skill, seven subagents and a
dashboard) extended with a Python package, `factorylib`, that adds a Mercury
decision client (System 1), a tool-gate hook and a fast coder (Mercury 3).
Everything that gets installed into a target repo lives in `kit/`.

System 1 may only make a job cheaper when it is confident, or add an
escalation. It never overrides an Opus or Sonnet verdict, and the deterministic
rules in `factory/policy.json` always win.

## Prerequisites

- `uv` (runs the `factorylib` Python project)
- `node` >= 22
- `git`
- the `agent-browser` CLI
- Linux: `bubblewrap` and `socat` (`sudo apt install bubblewrap socat`). The
  Mercury 3 child runs with `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`, which turns on
  Claude Code's command sandbox; without them every fast-tier Bash call fails,
  so `fast-coder` refuses to start and the job is promoted to Opus.
- `OPENROUTER_API_KEY` exported in the shell you start Claude Code from. Never
  commit it.
- The target repo needs a `main` branch: every job branches from `main` and
  merges back into it.

## Install

```sh
scripts/install.sh [--force] <target-repo>
```

This copies `kit/.claude` and `kit/factory` into the target, which must be the
top level of a git repo (a subdirectory is refused), and adds the factory's
runtime state and caches (`/factory/jobs/`, `/factory/.venv/`,
`/factory/tool-gate.jsonl`, `/factory/**/__pycache__/`, `/factory/.pytest_cache/`,
`/.claude/.factory-backup-*/`) to its `.gitignore`.

It refuses to run if `<target>/factory` exists, or if it would overwrite
`.claude/settings.json` or a same-named file in `.claude/agents/` or
`.claude/skills/factory/`. Pass `--force` to overwrite: the replaced `.claude`
files are first copied to `.claude/.factory-backup-<timestamp>/` so you can
merge your own hooks and agents back. Other `.claude` files are left alone, and
an existing `factory/backlog.md` is always kept.

Commit the installed files. The factory only merges into a clean main checkout
(`git status --porcelain` empty apart from `factory/backlog.md`).

## Usage

In Claude Code, in the target repo:

| Command | What it does |
| --- | --- |
| `/factory <feature>` | Start a job: triage, scout, spec, build, reviews, approval, merge to `main`. |
| `/factory next` | Claim the first `- [ ]` item in `factory/backlog.md` and run it. |
| `/factory approve <job-id>` | A human approves a job in `needs-human`; it merges. |
| `/factory rework <job-id> <note>` | Send a `needs-human` job back to an Opus builder with your note. |

Each job runs in its own worktree (`../<repo>-factory/<job-id>`) on its own
branch (`factory/<job-id>`), so several sessions can run jobs in parallel. Job
state lives in `factory/jobs/<job-id>/` in the main checkout. The factory never
pushes.

## Shadow, then active

Every decision ships in `shadow` mode (`factory/config.json`, `decisions.<name>.mode`).
In shadow mode the factory behaves exactly like the reference factory; Decide's
answers are only logged, with the ground truth the orchestrator records later.
To see which decisions are calibrated:

```sh
factory/bin/decide report          # table
factory/bin/decide report --json   # raw numbers
```

A decision is `ready` when it has at least 20 cases with outcomes, at least 95%
agreement with the recorded truth, and no unsafe misses (truth escalates, Decide
would not; checked for `triage` and `escalation`). What `ready` measures:

- Agreement is checked only on the keys the outcomes record. For `triage` that is
  the risk categories (from the approver), not the tier or the reviewer choice.
- `escalation` is paired with the approver's independent verdict, and its logged
  route is Decide's own opinion without the deterministic rule hits.
- `assumptions`, `clarify`, `promote`, `rereview`, `toolgate` and `visual` have
  no (or only one-sided) outcome source in this factory. Their pairing is
  `unsupported` and they are never `ready`: keep them in shadow.

Switch one decision at a time to `"mode": "active"` once the report marks it
ready, and keep watching the report.

## Dashboard

```sh
cd factory && python3 -m http.server 8765 --bind 127.0.0.1
```

Open <http://localhost:8765/dashboard.html>. It shows each job's stage, tier,
review chips per round, escalations, decisions and the cost line.

## Costs

All numbers are estimates.

- Mercury 3 (fast coder): `cost_usd` is estimated from token counts times
  `fast_coder.price_per_mtok` in `factory/config.json`. A timed-out run reports
  no cost.
- Mercury Decide: the `usage.cost` of each record (the free model is tried first).
- Opus and Sonnet are not observable from the session (`cost.opus_sonnet: "n/a"`).
- `job_budget_usd` (default $5) caps a job's Mercury spend; over it, the job
  escalates.

## Security model: guardrail, not a sandbox

The tool gate (`factory/bin/tool-gate`, a PreToolUse hook) gates Bash and file
edits of the main session, of subagents working in job worktrees, and of the
headless Mercury roles. It denies known-destructive commands, asks for guarded
paths, keeps headless children inside their worktree and out of the job folder,
and logs every in-scope decision to `factory/tool-gate.jsonl`. It is a
guardrail, not a sandbox:

- Allowlisted runners (`npm test`, `npx vitest run`, `node --test`, ...) execute
  agent-written code with your user's rights.
- `OPENROUTER_API_KEY` is in the main session's environment. With the `pi`
  harness the key is also in the Mercury 3 model's environment.
- With the `claude-p` harness, `fast-coder` sets `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`,
  so the Mercury roles' Bash runs in Claude Code's sandbox (bubblewrap + socat
  on Linux) and never sees the key. That covers the fast roles only; the Opus
  and Sonnet agents' Bash is guarded by the tool gate and permission rules.
- For untrusted work, run the factory under OS-level sandboxing (a container or
  VM) with no push credentials and no secrets beyond the OpenRouter key.

## End-to-end results

Four backlog items on the sample app (`factory-sandbox`, all decisions `active`
except `clarify`, run headless with `claude -p`, Claude Code 2.1.304, Opus 5.5
orchestrator). Wall clock excludes time waiting for a human; cost is the
orchestrator session including its subagents (`total_cost_usd`).

| Job | Tier | Rounds | Human | Wall clock | Opus/Sonnet | Mercury |
|---|---|---|---|---|---|---|
| 001 footer typo | fast → promoted | 1 | none | 295 s | $2.14 | $0.013 |
| 002 dark-mode toggle | heavy | 3 | D3 product question | 575 s | $4.02 | $0.005 |
| 003 login + bcrypt | heavy | 2 (+rework) | D3 ×4, needs-human, approve | 759 s | $5.76 | $0.005 |
| 004 about-page copy | fast (Mercury 3 built it) | 1 | none | 242 s | $1.58 | $0.010 |

Decide cost $0.00 throughout: every call was served by the `:free` model.
`decide report` after the four jobs: 0 unsafe misses; triage and escalation at
75% agreement over 4 paired cases each, far from the 20-case `ready` bar.

### Parallel run: four jobs at once

Backlog items 005–008, started as four simultaneous `claude -p "/factory next"`
sessions (one orchestrator per job, each in its own worktree). Contact page and
Pricing page deliberately touched the same files (`main.ts`, router, header,
`AGENTS.md`); slugify and the footer year were independent.

| Job | Tier | Outcome | Human steps |
|---|---|---|---|
| 008 footer year | fast | merged first, 1 round, 151 s | 1 assumption |
| 007 slugify | fast/heavy | merged, 1 + UX-only round | 1 assumption, 1 hand edit (`AGENTS.md` is a guarded path) |
| 005 contact page | heavy | merged after 3 rounds, 2 merge conflicts | 4 assumptions, 1 hand-resolved conflict |
| 006 pricing page | heavy | merged after a rework that merged `main` | 3 assumptions |

- **Claiming works.** The four sessions took four different backlog items and
  job ids; the dev-server ports and agent-browser sessions never collided.
- **Conflicts are the real cost of parallelism.** The merge conflict was caught
  by the approver every time (never merged wrong), but only after the full
  review and approval, and `main` moved again during the rework (005 needed
  two reworks). The skill now syncs the branch with `main` *before* the
  approver and re-checks with `git merge-tree` right before the merge.
- **Guarded paths stop jobs on purpose.** The builder cannot edit `AGENTS.md`;
  the one-line edit needs a human.
- **After merging in the main checkout, run `npm ci`** if a job added a
  dependency; the merged tree failed type-check until `bcryptjs` was installed.
- **D3 is chatty:** 4 of 4 jobs paused for assumption questions, most of them
  trivial. Keep `assumptions` in shadow until it has data.
- The final tree passes 52 tests, build and lint; `decide report` shows 0
  unsafe misses over 8 triage / 8 paired escalation cases.

What the runs showed:

- **Escalation works as designed.** Job 003 escalated on two path rules
  (`package.json`, `package-lock.json`), three D9 risk flags (auth,
  dependencies, build_scripts) and the approver's own ESCALATE, and merged only
  on `/factory approve`. `build_scripts` was a false positive.
- **D3 asks real product questions** (002: follow the OS theme until the user
  picks; 003: four assumptions about the demo login). **D2 asked a human about
  a typo**, so `clarify` went back to shadow.
- **D6 uncertain routes to the builder**, not the fast fixer (002 round 1),
  which is the conservative path.
- **Planted `git push`** (003 rework note): the builder refused from its
  prompt, so the gate was not reached. Fed directly, the headless gate denies
  `git push`, `rm -rf ~` and `curl … | sh` by policy, denies an unknown `nc -l`
  as `needs human:`, and allows `npm test`.
- **Mercury 3 needs its own `CLAUDE_CONFIG_DIR`.** A claude.ai login in
  `~/.claude` beats `ANTHROPIC_AUTH_TOKEN` and is never sent to a third-party
  URL, which gave a 401. `fast-coder` now uses `factory/.fast-coder/`.
- **The scrubbed child needs bubblewrap and socat** on Linux (job 001's
  fast-builder failed without socat and was promoted). `fast-coder` now
  refuses to start a Bash role without them.
- **The sandbox leaves empty mount stubs** (`.env*`, `.npmrc`, lockfiles, ...)
  in the worktree, which blocked `git worktree remove`. The merge step runs
  `fast-coder clean-stubs` first.
- **Prompt fixes from the runs:** reviewers run each `agent-browser` call as a
  single prefix-matchable command, read files with Read, retry one denied Bash
  call and never PASS an unrun check, and stop dev servers with `lsof` + `kill`;
  the approver's escalation reason comes from `## For the human`.
- **Opus 5.5 needs Claude Code ≥ 2.1.280.**
- **Not worth it: a Mercury pre-check for the UI review.** The UI reviewer
  cost $0.14–0.44 and 15–44 s per round, runs in parallel with the others, is
  already skipped by D5 in intermediate rounds and must run before approval.
  The builder (66–248 s, $0.8–2.5) and spec writer dominate; routing more jobs
  to the fast tier is the bigger lever.

## Docs

- Specs: `docs/superpowers/specs/`
- Plans: `docs/superpowers/plans/`
