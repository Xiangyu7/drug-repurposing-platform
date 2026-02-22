#!/usr/bin/env bash
set -Eeuo pipefail

# ═══════════════════════════════════════════════════════════════════
# Drug Repurposing Platform — 24/7 Continuous Runner
# ═══════════════════════════════════════════════════════════════════
#
# 整体架构 (4 个子模块):
#   1. dsmeta_signature_pipeline  — 多GSE差异表达 → meta分析 → 疾病基因签名
#   2. SigReverse                 — LINCS L1000 反查 → 签名反转药物排名
#   3. kg_explain                 — Drug→Target→Pathway→Disease 知识图谱 + V5排名
#   4. LLM+RAG 证据工程           — PubMed文献检索 + LLM结构化提取 + 5维打分门控
#
# 两条研究路线 (Direction A & B):
#
#   Direction A — 跨疾病迁移 (Cross-Disease Repurposing)
#     科学问题: 其他疾病的药能否迁移到目标疾病？
#     流程:     dsmeta → SigReverse → kg_explain(signature) → bridge_repurpose_cross.csv → LLM Step6-9
#     风格:     探索性 (Exploration), 高风险高回报
#
#   Direction B — 原疾病重评估 (Origin-Disease Reassessment)
#     科学问题: 失败试验中的药是否值得重新评估？（换终点/人群/剂量）
#     流程:     screen_drugs(CT.gov) → kg_explain(ctgov) → bridge_origin_reassess.csv → LLM Step6-9
#     风格:     利用性 (Exploitation), 低风险稳健
#
# kg_explain 两种输入模式:
#   Mode A (Signature): 从疾病基因签名出发, 反查作用于签名基因的药物 → 用于 Direction A
#   Mode B (CT.gov):    从失败临床试验出发, 提取试验中的药物           → 用于 Direction B
#
# bridge 桥接文件 (kg_explain → LLM+RAG 的衔接点):
#   bridge_repurpose_cross.csv  — 每个药物取全局最优疾病 (~50药) → Direction A
#   bridge_origin_reassess.csv  — 只看目标疾病内的得分 (~83药)   → Direction B
#
# LLM+RAG Step 6-9 (同一套代码, 分别用两个 bridge 文件跑两遍):
#   Step 6: PubMed RAG + LLM 证据提取 (多路检索 + 语义重排 + 结构化抽取)
#   Step 7: 5维打分 + 门控 (证据/机制/可转化性/安全/可行性 → GO/MAYBE/NO-GO)
#   Step 8: 发布门控 + 候选打包 (移除NO-GO, Top-K → Excel)
#   Step 9: 验证方案生成 (实验类型/关键问题/成功标准/优先级P1-P3)
#
# 运行模式:
#   RUN_MODE=dual        — 双路线 (Direction A + B)
#   RUN_MODE=origin_only — 仅 Direction B
#   RUN_MODE=cross_only  — 仅 Direction A (跨疾病迁移)
#
# Disease list format (pipe-separated, 4 columns):
#   disease_key|disease_query|origin_disease_ids(optional)|inject_yaml(optional)
#
# Example:
#   atherosclerosis|atherosclerosis|EFO_0003914,MONDO_0021661|kg_explain/configs/inject_atherosclerosis.yaml
#   type2_diabetes|type 2 diabetes|EFO_0001360|
#
# 环境变量 (用户可配置):
#   TOPN_PROFILE        — topn策略档位: stable|balanced|recall (默认 stable)
#   TOPN_CROSS          — Direction A bridge topn: auto|int (默认 auto)
#   TOPN_ORIGIN         — Direction B bridge topn: auto|int (默认 auto)
#   TOPN_STAGE2_ENABLE  — 质量未达标时是否允许二阶段扩容 (默认 1)
#   STEP_TIMEOUT        — 每步超时 (默认 1800s)
#   DISK_MIN_GB         — 最低可用磁盘空间GB (默认 5)
#   DSMETA_CLEANUP      — dsmeta跑完后是否自动清理workdir (默认 1=清理, 0=保留)
# ═══════════════════════════════════════════════════════════════════

# ── macOS compatibility: provide `timeout` if missing ──
if ! command -v timeout &>/dev/null; then
  if command -v gtimeout &>/dev/null; then
    # GNU coreutils installed via Homebrew
    timeout() { gtimeout "$@"; }
  else
    # Perl-based fallback (macOS always has perl)
    timeout() {
      local secs="$1"; shift
      perl -e '
        use POSIX ":sys_wait_h";
        my $timeout = shift @ARGV;
        my $pid = fork();
        if ($pid == 0) { exec @ARGV; die "exec failed: $!"; }
        eval {
          local $SIG{ALRM} = sub { kill "TERM", $pid; die "timeout\n"; };
          alarm $timeout;
          waitpid($pid, 0);
          alarm 0;
        };
        if ($@ && $@ eq "timeout\n") { waitpid($pid, WNOHANG); exit 124; }
        exit ($? >> 8);
      ' "${secs}" "$@"
    }
  fi
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DSMETA_DIR="${ROOT_DIR}/dsmeta_signature_pipeline"
ARCHS4_DIR="${ROOT_DIR}/archs4_signature_pipeline"
SIG_DIR="${ROOT_DIR}/sigreverse"
KG_DIR="${ROOT_DIR}/kg_explain"
LLM_DIR="${ROOT_DIR}/LLM+RAG证据工程"

RUNTIME_DIR="${ROOT_DIR}/runtime"
WORK_ROOT="${RUNTIME_DIR}/work"
RESULTS_ROOT="${RUNTIME_DIR}/results"
QUARANTINE_ROOT="${RUNTIME_DIR}/quarantine"
STATE_ROOT="${RUNTIME_DIR}/state"
LOG_DIR="${ROOT_DIR}/logs/continuous_runner"

DISEASE_LIST_FILE="${1:-${ROOT_DIR}/ops/disease_list.txt}"

resolve_runtime_python() {
  local override="${1:-}"
  local venv_py="${2:-}"
  if [[ -n "${override}" && -x "${override}" ]]; then
    printf '%s' "${override}"
    return 0
  fi
  if [[ -n "${override}" ]] && command -v "${override}" >/dev/null 2>&1; then
    printf '%s' "${override}"
    return 0
  fi
  if [[ -n "${venv_py}" && -x "${venv_py}" ]]; then
    printf '%s' "${venv_py}"
    return 0
  fi
  printf 'python3'
}

DSMETA_PY="$(resolve_runtime_python "${DSMETA_PY:-}" "${DSMETA_DIR}/.venv/bin/python3")"
ARCHS4_PY="$(resolve_runtime_python "${ARCHS4_PY:-}" "${ARCHS4_DIR}/.venv/bin/python3")"
SIG_PY="$(resolve_runtime_python "${SIG_PY:-}" "${SIG_DIR}/.venv/bin/python3")"
KG_PY="$(resolve_runtime_python "${KG_PY:-}" "${KG_DIR}/.venv/bin/python3")"
LLM_PY="$(resolve_runtime_python "${LLM_PY:-}" "${LLM_DIR}/.venv/bin/python3")"

SCREEN_MAX_STUDIES="${SCREEN_MAX_STUDIES:-500}"
TOPN_PROFILE="${TOPN_PROFILE:-stable}"
TOPN_CROSS="${TOPN_CROSS:-auto}"
TOPN_ORIGIN="${TOPN_ORIGIN:-auto}"
TOPN_STAGE2_ENABLE="${TOPN_STAGE2_ENABLE:-1}"
# ── Internal constants (set by profile, not user-tunable) ──
TOPN_MAX_EXPAND_ROUNDS=1
TOPN_CAP_ORIGIN=""
TOPN_CAP_CROSS=""
TOPN_EXPAND_RATIO="0.30"
TOPN_STAGE1_MIN_ORIGIN=""
TOPN_STAGE1_MAX_ORIGIN=""
TOPN_STAGE1_MIN_CROSS=""
TOPN_STAGE1_MAX_CROSS=""
SHORTLIST_MIN_GO_ORIGIN=3
SHORTLIST_MIN_GO_CROSS=2
TOPK_CROSS=5
TOPK_ORIGIN=10
STEP6_PUBMED_RETMAX=120
STEP6_PUBMED_PARSE_MAX=60
STEP6_MAX_RERANK_DOCS=40
STEP6_MAX_EVIDENCE_DOCS=12
STRICT_CONTRACT="${STRICT_CONTRACT:-1}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"
LOG_RETENTION_DAYS="${LOG_RETENTION_DAYS:-30}"
RUN_MODE="${RUN_MODE:-dual}" # dual | origin_only
STEP_TIMEOUT="${STEP_TIMEOUT:-1800}" # 30 min per step default
DSMETA_DISK_MIN_GB="${DSMETA_DISK_MIN_GB:-8}"  # min free GB before dsmeta (GEO downloads are large)
DSMETA_CLEANUP="${DSMETA_CLEANUP:-1}"          # auto-clean dsmeta workdir after each disease
SIG_PRIORITY="${SIG_PRIORITY:-dsmeta}"         # dsmeta | archs4 — which signature source to try first
LOCK_NAME="${LOCK_NAME:-${RUN_MODE}}"
LOCK_FILE="${STATE_ROOT}/runner_${LOCK_NAME}.lock"

TOPN_POLICY_PY="${ROOT_DIR}/ops/internal/topn_policy.py"

apply_topn_profile_defaults() {
  case "${TOPN_PROFILE}" in
    stable)
      TOPN_CAP_ORIGIN=18;  TOPN_CAP_CROSS=14
      TOPN_STAGE1_MIN_ORIGIN=12; TOPN_STAGE1_MAX_ORIGIN=14
      TOPN_STAGE1_MIN_CROSS=10;  TOPN_STAGE1_MAX_CROSS=12
      ;;
    balanced)
      TOPN_CAP_ORIGIN=24;  TOPN_CAP_CROSS=18
      TOPN_STAGE1_MIN_ORIGIN=14; TOPN_STAGE1_MAX_ORIGIN=18
      TOPN_STAGE1_MIN_CROSS=12;  TOPN_STAGE1_MAX_CROSS=16
      ;;
    recall)
      TOPN_CAP_ORIGIN=30;  TOPN_CAP_CROSS=24
      TOPN_STAGE1_MIN_ORIGIN=16; TOPN_STAGE1_MAX_ORIGIN=24
      TOPN_STAGE1_MIN_CROSS=14;  TOPN_STAGE1_MAX_CROSS=20
      ;;
    *)
      printf '[WARN] Unknown TOPN_PROFILE=%s, fallback to stable\n' "${TOPN_PROFILE}" >&2
      TOPN_PROFILE="stable"
      apply_topn_profile_defaults
      return 0
      ;;
  esac
}

apply_topn_profile_defaults

mkdir -p "${WORK_ROOT}" "${RESULTS_ROOT}" "${QUARANTINE_ROOT}" "${STATE_ROOT}" "${LOG_DIR}"
RUN_LOG="${LOG_DIR}/runner_${RUN_MODE}_$(date '+%Y%m%d_%H%M%S')_$$.log"

log() {
  printf '[%s] %s\n' "$(date '+%F %T')" "$*" | tee -a "${RUN_LOG}"
}

# Progress marker — clearly visible in logs for grep/monitoring
# Usage: progress "disease_key" "3/6" "step name"
# NOTE: Prefer next_step() which auto-increments. Direct progress() only for custom cases.
progress() {
  local disease="$1" step="$2" desc="$3"
  log "▶▶▶ [${disease}] PROGRESS ${step}: ${desc}"
}

# ── Timing & counting helpers ──────────────────────────────────────

format_duration() {
  local secs="$1"
  if [[ "${secs}" -lt 60 ]]; then
    printf '%ds' "${secs}"
  elif [[ "${secs}" -lt 3600 ]]; then
    printf '%dm %ds' $((secs/60)) $((secs%60))
  else
    printf '%dh %dm' $((secs/3600)) $(( (secs%3600)/60 ))
  fi
}

count_csv_rows() {
  # Count data rows in a CSV (minus header). Returns 0 if file missing.
  local f="$1"
  if [[ -f "${f}" ]]; then
    local total
    total="$(wc -l < "${f}" | tr -d ' ')"
    echo $((total > 0 ? total - 1 : 0))
  else
    echo 0
  fi
}

