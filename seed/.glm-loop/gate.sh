#!/usr/bin/env bash
# ── Quality ratchet gate ─────────────────────────────────────────────────
# Pure comparison: build the solution, count UNIQUE analyzer warnings, and
# succeed (exit 0) only when the count is at or under WARN_TARGET.
#
# WARN_TARGET is computed once per run by the entrypoint from the freshly
# cloned base branch (target = max(0, W0 - WARN_STEP)). The base branch's
# current warning count is the only state — no stored counter.
set -uo pipefail

SLN="${LOOP_SLN:-backend/HattanHealthTracker.sln}"
OUT="$(mktemp)"

# --no-incremental: force a full recompile so EVERY project re-emits its
# warnings. An incremental/up-to-date build prints none and would read 0.
dotnet build "$SLN" --nologo --no-incremental 2>&1 | tee "$OUT" >/dev/null

# If the build itself failed (errors, not warnings), the gate must fail hard —
# the loop is not allowed to leave the tree non-building.
if grep -qE ': error [A-Z]+[0-9]+' "$OUT"; then
  echo "gate: BUILD FAILED (errors present)" >&2
  grep -E ': error [A-Z]+[0-9]+' "$OUT" | head -20 >&2
  rm -f "$OUT"
  exit 2
fi

# Count unique warnings keyed by file(line,col):code so multi-project builds
# and restore chatter don't inflate the number.
count=$(grep -oE '[^ ]+\.cs\([0-9]+,[0-9]+\): warning [A-Z]+[0-9]+' "$OUT" \
        | sort -u | wc -l | tr -d ' ')
target="${WARN_TARGET:-0}"
rm -f "$OUT"

echo "warnings=$count target=$target"
[ "$count" -le "$target" ]
