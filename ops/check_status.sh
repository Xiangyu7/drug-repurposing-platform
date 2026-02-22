#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# check_status.sh — 一键查看管线运行状态
#
# 用法:
#   bash ops/check_status.sh              # 全局概览
#   bash ops/check_status.sh atherosclerosis  # 查看单个疾病详情
#   bash ops/check_status.sh --failures   # 只看失败的
#   bash ops/check_status.sh --latest     # 只看最近一轮
#   bash ops/check_status.sh --ollama     # 检查 Ollama 状态
#   bash ops/check_status.sh --disk       # 检查磁盘占用
#   bash ops/check_status.sh --all        # 全部检查
# ═══════════════════════════════════════════════════════════════════

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${ROOT_DIR}/runtime"
RESULTS_DIR="${RUNTIME_DIR}/results"
QUARANTINE_DIR="${RUNTIME_DIR}/quarantine"
WORK_DIR="${RUNTIME_DIR}/work"
LOG_DIR="${ROOT_DIR}/logs"
DSMETA_DIR="${ROOT_DIR}/dsmeta_signature_pipeline"
LLM_DIR="${ROOT_DIR}/LLM+RAG证据工程"
DUAL_LIST="${ROOT_DIR}/ops/internal/disease_list_day1_dual.txt"
ORIGIN_LIST="${ROOT_DIR}/ops/internal/disease_list_day1_origin.txt"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# ── Helpers ──────────────────────────────────────────────────────

header() {
  echo ""
  echo -e "${BOLD}${CYAN}═══ $1 ═══${NC}"
}

ok()   { echo -e "  ${GREEN}✅ $1${NC}"; }
warn() { echo -e "  ${YELLOW}⚠️  $1${NC}"; }
fail() { echo -e "  ${RED}❌ $1${NC}"; }
info() { echo -e "  ${BLUE}ℹ️  $1${NC}"; }

# ── 1. 全局概览 ──────────────────────────────────────────────────

show_overview() {
  header "管线运行状态概览"

  # Dual list diseases
  local dual_diseases=()
  if [[ -f "${DUAL_LIST}" ]]; then
    while IFS='|' read -r key rest; do
      key="$(echo "${key}" | xargs)"
      [[ -z "${key}" || "${key:0:1}" == "#" ]] && continue
      dual_diseases+=("${key}")
    done < "${DUAL_LIST}"
  fi

  # Origin list diseases
  local origin_diseases=()
  if [[ -f "${ORIGIN_LIST}" ]]; then
    while IFS='|' read -r key rest; do
      key="$(echo "${key}" | xargs)"
      [[ -z "${key}" || "${key:0:1}" == "#" ]] && continue
      origin_diseases+=("${key}")
    done < "${ORIGIN_LIST}"
  fi

  echo ""
  echo -e "${BOLD}  疾病                             │ A │ B │ 最近运行   │ 状态${NC}"
  echo "  ─────────────────────────────────┼───┼───┼────────────┼──────"

  for disease in "${origin_diseases[@]}"; do
    local in_dual="—"
    for d in "${dual_diseases[@]}"; do
      [[ "${d}" == "${disease}" ]] && in_dual="✅" && break
    done

    # Latest result
    local latest_result=""
    local latest_status=""
    local latest_date="—"

    if [[ -d "${RESULTS_DIR}/${disease}" ]]; then
      # Find most recent run_summary.json
      local summary
      summary="$(find "${RESULTS_DIR}/${disease}" -name "run_summary.json" -type f 2>/dev/null | sort -r | head -1)"
      if [[ -n "${summary}" ]]; then
        latest_date="$(python3 -c "
import json, sys
try:
    d = json.load(open('${summary}'))
    print(d.get('run_date', d.get('timestamp','?')[:10]))
except: print('?')
" 2>/dev/null)"
        local cross_s origin_s
        cross_s="$(python3 -c "import json; d=json.load(open('${summary}')); print(d.get('cross_status','?'))" 2>/dev/null)"
        origin_s="$(python3 -c "import json; d=json.load(open('${summary}')); print(d.get('origin_status','?'))" 2>/dev/null)"

        if [[ "${cross_s}" == "success" && "${origin_s}" == "success" ]]; then
          latest_status="${GREEN}全部成功${NC}"
        elif [[ "${origin_s}" == "success" ]]; then
          if [[ "${cross_s}" == "skipped" || "${cross_s}" == "not_run" ]]; then
            latest_status="${GREEN}B成功${NC}"
          else
            latest_status="${YELLOW}A失败 B成功${NC}"
          fi
        elif [[ "${cross_s}" == "success" ]]; then
          latest_status="${YELLOW}A成功 B失败${NC}"
        else
          latest_status="${RED}失败${NC}"
        fi
      fi
    fi

    # Check quarantine
    local q_count=0
    if [[ -d "${QUARANTINE_DIR}/${disease}" ]]; then
      q_count="$(find "${QUARANTINE_DIR}/${disease}" -name "FAILURE.json" -type f 2>/dev/null | wc -l | tr -d ' ')"
    fi

    if [[ -z "${latest_status}" ]]; then
      if [[ "${q_count}" -gt 0 ]]; then
        latest_status="${RED}失败(${q_count}次)${NC}"
      else
        latest_status="未运行"
      fi
    elif [[ "${q_count}" -gt 0 ]]; then
      latest_status="${latest_status} ${RED}(隔离${q_count})${NC}"
    fi

    printf "  %-35s│ %-1s │ ✅ │ %-10s │ " "${disease}" "${in_dual}" "${latest_date}"
    echo -e "${latest_status}"
  done

  echo ""
  info "Dual list: ${#dual_diseases[@]} 个疾病 (Direction A+B)"
  info "Origin list: ${#origin_diseases[@]} 个疾病 (Direction B)"
}

