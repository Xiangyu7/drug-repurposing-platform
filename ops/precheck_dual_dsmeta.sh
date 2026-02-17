#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DSMETA_DIR="${ROOT_DIR}/dsmeta_signature_pipeline"
LIST_FILE="${1:-${ROOT_DIR}/ops/disease_list_day1_dual.txt}"

MIN_CASE="${MIN_CASE:-8}"
MIN_CONTROL="${MIN_CONTROL:-8}"

DSMETA_PY="${DSMETA_DIR}/.venv/bin/python3"
if [[ ! -x "${DSMETA_PY}" ]]; then DSMETA_PY="python3"; fi

REPORT_DIR="${ROOT_DIR}/runtime/state"
mkdir -p "${REPORT_DIR}"
REPORT_PATH="${REPORT_DIR}/precheck_dual_$(date '+%Y%m%d_%H%M%S').tsv"

if [[ ! -f "${LIST_FILE}" ]]; then
  echo "ERROR: list file not found: ${LIST_FILE}" >&2
  exit 1
fi

printf 'disease_key\tstatus\tdetail\tconfig_path\n' > "${REPORT_PATH}"

fail_count=0
checked_count=0

count_group() {
  local file_path="$1"
  local target_group="$2"
  awk -F '\t' -v target="${target_group}" '
    NR==1 {
      for (i=1; i<=NF; i++) {
        if ($i=="group") group_col=i
      }
      next
    }
    group_col > 0 && $group_col == target { c++ }
    END { print c+0 }
  ' "${file_path}"
}

while IFS='|' read -r raw_key raw_query _raw_ids _raw_inject || [[ -n "${raw_key:-}" ]]; do
  disease_key="$(echo "${raw_key:-}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
  if [[ -z "${disease_key}" || "${disease_key:0:1}" == "#" ]]; then
    continue
  fi
  checked_count=$((checked_count + 1))

  cfg_path="${DSMETA_DIR}/configs/${disease_key}.yaml"
  if [[ ! -f "${cfg_path}" ]]; then
    printf '%s\tFAIL\tmissing dsmeta config\t%s\n' "${disease_key}" "${cfg_path}" >> "${REPORT_PATH}"
    fail_count=$((fail_count + 1))
    continue
  fi

  echo "[PRECHECK] ${disease_key}: run dsmeta step1-2"
  if ! (
    cd "${DSMETA_DIR}" && \
    "${DSMETA_PY}" run.py --config "${cfg_path}" --from-step 1 --to-step 2
  ); then
    printf '%s\tFAIL\tdsmeta step1-2 failed\t%s\n' "${disease_key}" "${cfg_path}" >> "${REPORT_PATH}"
    fail_count=$((fail_count + 1))
    continue
  fi

  shopt -s nullglob
  pheno_files=("${DSMETA_DIR}/work/${disease_key}/de/"*/pheno_labeled.tsv)
  shopt -u nullglob
  if [[ "${#pheno_files[@]}" -eq 0 ]]; then
    printf '%s\tFAIL\tno pheno_labeled.tsv under work/%s/de\t%s\n' "${disease_key}" "${disease_key}" "${cfg_path}" >> "${REPORT_PATH}"
    fail_count=$((fail_count + 1))
    continue
  fi

  per_gse_notes=()
  disease_fail=0
  for pheno_file in "${pheno_files[@]}"; do
    gse_id="$(basename "$(dirname "${pheno_file}")")"
    case_n="$(count_group "${pheno_file}" "case")"
    control_n="$(count_group "${pheno_file}" "control")"
    per_gse_notes+=("${gse_id}(case=${case_n},control=${control_n})")
    if [[ "${case_n}" -lt "${MIN_CASE}" || "${control_n}" -lt "${MIN_CONTROL}" ]]; then
      disease_fail=1
    fi
  done

  detail="$(IFS='; '; echo "${per_gse_notes[*]}")"
  if [[ "${disease_fail}" -eq 1 ]]; then
    printf '%s\tFAIL\t%s; threshold(case>=%s,control>=%s)\t%s\n' \
      "${disease_key}" "${detail}" "${MIN_CASE}" "${MIN_CONTROL}" "${cfg_path}" >> "${REPORT_PATH}"
    fail_count=$((fail_count + 1))
  else
    printf '%s\tPASS\t%s\t%s\n' "${disease_key}" "${detail}" "${cfg_path}" >> "${REPORT_PATH}"
  fi
done < "${LIST_FILE}"

echo "Precheck report: ${REPORT_PATH}"
echo "Checked diseases: ${checked_count}, Failed: ${fail_count}"

if [[ "${fail_count}" -gt 0 ]]; then
  exit 1
fi
