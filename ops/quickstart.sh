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
RUN_MODE="${RUN_MODE:-origin_only}"
DISEASE_LIST=""
SINGLE_DISEASE=""
MAX_CYCLES="${MAX_CYCLES:-0}"

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
            echo ""
            exit 0
            ;;
        *) fail "Unknown option: $1"; exit 1 ;;
    esac
done

# ── Phase 1: Environment Check ──────────────────────────────────────

check_environment() {
    header "Phase 1: Environment Check"
    local errors=0

    # Python 3
    if command -v python3 &>/dev/null; then
        py_ver="$(python3 --version 2>&1)"
        ok "Python3: ${py_ver}"
    else
        fail "Python3 not found"
        errors=$((errors + 1))
    fi

    # R
    if command -v Rscript &>/dev/null; then
        r_ver="$(Rscript --version 2>&1 | head -1)"
        ok "R: ${r_ver}"
    else
        warn "R not found (needed for Direction A / dsmeta)"
    fi

    # pip packages
    for pkg in requests yaml; do
        if python3 -c "import ${pkg}" 2>/dev/null; then
            ok "Python package: ${pkg}"
        else
            warn "Missing python package: ${pkg}"
        fi
    done

    # Disk space
    local avail_gb
    avail_gb=$(df -g "${ROOT_DIR}" 2>/dev/null | tail -1 | awk '{print $4}' || echo "0")
    if [[ -z "${avail_gb}" || "${avail_gb}" == "0" ]]; then
        avail_gb=$(df -BG "${ROOT_DIR}" 2>/dev/null | tail -1 | awk '{print $4}' | tr -d 'G' || echo "0")
    fi
    if [[ "${avail_gb}" -ge 5 ]]; then
        ok "Disk space: ${avail_gb} GB available"
    else
        warn "Low disk space: ${avail_gb} GB (recommend ≥5 GB)"
    fi

    # Network: NCBI
    if curl -sf --max-time 5 "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi" >/dev/null 2>&1; then
        ok "NCBI E-utilities: reachable"
    else
        warn "NCBI E-utilities: not reachable (GEO discovery won't work)"
    fi

    # Network: ClinicalTrials.gov
    if curl -sf --max-time 5 "https://clinicaltrials.gov/api/v2/studies?pageSize=1" >/dev/null 2>&1; then
        ok "ClinicalTrials.gov API: reachable"
    else
        warn "ClinicalTrials.gov API: not reachable (Direction B affected)"
    fi

    # Network: ChEMBL
    if curl -sf --max-time 5 "https://www.ebi.ac.uk/chembl/api/data/status.json" >/dev/null 2>&1; then
        ok "ChEMBL API: reachable"
    else
        warn "ChEMBL API: not reachable (kg_explain affected)"
    fi

    # Project directories
    for dir_name in "dsmeta_signature_pipeline" "sigreverse" "kg_explain" "LLM+RAG证据工程"; do
        local dir_path="${ROOT_DIR}/${dir_name}"
        if [[ -d "${dir_path}" ]]; then
            ok "Project: ${dir_name}"
        else
            fail "Missing project: ${dir_path}"
            errors=$((errors + 1))
        fi
    done

    # Virtual environments
    info ""
    info "Virtual Environment Status:"
    for dir_name in "kg_explain" "dsmeta_signature_pipeline" "sigreverse" "LLM+RAG证据工程"; do
        local venv="${ROOT_DIR}/${dir_name}/.venv"
        if [[ -d "${venv}" && -x "${venv}/bin/python3" ]]; then
            ok "  ${dir_name}/.venv — ready"
        else
            warn "  ${dir_name}/.venv — not found (will use system python3)"
        fi
    done

    # Disease configs
    info ""
    info "Disease Config Status:"
    local kg_count=0
    local dsmeta_count=0
    if [[ -d "${ROOT_DIR}/kg_explain/configs/diseases" ]]; then
        kg_count=$(find "${ROOT_DIR}/kg_explain/configs/diseases" -maxdepth 1 -type f -name "*.yaml" | wc -l | tr -d ' ')
    fi
    if [[ -d "${ROOT_DIR}/dsmeta_signature_pipeline/configs" ]]; then
        dsmeta_count=$(
            find "${ROOT_DIR}/dsmeta_signature_pipeline/configs" -maxdepth 1 -type f -name "*.yaml" \
            | awk 'BEGIN{c=0} $0 !~ /template/ && $0 !~ /athero_example/ {c++} END{print c+0}'
        )
    fi
    ok "  kg_explain disease configs: ${kg_count}"
    ok "  dsmeta disease configs: ${dsmeta_count}"

    # Disease lists
    info ""
    info "Disease List Status:"
    for list_file in "disease_list.txt" "disease_list_day1_origin.txt" "disease_list_day1_dual.txt"; do
        local path="${OPS_DIR}/${list_file}"
        if [[ -f "${path}" ]]; then
            local n_diseases
            n_diseases=$(awk 'BEGIN{c=0} $0 !~ /^[[:space:]]*#/ && $0 !~ /^[[:space:]]*$/ {c++} END{print c+0}' "${path}")
            if [[ "${n_diseases}" -gt 0 ]]; then
                ok "  ${list_file}: ${n_diseases} diseases"
            else
                warn "  ${list_file}: empty (no active diseases)"
            fi
        else
            warn "  ${list_file}: not found"
        fi
    done

    if [[ "${errors}" -gt 0 ]]; then
        fail "\n${errors} critical errors found. Fix them before proceeding."
        return 1
    fi

    # Disease list validation
    info ""
    info "Disease List Validation:"
    local list_errors=0
    for list_file in "${OPS_DIR}/disease_list_day1_origin.txt" "${OPS_DIR}/disease_list_day1_dual.txt" "${OPS_DIR}/disease_list_b_only.txt"; do
        if [[ ! -f "${list_file}" ]]; then continue; fi
        local fname
        fname="$(basename "${list_file}")"
        local line_num=0
        local list_ok=1
        while IFS= read -r line || [[ -n "${line:-}" ]]; do
            line_num=$((line_num + 1))
            # Skip comments and empty lines
            local trimmed
            trimmed="$(echo "${line}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
            if [[ -z "${trimmed}" || "${trimmed:0:1}" == "#" ]]; then continue; fi
            # Check format: must have at least disease_key|disease_query
            local field_count
            field_count="$(echo "${trimmed}" | awk -F'|' '{print NF}')"
            if [[ "${field_count}" -lt 2 ]]; then
                warn "  ${fname}:${line_num}: missing '|' separator (got ${field_count} fields)"
                list_ok=0
                list_errors=$((list_errors + 1))
            fi
            # Check disease_key is not empty
            local dkey
            dkey="$(echo "${trimmed}" | cut -d'|' -f1 | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
            if [[ -z "${dkey}" ]]; then
                warn "  ${fname}:${line_num}: empty disease_key"
                list_ok=0
                list_errors=$((list_errors + 1))
            fi
            # Check disease_query is not empty
            local dquery
            dquery="$(echo "${trimmed}" | cut -d'|' -f2 | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
            if [[ -z "${dquery}" ]]; then
                warn "  ${fname}:${line_num}: empty disease_query for key '${dkey}'"
                list_ok=0
                list_errors=$((list_errors + 1))
            fi
        done < "${list_file}"
        if [[ "${list_ok}" -eq 1 ]]; then
            ok "  ${fname}: valid"
        fi
    done
    if [[ "${list_errors}" -gt 0 ]]; then
        warn "${list_errors} disease list format warning(s) found"
    fi

    ok "\nEnvironment check passed!"
    return 0
}

