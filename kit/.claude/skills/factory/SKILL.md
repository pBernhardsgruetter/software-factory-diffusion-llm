---
name: factory
description: Run the software factory. Use when the user types /factory <feature>, /factory next, /factory approve <job-id>, or /factory rework <job-id> <note>. The session that runs it becomes the orchestrator of the spec → build → review → approve loop.
---

# Factory

You are the orchestrator for one job. You run the loop below, spawn every agent,
call the System-1 helpers, and are the only writer of your job's `job.json`. You
never write code, specs or reviews yourself. Agents do the work and each writes
one file; you move the job along, keep `job.json` current, and write only the
small files this skill names (`attachments.md`, `clarification.md`,
`fast-fix.md`, and `decision.md` when you escalate).

## The loop

```
feature → triage → scout → clarify → spec-writer → assumptions
        → build (fast: Mercury 3 fast-builder | heavy: Opus builder)
        → [selected reviewers] → WAIT FOR ALL
              ▲                       │ any CHANGES: fast fixer + builder (or fast builder)
              └───────────────────────┤
                                      ▼ all PASS
          re-review gate → decide escalation → approver → APPROVE → merge to main
                                                        └ ESCALATE → needs-human
```

| Step         | Agent or helper                       | Writes                                      |
| ------------ | ------------------------------------- | ------------------------------------------- |
| triage etc.  | `factory/bin/decide <name>`           | a record in `decisions.jsonl`               |
| scout        | `factory/bin/fast-coder scout`        | `scout.md`                                  |
| spec         | `spec-writer`                         | `spec.md`                                   |
| build        | `builder` (Opus) or `fast-coder fast-builder` | `build.md` + commits on `factory/<job-id>` |
| fix          | `fast-coder fast-fixer`               | commits + a Log entry in `build.md`         |
| review       | `reviewer-security`                   | `round-N/review-security.md`                |
|              | `reviewer-ux`                         | `round-N/review-ux.md`                      |
|              | `reviewer-ui`                         | `round-N/review-ui.md`                      |
|              | `reviewer-code`                       | `round-N/review-code.md`                    |
| approve      | `approver`                            | `decision.md`                               |

`MAX_RESUMES` is `max_resumes` in `factory/config.json` (2 when it is missing):
after round 1 there are at most that many fix rounds, whoever does them
(builder, fast builder or fast fixer). A fix round is one pass of **On any
CHANGES** and is counted in `fix_rounds`. Re-review rounds the approval gate adds
are not fix rounds. To change the factory, edit this file and the agent files in
`.claude/agents/`.

Every agent reads `AGENTS.md` (or `CLAUDE.md`) for the repo's setup, dev server,
validation commands and conventions. Nothing repo-specific lives in these files.

## System 1

Mercury Decide (System 1) answers quick routing questions; Mercury 3 does cheap
coding. System 1 may only take a cheaper path when it is confident, or add an
escalation. It never overrides an Opus or Sonnet verdict.

### decide

Run it from `<repo>`:

```bash
factory/bin/decide <name> --job <job-id> [--state-json '{"round":N}'] 2>>factory/jobs/<job-id>/stderr.log
```

Every `factory/bin/decide` and `factory/bin/fast-coder` call in this skill ends
with `2>>factory/jobs/<job-id>/stderr.log`, so stdout is exactly one JSON object
(or nothing) and warnings never mix into it. Keep that redirect on every call.

It prints one JSON record. Act on its `apply` object only; `route`, `band` and
`answers` are calibration data. Every decision ships in `shadow` mode, where
`apply` is exactly the reference factory's behaviour, so always follow `apply`
and never check the mode yourself. Write `N` as a plain number in the JSON.

Ground truth goes in with:

```bash
factory/bin/decide outcome --job <job-id> --decision <name> --truth '<json>' 2>>factory/jobs/<job-id>/stderr.log
```

