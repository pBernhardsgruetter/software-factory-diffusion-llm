#!/usr/bin/env bash
# Install the Mercury software factory kit into a target git repo.
# Usage: scripts/install.sh [--force] <target-repo>
#
# The target must be the TOP LEVEL of a git repo. Without --force the install
# refuses when <target>/factory exists or when it would overwrite any file in
# <target>/.claude (settings.json, agents/<kit agent>.md, skills/factory/*).
# With --force those .claude files are first copied to
# <target>/.claude/.factory-backup-<timestamp>/ (gitignored).
# Written for bash 3.2 (macOS) as well: no empty-array expansions under set -u.
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
target="$(cd "$target" && pwd -P)"
if ! top="$(git -C "$target" rev-parse --show-toplevel 2>/dev/null)"; then
  echo "error: target is not a git repository: $target" >&2
  exit 1
fi
top="$(cd "$top" && pwd -P)"
if [ "$top" != "$target" ]; then
  echo "error: $target is not the top level of its git repository ($top); install there" >&2
  exit 1
fi

# --- pre-flight: refuse before writing anything ---------------------------
if [ -e "$target/factory" ] && [ "$force" -ne 1 ]; then
  echo "error: $target/factory already exists; re-run with --force to overwrite the kit's files" >&2
  exit 1
fi

# Kit files that already exist in the target's .claude (they would be overwritten).
clash=()
nclash=0  # a plain counter: no array-length expansion under set -u (bash 3.2)
while IFS= read -r rel; do
  rel="${rel#./}"
  if [ -e "$target/.claude/$rel" ] || [ -L "$target/.claude/$rel" ]; then
    clash+=("$rel")
    nclash=$((nclash + 1))
  fi
done < <(cd "$kit/.claude" && find . -type f | LC_ALL=C sort)

if [ "$nclash" -gt 0 ] && [ "$force" -ne 1 ]; then
  echo "error: the kit would overwrite these files in $target/.claude:" >&2
  for rel in ${clash[@]+"${clash[@]}"}; do echo "  .claude/$rel" >&2; done
  echo "Merge them by hand, or re-run with --force (the originals are backed up to" >&2
  echo ".claude/.factory-backup-<timestamp>/ first)." >&2
  exit 1
fi

# --- back up what --force overwrites -------------------------------------
backup=""
if [ "$nclash" -gt 0 ]; then
  base="$target/.claude/.factory-backup-$(date +%Y%m%d-%H%M%S)"
  backup="$base"
  n=1
  while [ -e "$backup" ]; do backup="$base-$n"; n=$((n + 1)); done
  for rel in ${clash[@]+"${clash[@]}"}; do
    mkdir -p "$backup/$(dirname "$rel")"
    cp -P "$target/.claude/$rel" "$backup/$rel"
  done
fi

# Never clobber a backlog the target already has, even with --force.
keep_backlog=0
if [ -f "$target/factory/backlog.md" ]; then keep_backlog=1; fi

mkdir -p "$target/.claude" "$target/factory"

extra=()
if command -v rsync >/dev/null 2>&1; then
  rsync -a "$kit/.claude/" "$target/.claude/"
  if [ "$keep_backlog" -eq 1 ]; then extra+=(--exclude '/backlog.md'); fi
  rsync -a \
    --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' \
    --exclude '/jobs' ${extra[@]+"${extra[@]}"} \
    "$kit/factory/" "$target/factory/"
else
  tar -C "$kit/.claude" -cf - . | tar -C "$target/.claude" -xf -
  if [ "$keep_backlog" -eq 1 ]; then extra+=(--exclude './backlog.md'); fi
  tar -C "$kit/factory" -cf - \
    --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' \
    --exclude './jobs' ${extra[@]+"${extra[@]}"} . | tar -C "$target/factory" -xf -
fi

chmod +x "$target"/factory/bin/* 2>/dev/null || true

gitignore="$target/.gitignore"
touch "$gitignore"
# Runtime state, caches and install backups must never dirty the main checkout:
# the factory only merges into a clean one.
for line in /factory/jobs/ /factory/.venv/ /factory/tool-gate.jsonl \
    '/factory/**/__pycache__/' /factory/.pytest_cache/ '/.claude/.factory-backup-*/'; do
  if ! grep -qxF "$line" "$gitignore"; then
    # make sure we start on a fresh line
    if [ -s "$gitignore" ] && [ -n "$(tail -c1 "$gitignore")" ]; then echo >> "$gitignore"; fi
    echo "$line" >> "$gitignore"
  fi
done

echo "Installed Mercury factory kit into $target"
if [ -n "$backup" ]; then
  echo "Backed up the .claude files it replaced to ${backup#"$target"/}/ (merge back what you need)."
fi
echo "Next: export OPENROUTER_API_KEY in your shell, then run /factory <feature> in Claude Code."
