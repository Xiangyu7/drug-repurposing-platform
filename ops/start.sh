#!/usr/bin/env bash
set -Eeuo pipefail

# ═══════════════════════════════════════════════════════════════════
# start.sh — Drug Repurposing Pipeline 唯一入口
# ═══════════════════════════════════════════════════════════════════
#
# Usage:
#   bash ops/start.sh setup                  # 安装环境
#   bash ops/start.sh check                  # 检查环境
#   bash ops/start.sh run <disease>          # 跑单个疾病
#   bash ops/start.sh start                  # 启动批量管线（后台）
#   bash ops/start.sh status                 # 查看运行状态
#   bash ops/start.sh results [disease]      # 查看结果
#
# Options (放在子命令后面):
#   --mode <dual|origin_only|cross_only>     # 运行模式 (默认: dual)
#   --list <file>                            # 自定义疾病列表
#   --no-auto-repair                         # 禁用自动修复
#
# Examples:
#   bash ops/start.sh setup                  # 第一次用：装环境
#   bash ops/start.sh run atherosclerosis    # 跑一个疾病试试
#   bash ops/start.sh start                  # 正式批量跑
#   bash ops/start.sh status                 # 看状态
# ═══════════════════════════════════════════════════════════════════

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPS_DIR="${ROOT_DIR}/ops"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

ok()   { printf "${GREEN}✓${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}⚠${NC} %s\n" "$*"; }
fail() { printf "${RED}✗${NC} %s\n" "$*"; }
info() { printf "${BLUE}ℹ${NC} %s\n" "$*"; }
header() {
    printf "\n${BLUE}═══════════════════════════════════════════════${NC}\n"
    printf "${BLUE}  %s${NC}\n" "$*"
    printf "${BLUE}═══════════════════════════════════════════════${NC}\n\n"
}

# ── Parse Arguments ──────────────────────────────────────────────────

ACTION="help"
RUN_MODE="${RUN_MODE:-dual}"
DISEASE_LIST=""
SINGLE_DISEASE=""
CHECK_SCOPE="${CHECK_SCOPE:-all}"
AUTO_REPAIR=1
REPORT_JSON=""
STATE_DIR="${ROOT_DIR}/runtime/state"
ENV_GUARD_PY="${START_ENV_GUARD:-${QUICKSTART_ENV_GUARD:-${OPS_DIR}/internal/env_guard.py}}"
RUNNER_SCRIPT="${START_RUNNER:-${QUICKSTART_RUNNER:-${OPS_DIR}/internal/runner.sh}}"
LAST_ENV_REPORT=""
LAST_RESOLVED_ENV=""

show_help() {
    cat <<'USAGE'
Drug Repurposing Pipeline — start.sh

Commands:
  setup                   安装所有依赖 (venv + pip)
  check                   检查环境是否就绪
  run <disease>           跑单个疾病 (前台, 跑完退出)
  start                   启动批量管线 (后台常驻)
  status                  查看运行状态 (等同 check_status.sh)
  results [disease]       查看结果 (等同 show_results.sh)

Options:
  --mode <mode>           dual | origin_only | cross_only (默认: dual)
  --list <file>           自定义疾病列表
  --no-auto-repair        禁用环境自动修复
  --check-scope <scope>   检查范围: all | mode (默认: all)
  --report-json <path>    环境报告输出路径

Environment Variables:
  SIG_PRIORITY=dsmeta|archs4   签名优先级 (默认: dsmeta)
  SKIP_LLM=1                   跳过 LLM 步骤 (测试用)

Examples:
  bash ops/start.sh setup                          # 第一次用
  bash ops/start.sh run atherosclerosis             # 试跑一个疾病
  RUN_MODE=dual bash ops/start.sh run atherosclerosis  # A+B 双路线
  bash ops/start.sh start                           # 正式批量跑
  bash ops/start.sh status                          # 看状态
  SIG_PRIORITY=archs4 bash ops/start.sh run atherosclerosis --mode cross_only  # ARCHS4优先
USAGE
}

