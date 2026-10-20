#!/usr/bin/env bash
# S2 spike (throwaway): drive Mercury 3 via OpenRouter inside headless `claude -p`.
# Usage: zsh -ic 'spikes/s2_mercury_claude.sh <run-number> [resume]'   (needs OPENROUTER_API_KEY in env)
# The OpenRouter env vars are set ONLY on the child command line. The key is never printed/logged.
set -u
N="${1:?run number}"
MODE="${2:-fix}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$HERE/s2-repo$N"
MODEL="inception/mercury-3"

if [ "$MODE" = "fix" ]; then
  rm -rf "$REPO"; mkdir -p "$REPO"; cd "$REPO"
  git init -q
  git config user.email spike@example.com; git config user.name "S2 Spike"
  printf 'node_modules\nhook.log\nout*.json\nsession_id\n' > .git/info/exclude
  cat > add.js <<'JS'
function add(a, b) {
  return a - b;
}
module.exports = { add };
JS
  cat > add.test.js <<'JS'
const test = require('node:test');
const assert = require('node:assert');
const { add } = require('./add');
test('add', () => { assert.strictEqual(add(2, 3), 5); });
JS
  git add add.js add.test.js && git commit -q -m init
  python3 -c 'import uuid;print(uuid.uuid4())' > session_id
fi
cd "$REPO"
SID="$(cat session_id)"
HOOKLOG="$REPO/hook.log"
# Hook appends only the stdin event JSON (never env vars) to hook.log
SETTINGS="{\"hooks\":{\"PreToolUse\":[{\"matcher\":\"*\",\"hooks\":[{\"type\":\"command\",\"command\":\"cat >> $HOOKLOG; echo >> $HOOKLOG\"}]}]}}"

if [ "$MODE" = "fix" ]; then
  PROMPT="Fix the bug in add.js, run node --test, commit with message 'fix add'."
  SESS=(--session-id "$SID"); OUT=out.json
else
  PROMPT="Now also add a test for negative numbers and commit."
  SESS=(--resume "$SID"); OUT=out_resume.json
fi

key_usage() { curl -s https://openrouter.ai/api/v1/key -H "Authorization: Bearer $OPENROUTER_API_KEY" \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["data"]["usage"])' 2>/dev/null; }
U0=$(key_usage)
START=$(date +%s)
env ANTHROPIC_BASE_URL=https://openrouter.ai/api ANTHROPIC_AUTH_TOKEN="$OPENROUTER_API_KEY" ANTHROPIC_API_KEY= \
    ANTHROPIC_DEFAULT_HAIKU_MODEL=$MODEL CLAUDE_CODE_SUBAGENT_MODEL=$MODEL \
  timeout 600 claude -p --model $MODEL "${SESS[@]}" --permission-mode dontAsk \
    --allowedTools "Read,Grep,Glob,Edit,Write,Bash" --disallowedTools "Agent,WebFetch,WebSearch" \
    --setting-sources local --settings "$SETTINGS" --output-format json "$PROMPT" \
    < /dev/null > "$OUT" 2> "${OUT%.json}.err"
RC=$?
END=$(date +%s)
sleep 5; U1=$(key_usage)

TESTS=FAIL; node --test >/dev/null 2>&1 && TESTS=PASS
COMMITS=$(git log --oneline | wc -l)
HOOK=$( [ -s hook.log ] && grep -c '"hook_event_name"' hook.log || echo 0 )
echo "run=$N mode=$MODE rc=$RC wall=$((END-START))s tests=$TESTS commits=$COMMITS hook_events=$HOOK or_usage_before=$U0 after=$U1"
python3 - "$OUT" <<'PY'
import json,sys
try:
    d=json.load(open(sys.argv[1]))
    print({k:d.get(k) for k in ("is_error","subtype","num_turns","total_cost_usd","duration_ms","session_id")})
    print("result:", str(d.get("result"))[:300])
except Exception as e: print("no json:", e)
PY
git log --oneline | head -5
