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
#
# Disease list format (pipe-separated, 4 columns):
#   disease_key|disease_query|origin_disease_ids(optional)|inject_yaml(optional)
#
# Example:
#   atherosclerosis|atherosclerosis|EFO_0003914,MONDO_0021661|kg_explain/configs/inject_atherosclerosis.yaml
#   type2_diabetes|type 2 diabetes|EFO_0001360|
#
# 环境变量:
#   SLEEP_SECONDS       — 每轮循环间隔 (默认 300s)
#   TOPN_PROFILE        — topn策略档位: stable|balanced|recall (默认 stable)
#   TOPN_CROSS          — Direction A bridge topn: auto|int (默认 auto)
#   TOPN_ORIGIN         — Direction B bridge topn: auto|int (默认 auto)
#   TOPN_STAGE2_ENABLE  — 质量未达标时是否允许二阶段扩容 (默认 1)
#   TOPN_MAX_EXPAND_ROUNDS — 最大扩容轮数 (默认 1, 禁止无限扩容)
#   TOPN_CAP_ORIGIN     — origin路线扩容上限 (默认按档位, stable=18)
#   TOPN_CAP_CROSS      — cross路线扩容上限 (默认按档位, stable=14)
#   TOPN_EXPAND_RATIO   — stage2扩容阈值: score >= ratio*top_score (默认 0.30)
#   TOPN_STAGE1_MIN_ORIGIN / TOPN_STAGE1_MAX_ORIGIN — origin stage1边界
#   TOPN_STAGE1_MIN_CROSS / TOPN_STAGE1_MAX_CROSS   — cross stage1边界
#   SHORTLIST_MIN_GO_ORIGIN / SHORTLIST_MIN_GO_CROSS — 路由最低GO数量阈值
#   STEP6_PUBMED_RETMAX / STEP6_PUBMED_PARSE_MAX / STEP6_MAX_RERANK_DOCS / STEP6_MAX_EVIDENCE_DOCS
#                        — Step6单药预算上限参数
#   STEP_TIMEOUT        — 每步超时 (默认 1800s)
#   MAX_CYCLES          — 最大循环次数, 0=无限 (默认 0)
#   LOG_RETENTION_DAYS  — 日志保留天数 (默认 30)
#   CACHE_RETENTION_DAYS — kg_explain HTTP缓存保留天数 (默认 1)
#   DISK_MIN_GB         — 最低可用磁盘空间GB (默认 5)
#   API_BACKOFF_SECONDS — API全挂时等待时间 (默认 600s)
#   DSMETA_STEP_TIMEOUT — dsmeta每步超时 (默认 1800s, 由dsmeta run.py读取)
#   DSMETA_CLEANUP      — dsmeta跑完后是否自动清理workdir (默认 1=清理, 0=保留)
#                         清理后释放磁盘 (GEO expr.tsv 每个50-100MB), 不影响 outputs
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

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DSMETA_DIR="${ROOT_DIR}/dsmeta_signature_pipeline"
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

DSMETA_PY="${DSMETA_DIR}/.venv/bin/python3"
SIG_PY="${SIG_DIR}/.venv/bin/python3"
KG_PY="${KG_DIR}/.venv/bin/python3"
LLM_PY="${LLM_DIR}/.venv/bin/python3"

if [[ ! -x "${DSMETA_PY}" ]]; then DSMETA_PY="python3"; fi
if [[ ! -x "${SIG_PY}" ]]; then SIG_PY="python3"; fi
if [[ ! -x "${KG_PY}" ]]; then KG_PY="python3"; fi
if [[ ! -x "${LLM_PY}" ]]; then LLM_PY="python3"; fi