# ── 2. 单个疾病详情 ──────────────────────────────────────────────

show_disease_detail() {
  local disease="$1"
  header "疾病详情: ${disease}"

  # dsmeta config
  local dsmeta_cfg="${DSMETA_DIR}/configs/${disease}.yaml"
  if [[ -f "${dsmeta_cfg}" ]]; then
    local gse_count
    gse_count="$(grep -c '^\s*- GSE' "${dsmeta_cfg}" 2>/dev/null || echo 0)"
    ok "dsmeta config: ${dsmeta_cfg} (${gse_count} GSE)"
  else
    warn "无 dsmeta config (只走 Direction B)"
  fi

  # dsmeta outputs
  local dsmeta_out="${DSMETA_DIR}/outputs/${disease}"
  if [[ -d "${dsmeta_out}" ]]; then
    local manifest="${dsmeta_out}/run_manifest.json"
    if [[ -f "${manifest}" ]]; then
      local status duration
      status="$(python3 -c "import json; print(json.load(open('${manifest}')).get('status','?'))" 2>/dev/null)"
      duration="$(python3 -c "import json; print(json.load(open('${manifest}')).get('duration_seconds','?'))" 2>/dev/null)"
      if [[ "${status}" == "success" ]]; then
        ok "dsmeta pipeline: ${status} (${duration}s)"
      else
        fail "dsmeta pipeline: ${status}"
      fi
    fi
  fi

  # Latest results
  echo ""
  echo -e "  ${BOLD}最近运行记录:${NC}"
  if [[ -d "${RESULTS_DIR}/${disease}" ]]; then
    local summaries
    summaries="$(find "${RESULTS_DIR}/${disease}" -name "run_summary.json" -type f 2>/dev/null | sort -r | head -5)"
    if [[ -n "${summaries}" ]]; then
      while IFS= read -r summary; do
        python3 -c "
import json
d = json.load(open('${summary}'))
date = d.get('run_date', '?')
run_id = d.get('run_id', '?')
cross = d.get('cross_status', '?')
origin = d.get('origin_status', '?')
mode = d.get('run_mode', '?')

status_icon = '✅' if cross in ('success','skipped') and origin == 'success' else '❌'
print(f'    {status_icon} {date} [{run_id[:20]}] cross={cross} origin={origin} mode={mode}')
" 2>/dev/null
      done <<< "${summaries}"
    else
      info "  无成功记录"
    fi
  else
    info "  无运行记录"
  fi

  # Quarantine records
  echo ""
  echo -e "  ${BOLD}失败记录 (quarantine):${NC}"
  if [[ -d "${QUARANTINE_DIR}/${disease}" ]]; then
    local failures
    failures="$(find "${QUARANTINE_DIR}/${disease}" -name "FAILURE.json" -type f 2>/dev/null | sort -r | head -5)"
    if [[ -n "${failures}" ]]; then
      while IFS= read -r failure; do
        python3 -c "
import json
d = json.load(open('${failure}'))
ts = d.get('timestamp', '?')[:19]
phase = d.get('failed_phase', '?')
msg = d.get('message', '?')[:60]
print(f'    ❌ {ts} phase={phase}')
print(f'       {msg}')
" 2>/dev/null
      done <<< "${failures}"
    else
      ok "  无失败记录"
    fi
  else
    ok "  无失败记录"
  fi
}