count_json_genes() {
  # Read sigreverse_input.json → "N_up/N_down"
  local f="$1"
  if [[ -f "${f}" ]]; then
    python3 -c "
import json, sys
try:
    obj = json.loads(open(sys.argv[1]).read())
    print(f'{len(obj.get(\"up\",[]))}/{len(obj.get(\"down\",[]))}')
except Exception:
    print('?/?')
" "${f}" 2>/dev/null || echo "?/?"
  else
    echo "?/?"
  fi
}

# ── Dynamic step counter ──────────────────────────────────────────
# Replaces hardcoded "N/8" with mode-aware numbering.
# Usage: next_step "disease_key" "Step description"

CURRENT_STEP=0
TOTAL_STEPS=8

next_step() {
  CURRENT_STEP=$((CURRENT_STEP + 1))
  progress "$1" "${CURRENT_STEP}/${TOTAL_STEPS}" "$2"
}

# ── Step timings tracker ──────────────────────────────────────────
# Bash 4+ associative arrays not available on macOS default bash 3.
# Use a simple delimited string: "key1:val1|key2:val2|..."

STEP_TIMINGS=""
LAST_STEP_TS=0

record_step_timing() {
  local key="$1"
  local now=$SECONDS
  local secs=$((now - LAST_STEP_TS))
  LAST_STEP_TS=$now
  if [[ -z "${STEP_TIMINGS}" ]]; then
    STEP_TIMINGS="${key}:${secs}"
  else
    STEP_TIMINGS="${STEP_TIMINGS}|${key}:${secs}"
  fi
}

step_timings_to_json() {
  # Convert "k1:v1|k2:v2" → {"k1": v1, "k2": v2}
  if [[ -z "${STEP_TIMINGS}" ]]; then
    echo "{}"
    return
  fi
  python3 -c "
import json, sys
pairs = sys.argv[1].split('|')
d = {}
for p in pairs:
    k, v = p.split(':', 1)
    d[k] = int(v)
print(json.dumps(d))
" "${STEP_TIMINGS}" 2>/dev/null || echo "{}"
}

# ── Completed steps tracker (for FAILURE.json) ────────────────────

COMPLETED_STEPS=""

mark_step_done() {
  local step_name="$1"
  if [[ -z "${COMPLETED_STEPS}" ]]; then
    COMPLETED_STEPS="${step_name}"
  else
    COMPLETED_STEPS="${COMPLETED_STEPS},${step_name}"
  fi
}

completed_steps_to_json_array() {
  if [[ -z "${COMPLETED_STEPS}" ]]; then
    echo "[]"
    return
  fi
  python3 -c "
import json, sys
print(json.dumps(sys.argv[1].split(',')))
" "${COMPLETED_STEPS}" 2>/dev/null || echo "[]"
}

# ── Signature decision banner ─────────────────────────────────────

log_signature_decision() {
  # Args: primary_name primary_status primary_detail secondary_name secondary_status secondary_detail chosen_source
  # status: "ok" | "fail" | "skip"
  local pri_name="$1" pri_status="$2" pri_detail="$3"
  local sec_name="$4" sec_status="$5" sec_detail="$6"
  local chosen="$7"

  local pri_icon="·" sec_icon="·"
  case "${pri_status}" in
    ok)   pri_icon="✓" ;;
    fail) pri_icon="✗" ;;
  esac
  case "${sec_status}" in
    ok)   sec_icon="✓" ;;
    fail) sec_icon="✗" ;;
  esac

  log "┌─ 签名来源决策 (${SIG_PRIORITY}-first) ──────────────────────┐"
  log "│  ${pri_name}  ${pri_icon} ${pri_detail}"
  log "│  ${sec_name}  ${sec_icon} ${sec_detail}"
  if [[ "${chosen}" == "none" ]]; then
    log "│  → 签名不可用，Cross 路线终止"
  else
    local suffix=""
    [[ "${chosen}" != "${SIG_PRIORITY}" ]] && suffix=" (fallback)"
    log "│  → 使用: ${chosen}${suffix}"
  fi
  log "└──────────────────────────────────────────────────────────┘"
}

# ── Disease summary banner ────────────────────────────────────────

log_disease_summary() {
  local disease_key="$1" run_id="$2" elapsed="$3"
  local cross_status="$4" origin_status="$5"
  local sig_source="$6" cross_drugs="$7" origin_drugs="$8"
  local ab_overlap="$9" cross_elapsed="${10}" origin_elapsed="${11}"

  local dur
  dur="$(format_duration "${elapsed}")"
  local cross_dur="" origin_dur=""
  [[ -n "${cross_elapsed}" && "${cross_elapsed}" != "0" ]] && cross_dur="  耗时=$(format_duration "${cross_elapsed}")"
  [[ -n "${origin_elapsed}" && "${origin_elapsed}" != "0" ]] && origin_dur="  耗时=$(format_duration "${origin_elapsed}")"

  local c_icon="·" o_icon="·"
  case "${cross_status}" in
    success) c_icon="✓" ;;
    failed)  c_icon="✗" ;;
    skipped) c_icon="⏭" ;;
  esac
  case "${origin_status}" in
    success) o_icon="✓" ;;
    failed)  o_icon="✗" ;;
    skipped) o_icon="⏭" ;;
  esac

  log "┌─ ${disease_key} 完成 (run_id=${run_id}) ──────────────────────┐"
  log "│  总耗时: ${dur}   模式: ${RUN_MODE}"

  if [[ "${cross_status}" != "not_run" ]]; then
    local sig_label=""
    [[ -n "${sig_source}" && "${sig_source}" != "none" ]] && sig_label="  签名=${sig_source}"
    log "│  Cross:  ${c_icon} ${cross_status}${sig_label}  药物=${cross_drugs}${cross_dur}"
  fi

  if [[ "${origin_status}" != "not_run" ]]; then
    log "│  Origin: ${o_icon} ${origin_status}  药物=${origin_drugs}${origin_dur}"
  fi

  if [[ -n "${ab_overlap}" && "${ab_overlap}" != "0" && "${ab_overlap}" != "" ]]; then
    log "│  A+B:   ${ab_overlap} 个重叠药物"
  fi

  log "└──────────────────────────────────────────────────────────────┘"
}

trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "${s}"
}

run_in_dir() {
  local workdir="$1"
  shift
  (
    cd "${workdir}" || exit 1
    "$@"
  )
}

# [P1-3] run_cmd captures stdout/stderr to log + [P2-7] per-step timeout
# Uses background-process + watchdog pattern (works on macOS & Linux, supports bash functions)
run_cmd() {
  local label="$1"
  local timeout_sec="${STEP_TIMEOUT}"
  shift

  # optional: run_cmd "label" --timeout 600 cmd...
  if [[ "${1:-}" == "--timeout" ]]; then
    timeout_sec="$2"
    shift 2
  fi

  log "[RUN] ${label}: $*"
  local step_log="${CURRENT_STEP_LOG_DIR:-/tmp}/${label// /_}.log"
  local rc=0
  local start_ts=$SECONDS

  # Run command in a new process group so we can kill the whole tree on timeout
  set -m  # enable job control for process groups
  ("$@") > "${step_log}" 2>&1 &
  local cmd_pid=$!
  set +m  # restore
  ( sleep "${timeout_sec}" && kill -TERM -- -"${cmd_pid}" 2>/dev/null; kill -TERM "${cmd_pid}" 2>/dev/null; log "[TIMEOUT] ${label} exceeded ${timeout_sec}s, killed pid ${cmd_pid}" ) &
  local watcher_pid=$!

  wait "${cmd_pid}" 2>/dev/null
  rc=$?

  # Kill watchdog (no longer needed)
  kill "${watcher_pid}" 2>/dev/null
  wait "${watcher_pid}" 2>/dev/null

  local elapsed=$((SECONDS - start_ts))
  local dur
  dur="$(format_duration "${elapsed}")"

  # append step output to runner log
  if [[ -s "${step_log}" ]]; then
    {
      echo "--- [${label}] stdout/stderr begin ---"
      tail -100 "${step_log}"
      echo "--- [${label}] stdout/stderr end ---"
    } >> "${RUN_LOG}"
  fi

  if [[ "${rc}" -eq 0 ]]; then
    log "[OK] ${label} (${dur})"
    return 0
  fi
  if [[ "${rc}" -eq 124 ]]; then
    log "[ERROR] ${label} timed out after ${timeout_sec}s (${dur})"
  else
    log "[ERROR] ${label} failed (rc=${rc}) (${dur})"
  fi
  # print last 20 lines of step output for quick diagnosis
  if [[ -s "${step_log}" ]]; then
    log "[ERROR] ${label} last 20 lines:"
    tail -20 "${step_log}" | while IFS= read -r line; do
      log "  | ${line}"
    done
  fi
  return "${rc}"
}

acquire_lock() {
  if [[ -f "${LOCK_FILE}" ]]; then
    local old_pid
    old_pid="$(cat "${LOCK_FILE}" 2>/dev/null || true)"
    if [[ -n "${old_pid}" ]] && kill -0 "${old_pid}" 2>/dev/null; then
      log "[ERROR] Another runner is active (pid=${old_pid}). Exiting."
      exit 1
    fi
    log "[WARN] Removing stale lock file: ${LOCK_FILE}"
    rm -f "${LOCK_FILE}"
  fi
  echo "$$" > "${LOCK_FILE}"
}

release_lock() {
  rm -f "${LOCK_FILE}"
}

cleanup_old_runs() {
  local root="$1"
  if [[ ! -d "${root}" ]]; then
    return 0
  fi
  find "${root}" -mindepth 2 -maxdepth 2 -type d -mtime +"${RETENTION_DAYS}" -exec rm -rf {} + 2>/dev/null || true
  find "${root}" -mindepth 1 -maxdepth 1 -type d -empty -delete 2>/dev/null || true
}

# [P2-9] Log rotation
cleanup_old_logs() {
  find "${LOG_DIR}" -name "runner_*.log" -mtime +"${LOG_RETENTION_DAYS}" -delete 2>/dev/null || true
}

# Clean kg_explain HTTP cache (default: files older than CACHE_RETENTION_DAYS)
CACHE_RETENTION_DAYS="${CACHE_RETENTION_DAYS:-1}"
cleanup_kg_cache() {
  local cache_root="${KG_DIR}/cache/http_json"
  if [[ ! -d "${cache_root}" ]]; then
    return 0
  fi
  local count
  count="$(find "${cache_root}" -name "*.json" -mtime +"${CACHE_RETENTION_DAYS}" 2>/dev/null | wc -l | tr -d ' ')"
  if [[ "${count}" -gt 0 ]]; then
    find "${cache_root}" -name "*.json" -mtime +"${CACHE_RETENTION_DAYS}" -delete 2>/dev/null || true
    log "[CLEAN] Deleted ${count} expired cache files (>${CACHE_RETENTION_DAYS} days) from ${cache_root}"
  fi
}

# Clean dsmeta workdir after each disease run (frees 100-500 MB per disease)
cleanup_dsmeta_workdir() {
  local disease_key="$1"
  if [[ "${DSMETA_CLEANUP}" != "1" ]]; then
    return 0
  fi
  local dsmeta_workdir="${DSMETA_DIR}/work/${disease_key}"
  if [[ -d "${dsmeta_workdir}" ]]; then
    local size_mb
    size_mb="$(du -sm "${dsmeta_workdir}" 2>/dev/null | cut -f1)"
    rm -rf "${dsmeta_workdir}"
    log "[CLEAN] Deleted dsmeta workdir: ${dsmeta_workdir} (${size_mb:-?} MB freed)"
  fi
  # Also clean GPL annotation cache if >100MB total
  local gpl_cache="${DSMETA_DIR}/data/cache/gpl_annotations"
  if [[ -d "${gpl_cache}" ]]; then
    local gpl_size
    gpl_size="$(du -sm "${gpl_cache}" 2>/dev/null | cut -f1)"
    if [[ "${gpl_size:-0}" -gt 100 ]]; then
      rm -rf "${gpl_cache}"
      log "[CLEAN] Deleted GPL annotation cache: ${gpl_cache} (${gpl_size} MB freed)"
    fi
  fi
}

cleanup_state_files() {
  # Clean up env_check_*.json, env_repair_*.json, env_resolved_*.env older than RETENTION_DAYS
  local count=0
  while IFS= read -r -d '' f; do
    rm -f "${f}" && count=$((count + 1))
  done < <(find "${STATE_ROOT}" -maxdepth 1 \( -name "env_check_*.json" -o -name "env_repair_*.json" -o -name "env_resolved_*.env" \) -mtime +"${RETENTION_DAYS}" -print0 2>/dev/null)
  if [[ "${count}" -gt 0 ]]; then
    log "[CLEANUP] Removed ${count} stale state files from ${STATE_ROOT}"
  fi
}

