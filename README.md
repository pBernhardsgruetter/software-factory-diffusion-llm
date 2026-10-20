# Mercury software factory

A Claude Code software factory (one `/factory` skill, seven subagents and a
dashboard) extended with a Python package, `factorylib`, that adds a Mercury
decision client, a tool-gate hook and a fast coder. Everything that gets
installed into a target repo lives in `kit/`.

## Prerequisites

- `uv` (runs the `factorylib` Python project)
- `node` >= 22
- `git`
- the `agent-browser` CLI
- `OPENROUTER_API_KEY` exported in your shell. Never commit it.

## Install

```sh
scripts/install.sh <target-repo>
```

This copies `kit/.claude` and `kit/factory` into the target (which must be a git
repo) and adds `/factory/jobs/`, `/factory/.venv/` and `/factory/tool-gate.jsonl`
to its `.gitignore`. It refuses to run if `<target>/factory` already exists; pass
`--force` to overwrite the kit's files (an existing `factory/backlog.md` is kept).

## Docs

- Specs: `docs/superpowers/specs/`
- Plans: `docs/superpowers/plans/`
