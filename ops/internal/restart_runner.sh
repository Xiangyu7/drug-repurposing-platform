#!/usr/bin/env bash
set -Eeuo pipefail

# ============================================================
# restart_runner.sh — Gracefully restart the pipeline runner
# ============================================================
#
# Usage:
#   bash ops/internal/restart_runner.sh                  # restart all active runners
#   bash ops/internal/restart_runner.sh --runner dual    # restart specific runner
#   bash ops/internal/restart_runner.sh --stop           # stop only, don't restart
#
# This script:
#   1. Finds running runner processes via PID files
#   2. Sends SIGTERM for graceful shutdown
#   3. Waits for clean exit
#   4. Restarts with the same config (unless --stop)
# ============================================================

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${ROOT_DIR}/runtime/state"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

ok()   { printf "${GREEN}+${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}!${NC} %s\n" "$*"; }
fail() { printf "${RED}x${NC} %s\n" "$*"; }

ACTION="restart"
TARGET_RUNNER=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --runner)  TARGET_RUNNER="$2"; shift 2 ;;
        --stop)    ACTION="stop"; shift ;;
        -h|--help)
            echo "Usage: bash ops/internal/restart_runner.sh [options]"
            echo ""
            echo "Options:"
            echo "  --runner <name>  Target specific runner (dual, origin_only, cross_only)"
            echo "  --stop           Stop without restarting"
            exit 0
            ;;
        *)  echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ── Find active runners ──
echo ""
echo "Scanning for active runners..."
echo ""

found=0
for pid_file in "${STATE_DIR}"/*.pid; do
    if [[ ! -f "${pid_file}" ]]; then continue; fi

    runner_name="$(basename "${pid_file}" .pid)"
    pid="$(cat "${pid_file}" 2>/dev/null || true)"

    if [[ -n "${TARGET_RUNNER}" && "${runner_name}" != *"${TARGET_RUNNER}"* ]]; then
        continue
    fi

    if [[ -z "${pid}" ]]; then
        warn "${runner_name}: PID file empty, removing"
        rm -f "${pid_file}"
        continue
    fi

    if ! kill -0 "${pid}" 2>/dev/null; then
        warn "${runner_name}: PID ${pid} not running, removing stale PID file"
        rm -f "${pid_file}"
        # Also clean lock file
        rm -f "${STATE_DIR}/runner_"*.lock 2>/dev/null || true
        continue
    fi

    found=1
    echo "  ${runner_name}: PID ${pid} (running)"

    # Stop
    echo "  Sending SIGTERM to ${pid}..."
    kill -TERM "${pid}" 2>/dev/null || true

    # Wait for exit (max 30s)
    for i in $(seq 1 30); do
        if ! kill -0 "${pid}" 2>/dev/null; then
            ok "${runner_name}: stopped cleanly"
            break
        fi
        sleep 1
    done

    # Force kill if still running
    if kill -0 "${pid}" 2>/dev/null; then
        warn "${runner_name}: still running after 30s, sending SIGKILL"
        kill -9 "${pid}" 2>/dev/null || true
        sleep 1
    fi

    rm -f "${pid_file}"

    # Clean lock files
    for lock in "${STATE_DIR}"/runner_*.lock; do
        if [[ -f "${lock}" ]]; then
            local_pid="$(cat "${lock}" 2>/dev/null || true)"
            if [[ "${local_pid}" == "${pid}" ]]; then
                rm -f "${lock}"
                ok "Cleaned lock file: $(basename "${lock}")"
            fi
        fi
    done
done

if [[ "${found}" -eq 0 ]]; then
    warn "No active runners found"
    # Clean any stale lock files
    for lock in "${STATE_DIR}"/runner_*.lock; do
        if [[ -f "${lock}" ]]; then
            local_pid="$(cat "${lock}" 2>/dev/null || true)"
            if [[ -z "${local_pid}" ]] || ! kill -0 "${local_pid}" 2>/dev/null; then
                rm -f "${lock}"
                ok "Cleaned stale lock file: $(basename "${lock}")"
            fi
        fi
    done
fi

if [[ "${ACTION}" == "stop" ]]; then
    echo ""
    ok "All runners stopped."
    exit 0
fi

# ── Restart ──
echo ""
echo "To restart:"
echo "  bash ops/start.sh start                 # start pipeline"
echo "  bash ops/start.sh run <disease>          # single disease"
echo ""