SLEEP_SECONDS="${SLEEP_SECONDS:-300}"
SCREEN_MAX_STUDIES="${SCREEN_MAX_STUDIES:-500}"
TOPN_PROFILE="${TOPN_PROFILE:-stable}"
TOPN_CROSS="${TOPN_CROSS:-auto}"
TOPN_ORIGIN="${TOPN_ORIGIN:-auto}"
TOPN_STAGE2_ENABLE="${TOPN_STAGE2_ENABLE:-1}"
TOPN_MAX_EXPAND_ROUNDS="${TOPN_MAX_EXPAND_ROUNDS:-1}"
TOPN_CAP_ORIGIN="${TOPN_CAP_ORIGIN:-}"
TOPN_CAP_CROSS="${TOPN_CAP_CROSS:-}"
TOPN_EXPAND_RATIO="${TOPN_EXPAND_RATIO:-0.30}"
TOPN_STAGE1_MIN_ORIGIN="${TOPN_STAGE1_MIN_ORIGIN:-}"
TOPN_STAGE1_MAX_ORIGIN="${TOPN_STAGE1_MAX_ORIGIN:-}"
TOPN_STAGE1_MIN_CROSS="${TOPN_STAGE1_MIN_CROSS:-}"
TOPN_STAGE1_MAX_CROSS="${TOPN_STAGE1_MAX_CROSS:-}"
SHORTLIST_MIN_GO_ORIGIN="${SHORTLIST_MIN_GO_ORIGIN:-3}"
SHORTLIST_MIN_GO_CROSS="${SHORTLIST_MIN_GO_CROSS:-2}"
TOPK_CROSS="${TOPK_CROSS:-5}"
TOPK_ORIGIN="${TOPK_ORIGIN:-10}"
STEP6_PUBMED_RETMAX="${STEP6_PUBMED_RETMAX:-120}"
STEP6_PUBMED_PARSE_MAX="${STEP6_PUBMED_PARSE_MAX:-60}"
STEP6_MAX_RERANK_DOCS="${STEP6_MAX_RERANK_DOCS:-40}"
STEP6_MAX_EVIDENCE_DOCS="${STEP6_MAX_EVIDENCE_DOCS:-12}"
STRICT_CONTRACT="${STRICT_CONTRACT:-1}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"
LOG_RETENTION_DAYS="${LOG_RETENTION_DAYS:-30}"
MAX_CYCLES="${MAX_CYCLES:-0}"
RUN_MODE="${RUN_MODE:-dual}" # dual | origin_only
STEP_TIMEOUT="${STEP_TIMEOUT:-1800}" # 30 min per step default
LOCK_NAME="${LOCK_NAME:-${RUN_MODE}}"
LOCK_FILE="${STATE_ROOT}/runner_${LOCK_NAME}.lock"

TOPN_POLICY_PY="${ROOT_DIR}/ops/topn_policy.py"

apply_topn_profile_defaults() {
  case "${TOPN_PROFILE}" in
    stable)
      TOPN_CAP_ORIGIN="${TOPN_CAP_ORIGIN:-18}"
      TOPN_CAP_CROSS="${TOPN_CAP_CROSS:-14}"
      TOPN_STAGE1_MIN_ORIGIN="${TOPN_STAGE1_MIN_ORIGIN:-12}"
      TOPN_STAGE1_MAX_ORIGIN="${TOPN_STAGE1_MAX_ORIGIN:-14}"
      TOPN_STAGE1_MIN_CROSS="${TOPN_STAGE1_MIN_CROSS:-10}"
      TOPN_STAGE1_MAX_CROSS="${TOPN_STAGE1_MAX_CROSS:-12}"
      ;;
    balanced)
      TOPN_CAP_ORIGIN="${TOPN_CAP_ORIGIN:-24}"
      TOPN_CAP_CROSS="${TOPN_CAP_CROSS:-18}"
      TOPN_STAGE1_MIN_ORIGIN="${TOPN_STAGE1_MIN_ORIGIN:-14}"
      TOPN_STAGE1_MAX_ORIGIN="${TOPN_STAGE1_MAX_ORIGIN:-18}"
      TOPN_STAGE1_MIN_CROSS="${TOPN_STAGE1_MIN_CROSS:-12}"
      TOPN_STAGE1_MAX_CROSS="${TOPN_STAGE1_MAX_CROSS:-16}"
      ;;
    recall)
      TOPN_CAP_ORIGIN="${TOPN_CAP_ORIGIN:-30}"
      TOPN_CAP_CROSS="${TOPN_CAP_CROSS:-24}"
      TOPN_STAGE1_MIN_ORIGIN="${TOPN_STAGE1_MIN_ORIGIN:-16}"
      TOPN_STAGE1_MAX_ORIGIN="${TOPN_STAGE1_MAX_ORIGIN:-24}"
      TOPN_STAGE1_MIN_CROSS="${TOPN_STAGE1_MIN_CROSS:-14}"
      TOPN_STAGE1_MAX_CROSS="${TOPN_STAGE1_MAX_CROSS:-20}"
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

  # append step output to runner log
  if [[ -s "${step_log}" ]]; then
    {
      echo "--- [${label}] stdout/stderr begin ---"
      tail -100 "${step_log}"
      echo "--- [${label}] stdout/stderr end ---"
    } >> "${RUN_LOG}"
  fi

  if [[ "${rc}" -eq 0 ]]; then
    log "[OK] ${label}"
    return 0
  fi
  if [[ "${rc}" -eq 124 ]]; then
    log "[ERROR] ${label} timed out after ${timeout_sec}s"
  else
    log "[ERROR] ${label} failed (rc=${rc})"
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
# Enabled by default (DSMETA_CLEANUP=1). Set DSMETA_CLEANUP=0 to keep workdir for debugging.
DSMETA_CLEANUP="${DSMETA_CLEANUP:-1}"
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