cleanup_retention() {
  cleanup_old_runs "${WORK_ROOT}"
  cleanup_old_runs "${QUARANTINE_ROOT}"
  cleanup_old_logs
  cleanup_kg_cache
  cleanup_state_files
}

require_file() {
  local path="$1"
  local label="$2"
  if [[ ! -f "${path}" ]]; then
    log "[ERROR] Missing ${label}: ${path}"
    return 1
  fi
  return 0
}

json_get_field() {
  local json_path="$1"
  local field="$2"
  python3 - "${json_path}" "${field}" <<'PY'
import json
import sys
from pathlib import Path

p = Path(sys.argv[1])
field = sys.argv[2]
if not p.exists():
    print("")
    raise SystemExit(0)
try:
    obj = json.loads(p.read_text(encoding="utf-8"))
except Exception:
    print("")
    raise SystemExit(0)
v = obj.get(field, "")
if isinstance(v, bool):
    print("1" if v else "0")
else:
    print(v)
PY
}

is_auto_topn() {
  local v="${1:-}"
  # bash 3.x compatible lowercase comparison
  [[ "$(printf '%s' "${v}" | tr '[:upper:]' '[:lower:]')" == "auto" ]]
}

resolve_topn() {
  local configured="$1"
  local csv_path="$2"
  local route_label="$3"
  local stage="$4"
  local topk="$5"
  local stage1_min="$6"
  local stage1_max="$7"
  local cap="$8"
  local expand_ratio="$9"
  local fallback_min="${10}"
  local prev_topn="${11:-}"
  local out_json="${12}"

  if [[ ! -f "${TOPN_POLICY_PY}" ]]; then
    log "[ERROR] Missing topn policy script: ${TOPN_POLICY_PY}"
    return 1
  fi
  local policy_cmd=(
    "${KG_PY}" "${TOPN_POLICY_PY}" decide
    --bridge_csv "${csv_path}"
    --route "${route_label}"
    --profile "${TOPN_PROFILE}"
    --stage "${stage}"
    --topk "${topk}"
    --configured_topn "${configured}"
    --stage1_min "${stage1_min}"
    --stage1_max "${stage1_max}"
    --cap "${cap}"
    --expand_ratio "${expand_ratio}"
    --fallback_min "${fallback_min}"
    --output "${out_json}"
  )
  if [[ -n "${prev_topn}" ]]; then
    policy_cmd+=(--previous_topn "${prev_topn}")
  fi
  if ! "${policy_cmd[@]}" >/dev/null 2>&1; then
    log "[ERROR] ${route_label}: topn policy failed (stage=${stage})"
    return 1
  fi

  local resolved
  resolved="$(json_get_field "${out_json}" "resolved_topn")"
  if [[ -z "${resolved}" ]]; then
    log "[ERROR] ${route_label}: missing resolved_topn in ${out_json}"
    return 1
  fi

  # Detect degraded mode and warn loudly (but don't stop — pipeline explores more)
  local topn_mode
  topn_mode="$(json_get_field "${out_json}" "mode")"
  if [[ "${topn_mode}" == "degraded" ]]; then
    local topn_error
    topn_error="$(json_get_field "${out_json}" "error")"
    log "[WARN] ${route_label}: TopN running in DEGRADED mode (expanded to ${resolved}). Error: ${topn_error}"
    log "[WARN] ${route_label}: Results from this run should be reviewed — degraded flag set in decision JSON"
  fi

  printf '%s' "${resolved}"
  return 0
}

evaluate_topn_quality() {
  local step7_cards="$1"
  local step8_shortlist="$2"
  local route_label="$3"
  local stage="$4"
  local topk="$5"
  local min_go="$6"
  local out_json="$7"

  if [[ ! -f "${TOPN_POLICY_PY}" ]]; then
    log "[ERROR] Missing topn policy script: ${TOPN_POLICY_PY}"
    return 1
  fi

  if ! "${KG_PY}" "${TOPN_POLICY_PY}" quality \
    --step7_cards "${step7_cards}" \
    --step8_shortlist "${step8_shortlist}" \
    --topk "${topk}" \
    --min_go "${min_go}" \
    --route "${route_label}" \
    --stage "${stage}" \
    --output "${out_json}" >/dev/null 2>&1; then
    log "[WARN] ${route_label}: quality eval failed for ${stage}, write fallback quality json"
    python3 - "${out_json}" "${route_label}" "${stage}" "${topk}" "${min_go}" <<'PY'
import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
payload = {
    "ok": False,
    "route": sys.argv[2],
    "stage": sys.argv[3],
    "topk": int(sys.argv[4]),
    "min_go": int(sys.argv[5]),
    "shortlist_rows": 0,
    "shortlist_go_count": 0,
    "pass_shortlist_rows": False,
    "pass_go_threshold": False,
    "quality_passed": False,
    "trigger_stage2": True,
    "reasons": ["quality_eval_error"],
}
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
PY
  fi
}

write_topn_decision_skip_json() {
  local out_json="$1"
  local route_label="$2"
  local resolved_topn="$3"
  local reason="$4"
  python3 - "${out_json}" "${route_label}" "${resolved_topn}" "${reason}" <<'PY'
import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
payload = {
    "ok": True,
    "route": sys.argv[2],
    "stage": "stage2",
    "mode": "skipped",
    "resolved_topn": int(sys.argv[3]) if str(sys.argv[3]).strip() else 0,
    "should_expand": False,
    "reason": sys.argv[4],
}
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
PY
}

write_topn_quality_skip_json() {
  local out_json="$1"
  local route_label="$2"
  local topk="$3"
  local min_go="$4"
  local reason="$5"
  local stage_label="${6:-stage2}"
  python3 - "${out_json}" "${route_label}" "${topk}" "${min_go}" "${reason}" "${stage_label}" <<'PY'
import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
payload = {
    "ok": True,
    "route": sys.argv[2],
    "stage": sys.argv[6],
    "topk": int(sys.argv[3]),
    "min_go": int(sys.argv[4]),
    "shortlist_rows": 0,
    "shortlist_go_count": 0,
    "step7_cards_total": 0,
    "step7_cards_go_count": 0,
    "step7_cards_maybe_count": 0,
    "pass_shortlist_rows": False,
    "pass_go_threshold": False,
    "quality_passed": False,
    "trigger_stage2": False,
    "reasons": [sys.argv[5]],
}
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
PY
}

annotate_route_manifest_summary() {
  local step9_dir="$1"
  local route_label="$2"
  local selected_stage="$3"
  local resolved_topn="$4"
  local quality_passed="$5"
  local decision_stage1="$6"
  local quality_stage1="$7"
  local decision_stage2="$8"
  local quality_stage2="$9"

  local manifest_path="${step9_dir}/step9_manifest.json"
  if [[ ! -f "${manifest_path}" ]]; then
    return 0
  fi
  python3 - "${manifest_path}" "${route_label}" "${selected_stage}" "${resolved_topn}" "${quality_passed}" "${decision_stage1}" "${quality_stage1}" "${decision_stage2}" "${quality_stage2}" <<'PY'
import json
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1])
route_label = sys.argv[2]
selected_stage = sys.argv[3]
resolved_topn = int(sys.argv[4])
quality_passed = str(sys.argv[5]).strip() in {"1", "true", "True"}
decision_stage1 = sys.argv[6]
quality_stage1 = sys.argv[7]
decision_stage2 = sys.argv[8]
quality_stage2 = sys.argv[9]

obj = json.loads(manifest_path.read_text(encoding="utf-8"))
summary = obj.get("summary") or {}
summary["topn_policy"] = {
    "route": route_label,
    "selected_stage": selected_stage,
    "resolved_topn": resolved_topn,
    "quality_passed": quality_passed,
    "decision_stage1": decision_stage1,
    "quality_stage1": quality_stage1,
    "decision_stage2": decision_stage2,
    "quality_stage2": quality_stage2,
}
obj["summary"] = summary
manifest_path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
PY
}

run_route_llm_stage() {
  local route_title="$1"         # Origin | Cross
  local route_key="$2"           # origin | cross
  local stage="$3"               # stage1 | stage2
  local bridge_csv="$4"
  local neg_csv="$5"
  local step6_dir="$6"
  local step7_dir="$7"
  local step8_dir="$8"
  local step9_dir="$9"
  local disease_query="${10}"
  local topn="${11}"
  local topk="${12}"
  local min_go="${13}"
  local quality_json="${14}"

  # SKIP_LLM=1 → skip LLM step6-9, write skip markers, return success
  if [[ "${SKIP_LLM:-0}" == "1" ]]; then
    log "[INFO] SKIP_LLM=1: skipping ${route_title} LLM step6-9 (${stage})"
    mkdir -p "${step6_dir}" "${step7_dir}" "${step8_dir}" "${step9_dir}"
    write_topn_quality_skip_json "${quality_json}" "${route_key}" "${topk}" "${min_go}" "llm_skipped" "${stage}"
    return 0
  fi

  if ! run_cmd "${route_title}: step6 (${stage})" --timeout 7200 run_in_dir "${LLM_DIR}" "${LLM_PY}" scripts/step6_evidence_extraction.py --rank_in "${bridge_csv}" --neg "${neg_csv}" --out "${step6_dir}" --target_disease "${disease_query}" --topn "${topn}" --pubmed_retmax "${STEP6_PUBMED_RETMAX}" --pubmed_parse_max "${STEP6_PUBMED_PARSE_MAX}" --max_rerank_docs "${STEP6_MAX_RERANK_DOCS}" --max_evidence_docs "${STEP6_MAX_EVIDENCE_DOCS}"; then
    write_topn_quality_skip_json "${quality_json}" "${route_key}" "${topk}" "${min_go}" "${stage}_step6_failed" "${stage}"
    return 1
  fi

  if ! run_cmd "${route_title}: step7 (${stage})" --timeout 3600 run_in_dir "${LLM_DIR}" "${LLM_PY}" scripts/step7_score_and_gate.py --input "${step6_dir}" --out "${step7_dir}" --strict_contract "${STRICT_CONTRACT}"; then
    write_topn_quality_skip_json "${quality_json}" "${route_key}" "${topk}" "${min_go}" "${stage}_step7_failed" "${stage}"
    return 1
  fi

  if ! run_cmd "${route_title}: step8 (${stage})" --timeout 3600 run_in_dir "${LLM_DIR}" "${LLM_PY}" scripts/step8_candidate_pack.py --step7_dir "${step7_dir}" --neg "${neg_csv}" --bridge "${bridge_csv}" --outdir "${step8_dir}" --target_disease "${disease_query}" --topk "${topk}" --route "${route_key}" --include_explore 1 --strict_contract "${STRICT_CONTRACT}"; then
    write_topn_quality_skip_json "${quality_json}" "${route_key}" "${topk}" "${min_go}" "${stage}_step8_failed" "${stage}"
    return 1
  fi

  if ! run_cmd "${route_title}: step9 (${stage})" --timeout 3600 run_in_dir "${LLM_DIR}" "${LLM_PY}" scripts/step9_validation_plan.py --step8_dir "${step8_dir}" --step7_dir "${step7_dir}" --outdir "${step9_dir}" --target_disease "${disease_query}" --strict_contract "${STRICT_CONTRACT}"; then
    write_topn_quality_skip_json "${quality_json}" "${route_key}" "${topk}" "${min_go}" "${stage}_step9_failed" "${stage}"
    return 1
  fi

  local shortlist_csv="${step8_dir}/step8_shortlist_top${topk}.csv"
  local cards_json="${step7_dir}/step7_cards.json"
  evaluate_topn_quality "${cards_json}" "${shortlist_csv}" "${route_key}" "${stage}" "${topk}" "${min_go}" "${quality_json}"
  return 0
}

resolve_path_optional() {
  local input="$1"
  if [[ -z "${input}" ]]; then
    printf ''
    return 0
  fi
  if [[ -f "${input}" ]]; then
    printf '%s' "${input}"
    return 0
  fi
  if [[ -f "${ROOT_DIR}/${input}" ]]; then
    printf '%s' "${ROOT_DIR}/${input}"
    return 0
  fi
  if [[ -f "${KG_DIR}/${input}" ]]; then
    printf '%s' "${KG_DIR}/${input}"
    return 0
  fi
  return 1
}

