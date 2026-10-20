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
- For untrusted work, run the factory under OS-level sandboxing (a container or
  VM) with no push credentials and no secrets beyond the OpenRouter key.

## End-to-end results

pending

## Docs

- Specs: `docs/superpowers/specs/`
- Plans: `docs/superpowers/plans/`
