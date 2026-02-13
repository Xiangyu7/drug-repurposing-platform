# Drug Repurposing Platform — Human Judgment Checklist

> 本文件列出整个管道中**需要人工判断**的关键决策点。
> 代码已将这些决策暴露为可配置参数并提供了默认值，但最终数值需要根据具体疾病和研究目标由领域专家确定。

---

## 1. kg_explain — 知识图谱模块

| 决策点 | 当前默认值 | 配置位置 | 判断依据 |
|--------|-----------|---------|---------|
| Pathway-Disease 关联强度阈值 | config YAML 中定义 | `config.yaml → pathway_score_threshold` | 阈值过高会漏掉弱但真实的通路；过低会引入噪声 |
| Hub penalty lambda | config YAML 中定义 | `config.yaml → hub_penalty_lambda` | 惩罚高连接度节点的力度。太大漏掉真实 hub 基因，太小被噪声淹没 |
| 严重不良事件关键词 | `serious_ae_keywords` 列表 | `config.yaml → serious_ae_keywords` | 哪些 AE 术语算"严重"，需要临床药理学判断 |
| topk_paths_per_pair | config YAML 中定义 | `config.yaml → topk_paths_per_pair` | 每对 drug-disease 保留多少条通路。影响下游分析的覆盖面 vs 精度 |
| Trial status 过滤 | `["TERMINATED", "WITHDRAWN", "SUSPENDED"]` | `config.yaml → trial_statuses` | 哪些临床试验状态视为安全信号 |

---

## 2. LLM+RAG 证据工程 — Evidence Scoring

### 2.1 打分维度与权重

| 维度 | 满分 | 当前配置 | 判断依据 |
|------|------|---------|---------|
| Evidence Strength | 30 | `ScoringConfig` | 文献数量阈值: high=10篇, med=5篇, low=2篇。需根据疾病文献丰富度调整 |
| Mechanism Plausibility | 20 | `ScoringConfig` | 基于 PMID 数量和一致性打分，50/20/5 篇为断点 |
| Translatability | 20 | `ScoringConfig` | 研究活跃度 + benefit 证据。50/20/10/5 篇为断点 |
| Safety Fit | 20 | `ScoringConfig` | harm_penalty_per_paper=1.0, 黑名单惩罚=6.0 |
| Practicality | 10 | `ScoringConfig` | 仅基于 PMID 数量，50/20/10/5 为断点 |

**需要人工确认**: 5 个维度的满分比例 (30/20/20/20/10) 是否反映你的优先级偏好？

### 2.2 GO/NO-GO Gating 门控

| 参数 | 默认值 | 配置位置 | 判断依据 |
|------|-------|---------|---------|
| GO 阈值 | 60.0 | `GatingConfig.go_threshold` | 总分 >= 60 推进到验证阶段。需要根据风险容忍度调整 |
| MAYBE 阈值 | 40.0 | `GatingConfig.maybe_threshold` | 40-60 分进入观察队列 |
| 最少 benefit 论文 | 2 | `GatingConfig.min_benefit_papers` | 硬门控：少于 2 篇直接 NO-GO |
| 最少总 PMID | 3 | `GatingConfig.min_total_pmids` | 硬门控：证据不足直接 NO-GO |
| 最大 harm 比例 | 0.5 | `GatingConfig.max_harm_ratio` | 硬门控：超过 50% 文献报告 harm 则 NO-GO |
| 安全黑名单 | warfarin, dexamethasone, prednisone, prednisolone, hydrocortisone | `ScoringConfig.safety_blacklist_patterns` | 已知高风险药物，换疾病需重新定义 |

### 2.3 LLM 证据提取

| 参数 | 默认值 | 判断依据 |
|------|-------|---------|
| 幻觉检测 — 机制锚定阈值 | 30% | `detect_hallucination()` 中硬编码。LLM 生成的机制描述中至少 30% 的词需出现在原文 |
| Confidence 映射 | HIGH=0.9, MED=0.5, LOW=0.2 | 数值型置信度与分类标签的映射关系 |
| 重试温度序列 | [0.2, 0.1, 0.0] | 提取失败时逐步降低温度重试。0.0 = 确定性输出 |
| Direction 枚举 | benefit, harm, neutral, unclear | 是否需要更细粒度的分类 (如 "mixed") |
| Model 枚举 | human, animal, cell, computational, unclear | 是否需要区分 "ex vivo" 或 "organoid" |