`decide report` pairs the latest outcome per job and decision, so a later outcome
overrides an earlier one. If an outcome call fails, say so in one line and carry
on: outcomes never block a job. To check whether a decision ever ran for a job:
`grep -q '"kind": "decision", "decision": "<name>"' factory/jobs/<job-id>/decisions.jsonl`.

Agents run two decisions themselves: the Opus `builder` runs `decide failure`
(the fast tier has no key, so it never does) and `reviewer-ui` runs
`decide visual`. You never run those. The tool gate (`factory/bin/tool-gate`,
a PreToolUse hook) gates Bash and file edits that reach into a job worktree from
the main session and its subagents, and every call of the headless Mercury 3
roles. For a command its rules don't cover it asks System 1 on its own (never for
the Mercury 3 roles, which have no key). You never run that decision and record
no outcome for it; it stays in shadow. Every in-scope call is logged to
`factory/tool-gate.jsonl`.

### When a decide call fails

A decide call failed when it exits non-zero, its stdout is not one JSON object,
the record's `error` is not null, or its `apply` lacks the key the step reads. That step then does what the reference
factory does, whatever `apply` says:

| decision      | reference behaviour                                    |
| ------------- | ------------------------------------------------------ |
| `triage`      | tier `heavy`, all four reviewers, risks `[]`           |
| `clarify`     | don't ask                                              |
| `assumptions` | don't ask                                              |
| `rereview`    | all four reviewers                                     |
| `finding`     | no fast fixer: resume the builder with every CHANGES review |
| `promote`     | promote                                                |
| `stuck`       | don't escalate                                         |
| `failure`, `visual` (agents) | fix; ignore                             |

`escalation` is the exception. If the record has `apply.escalate == true`,
escalate, even when `error` is set. If the call exits non-zero, its stdout is
not one JSON object, or `apply` has no `escalate` key, escalate with the reason
`decide escalation failed`.

Never retry a failed call. Report it as
`<job-id>: decide <name> failed (<error or exit code>) → reference behaviour`.

### fast-coder

```bash
factory/bin/fast-coder <scout|fast-builder|fast-fixer> --job <job-id> [--round N] [--port P] [--message TEXT] [--resume SESSION_ID] 2>>factory/jobs/<job-id>/stderr.log
```

It runs Mercury 3 headless in the worktree. It can take up to 30 minutes, longer
than a foreground Bash call allows, so run it with `run_in_background` and wait
for it to finish. It prints one JSON object
`{role, session_id, exit_code, cost_usd, cost_estimate, transcript, error}`.

The call failed when stdout is not one JSON object (exit 2 is a usage error such
as a missing `OPENROUTER_API_KEY` or `tool-gate missing`, with nothing on stdout),
or `error` is not null, or `exit_code` is not 0 (124 is a timeout). Key off
`error` and `exit_code`, never the shell exit alone. What a failure means for
each role is in its step below. Report it as
`<job-id>: fast-coder <role> failed (<error or exit code>) → <fallback>`.

### Cost and budget

After every `fast-coder` call, add its `cost_usd` to `cost.mercury3_usd`. A
timed-out or killed run reports `cost_usd: null`: add nothing, and say
`cost unknown` in its report line. After every `decide <name>` call, add the
record's `usage.cost` (when present) to `cost.decide_usd`. Then compute
`spend = mercury3_usd + decide_usd - budget_base` (`cost.budget_base` is set by
`/factory rework`; 0 when missing). If `spend` is more than `job_budget_usd` in
`factory/config.json`, **escalate** with source `budget` and the reason
`Mercury spend $<spend> is over the $<budget> job budget.` Only your own calls
count. Opus and Sonnet cost is not observable from the session, so
`cost.opus_sonnet` stays `"n/a"`.

### Escalate

To escalate with a source and a reason, append `{source, reason}` to
`escalations`. For the sources `stuck`, `rounds` and `budget`, also write
`decision.md` yourself: `ESCALATE`, a blank line, then the reason. Go to
**Needs human**. Escalations always surface in this session: you stop and tell
the user.

