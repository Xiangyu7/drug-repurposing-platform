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
#   4. LLM+RAG 证据工程           — PubMed文献检索 + LLM结构化提取 + 5维打分门控 + 候选打包
#
# 两条研究路线 (Direction A & B):
#
#   Direction A — 跨疾病迁移 (Cross-Disease Repurposing)
#     科学问题: 其他疾病的药能否迁移到目标疾病？
#     流程:     dsmeta → SigReverse → kg_explain(signature) → bridge_repurpose_cross.csv → LLM Step6-8
#     风格:     探索性 (Exploration), 高风险高回报
#
#   Direction B — 原疾病重评估 (Origin-Disease Reassessment)
#     科学问题: 失败试验中的药是否值得重新评估？（换终点/人群/剂量）
#     流程:     screen_drugs(CT.gov) → kg_explain(ctgov) → bridge_origin_reassess.csv → LLM Step6-8
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
# LLM+RAG Step 6-8 (同一套代码, 分别用两个 bridge 文件跑两遍):
#   Step 6: PubMed RAG + LLM 证据提取 (多路检索 + 语义重排 + 结构化抽取)
#   Step 7: 5维打分 + 门控 (证据/机制/可转化性/安全/可行性 → GO/MAYBE/NO-GO)
#   Step 8: 发布门控 + 候选打包 (移除NO-GO, Top-K → Excel)
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
#   STEP_TIMEOUT        — 每步超时 (默认 10800s / 3h)
#   DISK_MIN_GB         — 最低可用磁盘空间GB (默认 5)
#   DSMETA_CLEANUP      — dsmeta跑完后是否自动清理workdir (默认 1=清理, 0=保留)
#   KG_MAX_DRUGS_SIGNATURE — signature模式KG最多处理的药物数 (默认 200, 0=不限)
#
# Modules (sourced in order):
#   runner_utils.sh  — logging, timing, run_cmd, helpers
#   runner_topn.sh   — TopN strategy (profiles, resolve, quality)
#   runner_ops.sh    — cleanup, health checks, archiving, lock
#   runner_routes.sh — pipeline orchestration (process_disease, routes)
# ═══════════════════════════════════════════════════════════════════

# ── Directory paths ──────────────────────────────────────────────

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DSMETA_DIR="${ROOT_DIR}/dsmeta_signature_pipeline"
ARCHS4_DIR="${ROOT_DIR}/archs4_signature_pipeline"
SIG_DIR="${ROOT_DIR}/sigreverse"
KG_DIR="${ROOT_DIR}/kg_explain"
LLM_DIR="${ROOT_DIR}/LLM+RAG证据工程"

RUNTIME_DIR="${ROOT_DIR}/runtime"
RUNS_ROOT="${RUNTIME_DIR}/runs"
COLLECT_ROOT="${RUNTIME_DIR}/collect"
QUARANTINE_ROOT="${RUNTIME_DIR}/quarantine"
STATE_ROOT="${RUNTIME_DIR}/state"
LOG_DIR="${ROOT_DIR}/logs/continuous_runner"

DISEASE_LIST_FILE="${1:-${ROOT_DIR}/ops/disease_list.txt}"

# ── Source utility module (provides resolve_runtime_python, log, etc.) ──

_RUNNER_MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${_RUNNER_MODULE_DIR}/runner_utils.sh"

# ── Python interpreters ──────────────────────────────────────────

# dsmeta & archs4 need R (Rscript) alongside Python — prefer conda dsmeta env
# which bundles both, then fall back to local .venv, then system python3.
# NOTE: avoid `conda run` — it mixes diagnostic output into stdout on many setups.
#       Instead, find the env prefix directly and construct the python path.
_conda_dsmeta_py=""
if command -v conda >/dev/null 2>&1; then
  _conda_prefix="$(conda env list 2>/dev/null | awk '$1=="dsmeta"{print $NF}')" || true
  if [[ -n "${_conda_prefix}" && -x "${_conda_prefix}/bin/python3" ]]; then
    _conda_dsmeta_py="${_conda_prefix}/bin/python3"
  elif [[ -n "${_conda_prefix}" && -x "${_conda_prefix}/bin/python" ]]; then
    _conda_dsmeta_py="${_conda_prefix}/bin/python"
  fi
fi
DSMETA_PY="$(resolve_runtime_python "${DSMETA_PY:-}" "${_conda_dsmeta_py:-${DSMETA_DIR}/.venv/bin/python3}")"
ARCHS4_PY="$(resolve_runtime_python "${ARCHS4_PY:-}" "${_conda_dsmeta_py:-${ARCHS4_DIR}/.venv/bin/python3}")"
SIG_PY="$(resolve_runtime_python "${SIG_PY:-}" "${SIG_DIR}/.venv/bin/python3")"
KG_PY="$(resolve_runtime_python "${KG_PY:-}" "${KG_DIR}/.venv/bin/python3")"
LLM_PY="$(resolve_runtime_python "${LLM_PY:-}" "${LLM_DIR}/.venv/bin/python3")"

