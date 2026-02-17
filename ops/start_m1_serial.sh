#!/usr/bin/env bash
set -Eeuo pipefail

# ═══════════════════════════════════════════════════════════════════
# start_m1_serial.sh — M1 Mac 串行启动 (省内存)
# ═══════════════════════════════════════════════════════════════════
#
# 与 start_day1_aliyun.sh 不同，这个脚本是串行的：
#   第一轮: dual list (7个疾病) → Direction A + B
#   第二轮: B-only list (9个疾病) → 只跑 Direction B
#
# 为什么不并行？M1 16GB 内存有限，两个 runner 同时跑 Ollama 会 OOM。
#
# 用法:
#   bash ops/start_m1_serial.sh              # 后台跑 (nohup)
#   bash ops/start_m1_serial.sh --foreground # 前台跑 (可看输出)
#   bash ops/start_m1_serial.sh --dry-run    # 只打印计划不执行
#
# 预计耗时:
#   Dual (7 diseases × ~60min)  ≈ 7 小时
#   B-only (9 diseases × ~40min) ≈ 6 小时
#   总计 ≈ 13 小时
# ═══════════════════════════════════════════════════════════════════

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="${ROOT_DIR}/ops/run_24x7_all_directions.sh"
DUAL_LIST="${ROOT_DIR}/ops/disease_list_day1_dual.txt"
BONLY_LIST="${ROOT_DIR}/ops/disease_list_b_only.txt"

LOG_DIR="${ROOT_DIR}/logs/m1_serial"
mkdir -p "${LOG_DIR}"

# Settings (M1 优化)
export SLEEP_SECONDS="${SLEEP_SECONDS:-60}"
export TOPN_CROSS="${TOPN_CROSS:-50}"
export TOPN_ORIGIN="${TOPN_ORIGIN:-80}"
export STRICT_CONTRACT="${STRICT_CONTRACT:-1}"
export RETENTION_DAYS="${RETENTION_DAYS:-7}"
export MAX_CYCLES=1
export DSMETA_CLEANUP="${DSMETA_CLEANUP:-1}"

MODE="${1:-}"

# ── Validation ────────────────────────────────────────────────

for f in "${RUNNER}" "${DUAL_LIST}" "${BONLY_LIST}"; do
  if [[ ! -f "${f}" ]]; then
    echo "ERROR: not found: ${f}" >&2
    exit 1
  fi
done

# Check Ollama
OLLAMA_HOST="$(grep '^OLLAMA_HOST=' "${ROOT_DIR}/LLM+RAG证据工程/.env" 2>/dev/null | cut -d= -f2 || echo "http://localhost:11434")"
if ! curl -sf --max-time 5 "${OLLAMA_HOST}/api/tags" >/dev/null 2>&1; then
  echo "ERROR: Ollama not running at ${OLLAMA_HOST}" >&2
  echo "  Start it: ollama serve" >&2
  exit 1
fi

n_dual="$(grep -v '^\s*#' "${DUAL_LIST}" | grep -v '^\s*$' | wc -l | tr -d ' ')"
n_bonly="$(grep -v '^\s*#' "${BONLY_LIST}" | grep -v '^\s*$' | wc -l | tr -d ' ')"

ts="$(date '+%Y%m%d_%H%M%S')"

echo "╔═══════════════════════════════════════════════╗"
echo "║    Drug Repurposing — M1 Serial Runner        ║"
echo "╚═══════════════════════════════════════════════╝"
echo ""
echo "  第一轮: Dual (A+B)   ${n_dual} 个疾病"
echo "  第二轮: B-only        ${n_bonly} 个疾病"
echo "  总计:                 $((n_dual + n_bonly)) 个疾病"
echo "  Ollama: ${OLLAMA_HOST} ✅"
echo "  Cleanup: DSMETA_CLEANUP=${DSMETA_CLEANUP}"
echo ""

if [[ "${MODE}" == "--dry-run" ]]; then
  echo "[DRY RUN] Would run:"
  echo "  1) RUN_MODE=dual bash ${RUNNER} ${DUAL_LIST}"
  echo "  2) RUN_MODE=origin_only bash ${RUNNER} ${BONLY_LIST}"
  exit 0
fi

# ── Run function ──────────────────────────────────────────────

run_serial() {
  local start_time
  start_time="$(date '+%s')"

  # ── Round 1: Dual (A+B) ──
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  第一轮: Dual (Direction A + B) — ${n_dual} 个疾病"
  echo "  开始时间: $(date '+%Y-%m-%d %H:%M:%S')"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""

  local dual_log="${LOG_DIR}/dual_${ts}.log"
  env \
    RUN_MODE=dual \
    LOCK_NAME=m1_dual \
    MAX_CYCLES=1 \
    bash "${RUNNER}" "${DUAL_LIST}" > "${dual_log}" 2>&1
  local dual_rc=$?

  if [[ "${dual_rc}" -eq 0 ]]; then
    echo "[$(date '+%H:%M:%S')] ✅ 第一轮完成 (Dual)"
  else
    echo "[$(date '+%H:%M:%S')] ⚠️  第一轮有错误 (exit=${dual_rc})，继续第二轮..."
  fi

  # ── Round 2: B-only ──
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  第二轮: Origin-only (Direction B) — ${n_bonly} 个疾病"
  echo "  开始时间: $(date '+%Y-%m-%d %H:%M:%S')"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""

  local bonly_log="${LOG_DIR}/bonly_${ts}.log"
  env \
    RUN_MODE=origin_only \
    LOCK_NAME=m1_bonly \
    MAX_CYCLES=1 \
    bash "${RUNNER}" "${BONLY_LIST}" > "${bonly_log}" 2>&1
  local bonly_rc=$?

  if [[ "${bonly_rc}" -eq 0 ]]; then
    echo "[$(date '+%H:%M:%S')] ✅ 第二轮完成 (B-only)"
  else
    echo "[$(date '+%H:%M:%S')] ⚠️  第二轮有错误 (exit=${bonly_rc})"
  fi

  # ── Summary ──
  local end_time
  end_time="$(date '+%s')"
  local elapsed=$(( (end_time - start_time) / 60 ))

  echo ""
  echo "╔═══════════════════════════════════════════════╗"
  echo "║  运行完成!                                     ║"
  echo "╚═══════════════════════════════════════════════╝"
  echo ""
  echo "  总耗时: ${elapsed} 分钟"
  echo "  Dual 日志: ${dual_log}"
  echo "  B-only 日志: ${bonly_log}"
  echo ""
  echo "  查看结果: bash ops/check_status.sh"
  echo "  查看失败: bash ops/check_status.sh --failures"
}

# ── Launch ────────────────────────────────────────────────────

if [[ "${MODE}" == "--foreground" ]]; then
  run_serial
else
  # Background mode — 用 nohup 重新调自己，带 --foreground 参数
  local_log="${LOG_DIR}/m1_serial_${ts}.log"
  nohup bash "${BASH_SOURCE[0]}" --foreground > "${local_log}" 2>&1 &
  pid=$!

  echo "  🚀 后台启动成功!"
  echo ""
  echo "  PID: ${pid}"
  echo "  日志: ${local_log}"
  echo ""
  echo "  监控命令:"
  echo "    tail -f ${local_log}                    # 看主日志"
  echo "    bash ops/check_status.sh                # 看状态概览"
  echo ""
  echo "  停止命令:"
  echo "    kill ${pid}"
  echo ""

  # Save PID
  mkdir -p "${ROOT_DIR}/runtime/state"
  echo "${pid}" > "${ROOT_DIR}/runtime/state/m1_serial.pid"
fi