cleanup_retention() {
  cleanup_old_runs "${WORK_ROOT}"
  cleanup_old_runs "${QUARANTINE_ROOT}"
  cleanup_old_logs
  cleanup_kg_cache
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

count_csv_rows() {
  local csv_path="$1"
  python3 - "${csv_path}" <<'PY'
import csv
import sys
from pathlib import Path

p = Path(sys.argv[1])
if not p.exists():
    print("0")
    raise SystemExit(0)
count = 0
with p.open("r", encoding="utf-8", newline="") as f:
    reader = csv.reader(f)
    next(reader, None)  # header
    for _ in reader:
        count += 1
print(count)
PY
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
  [[ "${v,,}" == "auto" ]]
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
    python3 "${TOPN_POLICY_PY}" decide
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

  if ! python3 "${TOPN_POLICY_PY}" quality \
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

  if ! run_cmd "${route_title}: step6 (${stage})" --timeout 7200 run_in_dir "${LLM_DIR}" "${LLM_PY}" scripts/step6_evidence_extraction.py --rank_in "${bridge_csv}" --neg "${neg_csv}" --out "${step6_dir}" --target_disease "${disease_query}" --topn "${topn}" --pubmed_retmax "${STEP6_PUBMED_RETMAX}" --pubmed_parse_max "${STEP6_PUBMED_PARSE_MAX}" --max_rerank_docs "${STEP6_MAX_RERANK_DOCS}" --max_evidence_docs "${STEP6_MAX_EVIDENCE_DOCS}"; then
    write_topn_quality_skip_json "${quality_json}" "${route_key}" "${topk}" "${min_go}" "${stage}_step6_failed" "${stage}"
    return 1
  fi

  if ! run_cmd "${route_title}: step7 (${stage})" --timeout 3600 run_in_dir "${LLM_DIR}" "${LLM_PY}" scripts/step7_score_and_gate.py --input "${step6_dir}" --out "${step7_dir}" --strict_contract "${STRICT_CONTRACT}"; then
    write_topn_quality_skip_json "${quality_json}" "${route_key}" "${topk}" "${min_go}" "${stage}_step7_failed" "${stage}"
    return 1
  fi

  if ! run_cmd "${route_title}: step8 (${stage})" --timeout 3600 run_in_dir "${LLM_DIR}" "${LLM_PY}" scripts/step8_candidate_pack.py --step7_dir "${step7_dir}" --neg "${neg_csv}" --bridge "${bridge_csv}" --outdir "${step8_dir}" --target_disease "${disease_query}" --topk "${topk}" --include_explore 1 --strict_contract "${STRICT_CONTRACT}"; then
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

validate_signature_meta_json() {
  local json_path="$1"
  local disease_key="$2"
  python3 - "${json_path}" "${disease_key}" <<'PY'
import json
import re
import sys
from pathlib import Path

p = Path(sys.argv[1])
disease_key = sys.argv[2]
if not p.exists():
    print(f"missing file: {p}", file=sys.stderr)
    raise SystemExit(2)
obj = json.loads(p.read_text(encoding="utf-8"))
for k in ("name", "up_genes", "down_genes"):
    if k not in obj:
        print(f"missing key: {k}", file=sys.stderr)
        raise SystemExit(3)
if not isinstance(obj.get("up_genes"), list) or not isinstance(obj.get("down_genes"), list):
    print("up_genes/down_genes must be lists", file=sys.stderr)
    raise SystemExit(4)

def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())

name_norm = norm(str(obj.get("name", "")))
key_norm = norm(disease_key)
if key_norm and key_norm not in name_norm:
    print(f"signature name mismatch: disease_key={disease_key}, name={obj.get('name')}", file=sys.stderr)
    raise SystemExit(5)
PY
}

validate_sigreverse_input_json() {
  local json_path="$1"
  local disease_key="$2"
  python3 - "${json_path}" "${disease_key}" <<'PY'
import json
import re
import sys
from pathlib import Path

p = Path(sys.argv[1])
disease_key = sys.argv[2]
if not p.exists():
    print(f"missing file: {p}", file=sys.stderr)
    raise SystemExit(2)
obj = json.loads(p.read_text(encoding="utf-8"))
for k in ("name", "up", "down"):
    if k not in obj:
        print(f"missing key: {k}", file=sys.stderr)
        raise SystemExit(3)
if not isinstance(obj.get("up"), list) or not isinstance(obj.get("down"), list):
    print("up/down must be lists", file=sys.stderr)
    raise SystemExit(4)

def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())

name_norm = norm(str(obj.get("name", "")))
key_norm = norm(disease_key)
if key_norm and key_norm not in name_norm:
    print(f"sigreverse input name mismatch: disease_key={disease_key}, name={obj.get('name')}", file=sys.stderr)
    raise SystemExit(5)
PY
}