# First arg: subcommand or legacy --flag
if [[ $# -gt 0 ]]; then
    case "$1" in
        # ── New subcommand style ──
        setup)    ACTION="setup"; shift ;;
        check)    ACTION="check"; shift ;;
        run)
            ACTION="single"; shift
            if [[ $# -gt 0 && "$1" != --* ]]; then
                SINGLE_DISEASE="$1"; shift
            else
                fail "Usage: bash ops/start.sh run <disease_key>"
                fail "Example: bash ops/start.sh run atherosclerosis"
                exit 1
            fi
            ;;
        start)    ACTION="run"; shift ;;
        status)
            shift
            exec bash "${OPS_DIR}/check_status.sh" "$@"
            ;;
        results)
            shift
            exec bash "${OPS_DIR}/show_results.sh" "$@"
            ;;
        help|-h|--help) show_help; exit 0 ;;
        *) ;;  # fall through to options parsing below
    esac
fi

# Parse remaining options
while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)           RUN_MODE="$2"; shift 2 ;;
        --list)           DISEASE_LIST="$2"; shift 2 ;;
        --check-scope)    CHECK_SCOPE="$2"; shift 2 ;;
        --no-auto-repair) AUTO_REPAIR=0; shift ;;
        --report-json)    REPORT_JSON="$2"; shift 2 ;;
        -h|--help)        show_help; exit 0 ;;
        *)                fail "Unknown option: $1"; show_help; exit 1 ;;
    esac
done

# Show help if no command given
if [[ "${ACTION}" == "help" ]]; then
    show_help
    exit 0
fi

case "${CHECK_SCOPE}" in
    all|mode) ;;
    *)
        fail "Invalid --check-scope: ${CHECK_SCOPE} (expected: all|mode)"
        exit 1
        ;;
esac

# ── Phase 1/2: Environment Check & Repair ────────────────────────────

_env_report_file() {
    local prefix="$1"
    if [[ -n "${REPORT_JSON}" && "${prefix}" == "env_check" ]]; then
        printf '%s\n' "${REPORT_JSON}"
        return 0
    fi
    mkdir -p "${STATE_DIR}"
    printf '%s/%s_%s.json\n' "${STATE_DIR}" "${prefix}" "$(date '+%Y%m%d_%H%M%S')"
}

_env_resolved_file() {
    mkdir -p "${STATE_DIR}"
    printf '%s/env_resolved_%s.env\n' "${STATE_DIR}" "$(date '+%Y%m%d_%H%M%S')"
}

apply_resolved_runtime_env() {
    local resolved_file="${1:-${LAST_RESOLVED_ENV}}"
    if [[ -z "${resolved_file}" || ! -f "${resolved_file}" ]]; then
        return 0
    fi
    while IFS='=' read -r k v; do
        case "${k}" in
            DSMETA_PY|SIG_PY|KG_PY|LLM_PY) export "${k}=${v}" ;;
        esac
    done < <(grep -E '^(DSMETA_PY|SIG_PY|KG_PY|LLM_PY)=' "${resolved_file}" || true)
}

check_environment() {
    header "Phase 1: Environment Check"
    if [[ ! -f "${ENV_GUARD_PY}" ]]; then
        fail "env guard not found: ${ENV_GUARD_PY}"
        return 1
    fi
    local report_file resolved_file
    report_file="$(_env_report_file env_check)"
    resolved_file="$(_env_resolved_file)"
    LAST_ENV_REPORT="${report_file}"
    LAST_RESOLVED_ENV="${resolved_file}"

    local -a guard_args
    guard_args=(
        check
        --mode "${RUN_MODE}"
        --scope "${CHECK_SCOPE}"
        --report-json "${report_file}"
        --resolved-env "${resolved_file}"
        --root-dir "${ROOT_DIR}"
    )
    if [[ -n "${SINGLE_DISEASE}" ]]; then
        guard_args+=(--single-disease "${SINGLE_DISEASE}")
    fi
    if python3 "${ENV_GUARD_PY}" "${guard_args[@]}"; then
        ok "Environment check passed"
        info "Report: ${report_file}"
        info "Resolved runtime: ${resolved_file}"
        apply_resolved_runtime_env "${resolved_file}"
        return 0
    fi
    fail "Environment check failed"
    info "Report: ${report_file}"
    info "Resolved runtime: ${resolved_file}"
    return 1
}

