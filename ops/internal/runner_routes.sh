#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# runner_routes.sh — Pipeline orchestration (process_disease, routes)
# ═══════════════════════════════════════════════════════════════════
# Sourced by runner.sh. Do not execute directly.

[[ -n "${_RUNNER_ROUTES_LOADED:-}" ]] && return 0; _RUNNER_ROUTES_LOADED=1

# ── Signature decision banner ─────────────────────────────────────

log_signature_decision() {
  # Args: primary_name primary_status primary_detail secondary_name secondary_status secondary_detail chosen_source
  # status: "ok" | "fail" | "skip"
  local pri_name="$1" pri_status="$2" pri_detail="$3"
  local sec_name="$4" sec_status="$5" sec_detail="$6"
  local chosen="$7"

  local pri_icon="·" sec_icon="·"
  case "${pri_status}" in
    ok)   pri_icon="✓" ;;
    fail) pri_icon="✗" ;;
  esac
  case "${sec_status}" in
    ok)   sec_icon="✓" ;;
    fail) sec_icon="✗" ;;
  esac

  log "┌─ 签名来源决策 (${SIG_PRIORITY}-first) ──────────────────────┐"
  log "│  ${pri_name}  ${pri_icon} ${pri_detail}"
  log "│  ${sec_name}  ${sec_icon} ${sec_detail}"
  if [[ "${chosen}" == "none" ]]; then
    log "│  → 签名不可用，Cross 路线终止"
  else
    local suffix=""
    [[ "${chosen}" != "${SIG_PRIORITY}" ]] && suffix=" (fallback)"
    log "│  → 使用: ${chosen}${suffix}"
  fi
  log "└──────────────────────────────────────────────────────────┘"
}

# ── Disease summary banner ────────────────────────────────────────

log_disease_summary() {
  local disease_key="$1" run_id="$2" elapsed="$3"
  local cross_status="$4" origin_status="$5"
  local sig_source="$6" cross_drugs="$7" origin_drugs="$8"
  local ab_overlap="$9" cross_elapsed="${10}" origin_elapsed="${11}"

  local dur
  dur="$(format_duration "${elapsed}")"
  local cross_dur="" origin_dur=""
  [[ -n "${cross_elapsed}" && "${cross_elapsed}" != "0" ]] && cross_dur="  耗时=$(format_duration "${cross_elapsed}")"
  [[ -n "${origin_elapsed}" && "${origin_elapsed}" != "0" ]] && origin_dur="  耗时=$(format_duration "${origin_elapsed}")"

  local c_icon="·" o_icon="·"
  case "${cross_status}" in
    success) c_icon="✓" ;;
    failed)  c_icon="✗" ;;
    skipped) c_icon="⏭" ;;
  esac
  case "${origin_status}" in
    success) o_icon="✓" ;;
    failed)  o_icon="✗" ;;
    skipped) o_icon="⏭" ;;
  esac

  log "┌─ ${disease_key} 完成 (run_id=${run_id}) ──────────────────────┐"
  log "│  总耗时: ${dur}   模式: ${RUN_MODE}"

  if [[ "${cross_status}" != "not_run" ]]; then
    local sig_label=""
    [[ -n "${sig_source}" && "${sig_source}" != "none" ]] && sig_label="  签名=${sig_source}"
    log "│  Cross:  ${c_icon} ${cross_status}${sig_label}  药物=${cross_drugs}${cross_dur}"
  fi

  if [[ "${origin_status}" != "not_run" ]]; then
    log "│  Origin: ${o_icon} ${origin_status}  药物=${origin_drugs}${origin_dur}"
  fi

  if [[ -n "${ab_overlap}" && "${ab_overlap}" != "0" && "${ab_overlap}" != "" ]]; then
    log "│  A+B:   ${ab_overlap} 个重叠药物"
  fi

  log "└──────────────────────────────────────────────────────────────┘"
}

annotate_route_manifest_summary() {
  local step8_dir="$1"
  local route_label="$2"
  local selected_stage="$3"
  local resolved_topn="$4"
  local quality_passed="$5"
  local decision_stage1="$6"
  local quality_stage1="$7"
  local decision_stage2="$8"
  local quality_stage2="$9"

  local manifest_path="${step8_dir}/step8_manifest.json"
  if [[ ! -f "${manifest_path}" ]]; then
    return 0
  fi
  python3 - "${manifest_path}" "${route_label}" "${selected_stage}" "${resolved_topn}" "${quality_passed}" "${decision_stage1}" "${quality_stage1}" "${decision_stage2}" "${quality_stage2}" <<'PY'
import json
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1])
route_label = sys.argv[2]
selected_stage = sys.argv[3]
resolved_topn = int(sys.argv[4])
quality_passed = str(sys.argv[5]).strip() in {"1", "true", "True"}
decision_stage1 = sys.argv[6]
quality_stage1 = sys.argv[7]
decision_stage2 = sys.argv[8]
quality_stage2 = sys.argv[9]

obj = json.loads(manifest_path.read_text(encoding="utf-8"))
summary = obj.get("summary") or {}
summary["topn_policy"] = {
    "route": route_label,
    "selected_stage": selected_stage,
    "resolved_topn": resolved_topn,
    "quality_passed": quality_passed,
    "decision_stage1": decision_stage1,
    "quality_stage1": quality_stage1,
    "decision_stage2": decision_stage2,
    "quality_stage2": quality_stage2,
}
obj["summary"] = summary
manifest_path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
PY
}

# Validate gene-list JSON (used for both signature meta and sigreverse input)
# Usage: validate_gene_json <json_path> <disease_key> <up_key> <down_key> <label> [<disease_query>]
validate_gene_json() {
  local json_path="$1"
  local disease_key="$2"
  local up_key="${3:-up_genes}"
  local down_key="${4:-down_genes}"
  local label="${5:-gene json}"
  local disease_query="${6:-}"
  python3 - "${json_path}" "${disease_key}" "${up_key}" "${down_key}" "${label}" "${disease_query}" <<'PY'
import json, re, sys
from pathlib import Path

p = Path(sys.argv[1])
disease_key, up_key, down_key, label = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
disease_query = sys.argv[6] if len(sys.argv) > 6 else ""
if not p.exists():
    print(f"missing file: {p}", file=sys.stderr); raise SystemExit(2)
obj = json.loads(p.read_text(encoding="utf-8"))
for k in ("name", up_key, down_key):
    if k not in obj:
        print(f"missing key: {k}", file=sys.stderr); raise SystemExit(3)
if not isinstance(obj.get(up_key), list) or not isinstance(obj.get(down_key), list):
    print(f"{up_key}/{down_key} must be lists", file=sys.stderr); raise SystemExit(4)
if len(obj[up_key]) == 0 and len(obj[down_key]) == 0:
    print(f"{label} has empty gene lists", file=sys.stderr); raise SystemExit(6)
norm = lambda s: "".join(w.rstrip("s") for w in re.split(r"[^a-z0-9]+", s.lower()) if w)
nn = norm(str(obj.get("name", "")))
# Check disease_key and disease_query (the query name from disease list may differ from key)
accepted = [norm(disease_key)]
if disease_query:
    accepted.append(norm(disease_query))
# Short keys (<=5 chars) are abbreviations (ipf, nash, nafld) — skip substring check
matched = any(
    (not kn or len(kn) <= 5 or kn in nn or nn in kn)
    for kn in accepted
)
if not matched:
    print(f"{label} name mismatch: disease_key={disease_key}, query={disease_query}, name={obj.get('name')}", file=sys.stderr)
    raise SystemExit(5)
PY
}

validate_signature_meta_json() {
  validate_gene_json "$1" "$2" "up_genes" "down_genes" "signature meta" "${3:-}"
}

validate_sigreverse_input_json() {
  validate_gene_json "$1" "$2" "up" "down" "sigreverse input" "${3:-}"
}

