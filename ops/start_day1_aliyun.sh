#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="${ROOT_DIR}/ops/run_24x7_all_directions.sh"
DUAL_LIST="${DUAL_LIST:-${ROOT_DIR}/ops/disease_list_day1_dual.txt}"
ORIGIN_LIST="${ORIGIN_LIST:-${ROOT_DIR}/ops/disease_list_day1_origin.txt}"

LOG_DIR="${ROOT_DIR}/logs/day1_aliyun"
mkdir -p "${LOG_DIR}"

: "${SLEEP_SECONDS:=300}"
: "${TOPN_CROSS:=50}"
: "${TOPN_ORIGIN:=80}"
: "${STRICT_CONTRACT:=1}"
: "${RETENTION_DAYS:=7}"
: "${MAX_CYCLES:=0}"

if [[ ! -f "${RUNNER}" ]]; then
  echo "ERROR: runner not found: ${RUNNER}" >&2
  exit 1
fi
if [[ ! -f "${DUAL_LIST}" ]]; then
  echo "ERROR: dual list not found: ${DUAL_LIST}" >&2
  exit 1
fi
if [[ ! -f "${ORIGIN_LIST}" ]]; then
  echo "ERROR: origin list not found: ${ORIGIN_LIST}" >&2
  exit 1
fi

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
echo "  dual pid=${dual_pid} log=${dual_log}"
echo "  origin pid=${origin_pid} log=${origin_log}"