# ── 3. 只看失败 ──────────────────────────────────────────────────

show_failures() {
  header "所有失败记录"

  if [[ ! -d "${QUARANTINE_DIR}" ]]; then
    ok "无失败记录 (quarantine 目录不存在)"
    return
  fi

  local total=0
  for disease_dir in "${QUARANTINE_DIR}"/*/; do
    [[ ! -d "${disease_dir}" ]] && continue
    local disease
    disease="$(basename "${disease_dir}")"
    local failures
    failures="$(find "${disease_dir}" -name "FAILURE.json" -type f 2>/dev/null | sort -r)"
    [[ -z "${failures}" ]] && continue

    local count
    count="$(echo "${failures}" | wc -l | tr -d ' ')"
    total=$((total + count))

    echo ""
    echo -e "  ${RED}${BOLD}${disease}${NC} — ${count} 次失败"

    # Show latest 3
    echo "${failures}" | head -3 | while IFS= read -r f; do
      python3 -c "
import json
d = json.load(open('${f}'))
ts = d.get('timestamp', '?')[:19]
phase = d.get('failed_phase', '?')
msg = d.get('message', '?')[:80]
cross = d.get('cross_status', '?')
origin = d.get('origin_status', '?')
print(f'    {ts} | phase={phase} | cross={cross} origin={origin}')
print(f'    └─ {msg}')
" 2>/dev/null
    done
  done

  echo ""
  if [[ "${total}" -eq 0 ]]; then
    ok "无失败记录"
  else
    fail "共 ${total} 次失败"
  fi
}

# ── 4. Ollama 状态 ───────────────────────────────────────────────

check_ollama() {
  header "Ollama 状态"

  local host
  if [[ -f "${LLM_DIR}/.env" ]]; then
    host="$(grep '^OLLAMA_HOST=' "${LLM_DIR}/.env" 2>/dev/null | cut -d= -f2 | tr -d ' ')"
  fi
  host="${host:-http://localhost:11434}"
  info "Host: ${host}"

  if curl -sf --max-time 5 "${host}/api/tags" >/dev/null 2>&1; then
    ok "Ollama 服务运行中"

    # List models
    local models
    models="$(curl -sf --max-time 5 "${host}/api/tags" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for m in data.get('models', []):
    name = m['name']
    size_gb = m.get('size', 0) / 1e9
    family = m.get('details', {}).get('family', '?')
    quant = m.get('details', {}).get('quantization_level', '?')
    print(f'    {name:40s} {size_gb:.1f}GB  {family}/{quant}')
" 2>/dev/null)"
    echo -e "  ${BOLD}已安装模型:${NC}"
    echo "${models}"

    # Check required models
    local llm_model embed_model
    if [[ -f "${LLM_DIR}/.env" ]]; then
      llm_model="$(grep '^OLLAMA_LLM_MODEL=' "${LLM_DIR}/.env" 2>/dev/null | cut -d= -f2)"
      embed_model="$(grep '^OLLAMA_EMBED_MODEL=' "${LLM_DIR}/.env" 2>/dev/null | cut -d= -f2)"
    fi
    llm_model="${llm_model:-qwen2.5:7b-instruct}"
    embed_model="${embed_model:-nomic-embed-text}"

    if curl -sf --max-time 5 "${host}/api/tags" | python3 -c "
import json, sys
models = [m['name'] for m in json.load(sys.stdin).get('models', [])]
# Match with or without :latest suffix
target = '${llm_model}'
sys.exit(0 if target in models or target+':latest' in models or any(m.startswith(target+':') for m in models) else 1)
" 2>/dev/null; then
      ok "LLM 模型就绪: ${llm_model}"
    else
      fail "LLM 模型缺失: ${llm_model} — 运行: ollama pull ${llm_model}"
    fi

    if curl -sf --max-time 5 "${host}/api/tags" | python3 -c "
import json, sys
models = [m['name'] for m in json.load(sys.stdin).get('models', [])]
target = '${embed_model}'
sys.exit(0 if target in models or target+':latest' in models or any(m.startswith(target+':') for m in models) else 1)
" 2>/dev/null; then
      ok "Embed 模型就绪: ${embed_model}"
    else
      fail "Embed 模型缺失: ${embed_model} — 运行: ollama pull ${embed_model}"
    fi

    # Quick inference test
    local t0 t1
    t0="$(python3 -c 'import time; print(time.time())')"
    if curl -sf --max-time 30 "${host}/api/chat" \
      -d "{\"model\":\"${llm_model}\",\"messages\":[{\"role\":\"user\",\"content\":\"Say ok\"}],\"stream\":false}" \
      >/dev/null 2>&1; then
      t1="$(python3 -c 'import time; print(time.time())')"
      local dur
      dur="$(python3 -c "print(f'{${t1}-${t0}:.1f}')")"
      ok "推理测试通过 (${dur}s)"
    else
      fail "推理测试失败"
    fi
  else
    fail "Ollama 服务未运行 — 运行: ollama serve"
  fi
}

# ── 5. 磁盘占用 ──────────────────────────────────────────────────

check_disk() {
  header "磁盘占用"

  # Total project size
  local total
  total="$(du -sh "${ROOT_DIR}" 2>/dev/null | cut -f1)"
  info "项目总大小: ${total}"

  echo ""
  echo -e "  ${BOLD}各模块占用:${NC}"

  local dirs=(
    "dsmeta_signature_pipeline/work:dsmeta workdir (中间文件)"
    "dsmeta_signature_pipeline/outputs:dsmeta outputs (最终结果)"
    "dsmeta_signature_pipeline/data/cache:dsmeta 缓存 (GPL注释)"
    "runtime/work:runner work (运行中)"
    "runtime/results:runner results (已归档)"
    "runtime/quarantine:runner quarantine (失败)"
    "kg_explain/cache:kg_explain HTTP 缓存"
    "LLM+RAG证据工程/data:LLM+RAG 数据缓存"
    "logs:日志文件"
  )

  for entry in "${dirs[@]}"; do
    local dir="${entry%%:*}"
    local label="${entry#*:}"
    local full_path="${ROOT_DIR}/${dir}"
    if [[ -d "${full_path}" ]]; then
      local size
      size="$(du -sh "${full_path}" 2>/dev/null | cut -f1)"
      printf "    %-8s %s\n" "${size}" "${label}"
    fi
  done

  # Free disk space
  echo ""
  local avail
  avail="$(df -h "${ROOT_DIR}" | awk 'NR==2 {print $4}')"
  info "可用磁盘空间: ${avail}"

  local avail_gb
  avail_gb="$(df -g "${ROOT_DIR}" 2>/dev/null | awk 'NR==2 {print $4}' || df -k "${ROOT_DIR}" | awk 'NR==2 {print int($4/1048576)}')"
  if [[ "${avail_gb}" -lt 5 ]]; then
    fail "磁盘空间不足! (<5GB) — 考虑清理 workdir"
  elif [[ "${avail_gb}" -lt 10 ]]; then
    warn "磁盘空间偏低 (<10GB)"
  else
    ok "磁盘空间充足"
  fi
}

# ── 6. 最近一轮 ──────────────────────────────────────────────────

show_latest() {
  header "最近一轮运行结果"

  if [[ ! -d "${RESULTS_DIR}" ]]; then
    info "无运行记录"
    return
  fi

  # Find all run_summary.json, sort by timestamp
  local summaries
  summaries="$(find "${RESULTS_DIR}" -name "run_summary.json" -type f 2>/dev/null | sort -r | head -20)"

  if [[ -z "${summaries}" ]]; then
    info "无运行记录"
    return
  fi

  # Group by latest run_date
  python3 -c "
import json, sys, os
from collections import defaultdict

files = '''${summaries}'''.strip().split('\n')
records = []
for f in files:
    if not f.strip(): continue
    try:
        d = json.load(open(f.strip()))
        records.append(d)
    except: pass

if not records:
    print('  无运行记录')
    sys.exit(0)

# Sort by timestamp
records.sort(key=lambda x: x.get('timestamp', ''), reverse=True)

# Get latest date
latest_date = records[0].get('run_date', '?')
latest_records = [r for r in records if r.get('run_date') == latest_date]

print(f'  日期: {latest_date}')
print(f'  运行数: {len(latest_records)}')
print()
print(f'  {\"疾病\":<28s} {\"Cross\":<10s} {\"Origin\":<10s} {\"模式\":<12s}')
print(f'  {\"─\"*28} {\"─\"*10} {\"─\"*10} {\"─\"*12}')

ok_count = 0
fail_count = 0
for r in latest_records:
    disease = r.get('disease_key', '?')
    cross = r.get('cross_status', '?')
    origin = r.get('origin_status', '?')
    mode = r.get('run_mode', '?')

    cross_icon = '✅' if cross == 'success' else ('⏭️' if cross in ('skipped','not_run') else '❌')
    origin_icon = '✅' if origin == 'success' else '❌'

    if origin == 'success' and cross in ('success', 'skipped', 'not_run'):
        ok_count += 1
    else:
        fail_count += 1

    print(f'  {disease:<28s} {cross_icon} {cross:<8s} {origin_icon} {origin:<8s} {mode}')

print()
print(f'  总计: ✅ {ok_count} 成功, ❌ {fail_count} 失败')
" 2>/dev/null
}

# ── Main ─────────────────────────────────────────────────────────

main() {
  echo -e "${BOLD}${CYAN}Drug Repurposing Pipeline Status Check${NC}"
  echo -e "时间: $(date '+%Y-%m-%d %H:%M:%S')"

  case "${1:-}" in
    --failures|-f)
      show_failures
      ;;
    --latest|-l)
      show_latest
      ;;
    --ollama|-o)
      check_ollama
      ;;
    --disk|-d)
      check_disk
      ;;
    --all|-a)
      show_overview
      show_latest
      show_failures
      check_ollama
      check_disk
      ;;
    --help|-h)
      echo ""
      echo "用法:"
      echo "  bash ops/check_status.sh              # 全局概览"
      echo "  bash ops/check_status.sh <disease>    # 单个疾病详情"
      echo "  bash ops/check_status.sh --failures   # 只看失败"
      echo "  bash ops/check_status.sh --latest     # 最近一轮结果"
      echo "  bash ops/check_status.sh --ollama     # Ollama 状态"
      echo "  bash ops/check_status.sh --disk       # 磁盘占用"
      echo "  bash ops/check_status.sh --all        # 全部检查"
      ;;
    "")
      show_overview
      ;;
    *)
      # Treat as disease name
      show_disease_detail "$1"
      ;;
  esac
}

main "$@"
