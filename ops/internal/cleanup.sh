#!/usr/bin/env bash
set -Eeuo pipefail

# ============================================================
# cleanup.sh — Clean up old data to free disk space
# ============================================================
#
# Usage:
#   bash ops/internal/cleanup.sh                # show disk usage summary
#   bash ops/internal/cleanup.sh --work 7       # remove work dirs older than 7 days
#   bash ops/internal/cleanup.sh --quarantine 3 # remove quarantine dirs older than 3 days
#   bash ops/internal/cleanup.sh --kg-cache     # remove kg_explain HTTP cache
#   bash ops/internal/cleanup.sh --dsmeta-work  # remove all dsmeta work dirs
#   bash ops/internal/cleanup.sh --logs 30      # remove logs older than 30 days
#   bash ops/internal/cleanup.sh --all 7        # all of the above (7 day threshold)
#   bash ops/internal/cleanup.sh --dry-run --all 7  # show what would be deleted
# ============================================================

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

DRY_RUN=0
ACTION=""
DAYS=7

while [[ $# -gt 0 ]]; do
    case "$1" in
        --work)         ACTION="work"; DAYS="${2:-7}"; shift 2 ;;
        --quarantine)   ACTION="quarantine"; DAYS="${2:-3}"; shift 2 ;;
        --kg-cache)     ACTION="kg-cache"; shift ;;
        --dsmeta-work)  ACTION="dsmeta-work"; shift ;;
        --logs)         ACTION="logs"; DAYS="${2:-30}"; shift 2 ;;
        --all)          ACTION="all"; DAYS="${2:-7}"; shift 2 ;;
        --dry-run)      DRY_RUN=1; shift ;;
        -h|--help)
            echo "Usage: bash ops/internal/cleanup.sh [action] [options]"
            echo ""
            echo "Actions:"
            echo "  (no action)       Show disk usage summary"
            echo "  --work <days>     Remove runtime/work dirs older than N days (default: 7)"
            echo "  --quarantine <d>  Remove quarantine dirs older than N days (default: 3)"
            echo "  --kg-cache        Remove kg_explain HTTP cache"
            echo "  --dsmeta-work     Remove all dsmeta work directories"
            echo "  --logs <days>     Remove log files older than N days (default: 30)"
            echo "  --all <days>      All of the above (default: 7)"
            echo ""
            echo "Options:"
            echo "  --dry-run         Show what would be deleted without deleting"
            exit 0
            ;;
        *)  echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ── Helpers ──
dir_size() {
    local dir="$1"
    if [[ -d "${dir}" ]]; then
        du -sh "${dir}" 2>/dev/null | cut -f1
    else
        echo "0"
    fi
}

file_count() {
    local dir="$1"
    if [[ -d "${dir}" ]]; then
        find "${dir}" -type f 2>/dev/null | wc -l | tr -d ' '
    else
        echo "0"
    fi
}

clean_old_dirs() {
    local label="$1"
    local root="$2"
    local days="$3"

    if [[ ! -d "${root}" ]]; then
        echo "  ${label}: (directory not found)"
        return 0
    fi

    local targets
    targets="$(find "${root}" -mindepth 2 -maxdepth 2 -type d -mtime +"${days}" 2>/dev/null || true)"
    local count
    count="$(echo "${targets}" | grep -c . 2>/dev/null || echo 0)"

    if [[ "${count}" -eq 0 || -z "${targets}" ]]; then
        printf "  ${GREEN}%s${NC}: nothing to clean (all < %s days old)\n" "${label}" "${days}"
        return 0
    fi

    if [[ "${DRY_RUN}" -eq 1 ]]; then
        printf "  ${YELLOW}%s${NC}: would delete %s dirs older than %s days:\n" "${label}" "${count}" "${days}"
        echo "${targets}" | while read -r d; do
            echo "    $(du -sh "${d}" 2>/dev/null | cut -f1)  ${d}"
        done
    else
        echo "${targets}" | xargs rm -rf 2>/dev/null || true
        find "${root}" -mindepth 1 -maxdepth 1 -type d -empty -delete 2>/dev/null || true
        printf "  ${GREEN}%s${NC}: deleted %s dirs older than %s days\n" "${label}" "${count}" "${days}"
    fi
}

clean_dir() {
    local label="$1"
    local dir="$2"

    if [[ ! -d "${dir}" ]]; then
        echo "  ${label}: (directory not found)"
        return 0
    fi

    local size
    size="$(dir_size "${dir}")"
    local count
    count="$(file_count "${dir}")"

    if [[ "${count}" -eq 0 ]]; then
        printf "  ${GREEN}%s${NC}: already empty\n" "${label}"
        return 0
    fi

    if [[ "${DRY_RUN}" -eq 1 ]]; then
        printf "  ${YELLOW}%s${NC}: would delete %s files (%s)\n" "${label}" "${count}" "${size}"
    else
        rm -rf "${dir}"
        mkdir -p "${dir}"
        printf "  ${GREEN}%s${NC}: deleted %s files (%s freed)\n" "${label}" "${count}" "${size}"
    fi
}

