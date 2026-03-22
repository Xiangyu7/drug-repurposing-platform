#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# runner_ops.sh — Operations (cleanup, health, archive, lock) for the Runner
# ═══════════════════════════════════════════════════════════════════
# Sourced by runner.sh. Do not execute directly.

[[ -n "${_RUNNER_OPS_LOADED:-}" ]] && return 0; _RUNNER_OPS_LOADED=1

acquire_lock() {
  if [[ -f "${LOCK_FILE}" ]]; then
    local old_pid
    old_pid="$(cat "${LOCK_FILE}" 2>/dev/null || true)"
    if [[ -n "${old_pid}" ]] && kill -0 "${old_pid}" 2>/dev/null; then
      log "[ERROR] Another runner is active (pid=${old_pid}). Exiting."
      exit 1
    fi
    log "[WARN] Removing stale lock file: ${LOCK_FILE}"
    rm -f "${LOCK_FILE}"
  fi
  echo "$$" > "${LOCK_FILE}"
}

release_lock() {
  rm -f "${LOCK_FILE}"
}

cleanup_old_runs() {
  local root="$1"
  if [[ ! -d "${root}" ]]; then
    return 0
  fi
  find "${root}" -mindepth 2 -maxdepth 2 -type d -mtime +"${RETENTION_DAYS}" -exec rm -rf {} + 2>/dev/null || true
  find "${root}" -mindepth 1 -maxdepth 1 -type d -empty -delete 2>/dev/null || true
}

# [P2-9] Log rotation
cleanup_old_logs() {
  find "${LOG_DIR}" -name "runner_*.log" -mtime +"${LOG_RETENTION_DAYS}" -delete 2>/dev/null || true
}

# Clean kg_explain HTTP cache (default: files older than CACHE_RETENTION_DAYS)
cleanup_kg_cache() {
  local cache_root="${KG_DIR}/cache/http_json"
  if [[ ! -d "${cache_root}" ]]; then
    return 0
  fi
  local count
  count="$(find "${cache_root}" -name "*.json" -mtime +"${CACHE_RETENTION_DAYS}" 2>/dev/null | wc -l | tr -d ' ')"
  if [[ "${count}" -gt 0 ]]; then
    find "${cache_root}" -name "*.json" -mtime +"${CACHE_RETENTION_DAYS}" -delete 2>/dev/null || true
    log "[CLEAN] Deleted ${count} expired cache files (>${CACHE_RETENTION_DAYS} days) from ${cache_root}"
  fi
}

# Clean dsmeta workdir after each disease run (frees 100-500 MB per disease)
cleanup_dsmeta_workdir() {
  local disease_key="$1"
  if [[ "${DSMETA_CLEANUP}" != "1" ]]; then
    return 0
  fi
  local dsmeta_workdir="${DSMETA_DIR}/work/${disease_key}"
  if [[ -d "${dsmeta_workdir}" ]]; then
    local size_mb
    size_mb="$(du -sm "${dsmeta_workdir}" 2>/dev/null | cut -f1)"
    rm -rf "${dsmeta_workdir}"
    log "[CLEAN] Deleted dsmeta workdir: ${dsmeta_workdir} (${size_mb:-?} MB freed)"
  fi
  # Also clean GPL annotation cache if >100MB total
  local gpl_cache="${DSMETA_DIR}/data/cache/gpl_annotations"
  if [[ -d "${gpl_cache}" ]]; then
    local gpl_size
    gpl_size="$(du -sm "${gpl_cache}" 2>/dev/null | cut -f1)"
    if [[ "${gpl_size:-0}" -gt 100 ]]; then
      rm -rf "${gpl_cache}"
      log "[CLEAN] Deleted GPL annotation cache: ${gpl_cache} (${gpl_size} MB freed)"
    fi
  fi
}

cleanup_state_files() {
  # Clean up env_check_*.json, env_repair_*.json, env_resolved_*.env older than RETENTION_DAYS
  local count=0
  while IFS= read -r -d '' f; do
    rm -f "${f}" && count=$((count + 1))
  done < <(find "${STATE_ROOT}" -maxdepth 1 \( -name "env_check_*.json" -o -name "env_repair_*.json" -o -name "env_resolved_*.env" \) -mtime +"${RETENTION_DAYS}" -print0 2>/dev/null)
  if [[ "${count}" -gt 0 ]]; then
    log "[CLEANUP] Removed ${count} stale state files from ${STATE_ROOT}"
  fi
}

