#!/usr/bin/env bash
# Container entrypoint for the quality-ratchet loop.
#
# Clones the base branch fresh, then runs the loop agent in real mode. The
# agent itself computes the baseline W0, sets target = max(0, W0 - WARN_STEP),
# fixes warnings via the model, and opens a PR — so this script stays thin.
#
# Required env:
#   REPO_URL         e.g. https://github.com/bilalurrahman/HattanMedicalHistory.git
#   GH_TOKEN         token used for clone + `gh pr create`
# Optional env (defaults shown):
#   GIT_BRANCH=quality  LOOP_PR_BASE=quality  LOOP_BRANCH_PREFIX=glm_quality
#   WARN_STEP=10  MAX_ITERS=5  LOOP_MODEL=glm-5.2:cloud
#   OLLAMA_HOST=http://host.docker.internal:11434
set -euo pipefail

: "${REPO_URL:?REPO_URL is required}"
: "${GH_TOKEN:?GH_TOKEN is required}"
BRANCH="${GIT_BRANCH:-quality}"
WORK="${WORKDIR:-/work/repo}"

export GH_TOKEN
git config --global credential.helper store
printf 'https://x-access-token:%s@github.com\n' "$GH_TOKEN" > "$HOME/.git-credentials"

echo "==> cloning $REPO_URL ($BRANCH)"
rm -rf "$WORK"; mkdir -p "$(dirname "$WORK")"
git clone --branch "$BRANCH" --depth 50 "$REPO_URL" "$WORK"

# Keep the loop's own seeded/config files out of the PR diff (deferred issue in
# the plan): everything that isn't target source.
cat >> "$WORK/.git/info/exclude" <<'EOF'
/STATE.md
/logs/
/.glm-loop/
EOF

echo "==> running quality loop"
exec python3 /app/agent/loop_agent.py \
  --repo "$WORK" \
  --sln "${LOOP_SLN:-backend/HattanHealthTracker.sln}" \
  --base-branch "$BRANCH" \
  --pr-base "${LOOP_PR_BASE:-$BRANCH}" \
  --branch-prefix "${LOOP_BRANCH_PREFIX:-glm_quality}" \
  --step "${WARN_STEP:-10}" \
  --max-iters "${MAX_ITERS:-5}" \
  --model "${LOOP_MODEL:-glm-5.2:cloud}" \
  --ollama-host "${OLLAMA_HOST:-http://host.docker.internal:11434}" \
  --port "${UI_PORT:-8787}" --host 0.0.0.0 \
  --open-pr --keep-alive