clean_old_logs() {
    local label="$1"
    local days="$2"

    local log_dirs=("${ROOT_DIR}/logs")
    local total_deleted=0

    for log_dir in "${log_dirs[@]}"; do
        if [[ ! -d "${log_dir}" ]]; then continue; fi
        local targets
        targets="$(find "${log_dir}" -name "*.log" -mtime +"${days}" 2>/dev/null || true)"
        local count
        count="$(echo "${targets}" | grep -c . 2>/dev/null || echo 0)"

        if [[ "${count}" -gt 0 && -n "${targets}" ]]; then
            if [[ "${DRY_RUN}" -eq 1 ]]; then
                printf "  ${YELLOW}%s${NC}: would delete %s log files older than %s days\n" "${label}" "${count}" "${days}"
            else
                echo "${targets}" | xargs rm -f 2>/dev/null || true
                total_deleted=$((total_deleted + count))
            fi
        fi
    done

    if [[ "${DRY_RUN}" -eq 0 ]]; then
        if [[ "${total_deleted}" -gt 0 ]]; then
            printf "  ${GREEN}%s${NC}: deleted %s log files older than %s days\n" "${label}" "${total_deleted}" "${days}"
        else
            printf "  ${GREEN}%s${NC}: nothing to clean\n" "${label}"
        fi
    fi
}

# ── Show disk usage summary (default action) ──
if [[ -z "${ACTION}" ]]; then
    echo ""
    printf "${BOLD}Disk Usage Summary${NC}\n"
    echo ""

    # System disk
    avail="$(df -h "${ROOT_DIR}" | awk 'NR==2 {print $4}')"
    total="$(df -h "${ROOT_DIR}" | awk 'NR==2 {print $2}')"
    printf "  System:              %s free / %s total\n" "${avail}" "${total}"
    echo ""

    # Per-directory
    printf "  %-35s %s\n" "Directory" "Size"
    printf "  %-35s %s\n" "---" "---"
    for dir_label in \
        "runtime/work:runtime/work" \
        "runtime/results:runtime/results" \
        "runtime/quarantine:runtime/quarantine" \
        "kg_explain/cache:kg_explain/cache" \
        "kg_explain/data:kg_explain/data" \
        "kg_explain/output:kg_explain/output" \
        "dsmeta/work:dsmeta_signature_pipeline/work" \
        "dsmeta/outputs:dsmeta_signature_pipeline/outputs" \
        "logs:logs"; do
        label="${dir_label%%:*}"
        dir="${ROOT_DIR}/${dir_label##*:}"
        size="$(dir_size "${dir}")"
        printf "  %-35s %s\n" "${label}" "${size}"
    done

    echo ""
    echo "Tip: bash ops/internal/cleanup.sh --all 7      # clean everything > 7 days"
    echo "     bash ops/internal/cleanup.sh --dry-run --all 7  # preview first"
    exit 0
fi

# ── Execute cleanup ──
echo ""
if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf "${BOLD}${YELLOW}DRY RUN — nothing will be deleted${NC}\n"
else
    printf "${BOLD}Cleaning up...${NC}\n"
fi
echo ""

case "${ACTION}" in
    work)
        clean_old_dirs "runtime/work" "${ROOT_DIR}/runtime/work" "${DAYS}"
        ;;
    quarantine)
        clean_old_dirs "runtime/quarantine" "${ROOT_DIR}/runtime/quarantine" "${DAYS}"
        ;;
    kg-cache)
        clean_dir "kg_explain/cache" "${ROOT_DIR}/kg_explain/cache/http_json"
        ;;
    dsmeta-work)
        clean_dir "dsmeta/work" "${ROOT_DIR}/dsmeta_signature_pipeline/work"
        ;;
    logs)
        clean_old_logs "logs" "${DAYS}"
        ;;
    all)
        clean_old_dirs "runtime/work" "${ROOT_DIR}/runtime/work" "${DAYS}"
        clean_old_dirs "runtime/quarantine" "${ROOT_DIR}/runtime/quarantine" "${DAYS}"
        clean_dir "kg_explain/cache" "${ROOT_DIR}/kg_explain/cache/http_json"
        clean_dir "dsmeta/work" "${ROOT_DIR}/dsmeta_signature_pipeline/work"
        clean_old_logs "logs" "${DAYS}"
        ;;
esac

echo ""
if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "Run without --dry-run to actually delete."
fi
