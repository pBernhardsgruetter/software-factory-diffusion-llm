#!/usr/bin/env bash
# Create a throwaway git repo from sample-app/ with the factory kit installed
# and the e2e backlog seeded.
# Usage: scripts/make-sandbox.sh [dir]   (default: ../factory-sandbox)
set -euo pipefail

repo="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
dir="${1:-$repo/../factory-sandbox}"

if [ -e "$dir" ]; then
  echo "error: $dir already exists; remove it or pass another directory" >&2
  exit 1
fi

mkdir -p "$dir"
dir="$(cd "$dir" && pwd)"

# Copy the app without build artifacts or the seed file itself.
tar -C "$repo/sample-app" \
  --exclude './node_modules' --exclude './dist' --exclude './BACKLOG.seed.md' \
  -cf - . | tar -C "$dir" -xf -

git -C "$dir" init -q -b main
if [ -z "$(git config --global user.email || true)" ]; then
  git -C "$dir" config user.email "sandbox@example.com"
fi
if [ -z "$(git config --global user.name || true)" ]; then
  git -C "$dir" config user.name "Factory Sandbox"
fi

(cd "$dir" && npm install --no-audit --no-fund)

"$repo/scripts/install.sh" "$dir"

# Seed the backlog.
seed="$repo/sample-app/BACKLOG.seed.md"
backlog="$dir/factory/backlog.md"
if [ -s "$backlog" ] && [ -n "$(tail -c1 "$backlog")" ]; then echo >> "$backlog"; fi
while IFS= read -r item || [ -n "$item" ]; do
  [ -n "${item//[[:space:]]/}" ] || continue
  printf -- '- [ ] %s\n' "$item" >> "$backlog"
done < "$seed"

git -C "$dir" add -A
git -C "$dir" commit -q -m "Initial sandbox"

echo "Sandbox ready at $dir"
