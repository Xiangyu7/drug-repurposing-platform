#!/usr/bin/env bash
set -Eeuo pipefail

# ============================================================
# start_day1_aliyun.sh — Launch parallel runners on cloud server
# ============================================================
#
# Starts two runners in background:
#   1. dual runner   — Direction A + B for diseases in dual list
#   2. origin runner — Direction B only for diseases in origin list
#
# Pre-flight checks:
#   - Disk space ≥ 5 GB
#   - Ollama reachable (warns but doesn't block)
#   - Network connectivity (CT.gov API)
#   - No existing runners with same lock names
# ============================================================

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="${ROOT_DIR}/ops/run_24x7_all_directions.sh"
DUAL_LIST="${DUAL_LIST:-${ROOT_DIR}/ops/disease_list_day1_dual.txt}"
ORIGIN_LIST="${ORIGIN_LIST:-${ROOT_DIR}/ops/disease_list_day1_origin.txt}"

LOG_DIR="${ROOT_DIR}/logs/day1_aliyun"
STATE_DIR="${ROOT_DIR}/runtime/state"
mkdir -p "${LOG_DIR}" "${STATE_DIR}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

ok()   { printf "${GREEN}+${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}!${NC} %s\n" "$*"; }
fail() { printf "${RED}x${NC} %s\n" "$*"; exit 1; }

: "${SLEEP_SECONDS:=300}"
: "${TOPN_CROSS:=50}"
: "${TOPN_ORIGIN:=80}"
: "${STRICT_CONTRACT:=1}"
: "${RETENTION_DAYS:=7}"
: "${MAX_CYCLES:=0}"

# ── Pre-flight checks ──

echo ""
echo "Running pre-flight checks..."
echo ""

# 1. Required files
if [[ ! -f "${RUNNER}" ]]; then
  fail "Runner not found: ${RUNNER}"
fi
if [[ ! -f "${DUAL_LIST}" ]]; then
  fail "Dual list not found: ${DUAL_LIST}"
fi
if [[ ! -f "${ORIGIN_LIST}" ]]; then
  fail "Origin list not found: ${ORIGIN_LIST}"
fi
ok "Required files found"

# 2. Disk space
avail_gb=0
avail_kb="$(df -k "${ROOT_DIR}" 2>/dev/null | awk 'NR==2 {print $4}' || echo "0")"
if [[ -n "${avail_kb}" && "${avail_kb}" != "0" ]]; then
  avail_gb=$(( avail_kb / 1024 / 1024 ))
fi
if [[ "${avail_gb}" -lt 5 ]]; then
  fail "Insufficient disk space: ${avail_gb}GB free (need ≥5GB). Run: bash ops/cleanup.sh --all 7"
fi
ok "Disk space: ${avail_gb}GB free"

# 3. Network (CT.gov)
if curl -sf --max-time 10 "https://clinicaltrials.gov/api/v2/studies?pageSize=1" >/dev/null 2>&1; then
  ok "Network: CT.gov API reachable"
else
  warn "Network: CT.gov API unreachable (pipeline may fail on screen_drugs)"
fi

# 4. Ollama
OLLAMA_HOST="${OLLAMA_HOST:-http://localhost:11434}"
if curl -sf --max-time 5 "${OLLAMA_HOST}/api/tags" >/dev/null 2>&1; then
  ok "Ollama: running at ${OLLAMA_HOST}"
else
  warn "Ollama: not reachable at ${OLLAMA_HOST} (LLM steps will fail)"
  warn "  Start with: ollama serve &"
fi

# 5. No existing runners with same lock names
for lock_name in dual_day1 origin_day1; do
  lock_file="${STATE_DIR}/runner_${lock_name}.lock"
  if [[ -f "${lock_file}" ]]; then
    old_pid="$(cat "${lock_file}" 2>/dev/null || true)"
    if [[ -n "${old_pid}" ]] && kill -0 "${old_pid}" 2>/dev/null; then
      fail "Runner '${lock_name}' already active (PID=${old_pid}). Stop first: bash ops/restart_runner.sh --stop"
    else
      warn "Stale lock file removed: ${lock_file}"
      rm -f "${lock_file}"
    fi
  fi
done
ok "No conflicting runners"

# 6. Disease list validation
for list_file in "${DUAL_LIST}" "${ORIGIN_LIST}"; do
  line_num=0
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line_num=$((line_num + 1))
    [[ -z "${line}" || "${line:0:1}" == "#" ]] && continue
    if ! echo "${line}" | grep -q '|'; then
      fail "$(basename "${list_file}"):${line_num}: missing '|' separator: ${line}"
    fi
  done < "${list_file}"
done
ok "Disease list format valid"

echo ""
ok "All pre-flight checks passed"
echo ""

# ── Launch runners ──

ts="$(date '+%Y%m%d_%H%M%S')"
dual_log="${LOG_DIR}/dual_${ts}.log"
origin_log="${LOG_DIR}/origin_${ts}.log"

nohup env \
  RUN_MODE=dual \
  LOCK_NAME=dual_day1 \
  SLEEP_SECONDS="${SLEEP_SECONDS}" \
  TOPN_CROSS="${TOPN_CROSS}" \
  TOPN_ORIGIN="${TOPN_ORIGIN}" \
  STRICT_CONTRACT="${STRICT_CONTRACT}" \
  RETENTION_DAYS="${RETENTION_DAYS}" \
  MAX_CYCLES="${MAX_CYCLES}" \
  bash "${RUNNER}" "${DUAL_LIST}" > "${dual_log}" 2>&1 &
dual_pid=$!

nohup env \
  RUN_MODE=origin_only \
  LOCK_NAME=origin_day1 \
  SLEEP_SECONDS="${SLEEP_SECONDS}" \
  TOPN_CROSS="${TOPN_CROSS}" \
  TOPN_ORIGIN="${TOPN_ORIGIN}" \
  STRICT_CONTRACT="${STRICT_CONTRACT}" \
  RETENTION_DAYS="${RETENTION_DAYS}" \
  MAX_CYCLES="${MAX_CYCLES}" \
  bash "${RUNNER}" "${ORIGIN_LIST}" > "${origin_log}" 2>&1 &
origin_pid=$!

echo "Started day-1 runners:"
echo "  dual   PID=${dual_pid}  log=${dual_log}"
echo "  origin PID=${origin_pid}  log=${origin_log}"
echo ""
echo "Monitor:"
echo "  tail -f ${dual_log}"
echo "  tail -f ${origin_log}"
echo "  bash ops/check_status.sh"
echo ""
echo "Stop:"
echo "  bash ops/restart_runner.sh --stop"