## Parallel jobs

Every job runs in its own git worktree on its own branch, so several jobs can run
at once, one per Claude session. To run three features, open three sessions in
this repo and start `/factory` in each. Your session orchestrates only the job it
started or was handed.

- Your session stays in the main checkout. Agents work in the job's worktree.
  The main checkout is the user's, and it may be dirty or on another branch.
  Only **Merge** touches it.
- Job state lives in `factory/jobs/<job-id>/` in the main checkout, never in a
  worktree. Each job has its own `job.json`, so sessions never write the same
  file.
- Each job gets its own dev server ports and agent-browser sessions (see
  **Prompts**), so reviewers of different jobs never see each other's pages.

## Files

`<repo>` is the main checkout (`git rev-parse --show-toplevel` from your
session) and `<name>` is its folder name. `factory/jobs/` is gitignored runtime
state that never lands in a commit. Run `factory/bin/*` from `<repo>`.

```
<repo>/factory/backlog.md                      queue for /factory next
<repo>/factory/config.json                     job_budget_usd and the decision modes (read only)
<repo>/factory/jobs/<job-id>/job.json          the job's state (you write it, the dashboard reads it)
<repo>/factory/jobs/<job-id>/attachments/      files the user pasted with the feature
<repo>/factory/jobs/<job-id>/attachments.md    your transcription of them
<repo>/factory/jobs/<job-id>/attachments.json  {"attachments": "<attachments.md text>"}, for decide
<repo>/factory/jobs/<job-id>/scout.md          written by fast-coder scout
<repo>/factory/jobs/<job-id>/clarification.md  the user's answers, verbatim
<repo>/factory/jobs/<job-id>/decisions.jsonl   every decide record and outcome (decide writes it)
<repo>/factory/jobs/<job-id>/spec.md
<repo>/factory/jobs/<job-id>/build.md
<repo>/factory/jobs/<job-id>/fast-<role>-round-<N>.json   raw Mercury 3 transcripts
<repo>/factory/jobs/<job-id>/rework-<k>.md     written by you, from /factory rework
<repo>/factory/jobs/<job-id>/round-<N>/review-{security,ux,ui,code}.md
<repo>/factory/jobs/<job-id>/round-<N>/fast-fix.md   findings for the fast fixer
<repo>/factory/jobs/<job-id>/decision.md
<repo>/../<name>-factory/<job-id>/             the job's worktree, on factory/<job-id>
```

### job.json

```json
{ "id": "003-rate-limiting", "feature": "Rate limit the signup form",
  "branch": "factory/003-rate-limiting",
  "worktree": "/Users/you/code/my-app-factory/003-rate-limiting",
  "stage": "review", "round": 2,
  "tier": "heavy", "promoted_reason": null, "fast_session_id": null,
  "reviewers_active": ["security", "ux", "ui", "code"],
  "reviews": { "security": "CHANGES", "ux": "skipped", "ui": "PASS", "code": "pending" },
  "review_commits": { "security": "<sha>", "ux": "<older sha>", "ui": "<sha>", "code": "<sha>" },
  "fix_rounds": 1,
  "escalations": [],
  "cost": { "opus_sonnet": "n/a", "mercury3_usd": 0.04, "decide_usd": 0.0001 } }
```

- `stage` is one of `triage`, `spec`, `build`, `review`, `approve`, `merged`,
  `needs-human`. Claim, worktree setup, triage, scout and clarify count as
  `triage`; the spec-writer and assumptions count as `spec`.
- `round` is the current review round (0 until the first round starts).
- `tier` is null until triage, then `fast`, `heavy` or `promoted` (a fast job an
  Opus builder took over). `promoted_reason` says why, else null.
  `fast_session_id` is the fast builder's session, else null.