# Copy KG manifest from per-disease or legacy path
copy_kg_manifest() {
  local disease_key="$1"
  local dest="$2"
  local src="${KG_DIR}/output/${disease_key}/pipeline_manifest.json"
  local src_legacy="${KG_DIR}/output/pipeline_manifest.json"
  if [[ -f "${src}" ]]; then
    cp "${src}" "${dest}"
  elif [[ -f "${src_legacy}" ]]; then
    cp "${src_legacy}" "${dest}"
  fi
}

# Validate gene-list JSON (used for both signature meta and sigreverse input)
# Usage: validate_gene_json <json_path> <disease_key> <up_key> <down_key> <label>
validate_gene_json() {
  local json_path="$1"
  local disease_key="$2"
  local up_key="${3:-up_genes}"
  local down_key="${4:-down_genes}"
  local label="${5:-gene json}"
  python3 - "${json_path}" "${disease_key}" "${up_key}" "${down_key}" "${label}" <<'PY'
import json, re, sys
from pathlib import Path

p = Path(sys.argv[1])
disease_key, up_key, down_key, label = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
if not p.exists():
    print(f"missing file: {p}", file=sys.stderr); raise SystemExit(2)
obj = json.loads(p.read_text(encoding="utf-8"))
for k in ("name", up_key, down_key):
    if k not in obj:
        print(f"missing key: {k}", file=sys.stderr); raise SystemExit(3)
if not isinstance(obj.get(up_key), list) or not isinstance(obj.get(down_key), list):
    print(f"{up_key}/{down_key} must be lists", file=sys.stderr); raise SystemExit(4)
if len(obj[up_key]) == 0 and len(obj[down_key]) == 0:
    print(f"{label} has empty gene lists", file=sys.stderr); raise SystemExit(6)
norm = lambda s: re.sub(r"[^a-z0-9]+", "", s.lower())
if (kn := norm(disease_key)) and kn not in norm(str(obj.get("name", ""))):
    print(f"{label} name mismatch: disease_key={disease_key}, name={obj.get('name')}", file=sys.stderr)
    raise SystemExit(5)
PY
}

validate_signature_meta_json() {
  validate_gene_json "$1" "$2" "up_genes" "down_genes" "signature meta"
}

validate_sigreverse_input_json() {
  validate_gene_json "$1" "$2" "up" "down" "sigreverse input"
}

resolve_cross_inputs() {
  local disease_key="$1"
  local preferred_source="${2:-}"  # "archs4" or "dsmeta"; empty = auto-detect

  local archs4_meta="${ARCHS4_DIR}/outputs/${disease_key}/signature/disease_signature_meta.json"
  local archs4_sig="${ARCHS4_DIR}/outputs/${disease_key}/signature/sigreverse_input.json"
  local meta_per="${DSMETA_DIR}/outputs/${disease_key}/signature/disease_signature_meta.json"
  local meta_legacy="${DSMETA_DIR}/outputs/signature/disease_signature_meta.json"
  local sig_per="${DSMETA_DIR}/outputs/${disease_key}/signature/sigreverse_input.json"
  local sig_legacy="${DSMETA_DIR}/outputs/signature/sigreverse_input.json"

  CROSS_SIGNATURE_META=""
  CROSS_SIGREVERSE_INPUT=""
  CROSS_SIGNATURE_SOURCE=""

  if [[ "${preferred_source}" == "dsmeta" ]]; then
    if [[ -f "${meta_per}" ]]; then
      CROSS_SIGNATURE_META="${meta_per}"
      CROSS_SIGNATURE_SOURCE="dsmeta"
    elif [[ -f "${meta_legacy}" ]]; then
      CROSS_SIGNATURE_META="${meta_legacy}"
      CROSS_SIGNATURE_SOURCE="dsmeta"
    fi
    if [[ -f "${sig_per}" ]]; then
      CROSS_SIGREVERSE_INPUT="${sig_per}"
    elif [[ -f "${sig_legacy}" ]]; then
      CROSS_SIGREVERSE_INPUT="${sig_legacy}"
    fi
  elif [[ "${preferred_source}" == "archs4" ]]; then
    if [[ -f "${archs4_meta}" ]]; then
      CROSS_SIGNATURE_META="${archs4_meta}"
      CROSS_SIGNATURE_SOURCE="archs4"
    fi
    if [[ -f "${archs4_sig}" ]]; then
      CROSS_SIGREVERSE_INPUT="${archs4_sig}"
    fi
  else
    # Auto-detect: prefer ARCHS4 if available, then dsmeta
    if [[ -f "${archs4_meta}" ]]; then
      CROSS_SIGNATURE_META="${archs4_meta}"
      CROSS_SIGNATURE_SOURCE="archs4"
    elif [[ -f "${meta_per}" ]]; then
      CROSS_SIGNATURE_META="${meta_per}"
      CROSS_SIGNATURE_SOURCE="dsmeta"
    elif [[ -f "${meta_legacy}" ]]; then
      CROSS_SIGNATURE_META="${meta_legacy}"
      CROSS_SIGNATURE_SOURCE="dsmeta"
    fi
    if [[ -f "${archs4_sig}" ]]; then
      CROSS_SIGREVERSE_INPUT="${archs4_sig}"
    elif [[ -f "${sig_per}" ]]; then
      CROSS_SIGREVERSE_INPUT="${sig_per}"
    elif [[ -f "${sig_legacy}" ]]; then
      CROSS_SIGREVERSE_INPUT="${sig_legacy}"
    fi
  fi

  require_file "${CROSS_SIGNATURE_META}" "signature_meta" || return 1
  require_file "${CROSS_SIGREVERSE_INPUT}" "sigreverse_input" || return 1
  return 0
}

kg_manifest_gate() {
  local manifest_path="$1"
  local expected_source="$2"

  python3 - "${manifest_path}" "${expected_source}" <<'PY'
import json
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1])
expected_source = sys.argv[2]
if not manifest_path.exists():
    print(f"manifest missing: {manifest_path}", file=sys.stderr)
    raise SystemExit(2)
m = json.loads(manifest_path.read_text(encoding="utf-8"))
actual_source = str(m.get("drug_source", ""))
if actual_source != expected_source:
    print(f"drug_source mismatch: expected={expected_source}, actual={actual_source}", file=sys.stderr)
    raise SystemExit(3)
# Non-critical steps: Pathway (Reactome 404s are common), Final ranking (may KeyError on bridge generation)
NON_CRITICAL = {"Pathway", "v5 排序", "v4 排序", "DTPD 排序"}
all_errors = [s for s in (m.get("step_timings") or []) if str(s.get("status", "")).lower() == "error"]
critical = [e for e in all_errors if not any(nc in str(e.get("step", "")) for nc in NON_CRITICAL)]
if all_errors and not critical:
    details = "; ".join(f"{e.get('step')}::{e.get('error','')}" for e in all_errors)
    print(f"[WARN] non-critical errors ignored: {details}", file=sys.stderr)
if critical:
    details = "; ".join(f"{e.get('step')}::{e.get('error','')}" for e in critical)
    print(f"manifest contains critical error steps: {details}", file=sys.stderr)
    raise SystemExit(4)
PY
}