# ── Phase 2: Setup Dependencies ─────────────────────────────────────

setup_venvs() {
    header "Phase 2: Setup Virtual Environments"

    # kg_explain venv
    local kg_venv="${ROOT_DIR}/kg_explain/.venv"
    if [[ -d "${kg_venv}" ]]; then
        ok "kg_explain/.venv already exists"
    else
        info "Creating kg_explain/.venv..."
        python3 -m venv "${kg_venv}"
        "${kg_venv}/bin/pip" install --upgrade pip -q
        if [[ -f "${ROOT_DIR}/kg_explain/requirements.txt" ]]; then
            "${kg_venv}/bin/pip" install -r "${ROOT_DIR}/kg_explain/requirements.txt" -q
            ok "kg_explain/.venv created and dependencies installed"
        else
            ok "kg_explain/.venv created (no requirements.txt found)"
        fi
    fi

    # sigreverse venv
    local sig_venv="${ROOT_DIR}/sigreverse/.venv"
    if [[ -d "${sig_venv}" ]]; then
        ok "sigreverse/.venv already exists"
    else
        info "Creating sigreverse/.venv..."
        python3 -m venv "${sig_venv}"
        "${sig_venv}/bin/pip" install --upgrade pip -q
        if [[ -f "${ROOT_DIR}/sigreverse/requirements.txt" ]]; then
            "${sig_venv}/bin/pip" install -r "${ROOT_DIR}/sigreverse/requirements.txt" -q
            ok "sigreverse/.venv created and dependencies installed"
        else
            ok "sigreverse/.venv created (no requirements.txt found)"
        fi
    fi

    # dsmeta: conda environment (can't auto-create, inform user)
    if [[ -d "${ROOT_DIR}/dsmeta_signature_pipeline/.venv" ]]; then
        ok "dsmeta/.venv already exists"
    else
        warn "dsmeta needs conda environment (R + Bioconductor dependencies)"
        warn "  Run: cd ${ROOT_DIR}/dsmeta_signature_pipeline && mamba env create -f environment.yml"
        warn "  Or create venv manually if R dependencies are already installed globally"
    fi

    # LLM+RAG venv
    local llm_venv="${ROOT_DIR}/LLM+RAG证据工程/.venv"
    if [[ -d "${llm_venv}" ]]; then
        ok "LLM+RAG/.venv already exists"
    else
        info "Creating LLM+RAG/.venv..."
        python3 -m venv "${llm_venv}"
        "${llm_venv}/bin/pip" install --upgrade pip -q
        if [[ -f "${ROOT_DIR}/LLM+RAG证据工程/requirements.txt" ]]; then
            "${llm_venv}/bin/pip" install -r "${ROOT_DIR}/LLM+RAG证据工程/requirements.txt" -q
            ok "LLM+RAG/.venv created and dependencies installed"
        else
            ok "LLM+RAG/.venv created (no requirements.txt found)"
        fi
    fi

    # ops dependencies (for auto_discover_geo.py)
    info "Checking ops tool dependencies..."
    python3 -c "import requests, yaml" 2>/dev/null || {
        info "Installing ops dependencies..."
        pip3 install requests pyyaml -q 2>/dev/null || true
    }
    ok "Setup complete"
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

    local runner="${OPS_DIR}/run_24x7_all_directions.sh"
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

    local runner="${OPS_DIR}/run_24x7_all_directions.sh"

    # Create temporary disease list
    local tmp_list
    tmp_list=$(mktemp)
    trap 'rm -f "${tmp_list}"' EXIT INT TERM
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
        check_environment || true
        setup_venvs
        ;;
    discover)
        run_geo_discovery
        ;;
    run)
        launch_pipeline
        ;;
    single)
        run_single_disease
        ;;
    full)
        check_environment || true
        info ""
        read -p "Continue with setup? [Y/n] " -r answer
        if [[ "${answer}" =~ ^[Nn] ]]; then
            info "Aborted."
            exit 0
        fi
        setup_venvs
        info ""
        read -p "Run GEO auto-discovery? [y/N] " -r answer
        if [[ "${answer}" =~ ^[Yy] ]]; then
            run_geo_discovery
        fi
        info ""
        read -p "Start pipeline? [Y/n] " -r answer
        if [[ "${answer}" =~ ^[Nn] ]]; then
            info "Setup complete. Start manually with: bash ops/quickstart.sh --run-only"
            exit 0
        fi
        launch_pipeline
        ;;
esac
