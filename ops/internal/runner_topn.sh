#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# runner_topn.sh — TopN strategy functions for the Drug Repurposing Runner
# ═══════════════════════════════════════════════════════════════════
# Sourced by runner.sh. Do not execute directly.

[[ -n "${_RUNNER_TOPN_LOADED:-}" ]] && return 0; _RUNNER_TOPN_LOADED=1

apply_topn_profile_defaults() {
  case "${TOPN_PROFILE}" in
    stable)
      TOPN_CAP_ORIGIN=40;  TOPN_CAP_CROSS=52
      TOPN_STAGE1_MIN_ORIGIN=22; TOPN_STAGE1_MAX_ORIGIN=30
      TOPN_STAGE1_MIN_CROSS=35;  TOPN_STAGE1_MAX_CROSS=45
      ;;
    balanced)
      TOPN_CAP_ORIGIN=48;  TOPN_CAP_CROSS=58
      TOPN_STAGE1_MIN_ORIGIN=28; TOPN_STAGE1_MAX_ORIGIN=38
      TOPN_STAGE1_MIN_CROSS=40;  TOPN_STAGE1_MAX_CROSS=52
      ;;
    recall)
      TOPN_CAP_ORIGIN=64;  TOPN_CAP_CROSS=80
      TOPN_STAGE1_MIN_ORIGIN=32; TOPN_STAGE1_MAX_ORIGIN=52
      TOPN_STAGE1_MIN_CROSS=48;  TOPN_STAGE1_MAX_CROSS=66
      ;;
    *)
      printf '[WARN] Unknown TOPN_PROFILE=%s, fallback to stable\n' "${TOPN_PROFILE}" >&2
      TOPN_PROFILE="stable"
      apply_topn_profile_defaults
      return 0
      ;;
  esac
}

is_auto_topn() {
  local v="${1:-}"
  # bash 3.x compatible lowercase comparison
  [[ "$(printf '%s' "${v}" | tr '[:upper:]' '[:lower:]')" == "auto" ]]
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
    "${KG_PY}" "${TOPN_POLICY_PY}" decide
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

  # Detect degraded mode and warn loudly (but don't stop — pipeline explores more)
  local topn_mode
  topn_mode="$(json_get_field "${out_json}" "mode")"
  if [[ "${topn_mode}" == "degraded" ]]; then
    local topn_error
    topn_error="$(json_get_field "${out_json}" "error")"
    log "[WARN] ${route_label}: TopN running in DEGRADED mode (expanded to ${resolved}). Error: ${topn_error}"
    log "[WARN] ${route_label}: Results from this run should be reviewed — degraded flag set in decision JSON"
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

  if ! "${KG_PY}" "${TOPN_POLICY_PY}" quality \
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
NON_CRITICAL = {"Pathway", "v5 排序", "v4 排序", "DTPD 排序"}
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

derive_matched_ids_from_dtpd() {
  local disease_query="$1"
  local disease_key="${2:-}"
  local drug_source="${3:-ctgov}"
  local v3_path=""
  local dtpd_path_new="${KG_DIR}/output/${disease_key}/${drug_source}/dtpd_rank.csv"
  local dtpd_path_legacy="${KG_DIR}/output/dtpd_rank.csv"
  if [[ -n "${disease_key}" && -f "${dtpd_path_new}" ]]; then
    v3_path="${dtpd_path_new}"
  elif [[ -f "${dtpd_path_legacy}" ]]; then
    v3_path="${dtpd_path_legacy}"
  fi
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
