# Drug Repurposing - 跑完审核清单

跑完一批疾病后，按此清单检查结果。

---

## 1. 全局状态

```bash
bash ops/start.sh status              # 全局概览
bash ops/start.sh status --latest     # 最近一轮结果
bash ops/start.sh status --failures   # 只看失败
bash ops/start.sh status <disease>    # 单个疾病详情
```

---

## 2. 必看文件（按优先级）

| 优先级 | 文件 | 看什么 |
|--------|------|--------|
| ★★★ | `ab_comparison.csv` | A+B 交叉验证，两路线都推荐 = 最高可信 |
| ★★★ | `step8_shortlist_topK.csv` | 最终候选药（含靶点/PDB/docking 可行性） |
| ★★★ | `step8_fusion_rank_report.xlsx` | Excel 报告（Shortlist sheet + 每药独立 Sheet） |
| ★★ | `step9_validation_plan.csv` | 验证方案（P1/P2/P3 优先级） |
| ★★ | `bridge_repurpose_cross.csv` | KG 排名全表（LLM 前的原始排名） |
| ★ | `step7_gating_decision.csv` | GO/MAYBE/NO-GO + 5 维打分明细 |
| ★ | `dossiers/*.json` | 某药的 PubMed 证据原文 |

### 审核要点

- [ ] `step8_fusion_rank_report.xlsx` → Shortlist sheet → 候选药数量和 gate 分布 (GO / MAYBE)
- [ ] 每药 Sheet → 靶点结构表：`PDB+AlphaFold` 可直接对接，`AlphaFold_only` 需谨慎
- [ ] `step8_shortlist_topK.csv` → `docking_feasibility_tier` 优先 `READY_PDB`
- [ ] `step9_validation_plan.csv` → 关注 P1 优先级
- [ ] 对比 Cross vs Origin → 重叠候选药 = 更可信（看 `ab_comparison.csv`）

---

## 3. 输出目录结构

```
runtime/results/<disease>/<date>/<run_id>/
├── direction_a/                        # Cross 路线
│   ├── step6_repurpose_cross/dossiers/
│   ├── step7_repurpose_cross/step7_gating_decision.csv
│   ├── step8_repurpose_cross/step8_fusion_rank_report.xlsx  ★
│   └── step9_repurpose_cross/step9_validation_plan.csv      ★
├── direction_b/                        # Origin 路线
│   ├── step8_repurpose_origin/
│   └── step9_repurpose_origin/
└── ab_comparison.csv                   ★ 交叉验证

kg_explain/output/<disease>/
├── signature/bridge_repurpose_cross.csv    # Cross KG 排名
└── ctgov/bridge_origin_reassess.csv        # Origin KG 排名
```

---

## 4. 签名质量检查（Cross 路线）

签名来源：dsmeta（GEO meta 分析）或 ARCHS4（H5 全平台搜索）

### 跑前检查

```bash
bash ops/start.sh check-keywords --list ops/disease_list_commercial.txt
```

### 跑后检查

- [ ] runner 日志中确认签名来源（`使用: archs4` 或 `使用: dsmeta`）
- [ ] 日志中 `X/Y up/down genes` 确认签名基因数

### 分层标准

| 签名基因数 | 处理 |
|------------|------|
| total < 30 或 min(up,down) < 10 | 自动跳过 Cross |
| 30 ≤ total < 100 | Cross 可用，cap 80 药物 |
| total ≥ 100 | Cross 正式可用，cap 200 |

- [ ] ARCHS4 签名 < 50 基因 → 检查关键词是否充分（`check-keywords`）
- [ ] Cross 被跳过 → 尝试 `SIG_PRIORITY=archs4` 或补 EXTRA_KEYWORDS

---

## 5. 后续动作

| 情况 | 下一步 |
|------|--------|
| 有 GO 候选 (P1) | 看 step9 验证方案 → 检查 docking 可行性 → 设计实验 |
| 只有 MAYBE | 看 step7 的 5 维打分 → 哪个维度低？→ 补文献或换签名源 |
| Cross 被跳过 | `check-keywords` 检查 → 补 EXTRA_KEYWORDS → 重跑 |
| Cross + Origin 有重叠 | **高可信度**，优先推进 |
| 候选全部陌生 | 纯探索性发现，需更多验证 |

---

## 6. 常见告警

| 告警 | 处理 |
|------|------|
| 签名基因极少 / 方向不平衡 | 确认跳过合理，或补充关键词重跑 |
| 药物截断 > 50% | 检查签名质量，考虑切换签名源 |
| `possible_toxicity_confounder=True` | SigReverse 毒性混淆，补做文献核查 |
| API unreachable | 网络超时已降级为 warn，重跑即可 |
