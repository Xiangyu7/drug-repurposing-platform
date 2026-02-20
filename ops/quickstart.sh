#!/usr/bin/env bash
set -Eeuo pipefail

# ═══════════════════════════════════════════════════════════════════
# quickstart.sh — One-Command Pipeline Setup & Launch
# ═══════════════════════════════════════════════════════════════════
#
# 整合了环境检查、venv安装、GEO发现、config生成、管线启动的一站式脚本
#
# Usage:
#   # 完整流程：检查环境 → 安装依赖 → GEO发现 → 生成配置 → 启动管线
#   bash ops/quickstart.sh
#
#   # 仅启动管线（跳过安装和GEO发现）
#   bash ops/quickstart.sh --run-only
#
#   # 仅检查环境
#   bash ops/quickstart.sh --check-only
#
#   # 仅安装依赖
#   bash ops/quickstart.sh --setup-only
#
#   # 仅运行 GEO 发现（不启动管线）
#   bash ops/quickstart.sh --discover-only
#
#   # 指定运行模式和疾病列表
#   bash ops/quickstart.sh --mode origin_only --list disease_list_day1_origin.txt
#
#   # 仅跑单个疾病（非24/7循环，跑完退出）
#   bash ops/quickstart.sh --single atherosclerosis
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

ACTION="full"
RUN_MODE="${RUN_MODE:-dual}"
DISEASE_LIST=""
SINGLE_DISEASE=""
MAX_CYCLES="${MAX_CYCLES:-0}"
CHECK_SCOPE="${CHECK_SCOPE:-all}"
AUTO_REPAIR=1
REPORT_JSON=""
STATE_DIR="${ROOT_DIR}/runtime/state"
ENV_GUARD_PY="${QUICKSTART_ENV_GUARD:-${OPS_DIR}/env_guard.py}"
RUNNER_SCRIPT="${QUICKSTART_RUNNER:-${OPS_DIR}/run_24x7_all_directions.sh}"
LAST_ENV_REPORT=""
LAST_RESOLVED_ENV=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check-only)   ACTION="check"; shift ;;
        --setup-only)   ACTION="setup"; shift ;;
        --discover-only) ACTION="discover"; shift ;;
        --run-only)     ACTION="run"; shift ;;
        --mode)         RUN_MODE="$2"; shift 2 ;;
        --list)         DISEASE_LIST="$2"; shift 2 ;;
        --single)       SINGLE_DISEASE="$2"; ACTION="single"; shift 2 ;;
        --cycles)       MAX_CYCLES="$2"; shift 2 ;;
        --check-scope)  CHECK_SCOPE="$2"; shift 2 ;;
        --no-auto-repair) AUTO_REPAIR=0; shift ;;
        --report-json)  REPORT_JSON="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: bash ops/quickstart.sh [options]"
            echo ""
            echo "Actions:"
            echo "  --check-only     Only check environment prerequisites"
            echo "  --setup-only     Only install dependencies"
            echo "  --discover-only  Only run GEO auto-discovery"
            echo "  --run-only       Only start the pipeline runner"
            echo "  --single <key>   Run pipeline once for a single disease"
            echo ""
            echo "Options:"
            echo "  --mode <mode>    Run mode: dual | origin_only | cross_only (default: origin_only)"
            echo "  --list <file>    Disease list file (default: auto-select)"
            echo "  --cycles <n>     Max cycles (default: 0=infinite)"
            echo "  --check-scope    Environment check scope: all | mode (default: all)"
            echo "  --no-auto-repair Disable auto-repair flow for --single/--run-only/full"
            echo "  --report-json    Override env report output path"
            echo ""
            exit 0
            ;;
        *) fail "Unknown option: $1"; exit 1 ;;
    esac
done

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

# ── Phase 3: GEO Discovery ──────────────────────────────────────────

