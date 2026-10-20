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

This copies `kit/.claude` and `kit/factory` into the target, which must be the
top level of a git repo (a subdirectory is refused), and adds the factory's
runtime state and caches to its `.gitignore`. It refuses to run if
`<target>/factory` exists, or if it would overwrite `.claude/settings.json` or a
same-named file in `.claude/agents/` or `.claude/skills/factory/`. Pass `--force`
to overwrite: the replaced `.claude` files are first copied to
`.claude/.factory-backup-<timestamp>/` (gitignored) so you can merge your own
hooks and agents back. An existing `factory/backlog.md` is always kept.

## Docs

- Specs: `docs/superpowers/specs/`
- Plans: `docs/superpowers/plans/`
