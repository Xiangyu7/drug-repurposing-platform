# Drug Repurposing Platform — 人工决策参数清单

> 本文件列出管道中**需要人工判断**的可配置参数。
> 代码提供了默认值，但具体数值需要根据疾病和研究目标调整。

---

## 1. 签名构建 — 签名源选择与质量门控

### 1.1 签名源选择

| 参数 | 默认值 | 配置方式 | 判断依据 |
|------|-------|---------|---------|
| SIG_PRIORITY | `dsmeta` | 环境变量 | `dsmeta` = GEO meta 分析优先（适合有已知 GSE 的常见病）；`archs4` = ARCHS4 H5 搜索优先（适合罕见病、无 GSE 列表） |

### 1.2 ARCHS4 配置

| 参数 | 默认值 | 配置位置 | 判断依据 |
|------|-------|---------|---------|
| case_keywords | 疾病名 + 缩写 | `archs4_signature_pipeline/configs/<disease>.yaml` | 关键词太泛会混入无关样本；太窄会搜不到。换疾病必须重新定义 |
| control_keywords | normal, healthy, control | 同上 | 控制组关键词，通常不需要改 |
| min_samples_per_group | 3 | 同上 | 每组最少样本数，< 3 统计意义不足 |
| max_samples_per_group | 50 | 同上 | 防止单个大系列主导 meta 分析 |
| max_series | 5 | 同上 | 搜索的最大 GEO 系列数 |

### 1.3 签名质量门控

| 参数 | 默认值 | 配置位置 | 判断依据 |
|------|-------|---------|---------|
| 总量阈值 | total ≥ 30 | `runner.sh` + `cli.py` 硬编码 | < 30 基因的签名覆盖通路不足，Cross 路线不可靠 |
| 平衡阈值 | min(up, down) ≥ 10 | 同上 | 单方向 < 10 说明签名严重偏斜 |
| Tier 1 药物上限 | 80 | `cli.py` 硬编码 | 30-100 基因时收紧 KG 规模 |
| Tier 2 药物上限 | 200 | `KG_MAX_DRUGS_SIGNATURE` 环境变量 | ≥ 100 基因时的默认上限，可按需调整 |

### 1.4 dsmeta 配置

| 参数 | 默认值 | 配置位置 | 判断依据 |
|------|-------|---------|---------|
| gse_list | 手动指定 | `dsmeta_signature_pipeline/configs/<disease>.yaml` | 需要人工确认 GSE 数据集与疾病相关，可用 `auto_discover_geo.py` 辅助 |
| case/control regex | 手动指定 | 同上 | 正则匹配样本分组，错误分组会导致签名方向反转 |
| top_n 签名基因数 | 300 (每方向) | 同上 `signature.top_n` | 300 是 LINCS CMap 的推荐值 |
| min_sign_concordance | 0.8 | 同上 `meta.min_sign_concordance` | 方向一致性要求，低于此值的基因被排除 |

---

## 2. kg_explain — 知识图谱

| 参数 | 默认值 | 配置位置 | 判断依据 |
|------|-------|---------|---------|
| Pathway-Disease 关联强度阈值 | config YAML | `config.yaml → pathway_score_threshold` | 过高漏掉弱但真实的通路；过低引入噪声 |
| Hub penalty lambda | config YAML | `config.yaml → hub_penalty_lambda` | 惩罚高连接度节点。太大漏掉真实 hub，太小被噪声淹没 |
| 严重不良事件关键词 | `serious_ae_keywords` | `config.yaml → serious_ae_keywords` | 哪些 AE 算"严重"，需临床药理学判断。**换疾病需重新定义** |
| topk_paths_per_pair | config YAML | `config.yaml → topk_paths_per_pair` | 每对 drug-disease 保留多少条通路 |
| Trial status 过滤 | TERMINATED, WITHDRAWN, SUSPENDED | `config.yaml → trial_statuses` | 哪些试验状态视为安全信号 |

---

## 3. LLM+RAG — 证据工程

### 3.1 打分维度与权重

| 维度 | 满分 | 判断依据 |
|------|------|---------|
| Evidence Strength | 30 | 文献数量阈值: high=10, med=5, low=2。需根据疾病文献丰富度调整 |
| Mechanism Plausibility | 20 | 基于 PMID 数量和一致性，50/20/5 篇为断点 |
| Translatability | 20 | 研究活跃度 + benefit 证据，50/20/10/5 篇为断点 |
| Safety Fit | 20 | harm_penalty_per_paper=1.0, 黑名单惩罚=6.0 |
| Practicality | 10 | 仅基于 PMID 数量，50/20/10/5 为断点 |

