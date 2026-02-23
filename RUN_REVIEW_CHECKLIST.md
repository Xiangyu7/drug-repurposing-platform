# Drug Repurposing 人工审核清单

本清单是整个仓库的最小人工审核流程。  
每次运行使用一份（按 `run_id`），并将审核产物与当次输出一起归档。

## 1) 每次 Run 必做

- [ ] 冻结本次 `run_id` 和 manifest（配置、模型版本、输出目录）。
- [ ] 运行环境体检：`bash ops/start.sh check` 并归档 `env_check_*.json`、`env_resolved_*.env`。
- [ ] 审核 trial screening 产物 `manual_review_queue.csv`。
- [ ] 审核 `manual_alias_review_queue.csv`；必要时更新 `manual_alias_overrides.csv`。
- [ ] 审核 Step7 的边缘候选（`MAYBE` / `explore`）及其关键文献。
- [ ] 生成并审核 reject 回抽队列（`build_reject_audit_queue.py` 输出 CSV）。
- [ ] Reviewer A/B 独立填写 `review_log_<run_id>.csv`。
- [ ] 在 `adjudication_<run_id>.md` 记录分歧仲裁结果。
- [ ] 在 `release_decision_<run_id>.md` 写最终放行结论。
- [ ] 将确认标注增量合并到 `data/gold_standard/gold_standard_v1.csv`。
- [ ] 重跑抽取评估（`eval_extraction.py`，建议含 holdout）并归档报告 JSON。

## 2) 触发告警时必做

- [ ] 泄漏审计失败（`pair_overlap` 不 clean）：立即停止放行，先修复 split/数据泄漏。
- [ ] `kg_explain` 出现 `Uncertainty quantification skipped`：修复前禁止放行。
- [ ] `sigreverse` 出现 `possible_toxicity_confounder=True`：补做文献核查并记录保留/剔除理由。
- [ ] Cross 路线出现 `签名方向不平衡` 或 `签名基因极少`：确认自动降级/跳过是否合理，或手动补充签名。
- [ ] KG 出现 `药物截断` 日志但截断量 > 50%：检查签名质量，考虑切换签名源。
- [ ] release gate 被阻断（kill/miss/IRR 超阈）：做人工根因复核并形成书面决策。

## 3) 新疾病或大改配置时必做

- [ ] 重新审核 `dsmeta` 的数据集选择（`gse_list`）和 case/control 规则。
- [ ] 重新审核 dsmeta QC 报告（`outputs/reports/qc_summary.html`）。
- [ ] 若使用 ARCHS4：检查 `archs4_signature_pipeline/configs/<disease>.yaml` 的 `case_keywords` 是否准确。
- [ ] 若启用 KEGG，手工提供 `kegg.gmt` 并记录来源与许可。
- [ ] 重新校准疾病相关安全黑名单、门控阈值和验证标准。

## 3.5) 签名质量门控审核 (Cross 路线)

- [ ] 确认签名来源：`run_summary.json` → `signature_source` 字段 (`dsmeta` / `archs4`)
- [ ] 检查签名基因数：up 基因数 + down 基因数 = 总数
  - `< 30` 或 `min(up, down) < 10` → runner 应已自动跳过 Cross（检查日志有无 `签名基因数不足` 或 `签名方向不平衡`）
  - `30-100` → Tier 1，KG 药物上限自动收紧至 80，结果仅供探索
  - `≥ 100` → Tier 2，正式可用，上限 200
- [ ] 检查 KG 药物截断日志：`药物截断: N → M` 确认 cap 生效
- [ ] 若 ARCHS4 签名 < 50 基因，建议人工审核 `disease_signature_meta.json` 中的基因列表是否有生物学意义
- [ ] 对比 `SIG_PRIORITY` 设置与实际使用源是否一致（runner 决策 banner 中显示）

## 4) 建议放行阈值

- [ ] `kill_rate <= 0.15`
- [ ] `miss_rate <= 0.10`
- [ ] `IRR (kappa) >= 0.60`
- [ ] 泄漏审计 `pair_overlap == 0`
- [ ] 最终日志无 uncertainty-skip 告警

## 5) 跑完一轮后数据检查 (按优先级)

### 第一优先：看结论
- [ ] 确认签名来源和质量层级：`pipeline_manifest_cross_signature.json` → `signature_source` + 基因数
- [ ] 打开 `step8_candidate_pack_from_step7.xlsx` → Shortlist sheet → 确认候选药数量和 gate 分布
- [ ] 每个药的 Sheet → 检查靶点结构表 (Structure Source 列)
  - `PDB+AlphaFold` → 可直接做分子对接，选实验 PDB
  - `AlphaFold_only` → 对接结果需谨慎解读
  - `none` → 无法做分子对接