resolve_cross_inputs() {
  local disease_key="$1"
  local preferred_source="${2:-}"  # "archs4" or "dsmeta"; empty = auto-detect

  local archs4_meta="${ARCHS4_DIR}/outputs/${disease_key}/signature/disease_signature_meta.json"
  local archs4_sig="${ARCHS4_DIR}/outputs/${disease_key}/signature/sigreverse_input.json"
  local meta_per="${DSMETA_DIR}/outputs/${disease_key}/signature/disease_signature_meta.json"
  local meta_legacy="${DSMETA_DIR}/outputs/signature/disease_signature_meta.json"
  local sig_per="${DSMETA_DIR}/outputs/${disease_key}/signature/sigreverse_input.json"
  local sig_legacy="${DSMETA_DIR}/outputs/signature/sigreverse_input.json"

  CROSS_SIGNATURE_META=""
  CROSS_SIGREVERSE_INPUT=""
  CROSS_SIGNATURE_SOURCE=""

  if [[ "${preferred_source}" == "dsmeta" ]]; then
    if [[ -f "${meta_per}" ]]; then
      CROSS_SIGNATURE_META="${meta_per}"
      CROSS_SIGNATURE_SOURCE="dsmeta"
    elif [[ -f "${meta_legacy}" ]]; then
      CROSS_SIGNATURE_META="${meta_legacy}"
      CROSS_SIGNATURE_SOURCE="dsmeta"
    fi
    if [[ -f "${sig_per}" ]]; then
      CROSS_SIGREVERSE_INPUT="${sig_per}"
    elif [[ -f "${sig_legacy}" ]]; then
      CROSS_SIGREVERSE_INPUT="${sig_legacy}"
    fi
  elif [[ "${preferred_source}" == "archs4" ]]; then
    if [[ -f "${archs4_meta}" ]]; then
      CROSS_SIGNATURE_META="${archs4_meta}"
      CROSS_SIGNATURE_SOURCE="archs4"
    fi
    if [[ -f "${archs4_sig}" ]]; then
      CROSS_SIGREVERSE_INPUT="${archs4_sig}"
    fi
  else
    # Auto-detect: prefer ARCHS4 if available, then dsmeta
    if [[ -f "${archs4_meta}" ]]; then
      CROSS_SIGNATURE_META="${archs4_meta}"
      CROSS_SIGNATURE_SOURCE="archs4"
    elif [[ -f "${meta_per}" ]]; then
      CROSS_SIGNATURE_META="${meta_per}"
      CROSS_SIGNATURE_SOURCE="dsmeta"
    elif [[ -f "${meta_legacy}" ]]; then
      CROSS_SIGNATURE_META="${meta_legacy}"
      CROSS_SIGNATURE_SOURCE="dsmeta"
    fi
    if [[ -f "${archs4_sig}" ]]; then
      CROSS_SIGREVERSE_INPUT="${archs4_sig}"
    elif [[ -f "${sig_per}" ]]; then
      CROSS_SIGREVERSE_INPUT="${sig_per}"
    elif [[ -f "${sig_legacy}" ]]; then
      CROSS_SIGREVERSE_INPUT="${sig_legacy}"
    fi
  fi

  require_file "${CROSS_SIGNATURE_META}" "signature_meta" || return 1
  require_file "${CROSS_SIGREVERSE_INPUT}" "sigreverse_input" || return 1
  return 0
}

ensure_kg_disease_config() {
  local disease_key="$1"
  local disease_query="$2"
  local cfg_path="${KG_DIR}/configs/diseases/${disease_key}.yaml"

  if [[ -f "${cfg_path}" ]]; then
    return 0
  fi

  log "Create missing kg disease config: ${cfg_path}"
  cat > "${cfg_path}" <<EOF_CFG
disease:
  name: "${disease_query}"
  condition: "${disease_query}"

drug_filter:
  include_types:
    - DRUG
    - BIOLOGICAL
  exclude_types:
    - DEVICE
    - PROCEDURE
    - BEHAVIORAL
    - DIETARY_SUPPLEMENT
EOF_CFG
}

ensure_cross_signature_config() {
  # Auto-discover GEO datasets and generate dsmeta config if neither
  # dsmeta nor ARCHS4 config exists.  This makes the Cross route work
  # out-of-the-box without a manual GEO curation step.
  #
  # Flow:
  #   1. auto_discover_geo.py → generate_dsmeta_configs.py → dsmeta config
  #   2. If GEO discovery fails → generate ARCHS4 config as fallback
  #   3. If both fail → return 1 (caller should skip Cross)
  local disease_key="$1"
  local disease_query="$2"
  local efo_ids="${3:-}"

  local archs4_cfg="${ARCHS4_DIR}/configs/${disease_key}.yaml"
  local dsmeta_cfg="${DSMETA_DIR}/configs/${disease_key}.yaml"

  # Already have a config — nothing to do
  if [[ -f "${archs4_cfg}" || -f "${dsmeta_cfg}" ]]; then
    return 0
  fi

  log "[INFO] Cross: no signature config for ${disease_key} — running auto GEO discovery..."

  local geo_out="${ROOT_DIR}/ops/internal/geo_curation"
  local discover_py="${ROOT_DIR}/ops/internal/auto_discover_geo.py"
  local generate_py="${ROOT_DIR}/ops/internal/generate_dsmeta_configs.py"

  local dsmeta_ok=false

  if [[ -f "${discover_py}" ]]; then
    # Step 1: discover GEO datasets
    if run_cmd "Cross: auto-discover GEO (${disease_key})" --timeout "${TIMEOUT_CROSS_AUTO_DISCOVER_GEO}" \
         python3 "${discover_py}" \
           --disease "${disease_query}" \
           --disease-key "${disease_key}" \
           --out-dir "${geo_out}" \
           --write-yaml \
           --top-k 5; then

      local selected_tsv="${geo_out}/${disease_key}/selected.tsv"
      local n_selected=0
      if [[ -f "${selected_tsv}" && -s "${selected_tsv}" ]]; then
        n_selected=$(tail -n +2 "${selected_tsv}" | wc -l | tr -d ' ')
      fi

      if [[ "${n_selected}" -gt 0 ]]; then
        # Step 2: generate dsmeta config from discovered GSEs
        if [[ -f "${generate_py}" ]]; then
          mkdir -p "${DSMETA_DIR}/configs"
          if run_cmd "Cross: generate dsmeta config (${disease_key})" --timeout "${TIMEOUT_CROSS_GENERATE_DSMETA_CONFIG}" \
               python3 "${generate_py}" \
                 --geo-dir "${geo_out}" \
                 --config-dir "${DSMETA_DIR}/configs" \
                 --disease "${disease_key}" \
                 --overwrite; then
            if [[ -f "${dsmeta_cfg}" ]]; then
              log "[INFO] Cross: auto-generated dsmeta config: ${dsmeta_cfg} (${n_selected} GSEs)"
              dsmeta_ok=true
            fi
          fi
        fi

        # Fallback: copy the candidate_config.yaml directly if generate_dsmeta_configs failed
        if [[ "${dsmeta_ok}" == false ]]; then
          local candidate_yaml="${geo_out}/${disease_key}/candidate_config.yaml"
          if [[ -f "${candidate_yaml}" ]]; then
            mkdir -p "${DSMETA_DIR}/configs"
            cp "${candidate_yaml}" "${dsmeta_cfg}"
            log "[INFO] Cross: copied candidate config as dsmeta config: ${dsmeta_cfg}"
            dsmeta_ok=true
          fi
        fi
      else
        log "[WARN] Cross: GEO discovery found 0 datasets for ${disease_key}"
      fi
    else
      log "[WARN] Cross: GEO auto-discovery failed for ${disease_key}"
    fi
  else
    log "[WARN] Cross: auto_discover_geo.py not found, skipping GEO discovery"
  fi

  # If dsmeta succeeded, we're done
  if [[ "${dsmeta_ok}" == true ]]; then
    return 0
  fi

  # ── Fallback: generate ARCHS4 config ──────────────────────────
  # ARCHS4 uses its own pre-indexed H5 database (not GEO), so it
  # can work even when no GEO expression datasets are found.
  # Use ARCHS4's own auto_generate_config.py which has optimized
  # DISEASE_KEYWORD_MAP (e.g. AMI/STEMI for myocardial_infarction).
  log "[INFO] Cross: GEO/dsmeta failed, generating ARCHS4 config as fallback for ${disease_key}..."

  local archs4_gen_py="${ARCHS4_DIR}/scripts/auto_generate_config.py"

  if [[ -z "${efo_ids}" ]]; then
    log "[WARN] Cross: no EFO ID for ${disease_key}, cannot generate ARCHS4 config"
    return 1
  fi

  if [[ ! -f "${archs4_gen_py}" ]]; then
    log "[WARN] Cross: auto_generate_config.py not found at ${archs4_gen_py}"
    return 1
  fi

  if run_cmd "Cross: generate ARCHS4 config (${disease_key})" --timeout "${TIMEOUT_CROSS_GENERATE_ARCHS4_CONFIG}" \
       python3 "${archs4_gen_py}" \
         --disease "${disease_key}" \
         --disease-name "${disease_query}" \
         --efo-id "${efo_ids}" \
         --outdir "${ARCHS4_DIR}/configs"; then
    if [[ -f "${archs4_cfg}" ]]; then
      log "[INFO] Cross: auto-generated ARCHS4 config: ${archs4_cfg}"
      return 0
    fi
  fi

  log "[WARN] Cross: could not generate any signature config for ${disease_key}"
  return 1
}

