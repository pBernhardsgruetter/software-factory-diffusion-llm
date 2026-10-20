#!/usr/bin/env bash
# Install the Mercury software factory kit into a target git repo.
# Usage: scripts/install.sh [--force] <target-repo>
set -euo pipefail

usage() { echo "usage: $(basename "$0") [--force] <target-repo>" >&2; }

force=0
target=""
for arg in "$@"; do
  case "$arg" in
    --force) force=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "error: unknown option: $arg" >&2; usage; exit 2 ;;
    *)
      if [ -n "$target" ]; then echo "error: more than one target given" >&2; usage; exit 2; fi
      target="$arg"
      ;;
  esac
done
if [ -z "$target" ]; then usage; exit 2; fi

kit="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../kit" && pwd)"

if [ ! -d "$target" ]; then
  echo "error: target is not a directory: $target" >&2
  exit 1
fi
target="$(cd "$target" && pwd)"
if ! git -C "$target" rev-parse --git-dir >/dev/null 2>&1; then
  echo "error: target is not a git repository: $target" >&2
  exit 1
fi
if [ -e "$target/factory" ] && [ "$force" -ne 1 ]; then
  echo "error: $target/factory already exists; re-run with --force to overwrite the kit's files" >&2
  exit 1
fi

# Never clobber a backlog the target already has, even with --force.
keep_backlog=0
if [ -f "$target/factory/backlog.md" ]; then keep_backlog=1; fi

mkdir -p "$target/.claude" "$target/factory"

if command -v rsync >/dev/null 2>&1; then
  rsync -a "$kit/.claude/" "$target/.claude/"
  extra=()
  if [ "$keep_backlog" -eq 1 ]; then extra+=(--exclude '/backlog.md'); fi
  rsync -a \
    --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' \
    --exclude '/jobs' "${extra[@]}" \
    "$kit/factory/" "$target/factory/"
else
  tar -C "$kit/.claude" -cf - . | tar -C "$target/.claude" -xf -
  extra=()
  if [ "$keep_backlog" -eq 1 ]; then extra+=(--exclude './backlog.md'); fi
  tar -C "$kit/factory" -cf - \
    --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' \
    --exclude './jobs' "${extra[@]}" . | tar -C "$target/factory" -xf -
fi

chmod +x "$target"/factory/bin/* 2>/dev/null || true

gitignore="$target/.gitignore"
touch "$gitignore"
# Runtime state and caches must never dirty the main checkout: the factory only
# merges into a clean one.
for line in /factory/jobs/ /factory/.venv/ /factory/tool-gate.jsonl \
    '/factory/**/__pycache__/' /factory/.pytest_cache/; do
  if ! grep -qxF "$line" "$gitignore"; then
    # make sure we start on a fresh line
    if [ -s "$gitignore" ] && [ -n "$(tail -c1 "$gitignore")" ]; then echo >> "$gitignore"; fi
    echo "$line" >> "$gitignore"
  fi
done

echo "Installed Mercury factory kit into $target"
echo "Next: export OPENROUTER_API_KEY in your shell, then run /factory <feature> in Claude Code."
