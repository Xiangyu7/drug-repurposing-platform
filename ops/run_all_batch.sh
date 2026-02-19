#!/usr/bin/env bash
set -Eeuo pipefail

# ═══════════════════════════════════════════════════════════════
# run_all_batch.sh — 批量运行 dsmeta + sigreverse 全部疾病
# ═══════════════════════════════════════════════════════════════
# Usage:
#   bash ops/run_all_batch.sh              # 全部流程
#   bash ops/run_all_batch.sh --dsmeta     # 只跑 dsmeta
#   bash ops/run_all_batch.sh --sigreverse # 只跑 sigreverse
# ═══════════════════════════════════════════════════════════════

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DSMETA_DIR="${ROOT_DIR}/dsmeta_signature_pipeline"
SIGREVERSE_DIR="${ROOT_DIR}/sigreverse"

# Parse args
RUN_DSMETA=1
RUN_SIGREVERSE=1
if [[ "${1:-}" == "--dsmeta" ]]; then
    RUN_SIGREVERSE=0
elif [[ "${1:-}" == "--sigreverse" ]]; then
    RUN_DSMETA=0
fi

FAILED=()
SUCCEEDED=()

timestamp() { date '+%Y-%m-%d %H:%M:%S'; }

# ── Part 1: dsmeta ────────────────────────────────────────────
if [[ "${RUN_DSMETA}" -eq 1 ]]; then
    echo ""
    echo "╔═══════════════════════════════════════════════════════╗"
    echo "║  Part 1: dsmeta — GEO 下载 + 疾病签名分析            ║"
    echo "╚═══════════════════════════════════════════════════════╝"
    echo ""

    cd "${DSMETA_DIR}"
    mkdir -p outputs

    for cfg in configs/*.yaml; do
        name=$(basename "$cfg" .yaml)
        # 跳过模板文件
        if [[ "$name" == "template" ]] || [[ "$name" == "athero_example" ]]; then
            continue
        fi

        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "[$(timestamp)] dsmeta START: $name"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

        if python run.py --config "$cfg" 2>&1 | tee "outputs/${name}_log.txt"; then
            echo "[$(timestamp)] dsmeta DONE: $name ✓"
            SUCCEEDED+=("dsmeta:${name}")
        else
            echo "[$(timestamp)] dsmeta FAILED: $name ✗"
            FAILED+=("dsmeta:${name}")
        fi
        echo ""
    done
fi

# ── Part 2: sigreverse ────────────────────────────────────────
if [[ "${RUN_SIGREVERSE}" -eq 1 ]]; then
    echo ""
    echo "╔═══════════════════════════════════════════════════════╗"
    echo "║  Part 2: sigreverse — LINCS/CMap 反转评分            ║"
    echo "╚═══════════════════════════════════════════════════════╝"
    echo ""

    cd "${SIGREVERSE_DIR}"

    # 从 dsmeta configs 自动获取疾病列表
    DISEASES=()
    for cfg in "${DSMETA_DIR}/configs/"*.yaml; do
        name=$(basename "$cfg" .yaml)
        if [[ "$name" == "template" ]] || [[ "$name" == "athero_example" ]]; then
            continue
        fi
        DISEASES+=("$name")
    done

    for disease in "${DISEASES[@]}"; do
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "[$(timestamp)] sigreverse START: $disease"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

        # 疾病名中的下划线替换为空格用于 CREEDS 查询
        fetch_name="${disease//_/ }"

        if python scripts/run.py \
            --config configs/default.yaml \
            --fetch "$fetch_name" \
            --out "data/output_${disease}" \
            2>&1 | tee "data/output_${disease}_log.txt"; then
            echo "[$(timestamp)] sigreverse DONE: $disease ✓"
            SUCCEEDED+=("sigreverse:${disease}")
        else
            echo "[$(timestamp)] sigreverse FAILED: $disease ✗"
            FAILED+=("sigreverse:${disease}")
        fi
        echo ""
    done
fi

# ── Summary ───────────────────────────────────────────────────
echo ""
echo "╔═══════════════════════════════════════════════════════╗"
echo "║  Summary                                             ║"
echo "╚═══════════════════════════════════════════════════════╝"
echo ""
echo "Succeeded (${#SUCCEEDED[@]}):"
for s in "${SUCCEEDED[@]:-}"; do
    [[ -n "$s" ]] && echo "  ✓ $s"
done
echo ""
if [[ ${#FAILED[@]} -gt 0 ]]; then
    echo "Failed (${#FAILED[@]}):"
    for f in "${FAILED[@]}"; do
        echo "  ✗ $f"
    done
else
    echo "Failed: 0 — All tasks completed successfully!"
fi
echo ""
echo "[$(timestamp)] ALL DONE"