derive_matched_ids_from_dtpd() {
  local disease_query="$1"
  local disease_key="${2:-}"
  local v3_path=""
  local dtpd_path_new="${KG_DIR}/output/${disease_key}/dtpd_rank.csv"
  local dtpd_path_legacy="${KG_DIR}/output/dtpd_rank.csv"
  if [[ -n "${disease_key}" && -f "${dtpd_path_new}" ]]; then
    v3_path="${dtpd_path_new}"
  elif [[ -f "${dtpd_path_legacy}" ]]; then
    v3_path="${dtpd_path_legacy}"
  fi
  if [[ ! -f "${v3_path}" ]]; then
    printf ''
    return 0
  fi
  python3 - "${v3_path}" "${disease_query}" <<'PY'
import csv
import sys

v3_path = sys.argv[1]
query = sys.argv[2]
q = query.lower()
ids = set()
try:
    with open(v3_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "diseaseName" not in reader.fieldnames or "diseaseId" not in reader.fieldnames:
            print("")
            raise SystemExit(0)
        for row in reader:
            name = (row.get("diseaseName") or "").lower()
            did = (row.get("diseaseId") or "").strip()
            if q in name and did:
                ids.add(did)
except Exception:
    print("")
    raise SystemExit(0)
ids = sorted(ids)
print(",".join(ids))
PY
}

ensure_kg_disease_config() {
  local disease_key="$1"
  local disease_query="$2"
  local cfg_path="${KG_DIR}/configs/diseases/${disease_key}.yaml"

  if [[ -f "${cfg_path}" ]]; then
    return 0
  fi

  log "Create missing kg disease config: ${cfg_path}"
  cat > "${cfg_path}" <<EOF_CFG
disease:
  name: "${disease_query}"
  condition: "${disease_query}"

drug_filter:
  include_types:
    - DRUG
    - BIOLOGICAL
  exclude_types:
    - DEVICE
    - PROCEDURE
    - BEHAVIORAL
    - DIETARY_SUPPLEMENT
EOF_CFG
}

write_failure_record() {
  local disease_key="$1"
  local run_id="$2"
  local phase="$3"
  local message="$4"
  local cross_status="$5"
  local origin_status="$6"

  local qdir="${QUARANTINE_ROOT}/${disease_key}/${run_id}"
  mkdir -p "${qdir}"
  FAILURE_DIR="${qdir}"

  local elapsed=$((SECONDS - DISEASE_START_TS))

  PHASE="${phase}" \
  MESSAGE="${message}" \
  CROSS_STATUS="${cross_status}" \
  ORIGIN_STATUS="${origin_status}" \
  RUN_ID="${run_id}" \
  DISEASE_KEY="${disease_key}" \
  RUN_LOG_PATH="${RUN_LOG}" \
  ELAPSED_SECONDS="${elapsed}" \
  SIGNATURE_SOURCE="${CROSS_SIGNATURE_SOURCE:-none}" \
  COMPLETED_STEPS_JSON="$(completed_steps_to_json_array)" \
  python3 - <<'PY' > "${qdir}/FAILURE.json"
import json
import os
from datetime import datetime, timezone

payload = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "disease_key": os.environ.get("DISEASE_KEY", ""),
    "run_id": os.environ.get("RUN_ID", ""),
    "failed_phase": os.environ.get("PHASE", ""),
    "message": os.environ.get("MESSAGE", ""),
    "cross_status": os.environ.get("CROSS_STATUS", ""),
    "origin_status": os.environ.get("ORIGIN_STATUS", ""),
    "runner_log": os.environ.get("RUN_LOG_PATH", ""),
    "elapsed_seconds": int(os.environ.get("ELAPSED_SECONDS", "0")),
    "signature_source": os.environ.get("SIGNATURE_SOURCE", "none"),
    "completed_steps": json.loads(os.environ.get("COMPLETED_STEPS_JSON", "[]")),
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY
}

archive_results() {
  local disease_key="$1"
  local disease_query="$2"
  local run_id="$3"
  local run_date="$4"
  local result_dir="$5"
  local cross_status="$6"
  local origin_status="$7"
  local origin_ids_input="$8"
  local origin_ids_effective="$9"
  local inject_path="${10}"
  local cross_signature_meta="${11}"
  local cross_sigreverse_input="${12}"
  local sig_out_dir="${13}"
  local cross_manifest_path="${14}"
  local origin_manifest_path="${15}"
  local bridge_cross_path="${16}"
  local bridge_origin_path="${17}"
  local step7_cross="${18}"
  local step8_cross="${19}"
  local step9_cross="${20}"
  local step7_origin="${21}"
  local step8_origin="${22}"
  local step9_origin="${23}"

  mkdir -p "${result_dir}/cross" "${result_dir}/origin" "${result_dir}/kg" "${result_dir}/sigreverse"

  if [[ "${cross_status}" == "success" ]]; then
    cp -R "${step7_cross}" "${result_dir}/cross/step7"
    cp -R "${step8_cross}" "${result_dir}/cross/step8"
    cp -R "${step9_cross}" "${result_dir}/cross/step9"
    cp "${bridge_cross_path}" "${result_dir}/kg/bridge_repurpose_cross.csv"
    cp "${cross_manifest_path}" "${result_dir}/kg/pipeline_manifest_cross_signature.json"

    local sig_rank="${sig_out_dir}/drug_reversal_rank.csv"
    if [[ -f "${sig_rank}" ]]; then
      cp "${sig_rank}" "${result_dir}/sigreverse/drug_reversal_rank.csv"
    fi
  fi

  if [[ "${origin_status}" == "success" ]]; then
    cp -R "${step7_origin}" "${result_dir}/origin/step7"
    cp -R "${step8_origin}" "${result_dir}/origin/step8"
    cp -R "${step9_origin}" "${result_dir}/origin/step9"
    cp "${bridge_origin_path}" "${result_dir}/kg/bridge_origin_reassess.csv"
    cp "${origin_manifest_path}" "${result_dir}/kg/pipeline_manifest_origin_ctgov.json"
  fi

  # Compute counts for enhanced summary
  local cross_drug_cnt=0 origin_drug_cnt=0 ab_overlap_cnt=0
  local sig_genes_up=0 sig_genes_down=0
  if [[ -n "${bridge_cross_path}" && -f "${bridge_cross_path}" ]]; then
    cross_drug_cnt="$(count_csv_rows "${bridge_cross_path}")"
  fi
  if [[ -n "${bridge_origin_path}" && -f "${bridge_origin_path}" ]]; then
    origin_drug_cnt="$(count_csv_rows "${bridge_origin_path}")"
  fi
  local ab_csv="${result_dir}/ab_comparison.csv"
  if [[ -f "${ab_csv}" ]]; then
    ab_overlap_cnt="$(count_csv_rows "${ab_csv}")"
  fi
  if [[ -n "${cross_sigreverse_input}" && -f "${cross_sigreverse_input}" ]]; then
    local gene_counts
    gene_counts="$(count_json_genes "${cross_sigreverse_input}")"
    sig_genes_up="${gene_counts%%/*}"
    sig_genes_down="${gene_counts##*/}"
  fi

  local elapsed=$((SECONDS - DISEASE_START_TS))

  DISEASE_KEY="${disease_key}" \
  DISEASE_QUERY="${disease_query}" \
  RUN_ID="${run_id}" \
  RUN_DATE="${run_date}" \
  CROSS_STATUS="${cross_status}" \
  ORIGIN_STATUS="${origin_status}" \
  ORIGIN_IDS_INPUT="${origin_ids_input}" \
  ORIGIN_IDS_EFFECTIVE="${origin_ids_effective}" \
  INJECT_PATH="${inject_path}" \
  CROSS_SIGNATURE_META="${cross_signature_meta}" \
  CROSS_SIGREVERSE_INPUT="${cross_sigreverse_input}" \
  RUN_MODE="${RUN_MODE}" \
  RESULT_DIR="${result_dir}" \
  SIGNATURE_SOURCE="${CROSS_SIGNATURE_SOURCE:-none}" \
  SIG_GENES_UP="${sig_genes_up}" \
  SIG_GENES_DOWN="${sig_genes_down}" \
  CROSS_DRUG_COUNT="${cross_drug_cnt}" \
  ORIGIN_DRUG_COUNT="${origin_drug_cnt}" \
  AB_OVERLAP_COUNT="${ab_overlap_cnt}" \
  ELAPSED_SECONDS="${elapsed}" \
  STEP_TIMINGS_JSON="$(step_timings_to_json)" \
  python3 - <<'PY' > "${result_dir}/run_summary.json"
import json
import os
from datetime import datetime, timezone

payload = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "disease_key": os.environ.get("DISEASE_KEY", ""),
    "disease_query": os.environ.get("DISEASE_QUERY", ""),
    "run_id": os.environ.get("RUN_ID", ""),
    "run_date": os.environ.get("RUN_DATE", ""),
    "cross_status": os.environ.get("CROSS_STATUS", ""),
    "origin_status": os.environ.get("ORIGIN_STATUS", ""),
    "origin_disease_ids_input": os.environ.get("ORIGIN_IDS_INPUT", ""),
    "origin_disease_ids_effective": os.environ.get("ORIGIN_IDS_EFFECTIVE", ""),
    "inject_yaml": os.environ.get("INJECT_PATH", ""),
    "cross_signature_meta": os.environ.get("CROSS_SIGNATURE_META", ""),
    "cross_sigreverse_input": os.environ.get("CROSS_SIGREVERSE_INPUT", ""),
    "run_mode": os.environ.get("RUN_MODE", ""),
    "result_dir": os.environ.get("RESULT_DIR", ""),
    "signature_source": os.environ.get("SIGNATURE_SOURCE", "none"),
    "signature_genes_up": int(os.environ.get("SIG_GENES_UP", "0")),
    "signature_genes_down": int(os.environ.get("SIG_GENES_DOWN", "0")),
    "cross_drug_count": int(os.environ.get("CROSS_DRUG_COUNT", "0")),
    "origin_drug_count": int(os.environ.get("ORIGIN_DRUG_COUNT", "0")),
    "ab_overlap_count": int(os.environ.get("AB_OVERLAP_COUNT", "0")),
    "elapsed_seconds": int(os.environ.get("ELAPSED_SECONDS", "0")),
    "step_timings": json.loads(os.environ.get("STEP_TIMINGS_JSON", "{}")),
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY
}

fail_disease() {
  local disease_key="$1"
  local run_id="$2"
  local phase="$3"
  local message="$4"
  local cross_status="$5"
  local origin_status="$6"

  write_failure_record "${disease_key}" "${run_id}" "${phase}" "${message}" "${cross_status}" "${origin_status}"
  log "[FAIL] disease=${disease_key} phase=${phase} message=${message} quarantine=${FAILURE_DIR}"
  return 1
}

process_disease() {
  local disease_key="$1"
  local disease_query="$2"
  local origin_ids_input="$3"
  local inject_raw="$4"

  local run_date
  run_date="$(date '+%F')"
  local run_id
  run_id="$(date '+%Y%m%d_%H%M%S')_${RANDOM}"

  local disease_work="${WORK_ROOT}/${disease_key}/${run_id}"
  local result_dir="${RESULTS_ROOT}/${disease_key}/${run_date}/${run_id}"
  mkdir -p "${disease_work}" "${result_dir}"

  # [P1-3] Per-step log directory for this run
  CURRENT_STEP_LOG_DIR="${disease_work}/step_logs"
  mkdir -p "${CURRENT_STEP_LOG_DIR}"

  # Reset per-disease trackers
  DISEASE_START_TS=$SECONDS
  LAST_STEP_TS=$SECONDS
  CURRENT_STEP=0
  STEP_TIMINGS=""
  COMPLETED_STEPS=""
  local cross_route_start=0 cross_route_elapsed=0
  local origin_route_start=0 origin_route_elapsed=0

  # Dynamic step count based on mode
  case "${RUN_MODE}" in
    dual)        TOTAL_STEPS=8 ;;
    origin_only) TOTAL_STEPS=4 ;;
    cross_only)  TOTAL_STEPS=6 ;;
    *)           TOTAL_STEPS=8 ;;
  esac

  local cross_status="not_run"
  local origin_status="not_run"
  local origin_ids_effective="${origin_ids_input}"

  local inject_path=""
  if ! inject_path="$(resolve_path_optional "${inject_raw}")"; then
    fail_disease "${disease_key}" "${run_id}" "validate_inject" "inject file not found: ${inject_raw}" "${cross_status}" "${origin_status}"
    return 1
  fi

  log "=== Disease start: key=${disease_key}, query=${disease_query}, origin_ids=${origin_ids_input:-N/A}, inject=${inject_path:-N/A}, run_id=${run_id}, mode=${RUN_MODE} ==="
  next_step "${disease_key}" "Screen drugs (CT.gov)"

  if ! ensure_kg_disease_config "${disease_key}" "${disease_query}"; then
    fail_disease "${disease_key}" "${run_id}" "ensure_kg_config" "cannot ensure kg disease config" "${cross_status}" "${origin_status}"
    return 1
  fi

  local screen_out="${disease_work}/screen"
  local neg_csv="${screen_out}/poolA_drug_level.csv"
  if ! run_cmd "Screen drugs" run_in_dir "${LLM_DIR}" "${LLM_PY}" scripts/screen_drugs.py --disease "${disease_query}" --max-studies "${SCREEN_MAX_STUDIES}" --outdir "${screen_out}"; then
    fail_disease "${disease_key}" "${run_id}" "screen_drugs" "screen_drugs failed" "${cross_status}" "${origin_status}"
    return 1
  fi

  if ! require_file "${neg_csv}" "drug level csv"; then
    fail_disease "${disease_key}" "${run_id}" "screen_output" "missing drug level csv" "${cross_status}" "${origin_status}"
    return 1
  fi
  record_step_timing "screen_drugs"
  mark_step_done "screen_drugs"

  # [P1-4] Isolate kg_explain output per run to avoid cross-disease contamination
  local kg_output_dir="${disease_work}/kg_output"
  mkdir -p "${kg_output_dir}"
  local kg_manifest="${kg_output_dir}/pipeline_manifest.json"

  local CROSS_SIGNATURE_META=""
  local CROSS_SIGREVERSE_INPUT=""
  local CROSS_SIGNATURE_SOURCE=""
  local sig_out_dir=""
  local cross_manifest_path=""
  local origin_manifest_path=""
  local bridge_cross=""
  local bridge_origin=""
  local step6_cross=""
  local step7_cross=""
  local step8_cross=""
  local step9_cross=""
  local step6_origin=""
  local step7_origin=""
  local step8_origin=""
  local step9_origin=""

  # ----- A) Cross route (optional, RUN_MODE=dual or cross_only) -----
  # [P1-5] Cross failure no longer blocks Origin route (in dual mode)
  # Priority: ARCHS4 config > dsmeta config
  if [[ "${RUN_MODE}" == "dual" || "${RUN_MODE}" == "cross_only" ]]; then
    local archs4_cfg="${ARCHS4_DIR}/configs/${disease_key}.yaml"
    local dsmeta_cfg="${DSMETA_DIR}/configs/${disease_key}.yaml"
    if [[ ! -f "${archs4_cfg}" && ! -f "${dsmeta_cfg}" ]]; then
      log "[WARN] Cross: no ARCHS4 or dsmeta config for ${disease_key}, skipping cross route"
      cross_status="failed"
      if [[ "${RUN_MODE}" == "cross_only" ]]; then
        fail_disease "${disease_key}" "${run_id}" "cross_no_config" "no ARCHS4 or dsmeta config for cross_only mode" "${cross_status}" "${origin_status}"
        return 1
      fi
    else
      # Run cross route in a block; failure sets cross_status but doesn't return
      cross_route_start=$SECONDS
      if run_cross_route "${disease_key}" "${disease_query}" "${run_id}" \
           "${disease_work}" "${dsmeta_cfg}" "${kg_output_dir}" "${kg_manifest}" "${neg_csv}"; then
        cross_status="success"
        cross_route_elapsed=$((SECONDS - cross_route_start))
      else
        cross_status="failed"
        cross_route_elapsed=$((SECONDS - cross_route_start))
        if [[ "${RUN_MODE}" == "cross_only" ]]; then
          fail_disease "${disease_key}" "${run_id}" "cross_route" "cross route failed in cross_only mode" "${cross_status}" "${origin_status}"
          return 1
        fi
        log "[WARN] Cross route failed for ${disease_key}, continuing with Origin route"
      fi
    fi
  else
    cross_status="skipped"
    log "[INFO] RUN_MODE=${RUN_MODE}: skip cross route for ${disease_key}"
  fi

  # ----- B) Origin route (skip if cross_only) -----
  if [[ "${RUN_MODE}" == "cross_only" ]]; then
    origin_status="skipped"
    log "[INFO] RUN_MODE=cross_only: skip origin route for ${disease_key}"
  fi

  if [[ "${RUN_MODE}" != "cross_only" ]]; then
  # ----- B) Origin route -----
  origin_route_start=$SECONDS
  next_step "${disease_key}" "Origin: KG ranking (CT.gov mode)"

  if ! run_cmd "Origin: kg ctgov" --timeout 3600 run_in_dir "${KG_DIR}" "${KG_PY}" -m src.kg_explain.cli pipeline --disease "${disease_key}" --version v5 --drug-source ctgov; then
    fail_disease "${disease_key}" "${run_id}" "origin_kg_ctgov" "kg ctgov pipeline failed" "${cross_status}" "${origin_status}"
    return 1
  fi

  # Always copy latest manifest from kg_explain output (Origin overwrites Cross manifest)
  copy_kg_manifest "${disease_key}" "${kg_manifest}"

  if ! run_cmd "Origin: manifest gate" kg_manifest_gate "${kg_manifest}" "ctgov"; then
    fail_disease "${disease_key}" "${run_id}" "origin_manifest_gate" "kg ctgov manifest check failed" "${cross_status}" "${origin_status}"
    return 1
  fi

  local origin_manifest_path="${disease_work}/pipeline_manifest_origin_ctgov.json"
  cp "${kg_manifest}" "${origin_manifest_path}"

  local kg_output_disease_dir="${KG_DIR}/output/${disease_key}"
  local kg_data_disease_dir="${KG_DIR}/data/${disease_key}"
  local bridge_origin="${kg_output_disease_dir}/bridge_origin_reassess.csv"
  # NOTE: legacy global bridge path removed — disease-specific bridge is required
  #       to prevent cross-disease contamination (see P1 #2 fix)

  local origin_cmd=("${KG_PY}" scripts/generate_disease_bridge.py)
  origin_cmd+=(--dtpd-rank "${kg_output_disease_dir}/dtpd_rank.csv")
  origin_cmd+=(--rank "${kg_output_disease_dir}/drug_disease_rank.csv")
  origin_cmd+=(--paths "${kg_output_disease_dir}/dtpd_paths.jsonl")
  origin_cmd+=(--chembl "${kg_data_disease_dir}/drug_chembl_map.csv")
  origin_cmd+=(--data-dir "${kg_data_disease_dir}")
  if [[ -n "${origin_ids_input}" ]]; then
    origin_cmd+=(--disease-ids "${origin_ids_input}")
    origin_ids_effective="${origin_ids_input}"
  else
    origin_cmd+=(--disease "${disease_query}")
  fi
  if [[ -n "${inject_path}" ]]; then
    origin_cmd+=(--inject "${inject_path}")
  fi
  origin_cmd+=(--out "${bridge_origin}")

  if ! run_cmd "Origin: generate bridge" run_in_dir "${KG_DIR}" "${origin_cmd[@]}"; then
    fail_disease "${disease_key}" "${run_id}" "origin_bridge" "generate_disease_bridge failed" "${cross_status}" "${origin_status}"
    return 1
  fi
  # Legacy bridge fallback removed: disease-specific bridge is now mandatory.
  # If bridge generation succeeded (line 1445) but output file is missing,
  # require_file below will fail the disease correctly.

  if [[ -z "${origin_ids_input}" ]]; then
    origin_ids_effective="$(derive_matched_ids_from_dtpd "${disease_query}" "${disease_key}")"
  fi

  if ! require_file "${bridge_origin}" "origin bridge"; then
    fail_disease "${disease_key}" "${run_id}" "origin_bridge_output" "missing bridge_origin_reassess.csv" "${cross_status}" "${origin_status}"
    return 1
  fi

  local step6_origin="${disease_work}/llm/step6_origin_reassess"
  local step7_origin="${disease_work}/llm/step7_origin_reassess"
  local step8_origin="${disease_work}/llm/step8_origin_reassess"
  local step9_origin="${disease_work}/llm/step9_origin_reassess"

  local llm_audit_dir="${disease_work}/llm"
  mkdir -p "${llm_audit_dir}"
  local origin_decision_stage1="${llm_audit_dir}/topn_decision_origin_stage1.json"
  local origin_quality_stage1="${llm_audit_dir}/topn_quality_origin_stage1.json"
  local origin_decision_stage2="${llm_audit_dir}/topn_decision_origin_stage2.json"
  local origin_quality_stage2="${llm_audit_dir}/topn_quality_origin_stage2.json"
  write_topn_decision_skip_json "${origin_decision_stage2}" "origin" "0" "stage1_not_completed"
  write_topn_quality_skip_json "${origin_quality_stage2}" "origin" "${TOPK_ORIGIN}" "${SHORTLIST_MIN_GO_ORIGIN}" "stage1_not_completed" "stage2"

  local origin_topn
  if ! origin_topn="$(resolve_topn "${TOPN_ORIGIN}" "${bridge_origin}" "origin" "stage1" "${TOPK_ORIGIN}" "${TOPN_STAGE1_MIN_ORIGIN}" "${TOPN_STAGE1_MAX_ORIGIN}" "${TOPN_CAP_ORIGIN}" "${TOPN_EXPAND_RATIO}" "${TOPN_STAGE1_MIN_ORIGIN}" "" "${origin_decision_stage1}")"; then
    fail_disease "${disease_key}" "${run_id}" "origin_topn_stage1" "cannot determine origin stage1 topn" "${cross_status}" "${origin_status}"
    return 1
  fi
  log "[INFO] Origin stage1 resolved topn=${origin_topn}"
  record_step_timing "kg_origin"
  mark_step_done "kg_origin"
  next_step "${disease_key}" "Origin: LLM evidence (Step6-9, topn=${origin_topn})"

  if ! run_route_llm_stage "Origin" "origin" "stage1" "${bridge_origin}" "${neg_csv}" "${step6_origin}" "${step7_origin}" "${step8_origin}" "${step9_origin}" "${disease_query}" "${origin_topn}" "${TOPK_ORIGIN}" "${SHORTLIST_MIN_GO_ORIGIN}" "${origin_quality_stage1}"; then
    fail_disease "${disease_key}" "${run_id}" "origin_stage1" "origin stage1 pipeline failed" "${cross_status}" "${origin_status}"
    return 1
  fi

  local origin_selected_stage="stage1"
  local origin_selected_topn="${origin_topn}"
  local origin_quality_passed
  origin_quality_passed="$(json_get_field "${origin_quality_stage1}" "quality_passed")"
  local origin_trigger_stage2
  origin_trigger_stage2="$(json_get_field "${origin_quality_stage1}" "trigger_stage2")"
  local origin_stage2_skip_reason
  if [[ "${TOPN_STAGE2_ENABLE}" != "1" ]]; then
    origin_stage2_skip_reason="stage2_disabled"
  elif ! is_auto_topn "${TOPN_ORIGIN}"; then
    origin_stage2_skip_reason="manual_topn_no_stage2"
  elif [[ "${TOPN_MAX_EXPAND_ROUNDS}" -le 0 ]]; then
    origin_stage2_skip_reason="max_expand_rounds_reached"
  elif [[ "${origin_trigger_stage2}" != "1" ]]; then
    origin_stage2_skip_reason="quality_gate_passed_no_expand"
  else
    origin_stage2_skip_reason="pending_stage2_decision"
  fi
  write_topn_decision_skip_json "${origin_decision_stage2}" "origin" "${origin_selected_topn}" "${origin_stage2_skip_reason}"
  write_topn_quality_skip_json "${origin_quality_stage2}" "origin" "${TOPK_ORIGIN}" "${SHORTLIST_MIN_GO_ORIGIN}" "${origin_stage2_skip_reason}"

  if [[ "${TOPN_STAGE2_ENABLE}" == "1" ]] && is_auto_topn "${TOPN_ORIGIN}" && [[ "${TOPN_MAX_EXPAND_ROUNDS}" -gt 0 ]] && [[ "${origin_trigger_stage2}" == "1" ]]; then
    local origin_topn_stage2
    if resolve_topn "${TOPN_ORIGIN}" "${bridge_origin}" "origin" "stage2" "${TOPK_ORIGIN}" "${TOPN_STAGE1_MIN_ORIGIN}" "${TOPN_STAGE1_MAX_ORIGIN}" "${TOPN_CAP_ORIGIN}" "${TOPN_EXPAND_RATIO}" "${TOPN_STAGE1_MIN_ORIGIN}" "${origin_topn}" "${origin_decision_stage2}" >/dev/null; then
      origin_topn_stage2="$(json_get_field "${origin_decision_stage2}" "resolved_topn")"
      local origin_should_expand
      origin_should_expand="$(json_get_field "${origin_decision_stage2}" "should_expand")"
      if [[ "${origin_should_expand}" == "1" ]]; then
        local step6_origin_stage2="${disease_work}/llm/step6_origin_reassess_stage2"
        local step7_origin_stage2="${disease_work}/llm/step7_origin_reassess_stage2"
        local step8_origin_stage2="${disease_work}/llm/step8_origin_reassess_stage2"
        local step9_origin_stage2="${disease_work}/llm/step9_origin_reassess_stage2"
        log "[INFO] Origin stage2 expansion: topn ${origin_topn} -> ${origin_topn_stage2}"
        if run_route_llm_stage "Origin" "origin" "stage2" "${bridge_origin}" "${neg_csv}" "${step6_origin_stage2}" "${step7_origin_stage2}" "${step8_origin_stage2}" "${step9_origin_stage2}" "${disease_query}" "${origin_topn_stage2}" "${TOPK_ORIGIN}" "${SHORTLIST_MIN_GO_ORIGIN}" "${origin_quality_stage2}"; then
          origin_selected_stage="stage2"
          origin_selected_topn="${origin_topn_stage2}"
          origin_quality_passed="$(json_get_field "${origin_quality_stage2}" "quality_passed")"
          step7_origin="${step7_origin_stage2}"
          step8_origin="${step8_origin_stage2}"
          step9_origin="${step9_origin_stage2}"
        else
          log "[WARN] Origin stage2 failed, keep stage1 outputs"
          write_topn_quality_skip_json "${origin_quality_stage2}" "origin" "${TOPK_ORIGIN}" "${SHORTLIST_MIN_GO_ORIGIN}" "stage2_pipeline_failed"
        fi
      else
        log "[INFO] Origin stage2 not needed: no additional topn expansion"
        write_topn_quality_skip_json "${origin_quality_stage2}" "origin" "${TOPK_ORIGIN}" "${SHORTLIST_MIN_GO_ORIGIN}" "stage2_no_additional_topn"
      fi
    else
      log "[WARN] Origin stage2 decision failed, keep stage1 outputs"
      write_topn_decision_skip_json "${origin_decision_stage2}" "origin" "${origin_selected_topn}" "stage2_decision_failed"
      write_topn_quality_skip_json "${origin_quality_stage2}" "origin" "${TOPK_ORIGIN}" "${SHORTLIST_MIN_GO_ORIGIN}" "stage2_decision_failed"
    fi
  fi

  annotate_route_manifest_summary "${step9_origin}" "origin" "${origin_selected_stage}" "${origin_selected_topn}" "${origin_quality_passed:-0}" "${origin_decision_stage1}" "${origin_quality_stage1}" "${origin_decision_stage2}" "${origin_quality_stage2}"

  origin_status="success"
  origin_route_elapsed=$((SECONDS - origin_route_start))
  record_step_timing "llm_origin"
  mark_step_done "llm_origin"

  fi  # end of: if [[ "${RUN_MODE}" != "cross_only" ]]

  # ----- A+B Cross-Validation Comparison -----
  local ab_comparison=""
  if [[ "${cross_status}" == "success" && "${origin_status}" == "success" \
        && -n "${bridge_cross}" && -f "${bridge_cross}" \
        && -n "${bridge_origin}" && -f "${bridge_origin}" ]]; then
    ab_comparison="${result_dir}/ab_comparison.csv"
    if run_cmd "A+B comparison" python3 "${ROOT_DIR}/ops/compare_ab_routes.py" \
         --bridge-a "${bridge_cross}" --bridge-b "${bridge_origin}" --out "${ab_comparison}"; then
      log "[OK] A+B cross-validation comparison saved: ${ab_comparison}"
    else
      log "[WARN] A+B comparison failed (non-fatal)"
      ab_comparison=""
    fi
  else
    log "[INFO] A+B comparison skipped (cross=${cross_status}, origin=${origin_status})"
  fi

  next_step "${disease_key}" "Archiving results"

  if ! archive_results \
    "${disease_key}" "${disease_query}" "${run_id}" "${run_date}" "${result_dir}" \
    "${cross_status}" "${origin_status}" "${origin_ids_input}" "${origin_ids_effective}" \
    "${inject_path}" "${CROSS_SIGNATURE_META}" "${CROSS_SIGREVERSE_INPUT}" \
    "${sig_out_dir}" "${cross_manifest_path}" "${origin_manifest_path}" \
    "${bridge_cross}" "${bridge_origin}" \
    "${step7_cross}" "${step8_cross}" "${step9_cross}" \
    "${step7_origin}" "${step8_origin}" "${step9_origin}"; then
    fail_disease "${disease_key}" "${run_id}" "archive" "failed to archive results" "${cross_status}" "${origin_status}"
    return 1
  fi

  # Delete large temporary path file only after origin bridge is archived.
  local evidence_paths_tmp_new="${KG_DIR}/output/${disease_key}/dtpd_paths.jsonl"
  local evidence_paths_tmp_legacy="${KG_DIR}/output/dtpd_paths.jsonl"
  if [[ -f "${evidence_paths_tmp_new}" ]]; then
    rm -f "${evidence_paths_tmp_new}"
    log "[CLEAN] Deleted temporary file: ${evidence_paths_tmp_new}"
  fi
  if [[ -f "${evidence_paths_tmp_legacy}" ]]; then
    rm -f "${evidence_paths_tmp_legacy}"
    log "[CLEAN] Deleted temporary file: ${evidence_paths_tmp_legacy}"
  fi

  record_step_timing "archive"
  mark_step_done "archive"

  # ── Disease summary banner ──
  local total_elapsed=$((SECONDS - DISEASE_START_TS))
  local cross_drugs=0 origin_drugs=0 ab_overlap=0
  [[ -n "${bridge_cross}" && -f "${bridge_cross}" ]] && cross_drugs="$(count_csv_rows "${bridge_cross}")"
  [[ -n "${bridge_origin}" && -f "${bridge_origin}" ]] && origin_drugs="$(count_csv_rows "${bridge_origin}")"
  [[ -n "${ab_comparison}" && -f "${ab_comparison}" ]] && ab_overlap="$(count_csv_rows "${ab_comparison}")"

  log_disease_summary "${disease_key}" "${run_id}" "${total_elapsed}" \
    "${cross_status}" "${origin_status}" \
    "${CROSS_SIGNATURE_SOURCE:-none}" "${cross_drugs}" "${origin_drugs}" \
    "${ab_overlap}" "${cross_route_elapsed}" "${origin_route_elapsed}"
  return 0
}