resolve_cross_inputs() {
  local disease_key="$1"

  local meta_per="${DSMETA_DIR}/outputs/${disease_key}/signature/disease_signature_meta.json"
  local meta_legacy="${DSMETA_DIR}/outputs/signature/disease_signature_meta.json"
  local sig_per="${DSMETA_DIR}/outputs/${disease_key}/signature/sigreverse_input.json"
  local sig_legacy="${DSMETA_DIR}/outputs/signature/sigreverse_input.json"

  CROSS_SIGNATURE_META=""
  CROSS_SIGREVERSE_INPUT=""

  if [[ -f "${meta_per}" ]]; then
    CROSS_SIGNATURE_META="${meta_per}"
  elif [[ -f "${meta_legacy}" ]]; then
    CROSS_SIGNATURE_META="${meta_legacy}"
  fi

  if [[ -f "${sig_per}" ]]; then
    CROSS_SIGREVERSE_INPUT="${sig_per}"
  elif [[ -f "${sig_legacy}" ]]; then
    CROSS_SIGREVERSE_INPUT="${sig_legacy}"
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
NON_CRITICAL = {"Pathway", "v5 排序", "v4 排序", "v3 排序"}
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

derive_matched_ids_from_v3() {
  local disease_query="$1"
  local v3_path="${KG_DIR}/output/drug_disease_rank_v3.csv"
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

  PHASE="${phase}" \
  MESSAGE="${message}" \
  CROSS_STATUS="${cross_status}" \
  ORIGIN_STATUS="${origin_status}" \
  RUN_ID="${run_id}" \
  DISEASE_KEY="${disease_key}" \
  RUN_LOG_PATH="${RUN_LOG}" \
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

  local cross_status="not_run"
  local origin_status="not_run"
  local origin_ids_effective="${origin_ids_input}"

  local inject_path=""
  if ! inject_path="$(resolve_path_optional "${inject_raw}")"; then
    fail_disease "${disease_key}" "${run_id}" "validate_inject" "inject file not found: ${inject_raw}" "${cross_status}" "${origin_status}"
    return 1
  fi

  log "=== Disease start: key=${disease_key}, query=${disease_query}, origin_ids=${origin_ids_input:-N/A}, inject=${inject_path:-N/A}, run_id=${run_id} ==="

  if ! ensure_kg_disease_config "${disease_key}" "${disease_query}"; then
    fail_disease "${disease_key}" "${run_id}" "ensure_kg_config" "cannot ensure kg disease config" "${cross_status}" "${origin_status}"
    return 1
  fi

  local screen_out="${disease_work}/screen"
  # [P0-2] Fixed: was poolA_negative_drug_level.csv, actual output is poolA_drug_level.csv
  local neg_csv="${screen_out}/poolA_drug_level.csv"
  if ! run_cmd "Screen drugs" run_in_dir "${LLM_DIR}" "${LLM_PY}" scripts/screen_drugs.py --disease "${disease_query}" --max-studies "${SCREEN_MAX_STUDIES}" --outdir "${screen_out}"; then
    fail_disease "${disease_key}" "${run_id}" "screen_drugs" "screen_drugs failed" "${cross_status}" "${origin_status}"
    return 1
  fi

  if ! require_file "${neg_csv}" "drug level csv"; then
    fail_disease "${disease_key}" "${run_id}" "screen_output" "missing drug level csv" "${cross_status}" "${origin_status}"
    return 1
  fi

  # [P1-4] Isolate kg_explain output per run to avoid cross-disease contamination
  local kg_output_dir="${disease_work}/kg_output"
  mkdir -p "${kg_output_dir}"
  local kg_manifest="${kg_output_dir}/pipeline_manifest.json"

  local CROSS_SIGNATURE_META=""
  local CROSS_SIGREVERSE_INPUT=""
  local sig_out_dir=""
  local cross_manifest_path=""
  local bridge_cross=""
  local step6_cross=""
  local step7_cross=""
  local step8_cross=""
  local step9_cross=""

  # ----- A) Cross route (optional, RUN_MODE=dual) -----
  # [P1-5] Cross failure no longer blocks Origin route
  if [[ "${RUN_MODE}" == "dual" ]]; then
    local dsmeta_cfg="${DSMETA_DIR}/configs/${disease_key}.yaml"
    if [[ ! -f "${dsmeta_cfg}" ]]; then
      log "[WARN] Cross: missing dsmeta config: ${dsmeta_cfg}, skipping cross route"
      cross_status="failed"
    else
      # Run cross route in a block; failure sets cross_status but doesn't return
      if run_cross_route "${disease_key}" "${disease_query}" "${run_id}" \
           "${disease_work}" "${dsmeta_cfg}" "${kg_output_dir}" "${kg_manifest}" "${neg_csv}"; then
        cross_status="success"
      else
        cross_status="failed"
        log "[WARN] Cross route failed for ${disease_key}, continuing with Origin route"
      fi
    fi
  else
    cross_status="skipped"
    log "[INFO] RUN_MODE=${RUN_MODE}: skip cross route for ${disease_key}"
  fi

  # ----- B) Origin route -----
  if ! run_cmd "Origin: kg ctgov" --timeout 3600 run_in_dir "${KG_DIR}" "${KG_PY}" -m src.kg_explain.cli pipeline --disease "${disease_key}" --version v5 --drug-source ctgov; then
    fail_disease "${disease_key}" "${run_id}" "origin_kg_ctgov" "kg ctgov pipeline failed" "${cross_status}" "${origin_status}"
    return 1
  fi

  if ! run_cmd "Origin: manifest gate" kg_manifest_gate "${kg_manifest}" "ctgov"; then
    # fallback: check default location if --output-dir not supported
    local kg_manifest_fallback_new="${KG_DIR}/output/${disease_key}/pipeline_manifest.json"
    local kg_manifest_fallback_legacy="${KG_DIR}/output/pipeline_manifest.json"
    if [[ -f "${kg_manifest_fallback_new}" ]]; then
      cp "${kg_manifest_fallback_new}" "${kg_manifest}"
    elif [[ -f "${kg_manifest_fallback_legacy}" ]]; then
      cp "${kg_manifest_fallback_legacy}" "${kg_manifest}"
    else
      fail_disease "${disease_key}" "${run_id}" "origin_manifest_gate" "kg ctgov manifest check failed" "${cross_status}" "${origin_status}"
      return 1
    fi

    if ! run_cmd "Origin: manifest gate (fallback)" kg_manifest_gate "${kg_manifest}" "ctgov"; then
      fail_disease "${disease_key}" "${run_id}" "origin_manifest_gate" "kg ctgov manifest check failed" "${cross_status}" "${origin_status}"
      return 1
    fi
  fi

  local origin_manifest_path="${disease_work}/pipeline_manifest_origin_ctgov.json"
  cp "${kg_manifest}" "${origin_manifest_path}"

  local bridge_origin="${kg_output_dir}/bridge_origin_reassess.csv"
  # fallback to default location
  if [[ ! -f "${bridge_origin}" ]]; then
    bridge_origin="${KG_DIR}/output/bridge_origin_reassess.csv"
  fi

  local origin_cmd=("${KG_PY}" scripts/generate_disease_bridge.py)
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

  if [[ -z "${origin_ids_input}" ]]; then
    origin_ids_effective="$(derive_matched_ids_from_v3 "${disease_query}")"
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
  if [[ -f "${KG_DIR}/output/evidence_paths_v3.jsonl" ]]; then
    rm -f "${KG_DIR}/output/evidence_paths_v3.jsonl"
    log "[CLEAN] Deleted temporary file: ${KG_DIR}/output/evidence_paths_v3.jsonl"
  fi

  log "=== Disease done: ${disease_key} run_id=${run_id} cross=${cross_status} origin=${origin_status} results=${result_dir} ==="
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

  if ! run_cmd "Cross: dsmeta (${disease_key})" --timeout 3600 run_in_dir "${DSMETA_DIR}" "${DSMETA_PY}" run.py --config "${dsmeta_cfg}"; then
    log "[ERROR] Cross: dsmeta pipeline failed"
    cleanup_dsmeta_workdir "${disease_key}"
    return 1
  fi

  # Free disk: dsmeta workdir no longer needed after outputs are in outdir
  cleanup_dsmeta_workdir "${disease_key}"

  if ! resolve_cross_inputs "${disease_key}"; then
    log "[ERROR] Cross: cannot resolve cross input files"
    return 1
  fi

  if ! run_cmd "Cross: validate signature_meta" validate_signature_meta_json "${CROSS_SIGNATURE_META}" "${disease_key}"; then
    log "[ERROR] Cross: invalid signature meta json"
    return 1
  fi

  if ! run_cmd "Cross: validate sigreverse_input" validate_sigreverse_input_json "${CROSS_SIGREVERSE_INPUT}" "${disease_key}"; then
    log "[ERROR] Cross: invalid sigreverse input json"
    return 1
  fi

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

  if ! run_cmd "Cross: kg signature" --timeout 3600 run_in_dir "${KG_DIR}" "${KG_PY}" -m src.kg_explain.cli pipeline --disease "${disease_key}" --version v5 --drug-source signature --signature-path "${CROSS_SIGNATURE_META}"; then
    log "[ERROR] Cross: kg signature pipeline failed"
    return 1
  fi

  if ! run_cmd "Cross: manifest gate" kg_manifest_gate "${kg_manifest}" "signature"; then
    # fallback: check default location
    local kg_manifest_fallback_new="${KG_DIR}/output/${disease_key}/pipeline_manifest.json"
    local kg_manifest_fallback_legacy="${KG_DIR}/output/pipeline_manifest.json"
    if [[ -f "${kg_manifest_fallback_new}" ]]; then
      cp "${kg_manifest_fallback_new}" "${kg_manifest}"
    elif [[ -f "${kg_manifest_fallback_legacy}" ]]; then
      cp "${kg_manifest_fallback_legacy}" "${kg_manifest}"
    else
      log "[ERROR] Cross: kg signature manifest check failed"
      return 1
    fi

    if ! run_cmd "Cross: manifest gate (fallback)" kg_manifest_gate "${kg_manifest}" "signature"; then
      log "[ERROR] Cross: kg signature manifest check failed"
      return 1
    fi
  fi

  cross_manifest_path="${disease_work}/pipeline_manifest_cross_signature.json"
  cp "${kg_manifest}" "${cross_manifest_path}"

  bridge_cross="${kg_output_dir}/bridge_repurpose_cross.csv"
  # fallback to default location
  if [[ ! -f "${bridge_cross}" ]]; then
    bridge_cross="${KG_DIR}/output/bridge_repurpose_cross.csv"
  fi
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

  return 0
}

if [[ ! -f "${DISEASE_LIST_FILE}" ]]; then
  log "[ERROR] Disease list not found: ${DISEASE_LIST_FILE}"
  exit 1
fi

# ── Pre-flight checks ──

DISK_MIN_GB="${DISK_MIN_GB:-5}"            # Minimum free disk space (GB)
API_BACKOFF_SECONDS="${API_BACKOFF_SECONDS:-600}"  # Sleep if APIs are all down

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
  dual|origin_only)
    ;;
  *)
    log "[ERROR] Invalid RUN_MODE='${RUN_MODE}'. Allowed: dual | origin_only"
    exit 1
    ;;