# ── Configuration variables ──────────────────────────────────────

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
# v3: TOPK_CROSS raised from 15→25.  Cross route (signature-driven) pools
# 100-200 candidates; 15 was too narrow and missed validated single-target
# drugs (e.g. tocilizumab, leflunomide) whose mechanism_score is lower than
# multi-target drugs due to the diversity bonus in the scoring formula.
TOPK_CROSS="${TOPK_CROSS:-25}"
TOPK_ORIGIN="${TOPK_ORIGIN:-10}"
STEP6_PUBMED_RETMAX=150
STEP6_PUBMED_PARSE_MAX=80
STEP6_MAX_RERANK_DOCS=50
STEP6_MAX_EVIDENCE_DOCS=20
STRICT_CONTRACT="${STRICT_CONTRACT:-1}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"
LOG_RETENTION_DAYS="${LOG_RETENTION_DAYS:-30}"
RUN_MODE="${RUN_MODE:-dual}" # dual | origin_only
STEP_TIMEOUT="${STEP_TIMEOUT:-10800}" # 3h per step default
DSMETA_DISK_MIN_GB="${DSMETA_DISK_MIN_GB:-8}"  # min free GB before dsmeta (GEO downloads are large)
DSMETA_CLEANUP="${DSMETA_CLEANUP:-1}"          # auto-clean dsmeta workdir after each disease
SIG_PRIORITY="${SIG_PRIORITY:-archs4}"         # archs4 | dsmeta — which signature source to try first
# Module-level timeout overrides (default: inherit STEP_TIMEOUT)
TIMEOUT_CROSS_AUTO_DISCOVER_GEO="${TIMEOUT_CROSS_AUTO_DISCOVER_GEO:-${STEP_TIMEOUT}}"
TIMEOUT_CROSS_GENERATE_DSMETA_CONFIG="${TIMEOUT_CROSS_GENERATE_DSMETA_CONFIG:-${STEP_TIMEOUT}}"
TIMEOUT_CROSS_GENERATE_ARCHS4_CONFIG="${TIMEOUT_CROSS_GENERATE_ARCHS4_CONFIG:-${STEP_TIMEOUT}}"
TIMEOUT_CROSS_ARCHS4="${TIMEOUT_CROSS_ARCHS4:-${STEP_TIMEOUT}}"
TIMEOUT_CROSS_DSMETA="${TIMEOUT_CROSS_DSMETA:-${STEP_TIMEOUT}}"
TIMEOUT_CROSS_SIGREVERSE="${TIMEOUT_CROSS_SIGREVERSE:-${STEP_TIMEOUT}}"
TIMEOUT_CROSS_KG_SIGNATURE="${TIMEOUT_CROSS_KG_SIGNATURE:-${STEP_TIMEOUT}}"
TIMEOUT_ORIGIN_KG_CTGOV="${TIMEOUT_ORIGIN_KG_CTGOV:-${STEP_TIMEOUT}}"
TIMEOUT_LLM_STEP6="${TIMEOUT_LLM_STEP6:-${STEP_TIMEOUT}}"
TIMEOUT_LLM_STEP7="${TIMEOUT_LLM_STEP7:-${STEP_TIMEOUT}}"
TIMEOUT_LLM_STEP8="${TIMEOUT_LLM_STEP8:-${STEP_TIMEOUT}}"
LOCK_NAME="${LOCK_NAME:-${RUN_MODE}}"
LOCK_FILE="${STATE_ROOT}/runner_${LOCK_NAME}.lock"

TOPN_POLICY_PY="${ROOT_DIR}/ops/internal/topn_policy.py"

CACHE_RETENTION_DAYS="${CACHE_RETENTION_DAYS:-1}"

# ── Source remaining modules ─────────────────────────────────────

source "${_RUNNER_MODULE_DIR}/runner_topn.sh"
source "${_RUNNER_MODULE_DIR}/runner_ops.sh"
source "${_RUNNER_MODULE_DIR}/runner_routes.sh"

# ── Apply profile defaults (must come after sourcing runner_topn.sh) ──

apply_topn_profile_defaults

# ── Create runtime directories & log file ────────────────────────

mkdir -p "${RUNS_ROOT}" "${COLLECT_ROOT}" "${QUARANTINE_ROOT}" "${STATE_ROOT}" "${LOG_DIR}"
RUN_LOG="${LOG_DIR}/runner_${RUN_MODE}_$(date '+%Y%m%d_%H%M%S')_$$.log"

# ── Validate disease list ────────────────────────────────────────

if [[ ! -f "${DISEASE_LIST_FILE}" ]]; then
  log "[ERROR] Disease list not found: ${DISEASE_LIST_FILE}"
  exit 1
fi

# ── Pre-flight checks ──

DISK_MIN_GB="${DISK_MIN_GB:-5}"            # Minimum free disk space (GB)

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
log "  module_timeouts: dsmeta=${TIMEOUT_CROSS_DSMETA}s sigreverse=${TIMEOUT_CROSS_SIGREVERSE}s kg=${TIMEOUT_CROSS_KG_SIGNATURE}s llm(step6/7/8)=${TIMEOUT_LLM_STEP6}/${TIMEOUT_LLM_STEP7}/${TIMEOUT_LLM_STEP8}s"

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

# ── Collect batch results ──
collect_batch_results "${DISEASE_LIST_FILE}"