resolve_path_optional() {
  local input="$1"
  if [[ -z "${input}" ]]; then
    printf ''
    return 0
  fi
  if [[ -f "${input}" ]]; then
    printf '%s' "${input}"
    return 0
  fi
  if [[ -f "${ROOT_DIR}/${input}" ]]; then
    printf '%s' "${ROOT_DIR}/${input}"
    return 0
  fi
  if [[ -f "${KG_DIR}/${input}" ]]; then
    printf '%s' "${KG_DIR}/${input}"
    return 0
  fi
  return 1
}

# Copy KG manifest from per-disease or legacy path
copy_kg_manifest() {
  local disease_key="$1"
  local dest="$2"
  local drug_source="${3:-ctgov}"
  local src="${KG_DIR}/output/${disease_key}/${drug_source}/pipeline_manifest.json"
  local src_legacy="${KG_DIR}/output/pipeline_manifest.json"
  if [[ -f "${src}" ]]; then
    cp "${src}" "${dest}"
  elif [[ -f "${src_legacy}" ]]; then
    cp "${src_legacy}" "${dest}"
  fi
}

run_route_llm_stage() {
  local route_title="$1"         # Origin | Cross
  local route_key="$2"           # origin | cross
  local stage="$3"               # stage1 | stage2
  local bridge_csv="$4"
  local neg_csv="$5"
  local step6_dir="$6"
  local step7_dir="$7"
  local step8_dir="$8"
  local disease_query="${9}"
  local topn="${10}"
  local topk="${11}"
  local min_go="${12}"
  local quality_json="${13}"
  local disease_id="${14:-}"      # Optional: EFO/MONDO IDs for OpenTargets enrichment

  # SKIP_LLM=1 → skip LLM step6-9, write skip markers, return success
  if [[ "${SKIP_LLM:-0}" == "1" ]]; then
    log "[INFO] SKIP_LLM=1: skipping ${route_title} LLM step6-9 (${stage})"
    mkdir -p "${step6_dir}" "${step7_dir}" "${step8_dir}"
    write_topn_quality_skip_json "${quality_json}" "${route_key}" "${topk}" "${min_go}" "llm_skipped" "${stage}"
    return 0
  fi

  # Build optional --disease_id flag for OpenTargets synonym/related-disease enrichment
  local _disease_id_flag=""
  if [[ -n "${disease_id}" ]]; then
    _disease_id_flag="--disease_id ${disease_id}"
  fi
  if ! run_cmd "${route_title}: step6 (${stage})" --timeout "${TIMEOUT_LLM_STEP6}" run_in_dir "${LLM_DIR}" "${LLM_PY}" scripts/step6_evidence_extraction.py --rank_in "${bridge_csv}" --neg "${neg_csv}" --out "${step6_dir}" --target_disease "${disease_query}" --topn "${topn}" --pubmed_retmax "${STEP6_PUBMED_RETMAX}" --pubmed_parse_max "${STEP6_PUBMED_PARSE_MAX}" --max_rerank_docs "${STEP6_MAX_RERANK_DOCS}" --max_evidence_docs "${STEP6_MAX_EVIDENCE_DOCS}" ${_disease_id_flag}; then
    write_topn_quality_skip_json "${quality_json}" "${route_key}" "${topk}" "${min_go}" "${stage}_step6_failed" "${stage}"
    return 1
  fi

  if ! run_cmd "${route_title}: step7 (${stage})" --timeout "${TIMEOUT_LLM_STEP7}" run_in_dir "${LLM_DIR}" "${LLM_PY}" scripts/step7_score_and_gate.py --input "${step6_dir}" --out "${step7_dir}" --strict_contract "${STRICT_CONTRACT}"; then
    write_topn_quality_skip_json "${quality_json}" "${route_key}" "${topk}" "${min_go}" "${stage}_step7_failed" "${stage}"
    return 1
  fi

  if ! run_cmd "${route_title}: step8 (${stage})" --timeout "${TIMEOUT_LLM_STEP8}" run_in_dir "${LLM_DIR}" "${LLM_PY}" scripts/step8_fusion_rank.py --step7_dir "${step7_dir}" --neg "${neg_csv}" --bridge "${bridge_csv}" --outdir "${step8_dir}" --target_disease "${disease_query}" --topk "${topk}" --route "${route_key}" --include_explore 1 --strict_contract "${STRICT_CONTRACT}" --sensitivity_n 1000; then
    write_topn_quality_skip_json "${quality_json}" "${route_key}" "${topk}" "${min_go}" "${stage}_step8_failed" "${stage}"
    return 1
  fi

  local shortlist_csv="${step8_dir}/step8_shortlist_top${topk}.csv"
  local cards_json="${step7_dir}/step7_cards.json"
  evaluate_topn_quality "${cards_json}" "${shortlist_csv}" "${route_key}" "${stage}" "${topk}" "${min_go}" "${quality_json}"
  return 0
}