- [ ] `step8_shortlist_topK.csv` → 检查 docking 列
  - `docking_feasibility_tier` 优先 `READY_PDB`
  - `docking_primary_structure_id` 非空且与 `target_details` 一致
  - `docking_risk_flags` 对 `AF_FALLBACK/NO_STRUCTURE` 给出降级原因
- [ ] 查看 `step9_validation_plan.csv` → 关注 P1 优先级候选药
- [ ] 对比 cross vs origin 两条路线 → 重叠候选药 = 更可信

### 第二优先：判断可信度
- [ ] `step7_cards.json` → 每药 GO/MAYBE/NO-GO 决策 + 5 维打分
- [ ] `step8_one_pagers_topK.md` → 候选药 Markdown 报告 (含靶点/UniProt/PDB 链接)
- [ ] `bridge_*.csv` → KG 排名 + 靶点信息 + 结构来源标记

### 第三优先：排查问题
- [ ] `drug_disease_rank.csv` → 某药排名高/低的原因
- [ ] `poolA_drug_level.csv` → CT.gov 拉到了哪些药
- [ ] `manual_review_queue.csv` → 需人工确认的试验
- [ ] `step6 dossiers/*.json` → 某药的 PubMed 证据原文
- [ ] `run_summary.json` → 运行是否有步骤失败/被隔离

## 6) 每次 Run 至少保留的产物

- [ ] `review_log_<run_id>.csv`
- [ ] `adjudication_<run_id>.md`
- [ ] `release_decision_<run_id>.md`
- [ ] Step6-9 manifest 与关键输出
- [ ] 评估报告 JSON（`eval_extraction`，若使用 holdout 也一起保留）
- [ ] reject audit 队列 CSV（含审核后版本）

## 7) TopN 策略审计（工业级新增）

- [ ] 检查 origin 决策文件：`topn_decision_origin_stage1.json` / `topn_quality_origin_stage1.json`
- [ ] 检查 cross 决策文件：`topn_decision_cross_stage1.json` / `topn_quality_cross_stage1.json`
- [ ] 检查 stage2 文件是否存在且可解释（触发扩容或 skip 原因）：`*_stage2.json`
- [ ] 核对 stage2 是否只执行 0 或 1 次（禁止第三轮扩容）
- [ ] 在 `step9_manifest.json` 的 `summary.topn_policy` 核对：
  - `selected_stage`
  - `resolved_topn`
  - `quality_passed`

## 8) 批量跑完后的产出总览与后续动作

### 8.1 输出目录结构

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

### 8.2 必看文件（按优先级）

| 优先级 | 文件 | 看什么 |
|--------|------|--------|
| ⭐⭐⭐ | `step9_validation_plan.md` | 最终结论：每个候选药的 GO/MAYBE、P1/P2 优先级、验证方案 |
| ⭐⭐⭐ | `step8_candidate_pack_from_step7.xlsx` | Excel 详情：Shortlist sheet 看候选数和 gate 分布，每药 Sheet 看靶点结构表 |
| ⭐⭐ | `step9_validation_plan.csv` | 机器可读版：方便批量汇总多疾病结果 |
| ⭐⭐ | `bridge_repurpose_cross.csv` | KG 排名全表（LLM 之前的原始排名，含靶点/PDB/AlphaFold） |
| ⭐ | `step7_gating_decision.csv` | 每药 5 维打分明细，理解 GO/NO-GO 原因 |
| ⭐ | `dossiers/*.json` | 某药的 PubMed 证据原文，排查 LLM 判断依据 |

### 8.3 批量扫描命令

```bash
# 全局状态概览
bash ops/check_status.sh --all

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

### 8.4 后续动作决策表

| 情况 | 动作 |
|------|------|
| 有 GO 候选 (P1) | 看 step9 验证方案 → 检查 docking 可行性 (`READY_PDB`) → 设计实验 |
| 只有 MAYBE | 看 step7 的 5 维打分 → 哪个维度拉低了？→ 补充文献或换签名源重跑 |
| Cross 被跳过 (签名不足) | 签名太小/不平衡 → 尝试 `SIG_PRIORITY=dsmeta` 或补充 ARCHS4 关键词 |
| KG 耗时过长 (>30min) | 检查药物数 → 调低 `KG_MAX_DRUGS_SIGNATURE` 或检查签名质量 |
| Origin + Cross 有重叠药物 | **高可信度信号**，两条独立路线交叉验证，优先推进 |
| Cross 候选与已知文献一致 | 管线验证通过，可信赖该疾病的排名结果 |
| Cross 候选全部陌生 | 纯探索性发现，需更多文献和实验验证 |