# [P1-5] Cross route extracted as separate function so failure doesn't block Origin
run_cross_route() {
  local disease_key="$1"
  local disease_query="$2"
  local run_id="$3"
  local disease_work="$4"
  local dsmeta_cfg="$5"
  local kg_output_dir="$6"
  local kg_manifest="$7"
  local neg_csv="$8"

  # --- Signature build: order determined by SIG_PRIORITY ---
  local archs4_cfg="${ARCHS4_DIR}/configs/${disease_key}.yaml"
  local signature_built=0
  local signature_built_by=""   # "archs4" or "dsmeta"
  local a4_status="skip" a4_detail="无配置"
  local ds_status="skip" ds_detail="跳过"
  local sig_start=$SECONDS

  next_step "${disease_key}" "Cross: 基因签名构建 (${SIG_PRIORITY}-first)"

  # -- helper: try ARCHS4 --
  _try_archs4() {
    if [[ ! -f "${archs4_cfg}" ]]; then
      a4_status="skip"; a4_detail="无配置"; return 1
    fi
    log "[INFO] Cross: trying ARCHS4 signature..."
    if run_cmd "Cross: archs4 (${disease_key})" --timeout 3600 run_in_dir "${ARCHS4_DIR}" "${ARCHS4_PY}" run.py --config "${archs4_cfg}"; then
      local archs4_sig_check="${ARCHS4_DIR}/outputs/${disease_key}/signature/sigreverse_input.json"
      if [[ -f "${archs4_sig_check}" ]] && python3 -c "
import json, sys
obj = json.loads(open(sys.argv[1]).read())
if len(obj.get('up',[])) == 0 and len(obj.get('down',[])) == 0:
    sys.exit(1)
" "${archs4_sig_check}" 2>/dev/null; then
        local a4_genes
        a4_genes="$(count_json_genes "${archs4_sig_check}")"
        a4_status="ok"; a4_detail="成功 (${a4_genes} up/down genes)"
        signature_built=1; signature_built_by="archs4"
      else
        a4_status="fail"; a4_detail="空签名 (0 genes)"
      fi
      # Clean ARCHS4 workdir
      local archs4_workdir="${ARCHS4_DIR}/work/${disease_key}"
      if [[ "${DSMETA_CLEANUP}" == "1" && -d "${archs4_workdir}" ]]; then
        local size_mb
        size_mb="$(du -sm "${archs4_workdir}" 2>/dev/null | cut -f1)"
        rm -rf "${archs4_workdir}"
        log "[CLEAN] Deleted archs4 workdir: ${archs4_workdir} (${size_mb:-?} MB freed)"
      fi
    else
      a4_status="fail"; a4_detail="pipeline 错误"
    fi
    [[ "${signature_built}" -eq 1 ]]
  }

  # -- helper: try dsmeta --
  _try_dsmeta() {
    if [[ ! -f "${dsmeta_cfg}" ]]; then
      ds_status="fail"; ds_detail="无配置"; return 1
    fi
    log "[INFO] Cross: trying dsmeta signature..."
    if ! check_disk_space "${DSMETA_DISK_MIN_GB:-8}"; then
      ds_status="fail"; ds_detail="磁盘不足 (需 ≥${DSMETA_DISK_MIN_GB:-8}GB)"; return 1
    fi
    if run_cmd "Cross: dsmeta (${disease_key})" --timeout 3600 run_in_dir "${DSMETA_DIR}" "${DSMETA_PY}" run.py --config "${dsmeta_cfg}"; then
      ds_status="ok"
      signature_built=1; signature_built_by="dsmeta"
      cleanup_dsmeta_workdir "${disease_key}"
    else
      ds_status="fail"; ds_detail="pipeline 错误"
      cleanup_dsmeta_workdir "${disease_key}"
    fi
    [[ "${signature_built}" -eq 1 ]]
  }

  # -- Execute in priority order --
  if [[ "${SIG_PRIORITY}" == "archs4" ]]; then
    _try_archs4 || { log "[INFO] Cross: falling back to dsmeta..."; _try_dsmeta || true; }
    [[ "${signature_built}" -eq 1 && "${a4_status}" == "ok" ]] && ds_detail="跳过 (ARCHS4 已成功)"
  else
    _try_dsmeta || { log "[INFO] Cross: falling back to ARCHS4..."; _try_archs4 || true; }
    [[ "${signature_built}" -eq 1 && "${ds_status}" == "ok" ]] && a4_detail="跳过 (dsmeta 已成功)"
  fi

  # -- Resolve which output files to use --
  if [[ "${signature_built}" -eq 0 ]]; then
    # Determine display order for the banner
    if [[ "${SIG_PRIORITY}" == "archs4" ]]; then
      log_signature_decision "ARCHS4" "${a4_status}" "${a4_detail}" "dsmeta" "${ds_status}" "${ds_detail}" "none"
    else
      log_signature_decision "dsmeta" "${ds_status}" "${ds_detail}" "ARCHS4" "${a4_status}" "${a4_detail}" "none"
    fi
    log "[ERROR] Cross: no signature source available — cannot proceed"
    return 1
  fi

  if ! resolve_cross_inputs "${disease_key}" "${signature_built_by}"; then
    if [[ "${SIG_PRIORITY}" == "archs4" ]]; then
      log_signature_decision "ARCHS4" "${a4_status}" "${a4_detail}" "dsmeta" "${ds_status}" "${ds_detail}" "none"
    else
      log_signature_decision "dsmeta" "${ds_status}" "${ds_detail}" "ARCHS4" "${a4_status}" "${a4_detail}" "none"
    fi
    log "[ERROR] Cross: cannot resolve cross input files"
    return 1
  fi

  # Count genes for the winning source's detail
  if [[ "${signature_built_by}" == "dsmeta" && "${ds_status}" == "ok" ]]; then
    local ds_genes
    ds_genes="$(count_json_genes "${CROSS_SIGREVERSE_INPUT}")"
    ds_detail="成功 (${ds_genes} up/down genes)"
  fi

  # Emit the decision banner (primary first, secondary second)
  local chosen_source="${CROSS_SIGNATURE_SOURCE:-none}"
  if [[ "${SIG_PRIORITY}" == "archs4" ]]; then
    log_signature_decision "ARCHS4" "${a4_status}" "${a4_detail}" "dsmeta" "${ds_status}" "${ds_detail}" "${chosen_source}"
  else
    log_signature_decision "dsmeta" "${ds_status}" "${ds_detail}" "ARCHS4" "${a4_status}" "${a4_detail}" "${chosen_source}"
  fi

  record_step_timing "signature_build"
  mark_step_done "signature_build"

  if ! run_cmd "Cross: validate signature_meta" validate_signature_meta_json "${CROSS_SIGNATURE_META}" "${disease_key}"; then
    log "[ERROR] Cross: invalid signature meta json"
    return 1
  fi

  if ! run_cmd "Cross: validate sigreverse_input" validate_sigreverse_input_json "${CROSS_SIGREVERSE_INPUT}" "${disease_key}"; then
    log "[ERROR] Cross: invalid sigreverse input json"
    return 1
  fi

  next_step "${disease_key}" "Cross: SigReverse (LINCS L1000)"

  sig_out_dir="${disease_work}/sigreverse_output"
  mkdir -p "${sig_out_dir}"
  if ! run_cmd "Cross: sigreverse" run_in_dir "${SIG_DIR}" "${SIG_PY}" scripts/run.py --config configs/default.yaml --in "${CROSS_SIGREVERSE_INPUT}" --out "${sig_out_dir}"; then
    log "[ERROR] Cross: sigreverse failed"
    return 1
  fi

  if ! require_file "${sig_out_dir}/drug_reversal_rank.csv" "sigreverse rank"; then
    log "[ERROR] Cross: missing sigreverse output"
    return 1
  fi
  record_step_timing "sigreverse"
  mark_step_done "sigreverse"

  next_step "${disease_key}" "Cross: KG ranking (signature mode)"

  if ! run_cmd "Cross: kg signature" --timeout 3600 run_in_dir "${KG_DIR}" "${KG_PY}" -m src.kg_explain.cli pipeline --disease "${disease_key}" --version v5 --drug-source signature --signature-path "${CROSS_SIGNATURE_META}"; then
    log "[ERROR] Cross: kg signature pipeline failed"
    return 1
  fi

  # Always copy latest manifest from kg_explain output
  copy_kg_manifest "${disease_key}" "${kg_manifest}"

  if ! run_cmd "Cross: manifest gate" kg_manifest_gate "${kg_manifest}" "signature"; then
    log "[ERROR] Cross: kg signature manifest check failed"
    return 1
  fi

  cross_manifest_path="${disease_work}/pipeline_manifest_cross_signature.json"
  cp "${kg_manifest}" "${cross_manifest_path}"

  bridge_cross="${KG_DIR}/output/${disease_key}/bridge_repurpose_cross.csv"
  # Legacy bridge fallback removed: disease-specific bridge is mandatory
  # to prevent cross-disease contamination (see P1 #2 fix)
  if ! require_file "${bridge_cross}" "cross bridge"; then
    log "[ERROR] Cross: missing bridge_repurpose_cross.csv"
    return 1
  fi

  step6_cross="${disease_work}/llm/step6_repurpose_cross"
  step7_cross="${disease_work}/llm/step7_repurpose_cross"
  step8_cross="${disease_work}/llm/step8_repurpose_cross"
  step9_cross="${disease_work}/llm/step9_repurpose_cross"

  local llm_audit_dir="${disease_work}/llm"
  mkdir -p "${llm_audit_dir}"
  local cross_decision_stage1="${llm_audit_dir}/topn_decision_cross_stage1.json"
  local cross_quality_stage1="${llm_audit_dir}/topn_quality_cross_stage1.json"
  local cross_decision_stage2="${llm_audit_dir}/topn_decision_cross_stage2.json"
  local cross_quality_stage2="${llm_audit_dir}/topn_quality_cross_stage2.json"
  write_topn_decision_skip_json "${cross_decision_stage2}" "cross" "0" "stage1_not_completed"
  write_topn_quality_skip_json "${cross_quality_stage2}" "cross" "${TOPK_CROSS}" "${SHORTLIST_MIN_GO_CROSS}" "stage1_not_completed" "stage2"

  local cross_topn
  if ! cross_topn="$(resolve_topn "${TOPN_CROSS}" "${bridge_cross}" "cross" "stage1" "${TOPK_CROSS}" "${TOPN_STAGE1_MIN_CROSS}" "${TOPN_STAGE1_MAX_CROSS}" "${TOPN_CAP_CROSS}" "${TOPN_EXPAND_RATIO}" "${TOPN_STAGE1_MIN_CROSS}" "" "${cross_decision_stage1}")"; then
    log "[ERROR] Cross: cannot determine stage1 topn"
    return 1
  fi
  log "[INFO] Cross stage1 resolved topn=${cross_topn}"
  record_step_timing "kg_cross"
  mark_step_done "kg_cross"
  next_step "${disease_key}" "Cross: LLM evidence (Step6-9, topn=${cross_topn})"

  if ! run_route_llm_stage "Cross" "cross" "stage1" "${bridge_cross}" "${neg_csv}" "${step6_cross}" "${step7_cross}" "${step8_cross}" "${step9_cross}" "${disease_query}" "${cross_topn}" "${TOPK_CROSS}" "${SHORTLIST_MIN_GO_CROSS}" "${cross_quality_stage1}"; then
    log "[ERROR] Cross: stage1 pipeline failed"
    return 1
  fi

  local cross_selected_stage="stage1"
  local cross_selected_topn="${cross_topn}"
  local cross_quality_passed
  cross_quality_passed="$(json_get_field "${cross_quality_stage1}" "quality_passed")"
  local cross_trigger_stage2
  cross_trigger_stage2="$(json_get_field "${cross_quality_stage1}" "trigger_stage2")"
  local cross_stage2_skip_reason
  if [[ "${TOPN_STAGE2_ENABLE}" != "1" ]]; then
    cross_stage2_skip_reason="stage2_disabled"
  elif ! is_auto_topn "${TOPN_CROSS}"; then
    cross_stage2_skip_reason="manual_topn_no_stage2"
  elif [[ "${TOPN_MAX_EXPAND_ROUNDS}" -le 0 ]]; then
    cross_stage2_skip_reason="max_expand_rounds_reached"
  elif [[ "${cross_trigger_stage2}" != "1" ]]; then
    cross_stage2_skip_reason="quality_gate_passed_no_expand"
  else
    cross_stage2_skip_reason="pending_stage2_decision"
  fi
  write_topn_decision_skip_json "${cross_decision_stage2}" "cross" "${cross_selected_topn}" "${cross_stage2_skip_reason}"
  write_topn_quality_skip_json "${cross_quality_stage2}" "cross" "${TOPK_CROSS}" "${SHORTLIST_MIN_GO_CROSS}" "${cross_stage2_skip_reason}"

  if [[ "${TOPN_STAGE2_ENABLE}" == "1" ]] && is_auto_topn "${TOPN_CROSS}" && [[ "${TOPN_MAX_EXPAND_ROUNDS}" -gt 0 ]] && [[ "${cross_trigger_stage2}" == "1" ]]; then
    local cross_topn_stage2
    if resolve_topn "${TOPN_CROSS}" "${bridge_cross}" "cross" "stage2" "${TOPK_CROSS}" "${TOPN_STAGE1_MIN_CROSS}" "${TOPN_STAGE1_MAX_CROSS}" "${TOPN_CAP_CROSS}" "${TOPN_EXPAND_RATIO}" "${TOPN_STAGE1_MIN_CROSS}" "${cross_topn}" "${cross_decision_stage2}" >/dev/null; then
      cross_topn_stage2="$(json_get_field "${cross_decision_stage2}" "resolved_topn")"
      local cross_should_expand
      cross_should_expand="$(json_get_field "${cross_decision_stage2}" "should_expand")"
      if [[ "${cross_should_expand}" == "1" ]]; then
        local step6_cross_stage2="${disease_work}/llm/step6_repurpose_cross_stage2"
        local step7_cross_stage2="${disease_work}/llm/step7_repurpose_cross_stage2"
        local step8_cross_stage2="${disease_work}/llm/step8_repurpose_cross_stage2"
        local step9_cross_stage2="${disease_work}/llm/step9_repurpose_cross_stage2"
        log "[INFO] Cross stage2 expansion: topn ${cross_topn} -> ${cross_topn_stage2}"
        if run_route_llm_stage "Cross" "cross" "stage2" "${bridge_cross}" "${neg_csv}" "${step6_cross_stage2}" "${step7_cross_stage2}" "${step8_cross_stage2}" "${step9_cross_stage2}" "${disease_query}" "${cross_topn_stage2}" "${TOPK_CROSS}" "${SHORTLIST_MIN_GO_CROSS}" "${cross_quality_stage2}"; then
          cross_selected_stage="stage2"
          cross_selected_topn="${cross_topn_stage2}"
          cross_quality_passed="$(json_get_field "${cross_quality_stage2}" "quality_passed")"
          step7_cross="${step7_cross_stage2}"
          step8_cross="${step8_cross_stage2}"
          step9_cross="${step9_cross_stage2}"
        else
          log "[WARN] Cross stage2 failed, keep stage1 outputs"
          write_topn_quality_skip_json "${cross_quality_stage2}" "cross" "${TOPK_CROSS}" "${SHORTLIST_MIN_GO_CROSS}" "stage2_pipeline_failed"
        fi
      else
        log "[INFO] Cross stage2 not needed: no additional topn expansion"
        write_topn_quality_skip_json "${cross_quality_stage2}" "cross" "${TOPK_CROSS}" "${SHORTLIST_MIN_GO_CROSS}" "stage2_no_additional_topn"
      fi
    else
      log "[WARN] Cross stage2 decision failed, keep stage1 outputs"
      write_topn_decision_skip_json "${cross_decision_stage2}" "cross" "${cross_selected_topn}" "stage2_decision_failed"
      write_topn_quality_skip_json "${cross_quality_stage2}" "cross" "${TOPK_CROSS}" "${SHORTLIST_MIN_GO_CROSS}" "stage2_decision_failed"
    fi
  fi

  annotate_route_manifest_summary "${step9_cross}" "cross" "${cross_selected_stage}" "${cross_selected_topn}" "${cross_quality_passed:-0}" "${cross_decision_stage1}" "${cross_quality_stage1}" "${cross_decision_stage2}" "${cross_quality_stage2}"

  record_step_timing "llm_cross"
  mark_step_done "llm_cross"

  return 0
}