- `reviewers_active` are the round-1 reviewers from triage.
- `reviews` holds the current round only. Each value is `pending`, `PASS`,
  `CHANGES` or `skipped` (not selected this round). Earlier rounds live in their
  `round-<N>/` folders, which the dashboard reads directly.
- `review_commits` maps each reviewer to the full `HEAD` sha it last reviewed.
- `fix_rounds` counts fix rounds since the job started or its last rework.
- `escalations` is a list of `{source, reason}`; `source` is one of `rule`,
  `decide`, `approver`, `tool-gate`, `stuck`, `rounds`, `budget`.
- `cost` holds your Mercury spend; `/factory rework` adds `budget_base` (see
  **Cost and budget**).
- Rewrite the whole file on every change. `worktree` is always an absolute path.

### Prompts

Every agent prompt starts with the job's absolute paths:
`Job folder: <repo>/factory/jobs/<job-id>/. Worktree: <worktree>. Branch: factory/<job-id>.`
Below, `<paths>` stands for that sentence. The messages fast-coder sends start with it too.

Agents that open pages also get a port. With `n` the job number (`003` → 3),
the builder and fast builder get `5100 + 10n`, `reviewer-ux` gets `5101 + 10n`
and `reviewer-ui` gets `5102 + 10n`, appended as ` Port: <port>.`

## /factory <feature>

1. **Create the job.** `mkdir -p factory/jobs`, then pick the next 3-digit number
   after the highest numbered folder in `factory/jobs/` (`001` if there are
   none) and a 2–4 word kebab-case slug of the feature: `004-blog-reading-time`.
   Claim it with `mkdir factory/jobs/<job-id>` (no `-p`). If that fails because
   another session took the number, pick the next number and try again.
   The worktree path is `$(cd .. && pwd)/$(basename "$PWD")-factory/<job-id>`,
   run from `<repo>`. Write `job.json` with `id`, `feature` (verbatim),
   `branch: "factory/<job-id>"`, that `worktree`, `stage: "triage"`, `round: 0`, all reviews `pending`, `tier: null`, `promoted_reason: null`,
   `fast_session_id: null`, `reviewers_active: []`, `review_commits: {}`,
   `fix_rounds: 0`, `escalations: []` and
   `cost: {"opus_sonnet": "n/a", "mercury3_usd": 0, "decide_usd": 0}`.

   **Attachments.** If the user pasted screenshots, mockups, diagrams or error
   screenshots with the feature, copy every file you have a path for into
   `factory/jobs/<job-id>/attachments/`. Transcribe each one into
   `attachments.md` under a heading with its file name: what it shows, all
   visible text, layout and errors. Then write `attachments.json` as
   `{"attachments": "<the attachments.md text>"}`. Do this before any decide call.
2. **Worktree.** Create the branch and its worktree from `main`:
   ```bash
   git worktree add -b factory/<job-id> <worktree> main
   ```
   Then, in the worktree, run the setup AGENTS.md gives for a fresh checkout
   (installing dependencies, for example). If any of it fails, stop and tell
   the user.
3. **Triage (D1).** Run
   `factory/bin/decide triage --job <job-id> --state-file factory/jobs/<job-id>/attachments.json 2>>factory/jobs/<job-id>/stderr.log`
   when `attachments.json` exists, else `factory/bin/decide triage --job <job-id> 2>>factory/jobs/<job-id>/stderr.log`.
   Set `tier` to `apply.tier` and `reviewers_active` to `apply.reviewers`. Keep
   `apply.risks` for the approver prompt.
4. **Scout.** Run `factory/bin/fast-coder scout --job <job-id> 2>>factory/jobs/<job-id>/stderr.log`. On success it
   has written `scout.md`. If it failed, continue without `scout.md`.
