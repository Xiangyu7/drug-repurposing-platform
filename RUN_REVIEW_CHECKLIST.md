# Drug Repurposing 跑完审核清单

跑完一批疾病后，按此清单检查结果和决定下一步。

---

## 1) 全局状态检查

```bash
# 看哪些成功、哪些失败
bash ops/check_status.sh

# 看所有疾病摘要
bash ops/check_status.sh --all
```

---

## 2) 输出目录结构

每个疾病的结果在 `runtime/work/<disease>/<run_id>/` 下：

```
runtime/work/<disease>/<run_id>/
├── screen/
│   └── poolA_drug_level.csv                    # CT.gov 已有药物（负面清单）
├── sigreverse_output/                          # LINCS L1000 反转排名
├── kg_output/
│   └── pipeline_manifest.json                  # KG 元数据
├── llm/
│   ├── step6_repurpose_cross/
│   │   └── dossiers/*.json                     # 每药的 PubMed 证据原文
│   ├── step7_repurpose_cross/
│   │   └── step7_gating_decision.csv           # GO/MAYBE/NO-GO + 5维打分
│   ├── step8_repurpose_cross/
│   │   ├── step8_candidate_pack_from_step7.xlsx  ⭐ 核心交付物 (Excel)
│   │   └── step8_shortlist_top5.csv
│   ├── step9_repurpose_cross/
│   │   ├── step9_validation_plan.md            ⭐ 可读版最终报告
│   │   ├── step9_validation_plan.csv           ⭐ 验证方案（含优先级）
│   │   └── step9_manifest.json
│   ├── step8_repurpose_origin/                 # Origin 路线（dual 模式才有）
│   └── step9_repurpose_origin/
├── step_logs/                                  # 各步骤日志
└── pipeline_manifest_cross_signature.json      # 全流程元数据
```

KG 中间产物在 `kg_explain/output/<disease>/`：

```
kg_explain/output/<disease>/
├── bridge_repurpose_cross.csv                  # Cross 路线 KG 排名全表
├── bridge_origin_reassess.csv                  # Origin 路线 KG 排名全表
├── drug_disease_rank.csv                       # 全量药物-疾病对排名
└── pipeline_manifest.json
```

---

## 3) 必看文件（按优先级）

| 优先级 | 文件 | 看什么 |
|--------|------|--------|
| ⭐⭐⭐ | `step9_validation_plan.md` | 最终结论：每个候选药的 GO/MAYBE、P1/P2 优先级、验证方案 |
| ⭐⭐⭐ | `step8_candidate_pack_from_step7.xlsx` | Excel 详情：Shortlist sheet 看候选数和 gate 分布，每药 Sheet 看靶点结构表 |
| ⭐⭐ | `step9_validation_plan.csv` | 机器可读版：方便批量汇总多疾病结果 |
| ⭐⭐ | `bridge_repurpose_cross.csv` | KG 排名全表（LLM 之前的原始排名，含靶点/PDB/AlphaFold） |
| ⭐ | `step7_gating_decision.csv` | 每药 5 维打分明细，理解 GO/NO-GO 原因 |
| ⭐ | `dossiers/*.json` | 某药的 PubMed 证据原文，排查 LLM 判断依据 |

### 看结论时重点关注

- [ ] `step8_candidate_pack_from_step7.xlsx` → Shortlist sheet → 候选药数量和 gate 分布 (GO / MAYBE)
- [ ] 每个药的 Sheet → 靶点结构表 (Structure Source 列)
  - `PDB+AlphaFold` → 可直接做分子对接，选实验 PDB
  - `AlphaFold_only` → 对接结果需谨慎解读
  - `none` → 无法做分子对接
- [ ] `step8_shortlist_topK.csv` → `docking_feasibility_tier` 优先 `READY_PDB`
- [ ] `step9_validation_plan.csv` → 关注 P1 优先级候选药
- [ ] 对比 cross vs origin 两条路线 → 重叠候选药 = 更可信

### 深入排查时

- [ ] `step7_cards.json` → 每药 GO/MAYBE/NO-GO 决策 + 5 维打分
- [ ] `bridge_*.csv` → KG 排名 + 靶点信息 + 结构来源标记
- [ ] `drug_disease_rank.csv` → 某药排名高/低的原因
- [ ] `poolA_drug_level.csv` → CT.gov 拉到了哪些药
- [ ] `step6 dossiers/*.json` → 某药的 PubMed 证据原文

