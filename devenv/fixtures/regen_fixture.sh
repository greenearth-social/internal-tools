#!/usr/bin/env bash
# regen_fixture.sh — regenerate the dev fixture from prod Elasticsearch.
#
# A full run is ~25 minutes of queries over a kubectl port-forward, so this
# wraps generate_sample.py with the things that make an unattended run
# survivable:
#
#   - a supervised port-forward that restarts itself when the tunnel drops
#     (kubectl port-forward dies routinely on long runs);
#   - output staged in a scratch directory, so the fixtures/data/ that
#     `devctl seed` reads is untouched until you deliberately swap it in —
#     you can keep developing, re-seeding, and restarting containers while
#     this runs;
#   - verification that the result is usable before anyone publishes it,
#     including the check that persona DIDs resolve to real accounts.
#
# It deliberately stops short of publishing. `publish_fixture.sh` uploads a
# public GitHub release, which is the one irreversible step here — run it
# yourself once you've read the verification output.
#
# Usage:
#   ./regen_fixture.sh [--hours N] [--out DIR] [-- <generate_sample.py args>]
#
# Requires: kubectl context on the prod cluster, GE_ELASTICSEARCH_API_KEY
# (a read-only key is enough), python3.
set -euo pipefail

FIXTURES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${GE_REGEN_STATE_DIR:-$FIXTURES_DIR/.regen}"
OUT_DIR="$FIXTURES_DIR/data.staging"
NAMESPACE="${GE_PROD_NAMESPACE:-greenearth-prod}"
ES_SERVICE="${GE_PROD_ES_SERVICE:-svc/greenearth-es-internal-lb}"
LOCAL_PORT="${GE_REGEN_ES_PORT:-9200}"
HOURS="48"
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
  --hours)
    HOURS="$2"
    shift 2
    ;;
  --out)
    OUT_DIR="$2"
    shift 2
    ;;
  --)
    shift
    EXTRA_ARGS=("$@")
    break
    ;;
  *)
    echo "regen_fixture: unknown argument: $1" >&2
    exit 2
    ;;
  esac
done

log() { echo "[$(date -u +%H:%M:%S)] $*"; }
die() {
  log "FATAL: $*"
  exit 1
}

mkdir -p "$STATE_DIR"
PF_LOG="$STATE_DIR/port-forward.log"
PF_PID_FILE="$STATE_DIR/port-forward.pid"

# --- preflight ------------------------------------------------------------
# Everything that can fail instantly is checked before the 25-minute part.

command -v kubectl >/dev/null || die "kubectl is required"
command -v python3 >/dev/null || die "python3 is required"

ctx="$(kubectl config current-context 2>/dev/null || true)"
[[ -n "$ctx" ]] || die "no kubectl context set"
log "kubectl context: $ctx"

if [[ -z "${GE_ELASTICSEARCH_API_KEY:-}" ]]; then
  # Fall back to the api checkout's .env, which normally holds a read-only key.
  api_env="$FIXTURES_DIR/../../../api/.env"
  if [[ -f "$api_env" ]]; then
    key="$(sed -n 's/^GE_ELASTICSEARCH_API_KEY=//p' "$api_env" | head -1 | tr -d '"'"'"'')"
    [[ -n "$key" ]] && export GE_ELASTICSEARCH_API_KEY="$key" &&
      log "using GE_ELASTICSEARCH_API_KEY from api/.env"
  fi
fi
[[ -n "${GE_ELASTICSEARCH_API_KEY:-}" ]] ||
  die "GE_ELASTICSEARCH_API_KEY is not set (export it, or put it in api/.env)"

export GE_ELASTICSEARCH_URL="https://localhost:${LOCAL_PORT}"

# --- supervised port-forward ----------------------------------------------
# kubectl port-forward exits on its own when the connection resets. Keep
# restarting it for as long as this script is alive; generate_sample.py retries
# individual requests across the gap (see EsClient.search).

stop_port_forward() {
  if [[ -f "$PF_PID_FILE" ]]; then
    local pid
    pid="$(cat "$PF_PID_FILE")"
    kill "$pid" 2>/dev/null || true
    pkill -P "$pid" 2>/dev/null || true
    rm -f "$PF_PID_FILE"
  fi
}
trap stop_port_forward EXIT

stop_port_forward
: >"$PF_LOG"
(
  while true; do
    kubectl port-forward "$ES_SERVICE" "${LOCAL_PORT}:9200" -n "$NAMESPACE" >>"$PF_LOG" 2>&1 || true
    echo "--- port-forward exited, restarting ---" >>"$PF_LOG"
    sleep 2
  done
) &
echo $! >"$PF_PID_FILE"
log "port-forward supervisor started (pid $(cat "$PF_PID_FILE")), log: $PF_LOG"

# Wait for the tunnel to answer before starting the long run.
log "waiting for prod ES to answer on ${LOCAL_PORT}..."
ready=0
for _ in $(seq 1 60); do
  code="$(curl -sk -o /dev/null -w "%{http_code}" \
    -H "Authorization: ApiKey ${GE_ELASTICSEARCH_API_KEY}" \
    "https://localhost:${LOCAL_PORT}/_cluster/health" 2>/dev/null || true)"
  if [[ "$code" == "200" ]]; then
    ready=1
    break
  fi
  # 401/403 means the tunnel is up but the key is wrong — retrying won't help.
  if [[ "$code" == "401" || "$code" == "403" ]]; then
    die "prod ES rejected the API key (HTTP $code) — check GE_ELASTICSEARCH_API_KEY"
  fi
  sleep 2
done
[[ "$ready" == "1" ]] || die "prod ES did not become reachable — see $PF_LOG"
log "prod ES is reachable"

# --- generate -------------------------------------------------------------
# Staged output: fixtures/data/ stays as-is so seeding keeps working while
# this runs.

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"
log "generating into $OUT_DIR (window: ${HOURS}h) — this takes ~25 minutes"

python3 "$FIXTURES_DIR/generate_sample.py" \
  --source prod-es \
  --window-hours "$HOURS" \
  --out "$OUT_DIR" \
  ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}

log "generation complete"

# --- verify ---------------------------------------------------------------

log "verifying the generated fixture..."
python3 "$FIXTURES_DIR/verify_fixture.py" "$OUT_DIR" || die "verification failed"

cat <<EOF

[$(date -u +%H:%M:%S)] Fixture staged and verified: $OUT_DIR

Next steps (both are yours to run — neither happens automatically):

  # Use it locally:
  rm -rf "$FIXTURES_DIR/data" && mv "$OUT_DIR" "$FIXTURES_DIR/data"
  cd "$FIXTURES_DIR/.." && ./devctl seed

  # Publish it for the team (creates a public GitHub release):
  "$FIXTURES_DIR/publish_fixture.sh"
EOF
