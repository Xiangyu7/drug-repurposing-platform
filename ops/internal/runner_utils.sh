#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# runner_utils.sh — Utility functions for the Drug Repurposing Runner
# ═══════════════════════════════════════════════════════════════════
# Sourced by runner.sh. Do not execute directly.

[[ -n "${_RUNNER_UTILS_LOADED:-}" ]] && return 0; _RUNNER_UTILS_LOADED=1

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
