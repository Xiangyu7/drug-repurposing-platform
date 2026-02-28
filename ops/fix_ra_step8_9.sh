#!/bin/bash
set -e
cd /root/drug-repurposing-platform/LLM+RAG证据工程
WD=/root/drug-repurposing-platform/runtime/work/rheumatoid_arthritis/20260228_212810_1370
KG=/root/drug-repurposing-platform/kg_explain

echo "=== Cross Step8 ==="
.venv/bin/python3 scripts/step8_fusion_rank.py \
  --step7_dir "$WD/llm/step7_repurpose_cross" \
  --neg "$WD/screen/poolA_drug_level.csv" \
  --bridge "$KG/output/rheumatoid_arthritis/signature/bridge_repurpose_cross.csv" \
  --outdir "$WD/llm/step8_repurpose_cross" \
  --target_disease "rheumatoid arthritis" \
  --topk 15 --route cross --include_explore 1 \
  --strict_contract 1 --sensitivity_n 1000

echo "=== Cross Step9 ==="
.venv/bin/python3 scripts/step9_validation_plan.py \
  --step8_dir "$WD/llm/step8_repurpose_cross" \
  --step7_dir "$WD/llm/step7_repurpose_cross" \
  --outdir "$WD/llm/step9_repurpose_cross" \
  --target_disease "rheumatoid arthritis" \
  --strict_contract 1

echo "=== Origin Step8 ==="
.venv/bin/python3 scripts/step8_fusion_rank.py \
  --step7_dir "$WD/llm/step7_origin_reassess" \
  --neg "$WD/screen/poolA_drug_level.csv" \
  --bridge "$KG/output/rheumatoid_arthritis/ctgov/bridge_origin_reassess.csv" \
  --outdir "$WD/llm/step8_origin_reassess" \
  --target_disease "rheumatoid arthritis" \
  --topk 10 --route origin --include_explore 1 \
  --strict_contract 1 --sensitivity_n 1000

echo "=== Origin Step9 ==="
.venv/bin/python3 scripts/step9_validation_plan.py \
  --step8_dir "$WD/llm/step8_origin_reassess" \
  --step7_dir "$WD/llm/step7_origin_reassess" \
  --outdir "$WD/llm/step9_origin_reassess" \
  --target_disease "rheumatoid arthritis" \
  --strict_contract 1

echo "=== Done! Results: ==="
echo "Cross: $WD/llm/step8_repurpose_cross/step8_shortlist_topK.csv"
echo "Origin: $WD/llm/step8_origin_reassess/step8_shortlist_topK.csv"