setup_venvs() {
    header "Phase 2: Environment Auto-Repair"
    if [[ ! -f "${ENV_GUARD_PY}" ]]; then
        fail "env guard not found: ${ENV_GUARD_PY}"
        return 1
    fi
    local report_file resolved_file
    report_file="$(_env_report_file env_repair)"
    resolved_file="$(_env_resolved_file)"
    LAST_ENV_REPORT="${report_file}"
    LAST_RESOLVED_ENV="${resolved_file}"

    local -a guard_args
    guard_args=(
        repair
        --mode "${RUN_MODE}"
        --scope "${CHECK_SCOPE}"
        --report-json "${report_file}"
        --resolved-env "${resolved_file}"
        --root-dir "${ROOT_DIR}"
    )
    if [[ -n "${SINGLE_DISEASE}" ]]; then
        guard_args+=(--single-disease "${SINGLE_DISEASE}")
    fi
    if python3 "${ENV_GUARD_PY}" "${guard_args[@]}"; then
        ok "Environment repair finished"
        info "Report: ${report_file}"
        info "Resolved runtime: ${resolved_file}"
        apply_resolved_runtime_env "${resolved_file}"
        return 0
    fi
    fail "Environment repair failed"
    info "Report: ${report_file}"
    info "Resolved runtime: ${resolved_file}"
    return 1
}

ensure_environment_ready() {
    if check_environment; then
        return 0
    fi
    if [[ "${AUTO_REPAIR}" -ne 1 ]]; then
        fail "Auto-repair disabled (--no-auto-repair)."
        return 1
    fi
    warn "Check failed, trying auto-repair..."
    if ! setup_venvs; then
        fail "Auto-repair step failed"
        return 1
    fi
    check_environment
}

# ── Phase 3: Launch Pipeline ────────────────────────────────────────

launch_pipeline() {
    header "Phase 4: Launch Pipeline"

    local runner="${RUNNER_SCRIPT}"
    if [[ ! -f "${runner}" ]]; then
        fail "Runner not found: ${runner}"
        return 1
    fi

    # Select disease list
    local list_file="${DISEASE_LIST}"
    if [[ -z "${list_file}" ]]; then
        if [[ "${RUN_MODE}" == "dual" ]]; then
            list_file="${OPS_DIR}/internal/disease_list_day1_dual.txt"
        else
            list_file="${OPS_DIR}/internal/disease_list_day1_origin.txt"
        fi
    fi

    if [[ ! -f "${list_file}" ]]; then
        fail "Disease list not found: ${list_file}"
        return 1
    fi

    local n_diseases
    n_diseases=$(awk 'BEGIN{c=0} $0 !~ /^[[:space:]]*#/ && $0 !~ /^[[:space:]]*$/ {c++} END{print c+0}' "${list_file}")
    if [[ "${n_diseases}" -eq 0 ]]; then
        fail "Disease list is empty: ${list_file}"
        return 1
    fi

    info "Run mode:     ${RUN_MODE}"
    info "Disease list: ${list_file} (${n_diseases} diseases)"
    info ""

    # Create log directory
    local log_dir="${ROOT_DIR}/logs/pipeline"
    mkdir -p "${log_dir}"
    local ts
    ts="$(date '+%Y%m%d_%H%M%S')"
    local log_file="${log_dir}/${RUN_MODE}_${ts}.log"

    info "Starting pipeline in background..."
    info "Log file: ${log_file}"
    info ""

    apply_resolved_runtime_env
    local runtime_dsmeta="${DSMETA_PY:-python3}"
    local runtime_sig="${SIG_PY:-python3}"
    local runtime_kg="${KG_PY:-python3}"
    local runtime_llm="${LLM_PY:-python3}"

    nohup env \
        RUN_MODE="${RUN_MODE}" \
        LOCK_NAME="qs_${RUN_MODE}" \
        TOPN_PROFILE="${TOPN_PROFILE:-stable}" \
        TOPN_CROSS="${TOPN_CROSS:-auto}" \
        TOPN_ORIGIN="${TOPN_ORIGIN:-auto}" \
        TOPN_STAGE2_ENABLE="${TOPN_STAGE2_ENABLE:-1}" \
        STRICT_CONTRACT="${STRICT_CONTRACT:-1}" \
        RETENTION_DAYS="${RETENTION_DAYS:-7}" \
        SIG_PRIORITY="${SIG_PRIORITY:-dsmeta}" \
        DSMETA_PY="${runtime_dsmeta}" \
        SIG_PY="${runtime_sig}" \
        KG_PY="${runtime_kg}" \
        LLM_PY="${runtime_llm}" \
        bash "${runner}" "${list_file}" > "${log_file}" 2>&1 &
    local pid=$!

    ok "Pipeline started! (pid=${pid})"
    info ""
    info "Monitor progress:"
    info "  tail -f ${log_file}"
    info "  bash ops/start.sh status"
    info ""
    info "Stop pipeline:"
    info "  kill ${pid}"
    info ""

    # Save PID for convenience
    echo "${pid}" > "${ROOT_DIR}/runtime/state/pipeline_${RUN_MODE}.pid"
}