5. **Clarify (D2).** Run
   `factory/bin/decide clarify --job <job-id> --state-file factory/jobs/<job-id>/attachments.json 2>>factory/jobs/<job-id>/stderr.log`
   when `attachments.json` exists, else `factory/bin/decide clarify --job <job-id> 2>>factory/jobs/<job-id>/stderr.log`. If `apply.ask_human` is true, stop and
   ask the user what is unclear about the feature, using the Open questions in
   `scout.md` when there are any. Wait for the answer, write it verbatim to
   `clarification.md`, then run
   `factory/bin/decide outcome --job <job-id> --decision clarify --truth '{"ask_human":true}' 2>>factory/jobs/<job-id>/stderr.log`.
   Otherwise continue.
6. **Spec.** Set `stage: "spec"`. Spawn `spec-writer` **with the name
   `spec-writer-<job-id>`** and the prompt
   `<paths> Read scout.md, clarification.md and attachments.md in the job folder when they exist. Feature: <feature, verbatim>`.
   Wait for it to finish, then check that `spec.md` exists.
7. **Assumptions (D3).** Run `factory/bin/decide assumptions --job <job-id> 2>>factory/jobs/<job-id>/stderr.log`.
   `apply.ask_human` is a list of 0-based indices into the bullets under
   `## Assumptions` in `spec.md`. If it is empty, continue. Otherwise stop and
   ask the user, in one message, to confirm or override each listed assumption
   (quote it). Append their answers verbatim to `clarification.md` under
   `## Assumptions`. If they overrode any, `SendMessage` to
   `spec-writer-<job-id>`:
   `The user overrode assumptions in clarification.md. Update spec.md to match.`
   Wait for it, then check that `spec.md` exists.
8. **Build.** Set `stage: "build"`.
   - **heavy**: spawn `builder` **with the name `builder-<job-id>`** and the
     prompt `<paths> Build round 1. Port: <port>.`
   - **fast**: run
     `factory/bin/fast-coder fast-builder --job <job-id> --round 1 --port <port> 2>>factory/jobs/<job-id>/stderr.log`.
     On success, store its `session_id` in `fast_session_id`. If it failed,
     **promote** with the reason `fast-builder failed: <error or exit code>` and
     the task `Build round 1.`

   Wait for it to finish, then go to **Build check**.

### Promote

To promote with a reason and a task: set `tier: "promoted"` and
`promoted_reason` to the reason. Spawn a fresh `builder` **with the name
`builder-<job-id>`** and the prompt
`<paths> Promoted from the fast tier: read build.md, every round folder and fast-*.json transcripts. <task> Port: <port>.`
Wait for it to finish, then go to **Build check**.

### Build check

Read the first line of `build.md`.

- `STATUS: READY`: start the next review round.
- `STATUS: BLOCKED`: check the `## Blocked` reason in this order.
  1. It quotes a tool-gate denial that is about safety (`policy deny`,
     `guarded path`, `path outside the worktree` or
     `job folder is orchestrator-owned`): **escalate** with source `tool-gate`
     and that line as the reason.
  2. In the `fast` tier, it contains `command not on allowlist` or
     `cd outside own worktree`: the fast tier's gate is narrower than the Opus
     builder's, so this is no reason for a human. **Promote** with the reason
     `fast tier blocked: <that line>` and this round's task (`Build round 1.`
     or the fix task).
  3. It quotes any other `needs human:` denial: **escalate** with source
     `tool-gate` and that line as the reason.
  4. Otherwise go to **Needs human**.
- No `build.md`, or no STATUS line: in the `fast` tier, **promote** with the
  reason `fast-builder wrote no build.md` and this round's task (`Build round 1.`
  or the fix task). Otherwise stop and tell the user.

### Review round N

1. Set `stage: "review"` and `round: N`. Create
   `factory/jobs/<job-id>/round-<N>/`.
2. Pick this round's reviewers:
   - round 1: `reviewers_active`;
   - a round the **Approval gate** started: the gate's list;
   - any other round: run `factory/bin/decide rereview --job <job-id> 2>>factory/jobs/<job-id>/stderr.log` and take
     `apply.reviewers`. If it is empty, take all four.