esac

acquire_lock
trap 'release_lock' EXIT INT TERM

log "Runner start"
log "Root: ${ROOT_DIR}"
log "Disease list: ${DISEASE_LIST_FILE}"
log "Log file: ${RUN_LOG}"
log "Lock file: ${LOCK_FILE}"
log "Sleep seconds between cycles: ${SLEEP_SECONDS}"
log "Retention days (work/quarantine): ${RETENTION_DAYS}"
log "Log retention days: ${LOG_RETENTION_DAYS}"
log "RUN_MODE: ${RUN_MODE}"
log "STRICT_CONTRACT: ${STRICT_CONTRACT}"
log "TOPN_PROFILE: ${TOPN_PROFILE}"
log "TOPN_CROSS: ${TOPN_CROSS} (auto|int)"
log "TOPN_ORIGIN: ${TOPN_ORIGIN} (auto|int)"
log "TOPN_STAGE2_ENABLE: ${TOPN_STAGE2_ENABLE}"
log "TOPN_MAX_EXPAND_ROUNDS: ${TOPN_MAX_EXPAND_ROUNDS}"
log "TOPN_STAGE1_MIN_ORIGIN/MAX: ${TOPN_STAGE1_MIN_ORIGIN}/${TOPN_STAGE1_MAX_ORIGIN}"
log "TOPN_STAGE1_MIN_CROSS/MAX: ${TOPN_STAGE1_MIN_CROSS}/${TOPN_STAGE1_MAX_CROSS}"
log "TOPN_CAP_ORIGIN/CROSS: ${TOPN_CAP_ORIGIN}/${TOPN_CAP_CROSS}"
log "TOPN_EXPAND_RATIO: ${TOPN_EXPAND_RATIO}"
log "SHORTLIST_MIN_GO_ORIGIN/CROSS: ${SHORTLIST_MIN_GO_ORIGIN}/${SHORTLIST_MIN_GO_CROSS}"
log "TOPK_CROSS: ${TOPK_CROSS}"
log "TOPK_ORIGIN: ${TOPK_ORIGIN}"
log "STEP6 budgets: retmax=${STEP6_PUBMED_RETMAX}, parse_max=${STEP6_PUBMED_PARSE_MAX}, rerank_docs=${STEP6_MAX_RERANK_DOCS}, evidence_docs=${STEP6_MAX_EVIDENCE_DOCS}"
log "SCREEN_MAX_STUDIES: ${SCREEN_MAX_STUDIES}"
log "STEP_TIMEOUT: ${STEP_TIMEOUT}s"
log "MAX_CYCLES: ${MAX_CYCLES} (0 means infinite)"
log "DISK_MIN_GB: ${DISK_MIN_GB}"
log "CACHE_RETENTION_DAYS: ${CACHE_RETENTION_DAYS}"
log "DSMETA_CLEANUP: ${DSMETA_CLEANUP} (1=auto-clean workdir after each disease)"
log "API_BACKOFF_SECONDS: ${API_BACKOFF_SECONDS}"