# ── Phase: Single Disease Run ────────────────────────────────────────

run_single_disease() {
    header "Single Disease Run: ${SINGLE_DISEASE}"

    local runner="${RUNNER_SCRIPT}"

    # Create temporary disease list
    local tmp_list
    tmp_list=$(mktemp)
    trap '[[ -n "${tmp_list:-}" ]] && rm -f "${tmp_list}"' EXIT INT TERM
    local disease_query="${SINGLE_DISEASE//_/ }"

    # Check if we have origin IDs
    local origin_ids=""
    local origin_list="${OPS_DIR}/internal/disease_list_day1_origin.txt"
    if [[ -f "${origin_list}" ]]; then
        origin_ids=$(grep "^${SINGLE_DISEASE}|" "${origin_list}" | cut -d'|' -f3 || true)
    fi

    # Check if we have inject yaml
    local inject=""
    local inject_path="${ROOT_DIR}/kg_explain/configs/inject_${SINGLE_DISEASE}.yaml"
    if [[ -f "${inject_path}" ]]; then
        inject="${inject_path}"
    fi

    echo "${SINGLE_DISEASE}|${disease_query}|${origin_ids}|${inject}" > "${tmp_list}"
    info "Disease: ${SINGLE_DISEASE}"
    info "Query: ${disease_query}"
    info "Origin IDs: ${origin_ids:-none}"
    info "Inject: ${inject:-none}"
    info "Mode: ${RUN_MODE}"
    info ""

    local log_dir="${ROOT_DIR}/logs/pipeline"
    mkdir -p "${log_dir}"
    local ts
    ts="$(date '+%Y%m%d_%H%M%S')"
    local log_file="${log_dir}/single_${SINGLE_DISEASE}_${ts}.log"

    info "Running pipeline (single cycle, foreground)..."
    info "Log: ${log_file}"
    info ""

    apply_resolved_runtime_env
    local runtime_dsmeta="${DSMETA_PY:-python3}"
    local runtime_sig="${SIG_PY:-python3}"
    local runtime_kg="${KG_PY:-python3}"
    local runtime_llm="${LLM_PY:-python3}"

    env \
        RUN_MODE="${RUN_MODE}" \
        LOCK_NAME="single_${SINGLE_DISEASE}" \
        TOPN_PROFILE="${TOPN_PROFILE:-stable}" \
        TOPN_CROSS="${TOPN_CROSS:-auto}" \
        TOPN_ORIGIN="${TOPN_ORIGIN:-auto}" \
        TOPN_STAGE2_ENABLE="${TOPN_STAGE2_ENABLE:-1}" \
        STRICT_CONTRACT="${STRICT_CONTRACT:-1}" \
        DSMETA_CLEANUP="${DSMETA_CLEANUP:-1}" \
        SIG_PRIORITY="${SIG_PRIORITY:-dsmeta}" \
        DSMETA_PY="${runtime_dsmeta}" \
        SIG_PY="${runtime_sig}" \
        KG_PY="${runtime_kg}" \
        LLM_PY="${runtime_llm}" \
        bash "${runner}" "${tmp_list}" 2>&1 | tee "${log_file}"

    local rc=${PIPESTATUS[0]}
    rm -f "${tmp_list}"

    if [[ "${rc}" -eq 0 ]]; then
        ok "Single disease run completed successfully"
    else
        fail "Pipeline exited with code ${rc}"
    fi
    return "${rc}"
}

# ── Main ─────────────────────────────────────────────────────────────

case "${ACTION}" in
    check)
        check_environment
        ;;
    setup)
        setup_venvs
        ;;
    run)
        ensure_environment_ready
        launch_pipeline
        ;;
    single)
        ensure_environment_ready
        run_single_disease
        ;;
esac