# Seed stage2's step6 dossiers directory with hard links from stage1 so that
# step6 skips already-processed drugs and only runs LLM on new candidates.
seed_stage2_dossiers() {
  local stage1_step6_dir="$1"
  local stage2_step6_dir="$2"
  local src_dir="${stage1_step6_dir}/dossiers"
  local dst_dir="${stage2_step6_dir}/dossiers"
  if [[ ! -d "${src_dir}" ]]; then
    log "[INFO] seed_stage2_dossiers: stage1 dossiers not found at ${src_dir}, skipping"
    return 0
  fi
  mkdir -p "${dst_dir}"
  local count=0
  for src_file in "${src_dir}"/*.json "${src_dir}"/*.md; do
    [[ -f "${src_file}" ]] || continue
    local fname
    fname="$(basename "${src_file}")"
    local dst_file="${dst_dir}/${fname}"
    if [[ ! -e "${dst_file}" ]]; then
      ln "${src_file}" "${dst_file}" 2>/dev/null || cp "${src_file}" "${dst_file}"
      (( count++ )) || true
    fi
  done
  log "[INFO] seed_stage2_dossiers: linked ${count} stage1 dossier files -> ${dst_dir}"
}

process_disease() {
  local disease_key="$1"
  local disease_query="$2"
  local origin_ids_input="$3"
  local inject_raw="$4"

  local run_date
  run_date="$(date '+%F')"
  local run_id
  run_id="$(date '+%Y%m%d_%H%M%S')_${RANDOM}"

  local run_dir="${RUNS_ROOT}/${disease_key}/${run_id}"
  mkdir -p "${run_dir}/work" "${run_dir}/output" "${run_dir}/logs"

  # [P1-3] Per-step log directory for this run
  CURRENT_STEP_LOG_DIR="${run_dir}/logs"

  # Reset per-disease trackers
  DISEASE_START_TS=$SECONDS
  LAST_STEP_TS=$SECONDS
  CURRENT_STEP=0
  STEP_TIMINGS=""
  COMPLETED_STEPS=""
  local cross_route_start=0 cross_route_elapsed=0
  local origin_route_start=0 origin_route_elapsed=0

  # Dynamic step count based on mode
  case "${RUN_MODE}" in
    dual)        TOTAL_STEPS=8 ;;
    origin_only) TOTAL_STEPS=4 ;;
    cross_only)  TOTAL_STEPS=6 ;;
    *)           TOTAL_STEPS=8 ;;
  esac

  local cross_status="not_run"
  local origin_status="not_run"
  local origin_ids_effective="${origin_ids_input}"

  local inject_path=""
  if ! inject_path="$(resolve_path_optional "${inject_raw}")"; then
    fail_disease "${disease_key}" "${run_id}" "validate_inject" "inject file not found: ${inject_raw}" "${cross_status}" "${origin_status}"
    return 1
  fi

  log "=== Disease start: key=${disease_key}, query=${disease_query}, origin_ids=${origin_ids_input:-N/A}, inject=${inject_path:-N/A}, run_id=${run_id}, mode=${RUN_MODE} ==="
  next_step "${disease_key}" "Screen drugs (CT.gov)"

  if ! ensure_kg_disease_config "${disease_key}" "${disease_query}"; then
    fail_disease "${disease_key}" "${run_id}" "ensure_kg_config" "cannot ensure kg disease config" "${cross_status}" "${origin_status}"
    return 1
  fi

  local screen_out="${run_dir}/work/screen"
  local neg_csv="${screen_out}/poolA_drug_level.csv"
  if ! run_cmd "Screen drugs" run_in_dir "${LLM_DIR}" "${LLM_PY}" scripts/screen_drugs.py --disease "${disease_query}" --max-studies "${SCREEN_MAX_STUDIES}" --outdir "${screen_out}"; then
    fail_disease "${disease_key}" "${run_id}" "screen_drugs" "screen_drugs failed" "${cross_status}" "${origin_status}"
    return 1
  fi

  if ! require_file "${neg_csv}" "drug level csv"; then
    fail_disease "${disease_key}" "${run_id}" "screen_output" "missing drug level csv" "${cross_status}" "${origin_status}"
    return 1
  fi
  record_step_timing "screen_drugs"
  mark_step_done "screen_drugs"

  # [P1-4] Isolate kg_explain output per run to avoid cross-disease contamination
  local kg_output_dir="${run_dir}/work/kg_output"
  mkdir -p "${kg_output_dir}"
  local kg_manifest="${kg_output_dir}/pipeline_manifest.json"

  local CROSS_SIGNATURE_META=""
  local CROSS_SIGREVERSE_INPUT=""
  local CROSS_SIGNATURE_SOURCE=""
  local sig_out_dir=""
  local cross_manifest_path=""
  local origin_manifest_path=""
  local bridge_cross=""
  local bridge_origin=""
  local step6_cross=""
  local step7_cross=""
  local step8_cross=""
  local step6_origin=""
  local step7_origin=""
  local step8_origin=""

  # ----- A) Cross route (optional, RUN_MODE=dual or cross_only) -----
  # [P1-5] Cross failure no longer blocks Origin route (in dual mode)
  # Priority: ARCHS4 config > dsmeta config
  # NEW: Auto-discover GEO + generate dsmeta config if neither exists
  if [[ "${RUN_MODE}" == "dual" || "${RUN_MODE}" == "cross_only" ]]; then
    local archs4_cfg="${ARCHS4_DIR}/configs/${disease_key}.yaml"
    local dsmeta_cfg="${DSMETA_DIR}/configs/${disease_key}.yaml"

    # Auto-discover: if no config exists, try to find GEO datasets and generate one
    if [[ ! -f "${archs4_cfg}" && ! -f "${dsmeta_cfg}" ]]; then
      ensure_cross_signature_config "${disease_key}" "${disease_query}" "${origin_ids_input}" || true
    fi

    if [[ ! -f "${archs4_cfg}" && ! -f "${dsmeta_cfg}" ]]; then
      log "[WARN] Cross: no ARCHS4 or dsmeta config for ${disease_key} (auto-discovery also failed), skipping cross route"
      cross_status="failed"
      if [[ "${RUN_MODE}" == "cross_only" ]]; then
        fail_disease "${disease_key}" "${run_id}" "cross_no_config" "no ARCHS4 or dsmeta config for cross_only mode" "${cross_status}" "${origin_status}"
        return 1
      fi
    else
      # Run cross route in a block; failure sets cross_status but doesn't return
      cross_route_start=$SECONDS
      if run_cross_route "${disease_key}" "${disease_query}" "${run_id}" \
           "${run_dir}" "${dsmeta_cfg}" "${kg_output_dir}" "${kg_manifest}" "${neg_csv}" "${origin_ids_input}"; then
        cross_status="success"
        cross_route_elapsed=$((SECONDS - cross_route_start))
      else
        cross_status="failed"
        cross_route_elapsed=$((SECONDS - cross_route_start))
        if [[ "${RUN_MODE}" == "cross_only" ]]; then
          fail_disease "${disease_key}" "${run_id}" "cross_route" "cross route failed in cross_only mode" "${cross_status}" "${origin_status}"
          return 1
        fi
        log "[WARN] Cross route failed for ${disease_key}, continuing with Origin route"
      fi
    fi
  else
    cross_status="skipped"
    log "[INFO] RUN_MODE=${RUN_MODE}: skip cross route for ${disease_key}"
  fi

  # ----- B) Origin route (skip if cross_only) -----
  if [[ "${RUN_MODE}" == "cross_only" ]]; then
    origin_status="skipped"
    log "[INFO] RUN_MODE=cross_only: skip origin route for ${disease_key}"
  fi

  if [[ "${RUN_MODE}" != "cross_only" ]]; then
  # ----- B) Origin route -----
  origin_route_start=$SECONDS
  next_step "${disease_key}" "Origin: KG ranking (CT.gov mode)"

  if ! run_cmd "Origin: kg ctgov" --timeout "${TIMEOUT_ORIGIN_KG_CTGOV}" run_in_dir "${KG_DIR}" "${KG_PY}" -m src.kg_explain.cli pipeline --disease "${disease_key}" --version v5 --drug-source ctgov; then
    fail_disease "${disease_key}" "${run_id}" "origin_kg_ctgov" "kg ctgov pipeline failed" "${cross_status}" "${origin_status}"
    return 1
  fi

  # Always copy latest manifest from kg_explain output (Origin overwrites Cross manifest)
  copy_kg_manifest "${disease_key}" "${kg_manifest}" "ctgov"

  if ! run_cmd "Origin: manifest gate" kg_manifest_gate "${kg_manifest}" "ctgov"; then
    fail_disease "${disease_key}" "${run_id}" "origin_manifest_gate" "kg ctgov manifest check failed" "${cross_status}" "${origin_status}"
    return 1
  fi

  local origin_manifest_path="${run_dir}/work/pipeline_manifest_origin_ctgov.json"
  cp "${kg_manifest}" "${origin_manifest_path}"

  local kg_output_disease_dir="${KG_DIR}/output/${disease_key}/ctgov"
  local kg_data_disease_dir="${KG_DIR}/data/${disease_key}/ctgov"
  local bridge_origin="${kg_output_disease_dir}/bridge_origin_reassess.csv"
  # NOTE: legacy global bridge path removed — disease-specific bridge is required
  #       to prevent cross-disease contamination (see P1 #2 fix)

  local origin_cmd=("${KG_PY}" scripts/generate_disease_bridge.py)
  origin_cmd+=(--dtpd-rank "${kg_output_disease_dir}/dtpd_rank.csv")
  origin_cmd+=(--rank "${kg_output_disease_dir}/drug_disease_rank.csv")
  origin_cmd+=(--paths "${kg_output_disease_dir}/dtpd_paths.jsonl")
  origin_cmd+=(--chembl "${kg_data_disease_dir}/drug_chembl_map.csv")
  origin_cmd+=(--data-dir "${kg_data_disease_dir}")
  if [[ -n "${origin_ids_input}" ]]; then
    origin_cmd+=(--disease-ids "${origin_ids_input}")
    origin_ids_effective="${origin_ids_input}"
  else
    origin_cmd+=(--disease "${disease_query}")
  fi
  if [[ -n "${inject_path}" ]]; then
    origin_cmd+=(--inject "${inject_path}")
  fi
  origin_cmd+=(--out "${bridge_origin}")

  if ! run_cmd "Origin: generate bridge" run_in_dir "${KG_DIR}" "${origin_cmd[@]}"; then
    fail_disease "${disease_key}" "${run_id}" "origin_bridge" "generate_disease_bridge failed" "${cross_status}" "${origin_status}"
    return 1
  fi
  # Legacy bridge fallback removed: disease-specific bridge is now mandatory.
  # If bridge generation succeeded (line 1445) but output file is missing,
  # require_file below will fail the disease correctly.

  if [[ -z "${origin_ids_input}" ]]; then
    origin_ids_effective="$(derive_matched_ids_from_dtpd "${disease_query}" "${disease_key}" "ctgov")"
  fi

  if ! require_file "${bridge_origin}" "origin bridge"; then
    fail_disease "${disease_key}" "${run_id}" "origin_bridge_output" "missing bridge_origin_reassess.csv" "${cross_status}" "${origin_status}"
    return 1
  fi

  # Merge SigReverse reversal_score into origin bridge CSV (if cross ran and sig output exists)
  local sig_rank_csv_origin="${run_dir}/work/sigreverse_output/drug_reversal_rank.csv"
  if [[ -f "${sig_rank_csv_origin}" ]]; then
    run_cmd "Origin: merge reversal_score into bridge" \
      python3 "${ROOT_DIR}/ops/merge_sig_to_bridge.py" \
        --bridge "${bridge_origin}" --sig-rank "${sig_rank_csv_origin}" || true
  fi

  local step6_origin="${run_dir}/work/llm/step6_origin_reassess"
  local step7_origin="${run_dir}/work/llm/step7_origin_reassess"
  local step8_origin="${run_dir}/work/llm/step8_origin_reassess"

  local llm_audit_dir="${run_dir}/work/llm"
  mkdir -p "${llm_audit_dir}"
  local origin_decision_stage1="${llm_audit_dir}/topn_decision_origin_stage1.json"
  local origin_quality_stage1="${llm_audit_dir}/topn_quality_origin_stage1.json"
  local origin_decision_stage2="${llm_audit_dir}/topn_decision_origin_stage2.json"
  local origin_quality_stage2="${llm_audit_dir}/topn_quality_origin_stage2.json"
  write_topn_decision_skip_json "${origin_decision_stage2}" "origin" "0" "stage1_not_completed"
  write_topn_quality_skip_json "${origin_quality_stage2}" "origin" "${TOPK_ORIGIN}" "${SHORTLIST_MIN_GO_ORIGIN}" "stage1_not_completed" "stage2"

  local origin_topn
  if ! origin_topn="$(resolve_topn "${TOPN_ORIGIN}" "${bridge_origin}" "origin" "stage1" "${TOPK_ORIGIN}" "${TOPN_STAGE1_MIN_ORIGIN}" "${TOPN_STAGE1_MAX_ORIGIN}" "${TOPN_CAP_ORIGIN}" "${TOPN_EXPAND_RATIO}" "${TOPN_STAGE1_MIN_ORIGIN}" "" "${origin_decision_stage1}")"; then
    fail_disease "${disease_key}" "${run_id}" "origin_topn_stage1" "cannot determine origin stage1 topn" "${cross_status}" "${origin_status}"
    return 1
  fi
  log "[INFO] Origin stage1 resolved topn=${origin_topn}"
  record_step_timing "kg_origin"
  mark_step_done "kg_origin"
  next_step "${disease_key}" "Origin: LLM evidence (Step6-9, topn=${origin_topn})"

  if ! run_route_llm_stage "Origin" "origin" "stage1" "${bridge_origin}" "${neg_csv}" "${step6_origin}" "${step7_origin}" "${step8_origin}" "${disease_query}" "${origin_topn}" "${TOPK_ORIGIN}" "${SHORTLIST_MIN_GO_ORIGIN}" "${origin_quality_stage1}" "${origin_ids_input}"; then
    fail_disease "${disease_key}" "${run_id}" "origin_stage1" "origin stage1 pipeline failed" "${cross_status}" "${origin_status}"
    return 1
  fi

  local origin_selected_stage="stage1"
  local origin_selected_topn="${origin_topn}"
  local origin_quality_passed
  origin_quality_passed="$(json_get_field "${origin_quality_stage1}" "quality_passed")"
  local origin_trigger_stage2
  origin_trigger_stage2="$(json_get_field "${origin_quality_stage1}" "trigger_stage2")"
  local origin_stage2_skip_reason
  if [[ "${TOPN_STAGE2_ENABLE}" != "1" ]]; then
    origin_stage2_skip_reason="stage2_disabled"
  elif ! is_auto_topn "${TOPN_ORIGIN}"; then
    origin_stage2_skip_reason="manual_topn_no_stage2"
  elif [[ "${TOPN_MAX_EXPAND_ROUNDS}" -le 0 ]]; then
    origin_stage2_skip_reason="max_expand_rounds_reached"
  elif [[ "${origin_trigger_stage2}" != "1" ]]; then
    origin_stage2_skip_reason="quality_gate_passed_no_expand"
  else
    origin_stage2_skip_reason="pending_stage2_decision"
  fi
  write_topn_decision_skip_json "${origin_decision_stage2}" "origin" "${origin_selected_topn}" "${origin_stage2_skip_reason}"
  write_topn_quality_skip_json "${origin_quality_stage2}" "origin" "${TOPK_ORIGIN}" "${SHORTLIST_MIN_GO_ORIGIN}" "${origin_stage2_skip_reason}"

  if [[ "${TOPN_STAGE2_ENABLE}" == "1" ]] && is_auto_topn "${TOPN_ORIGIN}" && [[ "${TOPN_MAX_EXPAND_ROUNDS}" -gt 0 ]] && [[ "${origin_trigger_stage2}" == "1" ]]; then
    local origin_topn_stage2
    if resolve_topn "${TOPN_ORIGIN}" "${bridge_origin}" "origin" "stage2" "${TOPK_ORIGIN}" "${TOPN_STAGE1_MIN_ORIGIN}" "${TOPN_STAGE1_MAX_ORIGIN}" "${TOPN_CAP_ORIGIN}" "${TOPN_EXPAND_RATIO}" "${TOPN_STAGE1_MIN_ORIGIN}" "${origin_topn}" "${origin_decision_stage2}" >/dev/null; then
      origin_topn_stage2="$(json_get_field "${origin_decision_stage2}" "resolved_topn")"
      local origin_should_expand
      origin_should_expand="$(json_get_field "${origin_decision_stage2}" "should_expand")"
      if [[ "${origin_should_expand}" == "1" ]]; then
        local step6_origin_stage2="${run_dir}/work/llm/step6_origin_reassess_stage2"
        local step7_origin_stage2="${run_dir}/work/llm/step7_origin_reassess_stage2"
        local step8_origin_stage2="${run_dir}/work/llm/step8_origin_reassess_stage2"
        log "[INFO] Origin stage2 expansion: topn ${origin_topn} -> ${origin_topn_stage2}"
        seed_stage2_dossiers "${step6_origin}" "${step6_origin_stage2}"
        if run_route_llm_stage "Origin" "origin" "stage2" "${bridge_origin}" "${neg_csv}" "${step6_origin_stage2}" "${step7_origin_stage2}" "${step8_origin_stage2}" "${disease_query}" "${origin_topn_stage2}" "${TOPK_ORIGIN}" "${SHORTLIST_MIN_GO_ORIGIN}" "${origin_quality_stage2}" "${origin_ids_input}"; then
          origin_selected_stage="stage2"
          origin_selected_topn="${origin_topn_stage2}"
          origin_quality_passed="$(json_get_field "${origin_quality_stage2}" "quality_passed")"
          step7_origin="${step7_origin_stage2}"
          step8_origin="${step8_origin_stage2}"
        else
          log "[WARN] Origin stage2 failed, keep stage1 outputs"
          write_topn_quality_skip_json "${origin_quality_stage2}" "origin" "${TOPK_ORIGIN}" "${SHORTLIST_MIN_GO_ORIGIN}" "stage2_pipeline_failed"
        fi
      else
        log "[INFO] Origin stage2 not needed: no additional topn expansion"
        write_topn_quality_skip_json "${origin_quality_stage2}" "origin" "${TOPK_ORIGIN}" "${SHORTLIST_MIN_GO_ORIGIN}" "stage2_no_additional_topn"
      fi
    else
      log "[WARN] Origin stage2 decision failed, keep stage1 outputs"
      write_topn_decision_skip_json "${origin_decision_stage2}" "origin" "${origin_selected_topn}" "stage2_decision_failed"
      write_topn_quality_skip_json "${origin_quality_stage2}" "origin" "${TOPK_ORIGIN}" "${SHORTLIST_MIN_GO_ORIGIN}" "stage2_decision_failed"
    fi
  fi

  annotate_route_manifest_summary "${step8_origin}" "origin" "${origin_selected_stage}" "${origin_selected_topn}" "${origin_quality_passed:-0}" "${origin_decision_stage1}" "${origin_quality_stage1}" "${origin_decision_stage2}" "${origin_quality_stage2}"

  origin_status="success"
  origin_route_elapsed=$((SECONDS - origin_route_start))
  record_step_timing "llm_origin"
  mark_step_done "llm_origin"

  fi  # end of: if [[ "${RUN_MODE}" != "cross_only" ]]

  # ----- A+B Cross-Validation Comparison -----
  local ab_comparison=""
  if [[ "${cross_status}" == "success" && "${origin_status}" == "success" \
        && -n "${bridge_cross}" && -f "${bridge_cross}" \
        && -n "${bridge_origin}" && -f "${bridge_origin}" ]]; then
    ab_comparison="${run_dir}/output/ab_comparison.csv"
    if run_cmd "A+B comparison" python3 "${ROOT_DIR}/ops/compare_ab_routes.py" \
         --bridge-a "${bridge_cross}" --bridge-b "${bridge_origin}" --out "${ab_comparison}"; then
      log "[OK] A+B cross-validation comparison saved: ${ab_comparison}"
    else
      log "[WARN] A+B comparison failed (non-fatal)"
      ab_comparison=""
    fi
  else
    log "[INFO] A+B comparison skipped (cross=${cross_status}, origin=${origin_status})"
  fi

  next_step "${disease_key}" "Archiving results"

  if ! archive_results \
    "${disease_key}" "${disease_query}" "${run_id}" "${run_date}" "${run_dir}" \
    "${cross_status}" "${origin_status}" "${origin_ids_input}" "${origin_ids_effective}" \
    "${inject_path}" "${CROSS_SIGNATURE_META}" "${CROSS_SIGREVERSE_INPUT}" \
    "${sig_out_dir}" "${cross_manifest_path}" "${origin_manifest_path}" \
    "${bridge_cross}" "${bridge_origin}" \
    "${step7_cross}" "${step8_cross}" \
    "${step7_origin}" "${step8_origin}"; then
    fail_disease "${disease_key}" "${run_id}" "archive" "failed to archive results" "${cross_status}" "${origin_status}"
    return 1
  fi

  # Delete large temporary path files only after origin bridge is archived.
  local evidence_paths_ctgov="${KG_DIR}/output/${disease_key}/ctgov/dtpd_paths.jsonl"
  local evidence_paths_sig="${KG_DIR}/output/${disease_key}/signature/dtpd_paths.jsonl"
  local evidence_paths_tmp_legacy="${KG_DIR}/output/dtpd_paths.jsonl"
  for _ep in "${evidence_paths_ctgov}" "${evidence_paths_sig}" "${evidence_paths_tmp_legacy}"; do
    if [[ -f "${_ep}" ]]; then
      rm -f "${_ep}"
      log "[CLEAN] Deleted temporary file: ${_ep}"
    fi
  done

  record_step_timing "archive"
  mark_step_done "archive"

  # ── Disease summary banner ──
  local total_elapsed=$((SECONDS - DISEASE_START_TS))
  local cross_drugs=0 origin_drugs=0 ab_overlap=0
  [[ -n "${bridge_cross}" && -f "${bridge_cross}" ]] && cross_drugs="$(count_csv_rows "${bridge_cross}")"
  [[ -n "${bridge_origin}" && -f "${bridge_origin}" ]] && origin_drugs="$(count_csv_rows "${bridge_origin}")"
  [[ -n "${ab_comparison}" && -f "${ab_comparison}" ]] && ab_overlap="$(count_csv_rows "${ab_comparison}")"

  log_disease_summary "${disease_key}" "${run_id}" "${total_elapsed}" \
    "${cross_status}" "${origin_status}" \
    "${CROSS_SIGNATURE_SOURCE:-none}" "${cross_drugs}" "${origin_drugs}" \
    "${ab_overlap}" "${cross_route_elapsed}" "${origin_route_elapsed}"
  return 0
}

# [P1-5] Cross route extracted as separate function so failure doesn't block Origin
run_cross_route() {
  local disease_key="$1"
  local disease_query="$2"
  local run_id="$3"
  local run_dir="$4"
  local dsmeta_cfg="$5"
  local kg_output_dir="$6"
  local kg_manifest="$7"
  local neg_csv="$8"
  local efo_ids="${9:-}"

  # --- Signature build: order determined by SIG_PRIORITY ---
  local archs4_cfg="${ARCHS4_DIR}/configs/${disease_key}.yaml"
  local signature_built=0
  local signature_built_by=""   # "archs4" or "dsmeta"
  local a4_status="skip" a4_detail="无配置"
  local ds_status="skip" ds_detail="跳过"
  local sig_start=$SECONDS

  next_step "${disease_key}" "Cross: 基因签名构建 (${SIG_PRIORITY}-first)"

  # -- helper: try ARCHS4 --
  _try_archs4() {
    # Auto-generate ARCHS4 config if it doesn't exist
    if [[ ! -f "${archs4_cfg}" ]]; then
      local archs4_gen_py="${ARCHS4_DIR}/scripts/auto_generate_config.py"
      if [[ -n "${efo_ids}" && -f "${archs4_gen_py}" ]]; then
        log "[INFO] Cross: ARCHS4 config not found, auto-generating for ${disease_key}..."
        if run_cmd "Cross: generate ARCHS4 config (${disease_key})" --timeout "${TIMEOUT_CROSS_GENERATE_ARCHS4_CONFIG:-120}" \
             python3 "${archs4_gen_py}" \
               --disease "${disease_key}" \
               --disease-name "${disease_query}" \
               --efo-id "${efo_ids}" \
               --outdir "${ARCHS4_DIR}/configs"; then
          if [[ -f "${archs4_cfg}" ]]; then
            log "[INFO] Cross: auto-generated ARCHS4 config: ${archs4_cfg}"
          fi
        fi
      fi
      # Still no config after auto-generation attempt
      if [[ ! -f "${archs4_cfg}" ]]; then
        a4_status="skip"; a4_detail="无配置 (auto-generate failed)"; return 1
      fi
    fi
    log "[INFO] Cross: trying ARCHS4 signature..."
    if run_cmd "Cross: archs4 (${disease_key})" --timeout "${TIMEOUT_CROSS_ARCHS4}" run_in_dir "${ARCHS4_DIR}" "${ARCHS4_PY}" run.py --config "${archs4_cfg}"; then
      local archs4_sig_check="${ARCHS4_DIR}/outputs/${disease_key}/signature/sigreverse_input.json"
      if [[ -f "${archs4_sig_check}" ]] && python3 -c "
import json, sys
obj = json.loads(open(sys.argv[1]).read())
if len(obj.get('up',[])) == 0 and len(obj.get('down',[])) == 0:
    sys.exit(1)
" "${archs4_sig_check}" 2>/dev/null; then
        local a4_genes
        a4_genes="$(count_json_genes "${archs4_sig_check}")"
        a4_status="ok"; a4_detail="成功 (${a4_genes} up/down genes)"
        signature_built=1; signature_built_by="archs4"
      else
        a4_status="fail"; a4_detail="空签名 (0 genes)"
      fi
      # Clean ARCHS4 workdir
      local archs4_workdir="${ARCHS4_DIR}/work/${disease_key}"
      if [[ "${DSMETA_CLEANUP}" == "1" && -d "${archs4_workdir}" ]]; then
        local size_mb
        size_mb="$(du -sm "${archs4_workdir}" 2>/dev/null | cut -f1)"
        rm -rf "${archs4_workdir}"
        log "[CLEAN] Deleted archs4 workdir: ${archs4_workdir} (${size_mb:-?} MB freed)"
      fi
    else
      a4_status="fail"; a4_detail="pipeline 错误"
    fi
    [[ "${signature_built}" -eq 1 ]]
  }

  # -- helper: try dsmeta --
  _try_dsmeta() {
    if [[ ! -f "${dsmeta_cfg}" ]]; then
      ds_status="fail"; ds_detail="无配置"; return 1
    fi
    log "[INFO] Cross: trying dsmeta signature..."
    if ! check_disk_space "${DSMETA_DISK_MIN_GB:-8}"; then
      ds_status="fail"; ds_detail="磁盘不足 (需 ≥${DSMETA_DISK_MIN_GB:-8}GB)"; return 1
    fi
    if run_cmd "Cross: dsmeta (${disease_key})" --timeout "${TIMEOUT_CROSS_DSMETA}" run_in_dir "${DSMETA_DIR}" "${DSMETA_PY}" run.py --config "${dsmeta_cfg}"; then
      # P1: Check gene count — if too few, treat as failure and let fallback take over
      local _ds_sig_check="${DSMETA_DIR}/outputs/${disease_key}/signature/sigreverse_input.json"
      if [[ ! -f "${_ds_sig_check}" ]]; then
        _ds_sig_check="${DSMETA_DIR}/outputs/signature/sigreverse_input.json"
      fi
      local _ds_min_genes="${DSMETA_MIN_GENES:-3}"
      if [[ -f "${_ds_sig_check}" ]]; then
        local _ds_gene_counts
        _ds_gene_counts="$(count_json_genes "${_ds_sig_check}")"
        local _ds_n_up="${_ds_gene_counts%%/*}"
        local _ds_n_down="${_ds_gene_counts##*/}"
        local _ds_total=$(( _ds_n_up + _ds_n_down ))
        if [[ "${_ds_total}" -lt "${_ds_min_genes}" ]]; then
          ds_status="fail"; ds_detail="基因数不足 (${_ds_n_up}up+${_ds_n_down}down=${_ds_total} < ${_ds_min_genes})"
          log "[WARN] Cross: dsmeta 签名基因太少 (${_ds_total} < ${_ds_min_genes}), 视为失败"
          cleanup_dsmeta_workdir "${disease_key}"
          return 1
        fi
      else
        ds_status="fail"; ds_detail="无输出文件"
        cleanup_dsmeta_workdir "${disease_key}"
        return 1
      fi
      ds_status="ok"
      signature_built=1; signature_built_by="dsmeta"
      cleanup_dsmeta_workdir "${disease_key}"
    else
      ds_status="fail"; ds_detail="pipeline 错误"
      cleanup_dsmeta_workdir "${disease_key}"
    fi
    [[ "${signature_built}" -eq 1 ]]
  }

  # -- Execute in priority order --
  if [[ "${SIG_PRIORITY}" == "archs4" ]]; then
    _try_archs4 || { log "[INFO] Cross: falling back to dsmeta..."; _try_dsmeta || true; }
    [[ "${signature_built}" -eq 1 && "${a4_status}" == "ok" ]] && ds_detail="跳过 (ARCHS4 已成功)"
  else
    _try_dsmeta || { log "[INFO] Cross: falling back to ARCHS4..."; _try_archs4 || true; }
    [[ "${signature_built}" -eq 1 && "${ds_status}" == "ok" ]] && a4_detail="跳过 (dsmeta 已成功)"
  fi

  # -- Resolve which output files to use --
  if [[ "${signature_built}" -eq 0 ]]; then
    # ── Agent fallback: use LLM to discover GEO datasets ──
    local agent_script="${ROOT_DIR}/ops/agents/geo_discover_agent.py"
    local agent_provider="${GEO_AGENT_PROVIDER:-}"
    local agent_model="${GEO_AGENT_MODEL:-}"
    local dsmeta_cfg="${DSMETA_DIR}/configs/${disease_key}.yaml"
    local efo_id_arg="${3:-}"  # origin_ids_input often contains EFO ID

    if [[ -n "${agent_provider}" && -f "${agent_script}" ]]; then
      log "[INFO] Cross: ARCHS4 + dsmeta both failed → trying GEO Discovery Agent..."

      local agent_cmd="python3 ${agent_script} --disease ${disease_key} --query '${disease_query}' --provider ${agent_provider}"
      [[ -n "${agent_model}" ]] && agent_cmd+=" --model ${agent_model}"
      agent_cmd+=" --write-yaml ${DSMETA_DIR}/configs"
      agent_cmd+=" --outdir ${ROOT_DIR}/geo_agent_results"
      [[ -n "${efo_id_arg}" ]] && agent_cmd+=" --efo-id ${efo_id_arg}"

      if eval "${agent_cmd}" > /dev/null 2>&1 && [[ -f "${dsmeta_cfg}" ]]; then
        log "[INFO] Cross: Agent generated dsmeta config → retrying dsmeta pipeline..."
        _try_dsmeta || true
        if [[ "${signature_built}" -eq 1 ]]; then
          ds_detail="agent-discovered GEO datasets"
          log "[OK] Cross: Agent fallback succeeded!"
        fi
      else
        log "[WARN] Cross: Agent found no usable GEO datasets for ${disease_key}"
      fi
    fi
  fi

  if [[ "${signature_built}" -eq 0 ]]; then
    # Determine display order for the banner
    if [[ "${SIG_PRIORITY}" == "archs4" ]]; then
      log_signature_decision "ARCHS4" "${a4_status}" "${a4_detail}" "dsmeta" "${ds_status}" "${ds_detail}" "none"
    else
      log_signature_decision "dsmeta" "${ds_status}" "${ds_detail}" "ARCHS4" "${a4_status}" "${a4_detail}" "none"
    fi
    log "[ERROR] Cross: no signature source available — cannot proceed"
    return 1
  fi

  if ! resolve_cross_inputs "${disease_key}" "${signature_built_by}"; then
    if [[ "${SIG_PRIORITY}" == "archs4" ]]; then
      log_signature_decision "ARCHS4" "${a4_status}" "${a4_detail}" "dsmeta" "${ds_status}" "${ds_detail}" "none"
    else
      log_signature_decision "dsmeta" "${ds_status}" "${ds_detail}" "ARCHS4" "${a4_status}" "${a4_detail}" "none"
    fi
    log "[ERROR] Cross: cannot resolve cross input files"
    return 1
  fi

  # Count genes for the winning source's detail
  if [[ "${signature_built_by}" == "dsmeta" && "${ds_status}" == "ok" ]]; then
    local ds_genes
    ds_genes="$(count_json_genes "${CROSS_SIGREVERSE_INPUT}")"
    ds_detail="成功 (${ds_genes} up/down genes)"
  fi

  # Emit the decision banner (primary first, secondary second)
  local chosen_source="${CROSS_SIGNATURE_SOURCE:-none}"
  if [[ "${SIG_PRIORITY}" == "archs4" ]]; then
    log_signature_decision "ARCHS4" "${a4_status}" "${a4_detail}" "dsmeta" "${ds_status}" "${ds_detail}" "${chosen_source}"
  else
    log_signature_decision "dsmeta" "${ds_status}" "${ds_detail}" "ARCHS4" "${a4_status}" "${a4_detail}" "${chosen_source}"
  fi

  record_step_timing "signature_build"
  mark_step_done "signature_build"

  if ! run_cmd "Cross: validate signature_meta" validate_signature_meta_json "${CROSS_SIGNATURE_META}" "${disease_key}" "${disease_query}"; then
    log "[ERROR] Cross: invalid signature meta json"
    return 1
  fi

  if ! run_cmd "Cross: validate sigreverse_input" validate_sigreverse_input_json "${CROSS_SIGREVERSE_INPUT}" "${disease_key}" "${disease_query}"; then
    log "[ERROR] Cross: invalid sigreverse input json"
    return 1
  fi

  # ── Signature quality gate: total < 30 or min(up,down) < 10 → skip Cross ──
  local _sig_genes _sig_up _sig_down _sig_min_dir
  read -r _sig_up _sig_down < <(python3 -c "
import json, sys
obj = json.load(open(sys.argv[1]))
print(len(obj.get('up_genes', [])), len(obj.get('down_genes', [])))
" "${CROSS_SIGNATURE_META}" 2>/dev/null || echo "0 0")
  _sig_genes=$(( _sig_up + _sig_down ))
  _sig_min_dir=$(( _sig_up < _sig_down ? _sig_up : _sig_down ))

  if [[ "${_sig_min_dir}" -lt 10 ]]; then
    log "[WARN] Cross: 签名方向不平衡 (${_sig_up} up / ${_sig_down} down, min=${_sig_min_dir} < 10), Cross 路线可信度过低"
    log "[WARN] Cross: 跳过 Cross 路线, 建议主线走 Origin (--mode dual)"
    return 1
  elif [[ "${_sig_genes}" -lt 30 ]]; then
    # v2: DON'T abort — run with tightened parameters instead.
    # Rationale: a signature with 20-29 genes may still contain genuine disease
    # biology that sigreverse can leverage. Aborting the entire cross route
    # throws away potential discoveries. Instead, tighten the KG drug limit
    # and log a warning for the researcher to review.
    log "[WARN] Cross: 签名基因数偏少 (${_sig_genes} < 30), 自动收紧参数运行 (不中断)"
  elif [[ "${_sig_genes}" -lt 100 ]]; then
    log "[INFO] Cross: 签名基因中等 (${_sig_genes}), 自动收紧 KG 药物上限"
  else
    log "[INFO] Cross: 签名基因充足 (${_sig_genes} ≥ 100)"
  fi

  next_step "${disease_key}" "Cross: SigReverse (LINCS L1000)"

  sig_out_dir="${run_dir}/work/sigreverse_output"
  mkdir -p "${sig_out_dir}"
  if ! run_cmd "Cross: sigreverse" --timeout "${TIMEOUT_CROSS_SIGREVERSE}" run_in_dir "${SIG_DIR}" "${SIG_PY}" scripts/run.py --config configs/default.yaml --in "${CROSS_SIGREVERSE_INPUT}" --out "${sig_out_dir}"; then
    log "[ERROR] Cross: sigreverse failed"
    return 1
  fi

  if ! require_file "${sig_out_dir}/drug_reversal_rank.csv" "sigreverse rank"; then
    log "[ERROR] Cross: missing sigreverse output"
    return 1
  fi
  record_step_timing "sigreverse"
  mark_step_done "sigreverse"

  next_step "${disease_key}" "Cross: KG ranking (signature mode)"

  local _sigreverse_rank_csv="${sig_out_dir}/drug_reversal_rank.csv"
  local -a _kg_sig_cmd=("${KG_PY}" -m src.kg_explain.cli pipeline --disease "${disease_key}" --version v5 --drug-source signature --signature-path "${CROSS_SIGNATURE_META}" --max-drugs "${KG_MAX_DRUGS_SIGNATURE:-200}")
  if [[ -f "${_sigreverse_rank_csv}" ]]; then
    _kg_sig_cmd+=(--sigreverse-rank "${_sigreverse_rank_csv}")
  fi
  if ! run_cmd "Cross: kg signature" --timeout "${TIMEOUT_CROSS_KG_SIGNATURE}" run_in_dir "${KG_DIR}" "${_kg_sig_cmd[@]}"; then
    log "[ERROR] Cross: kg signature pipeline failed"
    return 1
  fi

  # Always copy latest manifest from kg_explain output
  copy_kg_manifest "${disease_key}" "${kg_manifest}" "signature"

  if ! run_cmd "Cross: manifest gate" kg_manifest_gate "${kg_manifest}" "signature"; then
    log "[ERROR] Cross: kg signature manifest check failed"
    return 1
  fi

  cross_manifest_path="${run_dir}/work/pipeline_manifest_cross_signature.json"
  cp "${kg_manifest}" "${cross_manifest_path}"

  bridge_cross="${KG_DIR}/output/${disease_key}/signature/bridge_repurpose_cross.csv"
  # Legacy bridge fallback removed: disease-specific bridge is mandatory
  # to prevent cross-disease contamination (see P1 #2 fix)
  if ! require_file "${bridge_cross}" "cross bridge"; then
    log "[ERROR] Cross: missing bridge_repurpose_cross.csv"
    return 1
  fi

  # Merge SigReverse reversal_score into cross bridge CSV
  local sig_rank_csv="${sig_out_dir}/drug_reversal_rank.csv"
  if [[ -f "${sig_rank_csv}" ]]; then
    run_cmd "Cross: merge reversal_score into bridge" \
      python3 "${ROOT_DIR}/ops/merge_sig_to_bridge.py" \
        --bridge "${bridge_cross}" --sig-rank "${sig_rank_csv}" || true
  fi


  step6_cross="${run_dir}/work/llm/step6_repurpose_cross"
  step7_cross="${run_dir}/work/llm/step7_repurpose_cross"
  step8_cross="${run_dir}/work/llm/step8_repurpose_cross"

  local llm_audit_dir="${run_dir}/work/llm"
  mkdir -p "${llm_audit_dir}"
  local cross_decision_stage1="${llm_audit_dir}/topn_decision_cross_stage1.json"
  local cross_quality_stage1="${llm_audit_dir}/topn_quality_cross_stage1.json"
  local cross_decision_stage2="${llm_audit_dir}/topn_decision_cross_stage2.json"
  local cross_quality_stage2="${llm_audit_dir}/topn_quality_cross_stage2.json"
  write_topn_decision_skip_json "${cross_decision_stage2}" "cross" "0" "stage1_not_completed"
  write_topn_quality_skip_json "${cross_quality_stage2}" "cross" "${TOPK_CROSS}" "${SHORTLIST_MIN_GO_CROSS}" "stage1_not_completed" "stage2"

  local cross_topn
  if ! cross_topn="$(resolve_topn "${TOPN_CROSS}" "${bridge_cross}" "cross" "stage1" "${TOPK_CROSS}" "${TOPN_STAGE1_MIN_CROSS}" "${TOPN_STAGE1_MAX_CROSS}" "${TOPN_CAP_CROSS}" "${TOPN_EXPAND_RATIO}" "${TOPN_STAGE1_MIN_CROSS}" "" "${cross_decision_stage1}")"; then
    log "[ERROR] Cross: cannot determine stage1 topn"
    return 1
  fi
  log "[INFO] Cross stage1 resolved topn=${cross_topn}"
  record_step_timing "kg_cross"
  mark_step_done "kg_cross"
  next_step "${disease_key}" "Cross: LLM evidence (Step6-9, topn=${cross_topn})"

  if ! run_route_llm_stage "Cross" "cross" "stage1" "${bridge_cross}" "${neg_csv}" "${step6_cross}" "${step7_cross}" "${step8_cross}" "${disease_query}" "${cross_topn}" "${TOPK_CROSS}" "${SHORTLIST_MIN_GO_CROSS}" "${cross_quality_stage1}" "${origin_ids_input}"; then
    log "[ERROR] Cross: stage1 pipeline failed"
    return 1
  fi

  local cross_selected_stage="stage1"
  local cross_selected_topn="${cross_topn}"
  local cross_quality_passed
  cross_quality_passed="$(json_get_field "${cross_quality_stage1}" "quality_passed")"
  local cross_trigger_stage2
  cross_trigger_stage2="$(json_get_field "${cross_quality_stage1}" "trigger_stage2")"
  local cross_stage2_skip_reason
  if [[ "${TOPN_STAGE2_ENABLE}" != "1" ]]; then
    cross_stage2_skip_reason="stage2_disabled"
  elif ! is_auto_topn "${TOPN_CROSS}"; then
    cross_stage2_skip_reason="manual_topn_no_stage2"
  elif [[ "${TOPN_MAX_EXPAND_ROUNDS}" -le 0 ]]; then
    cross_stage2_skip_reason="max_expand_rounds_reached"
  elif [[ "${cross_trigger_stage2}" != "1" ]]; then
    cross_stage2_skip_reason="quality_gate_passed_no_expand"
  else
    cross_stage2_skip_reason="pending_stage2_decision"
  fi
  write_topn_decision_skip_json "${cross_decision_stage2}" "cross" "${cross_selected_topn}" "${cross_stage2_skip_reason}"
  write_topn_quality_skip_json "${cross_quality_stage2}" "cross" "${TOPK_CROSS}" "${SHORTLIST_MIN_GO_CROSS}" "${cross_stage2_skip_reason}"

  if [[ "${TOPN_STAGE2_ENABLE}" == "1" ]] && is_auto_topn "${TOPN_CROSS}" && [[ "${TOPN_MAX_EXPAND_ROUNDS}" -gt 0 ]] && [[ "${cross_trigger_stage2}" == "1" ]]; then
    local cross_topn_stage2
    if resolve_topn "${TOPN_CROSS}" "${bridge_cross}" "cross" "stage2" "${TOPK_CROSS}" "${TOPN_STAGE1_MIN_CROSS}" "${TOPN_STAGE1_MAX_CROSS}" "${TOPN_CAP_CROSS}" "${TOPN_EXPAND_RATIO}" "${TOPN_STAGE1_MIN_CROSS}" "${cross_topn}" "${cross_decision_stage2}" >/dev/null; then
      cross_topn_stage2="$(json_get_field "${cross_decision_stage2}" "resolved_topn")"
      local cross_should_expand
      cross_should_expand="$(json_get_field "${cross_decision_stage2}" "should_expand")"
      if [[ "${cross_should_expand}" == "1" ]]; then
        local step6_cross_stage2="${run_dir}/work/llm/step6_repurpose_cross_stage2"
        local step7_cross_stage2="${run_dir}/work/llm/step7_repurpose_cross_stage2"
        local step8_cross_stage2="${run_dir}/work/llm/step8_repurpose_cross_stage2"
        log "[INFO] Cross stage2 expansion: topn ${cross_topn} -> ${cross_topn_stage2}"
        seed_stage2_dossiers "${step6_cross}" "${step6_cross_stage2}"
        if run_route_llm_stage "Cross" "cross" "stage2" "${bridge_cross}" "${neg_csv}" "${step6_cross_stage2}" "${step7_cross_stage2}" "${step8_cross_stage2}" "${disease_query}" "${cross_topn_stage2}" "${TOPK_CROSS}" "${SHORTLIST_MIN_GO_CROSS}" "${cross_quality_stage2}" "${origin_ids_input}"; then
          cross_selected_stage="stage2"
          cross_selected_topn="${cross_topn_stage2}"
          cross_quality_passed="$(json_get_field "${cross_quality_stage2}" "quality_passed")"
          step7_cross="${step7_cross_stage2}"
          step8_cross="${step8_cross_stage2}"
        else
          log "[WARN] Cross stage2 failed, keep stage1 outputs"
          write_topn_quality_skip_json "${cross_quality_stage2}" "cross" "${TOPK_CROSS}" "${SHORTLIST_MIN_GO_CROSS}" "stage2_pipeline_failed"
        fi
      else
        log "[INFO] Cross stage2 not needed: no additional topn expansion"
        write_topn_quality_skip_json "${cross_quality_stage2}" "cross" "${TOPK_CROSS}" "${SHORTLIST_MIN_GO_CROSS}" "stage2_no_additional_topn"
      fi
    else
      log "[WARN] Cross stage2 decision failed, keep stage1 outputs"
      write_topn_decision_skip_json "${cross_decision_stage2}" "cross" "${cross_selected_topn}" "stage2_decision_failed"
      write_topn_quality_skip_json "${cross_quality_stage2}" "cross" "${TOPK_CROSS}" "${SHORTLIST_MIN_GO_CROSS}" "stage2_decision_failed"
    fi
  fi

  annotate_route_manifest_summary "${step8_cross}" "cross" "${cross_selected_stage}" "${cross_selected_topn}" "${cross_quality_passed:-0}" "${cross_decision_stage1}" "${cross_quality_stage1}" "${cross_decision_stage2}" "${cross_quality_stage2}"

  record_step_timing "llm_cross"
  mark_step_done "llm_cross"

  return 0
}