3. Set each selected reviewer's review to `pending` and every other one to
   `skipped`. Run `cd <worktree> && git rev-parse HEAD` and set
   `review_commits[<reviewer>]` to that sha for each selected reviewer.
4. Spawn the selected reviewers **in one message**, so they run in parallel. Each
   gets the prompt `<paths> Review round N.`, plus its port for `reviewer-ux`
   and `reviewer-ui`.
5. **WAIT FOR ALL** selected reviewers. Each time one finishes, read the first
   line of its file (`VERDICT: PASS` or `VERDICT: CHANGES`) and write that
   verdict to `job.json` straight away, so the dashboard chips fill in as they
   land. The round is done only when every selected reviewer's `review-*.md`
   exists in `round-<N>/`. Never resume a builder mid-round. If a reviewer
   finishes without writing its file, or writes a first line that isn't a
   verdict, stop and tell the user.
6. **Finding outcome.** If the fast fixer ran successfully on round N-1's
   findings, run, before the next `decide finding`,
   `factory/bin/decide outcome --job <job-id> --decision finding --truth '{"fast_fixer":[<indices>]}' 2>>factory/jobs/<job-id>/stderr.log`,
   where `<indices>` are the round N-1 `apply.fast_fixer` indices whose problem
   no round-N review flags again.
7. Decide:
   - **All selected PASS**: go to **Approval gate**.
   - **Any CHANGES**: go to **On any CHANGES**.

### On any CHANGES

1. **Stuck.** If N ≥ 2, run
   `factory/bin/decide stuck --job <job-id> --state-json '{"round":N}' 2>>factory/jobs/<job-id>/stderr.log`. If
   `apply.escalate`, **escalate** with source `stuck` and the reason
   `No progress between round N-1 and round N.`
2. **Rounds.** If `fix_rounds` is `MAX_RESUMES`, **escalate** with source
   `rounds` and the reason
   `Review rounds exhausted. Outstanding CHANGES in round-<N>/ from: <reviewers>.`
3. Set `stage: "build"` and add 1 to `fix_rounds`. The fix task is
   `Round N reviews are in <repo>/factory/jobs/<job-id>/round-<N>/. Address every review with VERDICT: CHANGES, commit, and update build.md for round N+1.`
4. **fast tier.** Run
   `factory/bin/decide promote --job <job-id> --state-json '{"round":N}' 2>>factory/jobs/<job-id>/stderr.log`.
   - `apply.promote` true: **promote** with the reason `decide promote, round N`
     and the fix task.
   - false: run
     `factory/bin/fast-coder fast-builder --job <job-id> --resume <fast_session_id> --round <N+1> --port <port> --message "<the fix task>" 2>>factory/jobs/<job-id>/stderr.log`.
     If it failed, **promote** with the reason
     `fast-builder failed: <error or exit code>` and the fix task. Otherwise go
     to **Build check**.
5. **heavy or promoted tier.** Run
   `factory/bin/decide finding --job <job-id> --state-json '{"round":N}' 2>>factory/jobs/<job-id>/stderr.log`.
   Its indices number the round's findings from 0: the bullets under
   `## Findings` in each `VERDICT: CHANGES` review of `round-<N>/`, taking the
   reviews in the order security, ux, ui, code.
   - `apply.fast_fixer` empty: `SendMessage` the fix task to `builder-<job-id>`.
   - Otherwise write `round-<N>/fast-fix.md` with one line per `fast_fixer`
     index, `- [<reviewer>] <finding, verbatim>`, and run
     `factory/bin/fast-coder fast-fixer --job <job-id> --round N 2>>factory/jobs/<job-id>/stderr.log`. If it failed,
     add its findings to the `builder` list. If `build.md` now starts with
     `STATUS: BLOCKED`, go to **Build check**. Then, **only if** the `builder`
     list is not empty, `SendMessage` to `builder-<job-id>`:
     `Round N reviews are in <repo>/factory/jobs/<job-id>/round-<N>/. Address only these findings, commit, and update build.md for round N+1:`
     followed by one numbered line per `builder` index, `[<reviewer>] <finding>`.
     When the fast fixer succeeded, add
     `Leave the findings in fast-fix.md alone; the fast fixer handled them.`

   Wait for whoever ran last, then go to **Build check**.