cycle=0
while true; do
  cycle=$((cycle + 1))
  ok_count=0
  fail_count=0

  cleanup_retention

  log "----- Cycle ${cycle} start -----"

  # Pre-flight: disk space
  if ! check_disk_space "${DISK_MIN_GB}"; then
    log "[PAUSE] Low disk space. Waiting ${API_BACKOFF_SECONDS}s before retry..."
    sleep "${API_BACKOFF_SECONDS}"
    continue
  fi

  # Pre-flight: API reachability
  if ! check_api_health; then
    log "[PAUSE] APIs unreachable. Waiting ${API_BACKOFF_SECONDS}s before retry..."
    sleep "${API_BACKOFF_SECONDS}"
    continue
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
      log "[ERROR] Disease failed: ${local_key}"
    fi
  done < "${DISEASE_LIST_FILE}"

  log "----- Cycle ${cycle} done: ok=${ok_count}, fail=${fail_count} -----"

  if [[ "${MAX_CYCLES}" != "0" ]] && [[ "${cycle}" -ge "${MAX_CYCLES}" ]]; then
    log "MAX_CYCLES reached (${MAX_CYCLES}). Exit."
    break
  fi

  log "Sleep ${SLEEP_SECONDS}s before next cycle"
  sleep "${SLEEP_SECONDS}"
done