cleanup_retention() {
  cleanup_old_runs "${RUNS_ROOT}"
  cleanup_old_runs "${QUARANTINE_ROOT}"
  cleanup_old_logs
  cleanup_kg_cache
  cleanup_state_files
}

check_disk_space() {
  local min_gb="$1"
  local avail_kb
  avail_kb="$(df -k "${ROOT_DIR}" | awk 'NR==2 {print $4}')"
  local avail_gb=$(( avail_kb / 1024 / 1024 ))
  if [[ "${avail_gb}" -lt "${min_gb}" ]]; then
    log "[CRITICAL] Disk space low: ${avail_gb}GB free < ${min_gb}GB minimum"
    return 1
  fi
  log "[OK] Disk space: ${avail_gb}GB free"
  return 0
}

check_api_health() {
  # Check critical external APIs. Returns 0 if at least one core API is reachable.
  local ok=0
  local total=0

  # CT.gov (core — used by both routes)
  total=$((total + 1))
  if curl -sf --max-time 10 "https://clinicaltrials.gov/api/v2/studies?pageSize=1" >/dev/null 2>&1; then
    ok=$((ok + 1))
  else
    log "[WARN] API unreachable: CT.gov"
  fi

  # ChEMBL (core — kg_explain)
  total=$((total + 1))
  if curl -sf --max-time 10 "https://www.ebi.ac.uk/chembl/api/data/status.json" >/dev/null 2>&1; then
    ok=$((ok + 1))
  else
    log "[WARN] API unreachable: ChEMBL"
  fi

  # PubMed (core — LLM+RAG step6)
  total=$((total + 1))
  if curl -sf --max-time 10 "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi" >/dev/null 2>&1; then
    ok=$((ok + 1))
  else
    log "[WARN] API unreachable: PubMed"
  fi

  log "[INFO] API health: ${ok}/${total} reachable"
  if [[ "${ok}" -eq 0 ]]; then
    log "[ERROR] All APIs unreachable — network down or maintenance window"
    return 1
  fi
  return 0
}

check_ollama_health() {
  # Check if Ollama is running and has the required model
  local ollama_host="${OLLAMA_HOST:-http://localhost:11434}"
  if ! curl -sf --max-time 5 "${ollama_host}/api/tags" >/dev/null 2>&1; then
    log "[WARN] Ollama not reachable at ${ollama_host}"
    return 1
  fi
  log "[OK] Ollama reachable at ${ollama_host}"
  return 0
}

write_failure_record() {
  local disease_key="$1"
  local run_id="$2"
  local phase="$3"
  local message="$4"
  local cross_status="$5"
  local origin_status="$6"

  local qdir="${QUARANTINE_ROOT}/${disease_key}/${run_id}"
  mkdir -p "${qdir}"
  FAILURE_DIR="${qdir}"

  local elapsed=$((SECONDS - DISEASE_START_TS))

  PHASE="${phase}" \
  MESSAGE="${message}" \
  CROSS_STATUS="${cross_status}" \
  ORIGIN_STATUS="${origin_status}" \
  RUN_ID="${run_id}" \
  DISEASE_KEY="${disease_key}" \
  RUN_LOG_PATH="${RUN_LOG}" \
  ELAPSED_SECONDS="${elapsed}" \
  SIGNATURE_SOURCE="${CROSS_SIGNATURE_SOURCE:-none}" \
  COMPLETED_STEPS_JSON="$(completed_steps_to_json_array)" \
  python3 - <<'PY' > "${qdir}/FAILURE.json"
import json
import os
from datetime import datetime, timezone

payload = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "disease_key": os.environ.get("DISEASE_KEY", ""),
    "run_id": os.environ.get("RUN_ID", ""),
    "failed_phase": os.environ.get("PHASE", ""),
    "message": os.environ.get("MESSAGE", ""),
    "cross_status": os.environ.get("CROSS_STATUS", ""),
    "origin_status": os.environ.get("ORIGIN_STATUS", ""),
    "runner_log": os.environ.get("RUN_LOG_PATH", ""),
    "elapsed_seconds": int(os.environ.get("ELAPSED_SECONDS", "0")),
    "signature_source": os.environ.get("SIGNATURE_SOURCE", "none"),
    "completed_steps": json.loads(os.environ.get("COMPLETED_STEPS_JSON", "[]")),
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY
}