### 2.4 Gold Standard 标注

| 需要人工标注的字段 | 说明 |
|------------------|------|
| direction | 每篇论文对该药物的结论方向: benefit / harm / neutral |
| model | 研究模型: human / animal / cell / computational |
| endpoint | 研究终点: PLAQUE_IMAGING / CV_EVENTS / BIOMARKER / OTHER 等 |
| confidence | 标注者对自己判断的信心: HIGH / MED / LOW |

**注意**: `bootstrap_from_dossiers()` 可以从 LLM 提取结果自动生成初始标注，但必须经过人工审校后才能作为评估基准。

---

## 3. Drug Aggregation — 药物聚合

| 决策点 | 默认值 | 判断依据 |
|--------|-------|---------|
| 药物别名合并 | `drug_alias_override.csv` | 需要人工审核哪些药名是同一个药 (如 "aspirin" vs "ASA" vs "acetylsalicylic acid") |
| Fuzzy matching 阈值 | 92 | 自动匹配相似药名的相似度门槛。需检查 `manual_review_queue.csv` 确认误匹配 |
| Fuzzy matching topK | 200 | 对前 200 个高频药物做模糊匹配。数据集大时可能需要增加 |
| 最短药名长度 | 4 字符 | 短于 4 字符的药名不参与模糊匹配，避免误匹配 |

---

## 4. Validation Planner — 验证规划（动脉粥样硬化特定）

> **警告**: 以下参数全部硬编码为动脉粥样硬化场景。换疾病时必须重新定义。

| 参数 | 当前值 | 说明 |
|------|-------|------|
| 临床试验 Phase | Phase 2 | 假设候选药物已有安全性数据 |
| 预估样本量 N | 200 | 需要统计功效分析确认 |
| 预估时长 | 12 个月 | 取决于主要终点 |
| 纳入标准 — ASCVD 风险 | >= 7.5% | 10 年心血管风险 |
| 纳入标准 — CIMT | >= 0.7 mm | 颈动脉内膜中层厚度 |
| 纳入标准 — LDL | >= 70 mg/dL | 低密度脂蛋白 |
| 年龄范围 | 40-75 | |
| 排除标准 | MI/中风 < 3月, 肾/肝损, 活动性癌症, 妊娠 | |
| 验证阶段判定阈值 | total_pmids < 5 → 文献综述; mechanism < 12 → 机制验证 | 需根据研究资源调整 |
| 优先级阈值 | total_score >= 75 → 高优先级 | |

---

## 5. Retrieval — 文献检索

| 决策点 | 默认值 | 判断依据 |
|--------|-------|---------|
| PubMed 最大检索量 | 100 篇/查询 | 太少可能遗漏关键文献；太多增加 LLM 处理成本 |
| BM25 topK | 80 | 初筛保留 80 篇进入重排序 |
| Embedding rerank topK | 60 | 语义重排后保留 60 篇 |
| RRF k 参数 | 60 | Reciprocal Rank Fusion 平滑参数 |
| Cross-encoder 评分阈值 | 0-10 分制 | LLM 逐篇打分的相关性量表 |
| BM25 参数 k1/b | k1=1.5, b=0.75 | TF 饱和度和文档长度归一化，经典默认值但可能需要针对生物医学文献调优 |

---

## 6. 跨模块一致性检查

在调整上述参数时，请确保以下一致性：

1. **Confidence 编码一致**: 全管道使用相同的 HIGH/MED/LOW → 数值映射
2. **药物名称归一化一致**: kg_explain 和 LLM+RAG 使用相同的 canonical name
3. **疾病 ID 格式一致**: kg_explain 用 OpenTargets diseaseId, LLM+RAG 用文本匹配
4. **安全黑名单同步**: kg_explain 的 `serious_ae_keywords` 和 LLM+RAG 的 `safety_blacklist_patterns` 应覆盖相同的药物