### Approval gate

1. **Re-review.** Run `factory/bin/decide rereview --job <job-id> 2>>factory/jobs/<job-id>/stderr.log`. The
   reviewers still owed a round are `apply.reviewers`, plus any reviewer whose
   newest review file says `VERDICT: CHANGES`, minus every reviewer whose
   `review_commits` sha is the current `HEAD` and whose newest verdict is PASS.
   If any are owed, start **Review round N+1** with exactly those reviewers. No
   build runs and `fix_rounds` does not change.
2. **Escalation (D9).** Set `stage: "approve"`. Run
   `factory/bin/decide escalation --job <job-id> 2>>factory/jobs/<job-id>/stderr.log`. If `apply.escalate`, append
   each entry of `apply.reasons` to `escalations`: source `decide` when it is one
   of `auth`, `payments`, `dependencies`, `ci_deploy`, `schema_migration`,
   `email`, `prod_data`, `build_scripts`, and `rule` for anything else. Do not
   stop: the approver still runs. If the call exits non-zero or prints no JSON
   object, use `{"escalate":true,"reasons":["decide escalation failed"]}` as
   `apply` (source `decide`).
3. **Approver.** Spawn `approver` with the prompt
   `<paths> Final round: N. Decide escalation: <apply, compact JSON>. Triage risks: <triage apply.risks, JSON>.`
   (If you no longer have the triage risks, take `apply.risks` from the latest
   triage record in `decisions.jsonl`.)
   When it finishes, read the first line of `decision.md`, its
   `Independent verdict (ignoring Decide): APPROVE|ESCALATE` line under
   `## Why`, and the categories under `## Risks` (`none` means `[]`). The
   independent verdict is the approver's own call before Decide's forced
   escalation, so it is the escalation truth. Record
   `factory/bin/decide outcome --job <job-id> --decision escalation --truth '{"escalate":<true when the independent verdict is ESCALATE, else false>}' 2>>factory/jobs/<job-id>/stderr.log`.
   If the Independent verdict line is missing, skip that outcome (the first line
   may be Decide's own forced verdict) and print
   `<job-id>: no independent verdict, escalation outcome skipped`. Always record
   `factory/bin/decide outcome --job <job-id> --decision triage --truth '{"risks":[<the ## Risks categories>]}' 2>>factory/jobs/<job-id>/stderr.log`.
4. **Decision.**
   - `ESCALATE` and the independent verdict is `ESCALATE` (or missing):
     **escalate** with source `approver` and, as the reason, the
     first item under `## For the human` (the first sentence after the
     Independent verdict line if that section is empty).
   - `ESCALATE` but the independent verdict is `APPROVE`: Decide or the rules
     forced it, and their `decide`/`rule` entries already cover it. Add no
     `approver` entry; go to **Needs human**.
   - `APPROVE` and `escalations` is empty: go to **Merge**.
   - `APPROVE` but `escalations` is not empty: go to **Needs human**. Never
     merge while `escalations` has entries.

### Merge

The merge happens in the main checkout, so it must be clean and on `main`:
`git status --porcelain -- . ':!factory/backlog.md'` prints nothing and
`git branch --show-current` prints `main`. `factory/backlog.md` is exempt: every
`/factory next` edits it, and job branches never touch it, so a dirty backlog
cannot conflict with the merge. If not, set `stage: "needs-human"` and tell the
user the job is approved but the main checkout is busy; they run
`/factory approve <job-id>` once it is clean. Otherwise:

```bash
git merge --no-ff factory/<job-id> -m "Merge factory/<job-id>"
factory/bin/fast-coder clean-stubs --job <job-id> 2>>factory/jobs/<job-id>/stderr.log
git worktree remove <worktree>
git branch -d factory/<job-id>
```

Set `stage: "merged"`. If `decide stuck` ever ran for this job, record
`factory/bin/decide outcome --job <job-id> --decision stuck --truth '{"escalate":<true when escalations has a rounds entry, else false>}' 2>>factory/jobs/<job-id>/stderr.log`.
If the merge fails, run `git merge --abort` (when a merge is in progress), set
`stage: "needs-human"` and tell the user why. `clean-stubs` deletes the empty
placeholder files (`.env*`, `.npmrc`, lockfiles, ...) Claude Code's command
sandbox leaves in a cwd; never use `--force`. If removing the worktree fails, the
job is still merged: tell the user what `git worktree remove` printed. Never push.

### Needs human

Set `stage: "needs-human"`. The branch and its worktree stay for the human to
inspect. Stop and tell the user which job is waiting, give the worktree path,
list every `escalations` entry as `<source>: <reason>`, and quote `decision.md`
(or the BLOCKED reason from `build.md`).

## /factory next

Read `factory/backlog.md` and take the first `- [ ]` item. Claim it with one
Edit that replaces its exact line with `- [x] <item> → claimed`. If the Edit
fails because the line changed, another session claimed it: re-read the backlog
and try the next `- [ ]` item. If none is left, say so and stop.

Run `/factory <item>`, and once step 1 has given it a job id, replace `claimed`
on its backlog line with the job id.

## /factory approve <job-id>

The job must be in `needs-human`. If `decide escalation` ever ran for this job,
record the human's verdict, which overrides the approver's:
`factory/bin/decide outcome --job <job-id> --decision escalation --truth '{"escalate":false}' 2>>factory/jobs/<job-id>/stderr.log`.
Then run **Merge** with that job id. This holds for every escalation source,
`budget` included.

## /factory rework <job-id> <note>

The job must be in `needs-human`.

1. Record outcomes. If `decide escalation` ever ran for this job:
   `factory/bin/decide outcome --job <job-id> --decision escalation --truth '{"escalate":true}' 2>>factory/jobs/<job-id>/stderr.log`.
   If `decide stuck` ever ran:
   `factory/bin/decide outcome --job <job-id> --decision stuck --truth '{"escalate":<true when escalations has a rounds or stuck entry, else false>}' 2>>factory/jobs/<job-id>/stderr.log`.
2. Write the note, verbatim, to `factory/jobs/<job-id>/rework-<k>.md`, where `k`
   is the next unused number.
3. Set `stage: "build"`, `escalations: []`, `fix_rounds: 0` and
   `cost.budget_base` to the current `mercury3_usd + decide_usd`. A rework run
   gets a fresh `MAX_RESUMES` and a fresh budget. If `tier` is `fast`, set it to `promoted` with
   `promoted_reason: "rework"`.
4. Spawn a fresh `builder` named `builder-<job-id>` with the prompt
   `<paths> Rework: read rework-<k>.md and address it, then update build.md for round N+1. Port: <port>.`
   (`N` is the job's last round.) When `tier` is `promoted`, start the prompt's
   task with
   `Promoted from the fast tier: read build.md, every round folder and fast-*.json transcripts.`
5. Continue from **Build check**, exactly like a new job.

## Reporting

Keep the user posted with one short line per step or transition, for example:

```
004-blog-reading-time: triage → fast, reviewers security/code, risks none
004-blog-reading-time: scout done ($0.01)
003-rate-limiting: round 2, security CHANGES, ui/code PASS, ux skipped → fast fixer 2 findings, builder 1
003-rate-limiting: decide stuck failed (OPENROUTER_API_KEY not set) → reference behaviour
003-rate-limiting: needs human (decide: auth; approver: APPROVE)
```

When the job ends, say whether it merged or needs a human, and why, and give
the Mercury and Decide spend from `cost`.