if [[ ! -f "${DISEASE_LIST_FILE}" ]]; then
  log "[ERROR] Disease list not found: ${DISEASE_LIST_FILE}"
  exit 1
fi

# ── Pre-flight checks ──

DISK_MIN_GB="${DISK_MIN_GB:-5}"            # Minimum free disk space (GB)

check_disk_space() {
  local min_gb="$1"
  local avail_kb
  avail_kb="$(df -k "${ROOT_DIR}" | awk 'NR==2 {print $4}')"
  local avail_gb=$(( avail_kb / 1024 / 1024 ))
  if [[ "${avail_gb}" -lt "${min_gb}" ]]; then
    log "[CRITICAL] Disk space low: ${avail_gb}GB free < ${min_gb}GB minimum"
    return 1
  fi
  log "[OK] Disk space: ${avail_gb}GB free"
  return 0
}

check_api_health() {
  # Check critical external APIs. Returns 0 if at least one core API is reachable.
  local ok=0
  local total=0

  # CT.gov (core — used by both routes)
  total=$((total + 1))
  if curl -sf --max-time 10 "https://clinicaltrials.gov/api/v2/studies?pageSize=1" >/dev/null 2>&1; then
    ok=$((ok + 1))
  else
    log "[WARN] API unreachable: CT.gov"
  fi

  # ChEMBL (core — kg_explain)
  total=$((total + 1))
  if curl -sf --max-time 10 "https://www.ebi.ac.uk/chembl/api/data/status.json" >/dev/null 2>&1; then
    ok=$((ok + 1))
  else
    log "[WARN] API unreachable: ChEMBL"
  fi

  # PubMed (core — LLM+RAG step6)
  total=$((total + 1))
  if curl -sf --max-time 10 "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi" >/dev/null 2>&1; then
    ok=$((ok + 1))
  else
    log "[WARN] API unreachable: PubMed"
  fi

  log "[INFO] API health: ${ok}/${total} reachable"
  if [[ "${ok}" -eq 0 ]]; then
    log "[ERROR] All APIs unreachable — network down or maintenance window"
    return 1
  fi
  return 0
}