---

## 4) 签名质量检查 (Cross 路线)

签名来源有两个：dsmeta (GEO meta 分析) 和 ARCHS4 (H5 全平台搜索)，由 `SIG_PRIORITY` 控制。

- [ ] 确认签名来源：runner 日志中的决策 banner（`使用: archs4` 或 `使用: dsmeta`）
- [ ] 检查签名基因数（日志中 `X/Y up/down genes`）

### 分层质量标准

| 条件 | 层级 | 自动处理 | KG 药物上限 |
|------|------|---------|------------|
| total < 30 或 min(up,down) < 10 | Tier 0 | 跳过 Cross 路线 | N/A |
| 30 ≤ total < 100 | Tier 1 | Cross 可用，自动收紧 | cap 80 |
| total ≥ 100 | Tier 2 | Cross 正式可用 | cap 200 (默认) |

- [ ] 若 ARCHS4 签名 < 50 基因：人工看 `disease_signature_meta.json` 中的基因列表是否有生物学意义
- [ ] 检查 KG 日志中 `药物截断: N → M` 确认 cap 生效

---

## 5) 批量扫描命令

```bash
# 每个疾病的 GO/MAYBE 候选数
for d in runtime/work/*/; do
  disease=$(basename "$d")
  run=$(ls -t "$d" 2>/dev/null | head -1)
  csv="$d/$run/llm/step9_repurpose_cross/step9_validation_plan.csv"
  if [[ -f "$csv" ]]; then
    go=$(grep -c ',GO,' "$csv" 2>/dev/null || echo 0)
    maybe=$(grep -c ',MAYBE,' "$csv" 2>/dev/null || echo 0)
    printf "%-35s GO=%-3s MAYBE=%-3s\n" "$disease" "$go" "$maybe"
  else
    printf "%-35s (无结果)\n" "$disease"
  fi
done

# 每个疾病的签名来源和药物数
for d in runtime/work/*/; do
  disease=$(basename "$d")
  run=$(ls -t "$d" 2>/dev/null | head -1)
  manifest="$d/$run/pipeline_manifest_cross_signature.json"
  if [[ -f "$manifest" ]]; then
    python3 -c "
import json
m = json.load(open('$manifest'))
src = m.get('signature_source', '?')
up = m.get('signature_genes_up', 0)
dn = m.get('signature_genes_down', 0)
drugs = m.get('cross_drugs', m.get('kg_drugs', '?'))
print(f'$disease: source={src}  genes={up}+{dn}={up+dn}  drugs={drugs}')
" 2>/dev/null || printf "%-35s (manifest 格式不同)\n" "$disease"
  fi
done
```

---

## 6) 后续动作决策表

| 情况 | 下一步 |
|------|--------|
| 有 GO 候选 (P1) | 看 step9 验证方案 → 检查 docking 可行性 (`READY_PDB`) → 设计实验 |
| 只有 MAYBE | 看 step7 的 5 维打分 → 哪个维度拉低了？→ 补充文献或换签名源重跑 |
| Cross 被跳过 (签名不足) | 签名太小/不平衡 → 尝试 `SIG_PRIORITY=dsmeta` 或补充 ARCHS4 关键词 |
| KG 耗时过长 (>30min) | 检查药物数 → 调低 `KG_MAX_DRUGS_SIGNATURE` 或检查签名质量 |
| Origin + Cross 有重叠药物 | **高可信度信号**，两条独立路线交叉验证，优先推进 |
| Cross 候选与已知文献一致 | 管线验证通过，可信赖该疾病的排名结果 |
| Cross 候选全部陌生 | 纯探索性发现，需更多文献和实验验证 |

---

## 7) 告警处理

| 告警日志 | 处理 |
|----------|------|
| `签名方向不平衡` 或 `签名基因极少` | 确认自动跳过是否合理，或手动补充签名 |
| `药物截断` 且截断量 > 50% | 检查签名质量，考虑切换签名源 |
| `Uncertainty quantification skipped` | KG 置信区间缺失，结果需谨慎解读 |
| `possible_toxicity_confounder=True` | SigReverse 毒性混淆，补做文献核查 |
| API unreachable (ChEMBL/OpenTargets) | 网络超时（已降级为 warn），不阻塞 pipeline，重跑即可 |