**需要确认**: 5 个维度的满分比例 (30/20/20/20/10) 是否反映你的优先级偏好？

### 3.2 GO/NO-GO 门控

| 参数 | 默认值 | 配置位置 | 判断依据 |
|------|-------|---------|---------|
| GO 阈值 | 60.0 | `GatingConfig.go_threshold` | 总分 ≥ 60 推进到验证阶段 |
| MAYBE 阈值 | 40.0 | `GatingConfig.maybe_threshold` | 40-60 进入观察队列 |
| 最少 benefit 论文 | 2 | `GatingConfig.min_benefit_papers` | 硬门控：少于 2 篇 → NO-GO |
| 最少总 PMID | 3 | `GatingConfig.min_total_pmids` | 硬门控：证据不足 → NO-GO |
| 最大 harm 比例 | 0.5 | `GatingConfig.max_harm_ratio` | 硬门控：> 50% harm → NO-GO |
| 安全黑名单 | warfarin, dexamethasone 等 | `ScoringConfig.safety_blacklist_patterns` | **换疾病必须重新定义** |

### 3.3 LLM 证据提取

| 参数 | 默认值 | 判断依据 |
|------|-------|---------|
| 幻觉检测 — 机制锚定阈值 | 30% | LLM 机制描述中至少 30% 的词需出现在原文 |
| Confidence 映射 | HIGH=0.9, MED=0.5, LOW=0.2 | 置信度标签 → 数值映射 |
| 重试温度序列 | [0.2, 0.1, 0.0] | 提取失败时降温重试 |

### 3.4 文献检索

| 参数 | 默认值 | 判断依据 |
|------|-------|---------|
| PubMed 最大检索量 | 120 篇/查询 | 太少遗漏文献；太多增加 LLM 成本 |
| BM25 topK | 80 | 初筛保留量 |
| Semantic rerank topK | 40 | 语义重排后保留量 |
| Max evidence docs | 12 | 最终送入 LLM 的文献数 |

---

## 4. 质量保障

### 4.1 Release Gate

| 参数 | 默认值 | 判断依据 |
|------|-------|---------|
| block_nogo | True | 是否自动移除 NO-GO 药物 |
| require_dual_review | True | 正式 run 必须 True（双人独立审核） |
| min_irr_kappa | 0.60 | < 0.6 说明评审标准不一致 |
| max_kill_rate | 0.20 | > 20% 说明自动评分太松 |
| max_miss_rate | 0.15 | > 15% 说明自动评分太严 |
| strict_contract | 1 | 列名不匹配即报错，调试时可设 0 |

### 4.2 Bootstrap CI

| 参数 | 默认值 | 判断依据 |
|------|-------|---------|
| n_bootstrap | 1000 | 重采样次数，5000 更精确但更慢 |
| ci | 0.95 | 置信水平 |
| HIGH 阈值 | ci_width < 0.10 | CI 窄 = 排名稳定 |
| MEDIUM 阈值 | ci_width < 0.25 | 0.10-0.25 为中等置信 |

---

## 5. 换疾病时必须改的参数

| 参数 | 位置 | 原因 |
|------|------|------|
| `case_keywords` | ARCHS4 config YAML | 疾病关键词不同 |
| `gse_list` + `regex_rules` | dsmeta config YAML | GEO 数据集不同 |
| `safety_blacklist_patterns` | LLM ScoringConfig | 高风险药物因疾病而异 |
| `serious_ae_keywords` | KG config YAML | 严重 AE 定义因疾病而异 |

其他参数（打分权重、门控阈值、检索参数等）通常不需要改，但如果疾病文献量差异很大（如罕见病 vs 常见病），可能需要调低文献数量断点。

---

## 6. 跨模块一致性

调参时确保以下一致性：

1. **Confidence 编码**: 全管道使用相同的 HIGH/MED/LOW → 数值映射
2. **药物名称归一化**: kg_explain 和 LLM+RAG 使用相同的 canonical name
3. **安全黑名单同步**: kg_explain 的 `serious_ae_keywords` 和 LLM+RAG 的 `safety_blacklist_patterns` 应覆盖相同的药物
