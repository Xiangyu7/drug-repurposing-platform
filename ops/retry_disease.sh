#!/usr/bin/env bash
set -Eeuo pipefail

# ============================================================
# retry_disease.sh — Retry a failed disease run
# ============================================================
#
# Usage:
#   bash ops/retry_disease.sh atherosclerosis                    # retry Direction B (default)
#   bash ops/retry_disease.sh atherosclerosis --mode dual        # retry A + B
#   bash ops/retry_disease.sh atherosclerosis --mode cross_only  # retry A only
#
# This script:
#   1. Looks up the disease in existing disease lists
#   2. Auto-constructs a single-disease list with correct metadata
#   3. Cleans up stale work/manifest files from previous failed run
#   4. Runs the pipeline for that disease only (MAX_CYCLES=1)
# ============================================================

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPS_DIR="${ROOT_DIR}/ops"
RUNNER="${OPS_DIR}/run_24x7_all_directions.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

ok()   { printf "${GREEN}+${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}!${NC} %s\n" "$*"; }
fail() { printf "${RED}x${NC} %s\n" "$*"; exit 1; }
info() { printf "${BLUE}i${NC} %s\n" "$*"; }

# ── Parse arguments ──
DISEASE_KEY=""
RUN_MODE="${RUN_MODE:-origin_only}"
CLEAN_PREVIOUS="${CLEAN_PREVIOUS:-1}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)       RUN_MODE="$2"; shift 2 ;;
        --no-clean)   CLEAN_PREVIOUS=0; shift ;;
        -h|--help)
            echo "Usage: bash ops/retry_disease.sh <disease_key> [options]"
            echo ""
            echo "Options:"
            echo "  --mode <mode>    Run mode: dual | origin_only | cross_only (default: origin_only)"
            echo "  --no-clean       Don't clean up previous failed run artifacts"
            echo ""
            echo "Examples:"
            echo "  bash ops/retry_disease.sh atherosclerosis"
            echo "  bash ops/retry_disease.sh heart_failure --mode dual"
            echo "  bash ops/retry_disease.sh stroke --mode cross_only"
            exit 0
            ;;
        -*)
            fail "Unknown option: $1"
            ;;
        *)
            if [[ -z "${DISEASE_KEY}" ]]; then
                DISEASE_KEY="$1"
            else
                fail "Unexpected argument: $1"
            fi
            shift
            ;;
    esac
done

if [[ -z "${DISEASE_KEY}" ]]; then
    fail "Usage: bash ops/retry_disease.sh <disease_key> [--mode dual|origin_only|cross_only]"
fi

# ── Look up disease metadata from existing lists ──
disease_line=""
for list_file in "${OPS_DIR}/disease_list_day1_dual.txt" "${OPS_DIR}/disease_list_day1_origin.txt" "${OPS_DIR}/disease_list_b_only.txt" "${OPS_DIR}/disease_list.txt"; do
    if [[ -f "${list_file}" ]]; then
        match="$(grep "^${DISEASE_KEY}|" "${list_file}" || true)"
        if [[ -n "${match}" ]]; then
            disease_line="${match}"
            info "Found ${DISEASE_KEY} in $(basename "${list_file}")"
            break
        fi
    fi
done

if [[ -z "${disease_line}" ]]; then
    warn "Disease '${DISEASE_KEY}' not found in any disease list"
    info "Creating minimal entry: ${DISEASE_KEY}|${DISEASE_KEY//_/ }||"
    disease_line="${DISEASE_KEY}|${DISEASE_KEY//_/ }||"
fi

# ── Show what we're about to do ──
echo ""
echo "  Disease:  ${DISEASE_KEY}"
echo "  Mode:     ${RUN_MODE}"
echo "  Entry:    ${disease_line}"
echo ""

# ── Clean up previous failed artifacts ──
if [[ "${CLEAN_PREVIOUS}" == "1" ]]; then
    # Clean stale work dirs
    if [[ -d "${ROOT_DIR}/runtime/work/${DISEASE_KEY}" ]]; then
        local_size="$(du -sh "${ROOT_DIR}/runtime/work/${DISEASE_KEY}" 2>/dev/null | cut -f1 || echo "?")"
        rm -rf "${ROOT_DIR}/runtime/work/${DISEASE_KEY}"
        ok "Cleaned stale work dir (${local_size})"
    fi

    # Clean stale kg_explain output (prevents manifest mismatch)
    if [[ -d "${ROOT_DIR}/kg_explain/output/${DISEASE_KEY}" ]]; then
        rm -rf "${ROOT_DIR}/kg_explain/output/${DISEASE_KEY}"
        ok "Cleaned stale kg_explain output"
    fi

    # Clean stale dsmeta work
    if [[ -d "${ROOT_DIR}/dsmeta_signature_pipeline/work/${DISEASE_KEY}" ]]; then
        rm -rf "${ROOT_DIR}/dsmeta_signature_pipeline/work/${DISEASE_KEY}"
        ok "Cleaned stale dsmeta work dir"
    fi
fi

# ── Create temp disease list ──
tmp_list="$(mktemp)"
trap 'rm -f "${tmp_list}"' EXIT
echo "${disease_line}" > "${tmp_list}"

# ── Run ──
info "Starting retry..."
echo ""

env \
    RUN_MODE="${RUN_MODE}" \
    LOCK_NAME="retry_${DISEASE_KEY}" \
    MAX_CYCLES=1 \
    SLEEP_SECONDS=0 \
    TOPN_PROFILE="${TOPN_PROFILE:-stable}" \
    STRICT_CONTRACT="${STRICT_CONTRACT:-1}" \
    DSMETA_CLEANUP="${DSMETA_CLEANUP:-1}" \
    bash "${RUNNER}" "${tmp_list}"

rc=$?
echo ""
if [[ "${rc}" -eq 0 ]]; then
    ok "Retry completed successfully for ${DISEASE_KEY}"
    echo ""
    info "View results: bash ops/show_results.sh ${DISEASE_KEY}"
else
    fail "Retry failed for ${DISEASE_KEY} (exit code: ${rc})"
fi
