#!/usr/bin/env bash
set -Eeuo pipefail

# ============================================================
# show_results.sh — Find and display pipeline results
# ============================================================
#
# Usage:
#   bash ops/show_results.sh                       # list all diseases with results
#   bash ops/show_results.sh atherosclerosis       # show latest results for a disease
#   bash ops/show_results.sh atherosclerosis --all  # show all runs for a disease
#   bash ops/show_results.sh --copy /tmp/export    # copy latest results to a folder
# ============================================================

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RESULTS_DIR="${ROOT_DIR}/runtime/results"

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# ── Parse arguments ──
DISEASE_KEY=""
SHOW_ALL=0
COPY_TO=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --all)      SHOW_ALL=1; shift ;;
        --copy)     COPY_TO="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: bash ops/show_results.sh [disease_key] [options]"
            echo ""
            echo "Options:"
            echo "  --all            Show all runs (not just latest)"
            echo "  --copy <dir>     Copy latest results to specified directory"
            echo ""
            echo "Examples:"
            echo "  bash ops/show_results.sh                          # list all diseases"
            echo "  bash ops/show_results.sh atherosclerosis          # latest results"
            echo "  bash ops/show_results.sh atherosclerosis --all    # all runs"
            echo "  bash ops/show_results.sh atherosclerosis --copy /tmp/export"
            exit 0
            ;;
        -*)  echo "Unknown option: $1"; exit 1 ;;
        *)   DISEASE_KEY="$1"; shift ;;
    esac
done

# ── Helper: find key output files ──
show_run_files() {
    local run_dir="$1"
    local run_name
    run_name="$(basename "${run_dir}")"

    # Read run_summary.json if exists
    local summary="${run_dir}/run_summary.json"
    if [[ -f "${summary}" ]]; then
        local cross_st origin_st run_mode
        cross_st="$(python3 -c "import json; d=json.load(open('${summary}')); print(d.get('cross_status','?'))" 2>/dev/null || echo "?")"
        origin_st="$(python3 -c "import json; d=json.load(open('${summary}')); print(d.get('origin_status','?'))" 2>/dev/null || echo "?")"
        run_mode="$(python3 -c "import json; d=json.load(open('${summary}')); print(d.get('run_mode','?'))" 2>/dev/null || echo "?")"
        printf "  ${CYAN}%-24s${NC} mode=%-12s cross=%-8s origin=%-8s\n" "${run_name}" "${run_mode}" "${cross_st}" "${origin_st}"
    else
        printf "  ${CYAN}%-24s${NC}\n" "${run_name}"
    fi

    # Key output files
    local found=0

    # Cross route results
    for f in "${run_dir}/cross/step8"/shortlist_top*.xlsx "${run_dir}/cross/step8"/shortlist_top*.csv; do
        if [[ -f "${f}" ]]; then
            printf "    ${GREEN}[A]${NC} %s\n" "$(basename "${f}")"
            found=1
        fi
    done
    for f in "${run_dir}/cross/step9"/*.md "${run_dir}/cross/step9"/*.json; do
        if [[ -f "${f}" ]]; then
            printf "    ${GREEN}[A]${NC} %s\n" "$(basename "${f}")"
            found=1
        fi
    done

    # Origin route results
    for f in "${run_dir}/origin/step8"/shortlist_top*.xlsx "${run_dir}/origin/step8"/shortlist_top*.csv; do
        if [[ -f "${f}" ]]; then
            printf "    ${BLUE}[B]${NC} %s\n" "$(basename "${f}")"
            found=1
        fi
    done
    for f in "${run_dir}/origin/step9"/*.md "${run_dir}/origin/step9"/*.json; do
        if [[ -f "${f}" ]]; then
            printf "    ${BLUE}[B]${NC} %s\n" "$(basename "${f}")"
            found=1
        fi
    done

    # Bridge files
    for f in "${run_dir}/kg"/bridge_*.csv; do
        if [[ -f "${f}" ]]; then
            printf "    ${YELLOW}[KG]${NC} %s\n" "$(basename "${f}")"
            found=1
        fi
    done

    if [[ "${found}" -eq 0 ]]; then
        echo "    (no output files yet)"
    fi
}

# ── No disease specified: list all ──
if [[ -z "${DISEASE_KEY}" ]]; then
    if [[ ! -d "${RESULTS_DIR}" ]]; then
        echo "No results yet. Run the pipeline first."
        exit 0
    fi

    echo ""
    printf "${BOLD}Diseases with results:${NC}\n"
    echo ""

    for disease_dir in "${RESULTS_DIR}"/*/; do
        if [[ ! -d "${disease_dir}" ]]; then continue; fi
        disease="$(basename "${disease_dir}")"
        n_runs="$(find "${disease_dir}" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')"

        # Find latest run
        latest="$(find "${disease_dir}" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort -r | head -1)"
        if [[ -n "${latest}" ]]; then
            printf "  ${BOLD}%-30s${NC} %s run(s)\n" "${disease}" "${n_runs}"
            show_run_files "${latest}"
        fi
    done
    echo ""
    echo "Tip: bash ops/show_results.sh <disease_key> for details"
    exit 0
fi

# ── Specific disease ──
disease_dir="${RESULTS_DIR}/${DISEASE_KEY}"
if [[ ! -d "${disease_dir}" ]]; then
    echo "No results for '${DISEASE_KEY}'"
    echo ""
    echo "Available diseases:"
    if [[ -d "${RESULTS_DIR}" ]]; then
        for d in "${RESULTS_DIR}"/*/; do
            [[ -d "${d}" ]] && echo "  $(basename "${d}")"
        done
    else
        echo "  (none)"
    fi
    exit 1
fi

echo ""
printf "${BOLD}Results for: ${DISEASE_KEY}${NC}\n"
echo ""

if [[ "${SHOW_ALL}" -eq 1 ]]; then
    # Show all runs
    for run_dir in $(find "${disease_dir}" -mindepth 1 -maxdepth 1 -type d | sort -r); do
        show_run_files "${run_dir}"
        echo ""
    done
else
    # Show latest only
    latest="$(find "${disease_dir}" -mindepth 1 -maxdepth 1 -type d | sort -r | head -1)"
    if [[ -n "${latest}" ]]; then
        show_run_files "${latest}"

        echo ""
        printf "${BOLD}Full path:${NC} ${latest}\n"
    fi
fi

# ── Copy results ──
if [[ -n "${COPY_TO}" ]]; then
    latest="$(find "${disease_dir}" -mindepth 1 -maxdepth 1 -type d | sort -r | head -1)"
    if [[ -z "${latest}" ]]; then
        echo "No runs to copy"
        exit 1
    fi
    mkdir -p "${COPY_TO}"
    cp -R "${latest}"/* "${COPY_TO}/"
    echo ""
    printf "${GREEN}Copied to: ${COPY_TO}${NC}\n"
fi
