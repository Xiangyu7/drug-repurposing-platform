#!/usr/bin/env bash
set -euo pipefail

mkdir -p archive_step4_manual
# 旧的“全人工路线”（建议归档）
mv -f step4_make_review_sheet.py archive_step4_manual/ 2>/dev/null || true
mv -f step4_merge_labels.py archive_step4_manual/ 2>/dev/null || true

# 可选：把辅助脚本放到 tools/
mkdir -p tools_step4
mv -f step4_auto_review_aids.py tools_step4/ 2>/dev/null || true

echo "Archived manual-route scripts to archive_step4_manual/"
echo "Moved optional helper to tools_step4/"