---

## 操作建议

### 首次使用新疾病时

1. 修改 `safety_blacklist_patterns` — 移除动脉粥样硬化特定药物，添加新疾病的高风险药物
2. 修改 `ValidationPlanner` 的纳排标准和终点定义
3. 审核 `drug_alias_override.csv` 和 `manual_review_queue.csv`
4. 标注至少 50 篇论文作为 Gold Standard
5. 用 Gold Standard 校准 GO/NO-GO 阈值

### 定期审查

- 每季度审查 `safety_blacklist_patterns`（新的安全信号可能出现）
- 每次更新 LLM 模型后重新跑 Gold Standard 评估
- 新增大量文献后重新评估 BM25/embedding 参数
- 每次调整 Release Gate 阈值后重跑 Step8 验证 shortlist 变化
- 每次修改 Bootstrap CI 阈值后检查 confidence_tier 分布是否合理

---

## 7. 质量保障模块决策点 (2026-02-12 新增)

### 7.1 Release Gate 配置

| 参数 | 默认值 | 配置位置 | 判断依据 |
|------|-------|---------|---------|
| block_nogo | True | `ReleaseGateConfig.block_nogo` | 是否自动移除 NO-GO 药物。设为 False 允许 NO-GO 进入 shortlist (需人工审核) |
| min_go_ratio | 0.0 | `ReleaseGateConfig.min_go_ratio` | shortlist 中 GO 药物最低比例。设 0.5 要求至少一半是 GO |
| require_dual_review | True | `ReleaseGateConfig.require_dual_review` | 是否要求双人独立审核。正式 run 必须 True |
| min_irr_kappa | 0.60 | `ReleaseGateConfig.min_irr_kappa` | 审核者间一致性最低 Kappa 值。<0.6 说明评审标准不一致 |
| max_kill_rate | 0.20 | `ReleaseGateConfig.max_kill_rate` | 人审否决率上限。>20% 说明自动评分太松 |
| max_miss_rate | 0.15 | `ReleaseGateConfig.max_miss_rate` | 人审漏报率上限。>15% 说明自动评分太严 |
| strict | True | `ReleaseGateConfig.strict` | True=blocker 阻断输出, False=warn only |

### 7.2 Schema 强制执行 (ContractEnforcer)

| 参数 | 默认值 | 配置位置 | 判断依据 |
|------|-------|---------|---------|
| strict_contract | 1 | Step7/8/9 CLI `--strict_contract` | 1=列名不匹配即报错; 0=warn 继续。开发调试设 0，正式 run 必须设 1 |

### 7.3 Bootstrap CI 不确定性

| 参数 | 默认值 | 配置位置 | 判断依据 |
|------|-------|---------|---------|
| n_bootstrap | 1000 | `uncertainty.py bootstrap_ci()` | 重采样次数。5000 更精确但更慢 |
| ci | 0.95 | `uncertainty.py bootstrap_ci()` | 置信水平。0.99 更保守但 CI 更宽 |
| HIGH 阈值 | ci_width < 0.10 | `assign_confidence_tier()` | CI 宽度决定置信标签 |
| MEDIUM 阈值 | ci_width < 0.25 | `assign_confidence_tier()` | 0.10-0.25 为中等置信 |

**需要人工确认**: confidence_tier 阈值 (0.10/0.25) 是否合理？取决于你对"排名可信度"的期望。

### 7.4 数据泄漏审计

| 决策点 | 判断依据 |
|--------|---------|
| Drug overlap 是否可接受 | 训练/测试集有相同药物但不同疾病 → 通常可接受 |
| Disease overlap 是否可接受 | 相同疾病出现在训练/测试集 → 通常可接受 |
| Pair overlap 是否可接受 | 完全相同的 (drug, disease) 对 → **不可接受**，说明数据泄漏 |
| seen_drug_test_fraction 阈值 | 测试集中药物在训练集出现比例。>80% 可能意味着评估过于乐观 |