fail_disease() {
  local disease_key="$1"
  local run_id="$2"
  local phase="$3"
  local message="$4"
  local cross_status="$5"
  local origin_status="$6"

  write_failure_record "${disease_key}" "${run_id}" "${phase}" "${message}" "${cross_status}" "${origin_status}"
  log "[FAIL] disease=${disease_key} phase=${phase} message=${message} quarantine=${FAILURE_DIR}"
  return 1
}

archive_results() {
  local disease_key="$1"
  local disease_query="$2"
  local run_id="$3"
  local run_date="$4"
  local run_dir="$5"
  local cross_status="$6"
  local origin_status="$7"
  local origin_ids_input="$8"
  local origin_ids_effective="$9"
  local inject_path="${10}"
  local cross_signature_meta="${11}"
  local cross_sigreverse_input="${12}"
  local sig_out_dir="${13}"
  local cross_manifest_path="${14}"
  local origin_manifest_path="${15}"
  local bridge_cross_path="${16}"
  local bridge_origin_path="${17}"
  local step7_cross="${18}"
  local step8_cross="${19}"
  local step7_origin="${20}"
  local step8_origin="${21}"

  local output_dir="${run_dir}/output"
  mkdir -p "${output_dir}/cross/step7" "${output_dir}/cross/step8" \
           "${output_dir}/origin/step7" "${output_dir}/origin/step8" \
           "${output_dir}/kg" "${output_dir}/sigreverse"

  if [[ "${cross_status}" == "success" ]]; then
    cp -R "${step7_cross}/." "${output_dir}/cross/step7/"
    cp -R "${step8_cross}/." "${output_dir}/cross/step8/"
    cp "${bridge_cross_path}" "${output_dir}/cross/bridge_repurpose_cross.csv"
    cp "${cross_manifest_path}" "${output_dir}/kg/pipeline_manifest_cross_signature.json"

    local sig_rank="${sig_out_dir}/drug_reversal_rank.csv"
    if [[ -f "${sig_rank}" ]]; then
      cp "${sig_rank}" "${output_dir}/sigreverse/drug_reversal_rank.csv"
    fi
  fi

  if [[ "${origin_status}" == "success" ]]; then
    cp -R "${step7_origin}/." "${output_dir}/origin/step7/"
    cp -R "${step8_origin}/." "${output_dir}/origin/step8/"
    cp "${bridge_origin_path}" "${output_dir}/origin/bridge_origin_reassess.csv"
    cp "${origin_manifest_path}" "${output_dir}/kg/pipeline_manifest_origin_ctgov.json"
  fi

  # Compute counts for enhanced summary
  local cross_drug_cnt=0 origin_drug_cnt=0 ab_overlap_cnt=0
  local sig_genes_up=0 sig_genes_down=0
  if [[ -n "${bridge_cross_path}" && -f "${bridge_cross_path}" ]]; then
    cross_drug_cnt="$(count_csv_rows "${bridge_cross_path}")"
  fi
  if [[ -n "${bridge_origin_path}" && -f "${bridge_origin_path}" ]]; then
    origin_drug_cnt="$(count_csv_rows "${bridge_origin_path}")"
  fi
  local ab_csv="${output_dir}/ab_comparison.csv"
  if [[ -f "${ab_csv}" ]]; then
    ab_overlap_cnt="$(count_csv_rows "${ab_csv}")"
  fi
  if [[ -n "${cross_sigreverse_input}" && -f "${cross_sigreverse_input}" ]]; then
    local gene_counts
    gene_counts="$(count_json_genes "${cross_sigreverse_input}")"
    sig_genes_up="${gene_counts%%/*}"
    sig_genes_down="${gene_counts##*/}"
  fi

  local elapsed=$((SECONDS - DISEASE_START_TS))

  DISEASE_KEY="${disease_key}" \
  DISEASE_QUERY="${disease_query}" \
  RUN_ID="${run_id}" \
  RUN_DATE="${run_date}" \
  CROSS_STATUS="${cross_status}" \
  ORIGIN_STATUS="${origin_status}" \
  ORIGIN_IDS_INPUT="${origin_ids_input}" \
  ORIGIN_IDS_EFFECTIVE="${origin_ids_effective}" \
  INJECT_PATH="${inject_path}" \
  CROSS_SIGNATURE_META="${cross_signature_meta}" \
  CROSS_SIGREVERSE_INPUT="${cross_sigreverse_input}" \
  RUN_MODE="${RUN_MODE}" \
  RUN_DIR="${run_dir}" \
  SIGNATURE_SOURCE="${CROSS_SIGNATURE_SOURCE:-none}" \
  SIG_GENES_UP="${sig_genes_up}" \
  SIG_GENES_DOWN="${sig_genes_down}" \
  CROSS_DRUG_COUNT="${cross_drug_cnt}" \
  ORIGIN_DRUG_COUNT="${origin_drug_cnt}" \
  AB_OVERLAP_COUNT="${ab_overlap_cnt}" \
  ELAPSED_SECONDS="${elapsed}" \
  STEP_TIMINGS_JSON="$(step_timings_to_json)" \
  python3 - <<'PY' > "${run_dir}/run_summary.json"
import json
import os
from datetime import datetime, timezone

payload = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "disease_key": os.environ.get("DISEASE_KEY", ""),
    "disease_query": os.environ.get("DISEASE_QUERY", ""),
    "run_id": os.environ.get("RUN_ID", ""),
    "run_date": os.environ.get("RUN_DATE", ""),
    "cross_status": os.environ.get("CROSS_STATUS", ""),
    "origin_status": os.environ.get("ORIGIN_STATUS", ""),
    "origin_disease_ids_input": os.environ.get("ORIGIN_IDS_INPUT", ""),
    "origin_disease_ids_effective": os.environ.get("ORIGIN_IDS_EFFECTIVE", ""),
    "inject_yaml": os.environ.get("INJECT_PATH", ""),
    "cross_signature_meta": os.environ.get("CROSS_SIGNATURE_META", ""),
    "cross_sigreverse_input": os.environ.get("CROSS_SIGREVERSE_INPUT", ""),
    "run_mode": os.environ.get("RUN_MODE", ""),
    "run_dir": os.environ.get("RUN_DIR", ""),
    "signature_source": os.environ.get("SIGNATURE_SOURCE", "none"),
    "signature_genes_up": int(os.environ.get("SIG_GENES_UP", "0")),
    "signature_genes_down": int(os.environ.get("SIG_GENES_DOWN", "0")),
    "cross_drug_count": int(os.environ.get("CROSS_DRUG_COUNT", "0")),
    "origin_drug_count": int(os.environ.get("ORIGIN_DRUG_COUNT", "0")),
    "ab_overlap_count": int(os.environ.get("AB_OVERLAP_COUNT", "0")),
    "elapsed_seconds": int(os.environ.get("ELAPSED_SECONDS", "0")),
    "step_timings": json.loads(os.environ.get("STEP_TIMINGS_JSON", "{}")),
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY
}