check_ollama_health() {
  # Check if Ollama is running and has the required model
  local ollama_host="${OLLAMA_HOST:-http://localhost:11434}"
  if ! curl -sf --max-time 5 "${ollama_host}/api/tags" >/dev/null 2>&1; then
    log "[WARN] Ollama not reachable at ${ollama_host}"
    return 1
  fi
  log "[OK] Ollama reachable at ${ollama_host}"
  return 0
}

case "${RUN_MODE}" in
  dual|origin_only|cross_only)
    ;;
  *)
    log "[ERROR] Invalid RUN_MODE='${RUN_MODE}'. Allowed: dual | origin_only | cross_only"
    exit 1
    ;;
esac

acquire_lock
trap 'release_lock' EXIT INT TERM

log "Runner start | mode=${RUN_MODE} | profile=${TOPN_PROFILE}"
log "  dirs: root=${ROOT_DIR} | disease_list=${DISEASE_LIST_FILE}"
log "  topn: origin=${TOPN_ORIGIN}[${TOPN_STAGE1_MIN_ORIGIN}-${TOPN_STAGE1_MAX_ORIGIN},cap=${TOPN_CAP_ORIGIN}] cross=${TOPN_CROSS}[${TOPN_STAGE1_MIN_CROSS}-${TOPN_STAGE1_MAX_CROSS},cap=${TOPN_CAP_CROSS}] stage2=${TOPN_STAGE2_ENABLE} ratio=${TOPN_EXPAND_RATIO}"
log "  topk: origin=${TOPK_ORIGIN} cross=${TOPK_CROSS} | min_go: origin=${SHORTLIST_MIN_GO_ORIGIN} cross=${SHORTLIST_MIN_GO_CROSS}"
log "  step6: retmax=${STEP6_PUBMED_RETMAX} parse=${STEP6_PUBMED_PARSE_MAX} rerank=${STEP6_MAX_RERANK_DOCS} evidence=${STEP6_MAX_EVIDENCE_DOCS}"
log "  limits: timeout=${STEP_TIMEOUT}s disk_min=${DISK_MIN_GB}GB cleanup=${DSMETA_CLEANUP} contract=${STRICT_CONTRACT}"

ok_count=0
fail_count=0
failed_diseases=""
run_start_ts=$SECONDS

cleanup_retention

log "═══ Pipeline 开始 ═══════════════════════════════════════"

# Pre-flight: disk space
if ! check_disk_space "${DISK_MIN_GB}"; then
  log "[FATAL] Low disk space. Aborting."
  exit 1
fi

# Pre-flight: API reachability
if ! check_api_health; then
  log "[FATAL] APIs unreachable. Aborting."
  exit 1
fi

# Pre-flight: Ollama (non-blocking — warn but continue, step6 will fail and quarantine)
check_ollama_health || true

while IFS='|' read -r raw_key raw_query raw_origin_ids raw_inject || [[ -n "${raw_key:-}" ]]; do
  local_key="$(trim "${raw_key:-}")"
  local_query="$(trim "${raw_query:-}")"
  local_origin_ids="$(trim "${raw_origin_ids:-}")"
  local_inject="$(trim "${raw_inject:-}")"

  if [[ -z "${local_key}" ]]; then
    continue
  fi
  if [[ "${local_key:0:1}" == "#" ]]; then
    continue
  fi
  if [[ -z "${local_query}" ]]; then
    local_query="${local_key//_/ }"
  fi

  if process_disease "${local_key}" "${local_query}" "${local_origin_ids}" "${local_inject}"; then
    ok_count=$((ok_count + 1))
  else
    fail_count=$((fail_count + 1))
    if [[ -z "${failed_diseases}" ]]; then
      failed_diseases="${local_key}"
    else
      failed_diseases="${failed_diseases}, ${local_key}"
    fi
    log "[ERROR] Disease failed: ${local_key}"
  fi
done < "${DISEASE_LIST_FILE}"

run_elapsed=$((SECONDS - run_start_ts))
run_dur="$(format_duration "${run_elapsed}")"
log "═══ Pipeline 完成 ═══════════════════════════════════════"
log "  成功: ${ok_count}    失败: ${fail_count}    耗时: ${run_dur}"
if [[ -n "${failed_diseases}" ]]; then
  log "  失败疾病: ${failed_diseases}"
fi
log "═════════════════════════════════════════════════════════════"