run_geo_discovery() {
    header "Phase 3: GEO Auto-Discovery"

    local origin_list="${OPS_DIR}/disease_list_day1_origin.txt"
    if [[ ! -f "${origin_list}" ]]; then
        warn "No disease list found at ${origin_list}"
        return 1
    fi

    local geo_dir="${OPS_DIR}/geo_curation"

    info "Running auto_discover_geo.py in batch mode..."
    info "This may take 5-15 minutes (NCBI rate limiting)..."
    info ""

    python3 "${OPS_DIR}/auto_discover_geo.py" \
        --batch "${origin_list}" \
        --out-dir "${geo_dir}" \
        --top-k 5 \
        --write-yaml \
        --min-samples 6

    local exit_code=$?
    if [[ "${exit_code}" -ne 0 ]]; then
        warn "GEO discovery completed with some errors (exit code: ${exit_code})"
    else
        ok "GEO discovery completed"
    fi

    # Generate dsmeta configs
    info ""
    info "Generating dsmeta configs from discovery results..."
    python3 "${OPS_DIR}/generate_dsmeta_configs.py" \
        --geo-dir "${geo_dir}" \
        --config-dir "${ROOT_DIR}/dsmeta_signature_pipeline/configs" \
        --update-disease-list

    ok "Config generation complete"

    # Show summary
    info ""
    info "Review the generated configs:"
    info "  GEO curation:  ${geo_dir}/"
    info "  dsmeta configs: ${ROOT_DIR}/dsmeta_signature_pipeline/configs/"
    info ""
    info "Check each disease's discovery_log.txt for details."
    info "Configs with ⚠️ TODO need manual review before running."
}

# ── Phase 4: Launch Pipeline ────────────────────────────────────────

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
            list_file="${OPS_DIR}/disease_list_day1_dual.txt"
        else
            list_file="${OPS_DIR}/disease_list_day1_origin.txt"
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
    info "Max cycles:   ${MAX_CYCLES} (0=infinite)"
    info ""

    # Create log directory
    local log_dir="${ROOT_DIR}/logs/quickstart"
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
        MAX_CYCLES="${MAX_CYCLES}" \
        SLEEP_SECONDS="${SLEEP_SECONDS:-300}" \
        TOPN_PROFILE="${TOPN_PROFILE:-stable}" \
        TOPN_CROSS="${TOPN_CROSS:-auto}" \
        TOPN_ORIGIN="${TOPN_ORIGIN:-auto}" \
        TOPN_STAGE2_ENABLE="${TOPN_STAGE2_ENABLE:-1}" \
        TOPN_MAX_EXPAND_ROUNDS="${TOPN_MAX_EXPAND_ROUNDS:-1}" \
        STRICT_CONTRACT="${STRICT_CONTRACT:-1}" \
        RETENTION_DAYS="${RETENTION_DAYS:-7}" \
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
    info ""
    info "Stop pipeline:"
    info "  kill ${pid}"
    info ""

    # Save PID for convenience
    echo "${pid}" > "${ROOT_DIR}/runtime/state/quickstart_${RUN_MODE}.pid"
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
    local origin_list="${OPS_DIR}/disease_list_day1_origin.txt"
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

    local log_dir="${ROOT_DIR}/logs/quickstart"
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
        MAX_CYCLES=1 \
        SLEEP_SECONDS=0 \
        TOPN_PROFILE="${TOPN_PROFILE:-stable}" \
        TOPN_CROSS="${TOPN_CROSS:-auto}" \
        TOPN_ORIGIN="${TOPN_ORIGIN:-auto}" \
        TOPN_STAGE2_ENABLE="${TOPN_STAGE2_ENABLE:-1}" \
        TOPN_MAX_EXPAND_ROUNDS="${TOPN_MAX_EXPAND_ROUNDS:-1}" \
        STRICT_CONTRACT="${STRICT_CONTRACT:-1}" \
        DSMETA_CLEANUP="${DSMETA_CLEANUP:-1}" \
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
    discover)
        run_geo_discovery
        ;;
    run)
        ensure_environment_ready
        launch_pipeline
        ;;
    single)
        ensure_environment_ready
        run_single_disease
        ;;
    full)
        if ! ensure_environment_ready; then
            fail "Environment is not ready."
            exit 1
        fi
        info ""
        read -p "Run GEO auto-discovery? [y/N] " -r answer
        if [[ "${answer}" =~ ^[Yy] ]]; then
            run_geo_discovery
        fi
        info ""
        read -p "Start pipeline? [Y/n] " -r answer
        if [[ "${answer}" =~ ^[Nn] ]]; then
            info "Environment ready. Start manually with: bash ops/quickstart.sh --run-only"
            exit 0
        fi
        launch_pipeline
        ;;
esac