collect_batch_results() {
  local disease_list_file="$1"

  if [[ -z "${disease_list_file}" || ! -f "${disease_list_file}" ]]; then
    log "[COLLECT] No disease list file provided or file not found, skipping batch collection"
    return 0
  fi

  # Derive batch_name from filename: disease_list_commercial_batch8.txt → commercial_batch8
  local list_basename
  list_basename="$(basename "${disease_list_file}" .txt)"
  local batch_name="${list_basename#disease_list_}"
  # If filename didn't start with disease_list_, use the full basename
  if [[ "${batch_name}" == "${list_basename}" ]]; then
    batch_name="${list_basename}"
  fi

  local collect_dir="${COLLECT_ROOT}/${batch_name}"
  mkdir -p "${collect_dir}"

  log "[COLLECT] Collecting batch results: batch=${batch_name} → ${collect_dir}"

  local collected=0
  local skipped=0
  local disease_statuses=""

  while IFS='|' read -r raw_key _ _ _ || [[ -n "${raw_key:-}" ]]; do
    local dk
    dk="$(echo "${raw_key}" | tr -d '[:space:]')"
    [[ -z "${dk}" || "${dk:0:1}" == "#" ]] && continue

    local disease_runs_dir="${RUNS_ROOT}/${dk}"
    if [[ ! -d "${disease_runs_dir}" ]]; then
      log "[COLLECT] ${dk}: no runs directory found, skipping"
      skipped=$((skipped + 1))
      disease_statuses="${disease_statuses}{\"disease\":\"${dk}\",\"status\":\"no_runs\",\"run_id\":\"\"},"
      continue
    fi

    # Find the latest run_id (sorted lexicographically — timestamp-based names sort correctly)
    local latest_run_id
    latest_run_id="$(ls -1 "${disease_runs_dir}" 2>/dev/null | sort -r | head -1)"
    if [[ -z "${latest_run_id}" ]]; then
      log "[COLLECT] ${dk}: no run_id directories found, skipping"
      skipped=$((skipped + 1))
      disease_statuses="${disease_statuses}{\"disease\":\"${dk}\",\"status\":\"no_run_id\",\"run_id\":\"\"},"
      continue
    fi

    local latest_run_dir="${disease_runs_dir}/${latest_run_id}"

    # Collect cross step8 xlsx
    local cross_xlsx="${latest_run_dir}/output/cross/step8/step8_fusion_rank_report.xlsx"
    if [[ -f "${cross_xlsx}" ]]; then
      cp "${cross_xlsx}" "${collect_dir}/${dk}.xlsx"
      collected=$((collected + 1))
      disease_statuses="${disease_statuses}{\"disease\":\"${dk}\",\"status\":\"success\",\"run_id\":\"${latest_run_id}\",\"type\":\"cross\"},"
    else
      # Try CSV fallback
      local cross_csv="${latest_run_dir}/output/cross/step8/step8_fusion_rank_report.csv"
      if [[ -f "${cross_csv}" ]]; then
        cp "${cross_csv}" "${collect_dir}/${dk}.csv"
        collected=$((collected + 1))
        disease_statuses="${disease_statuses}{\"disease\":\"${dk}\",\"status\":\"success_csv\",\"run_id\":\"${latest_run_id}\",\"type\":\"cross\"},"
      else
        disease_statuses="${disease_statuses}{\"disease\":\"${dk}\",\"status\":\"no_output\",\"run_id\":\"${latest_run_id}\"},"
        skipped=$((skipped + 1))
      fi
    fi

    # Collect origin step8 xlsx if exists
    local origin_xlsx="${latest_run_dir}/output/origin/step8/step8_fusion_rank_report.xlsx"
    if [[ -f "${origin_xlsx}" ]]; then
      cp "${origin_xlsx}" "${collect_dir}/${dk}_origin.xlsx"
    else
      local origin_csv="${latest_run_dir}/output/origin/step8/step8_fusion_rank_report.csv"
      if [[ -f "${origin_csv}" ]]; then
        cp "${origin_csv}" "${collect_dir}/${dk}_origin.csv"
      fi
    fi

  done < "${disease_list_file}"

  # Remove trailing comma from disease_statuses
  disease_statuses="${disease_statuses%,}"

  # Write summary.json
  BATCH_NAME="${batch_name}" \
  COLLECTED="${collected}" \
  SKIPPED="${skipped}" \
  DISEASE_STATUSES="[${disease_statuses}]" \
  python3 - <<'PY' > "${collect_dir}/summary.json"
import json
import os
from datetime import datetime, timezone

payload = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "batch_name": os.environ.get("BATCH_NAME", ""),
    "collected": int(os.environ.get("COLLECTED", "0")),
    "skipped": int(os.environ.get("SKIPPED", "0")),
    "diseases": json.loads(os.environ.get("DISEASE_STATUSES", "[]")),
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY

  log "[COLLECT] Batch ${batch_name}: collected=${collected}, skipped=${skipped} → ${collect_dir}"
}
