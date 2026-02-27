# LLM+RAG证据工程 — 步骤索引

## 数据流

```
seed_nct_list.csv
  → step0  → poolA_trials.csv
  → step1-3 → poolA_trials_labeled.csv
  → step4  → step4_final_trial_labels.csv
  → step5  → drug_master.csv + poolA_drug_level.csv
  → step6  → dossiers/ + step6_rank_v2.csv
  → step7  → hypothesis_cards + gating_decision
  → step8  → step8_shortlist_topK.csv + fusion_rank_report.xlsx
  → step9  → step9_validation_plan.csv
```

## 步骤说明

| Step | 脚本 | 输入 | 输出 | 说明 |
|------|------|------|------|------|
| 0 | step0_build_pool.py | seed_nct_list.csv | poolA_trials.csv | 从种子 NCT 构建试验池 |
| 1-3 | step1_fetch_trial_drugs.py | poolA_trials.csv | poolA_trials_labeled.csv | 获取失败药物信息 |
| 4 | step4_label_trials.py | poolA_trials.csv | step4_final_trial_labels.csv | AI+人工标注试验结果 |
| 5 | step5_normalize_drugs.py | step4 输出 | drug_master.csv, poolA_drug_level.csv | 药物归一化和聚合 |
| 6 | step6_evidence_extraction.py | poolA_drug_level.csv | dossiers/ + step6_rank_v2.csv | PubMed RAG + LLM 证据提取 |
| 7 | step7_score_and_gate.py | step6 dossiers | hypothesis_cards, gating_decision | 评分和门控决策 |
| 8 | step8_fusion_rank.py | step7 输出 + bridge CSV | step8_shortlist_topK.csv (含靶点+docking就绪列) + fusion_rank_report.xlsx (含靶点结构表) + step8_manifest.json | 打包候选药物 (含靶点/PDB/AlphaFold + docking优先级) |
| 9 | step9_validation_plan.py | step8 输出 | step9_validation_plan.csv + step9_manifest.json | 验证计划生成 |

## Step 6 子步骤（核心）

Step 6 是 pipeline 中最复杂的步骤，内部包含多个子步骤：

| 子步骤 | 模块 | 说明 |
|--------|------|------|
| 6.1 PubMed 多路检索 | `src/dr/retrieval/` + `step6` | 多路 query（exact disease / endpoint+mechanism / cross-disease）→ ESearch → EFetch |
| 6.2 BM25 粗排 | `src/dr/evidence/ranker.py` (BM25Ranker) | TF-IDF 词频匹配，快速筛出 top-80 相关文献 |
| 6.3 Route 融合 + Embedding 精排 | `src/dr/evidence/ranker.py` | 各 route BM25 列表先做 RRF 融合，再做 embedding rerank，选出 top-30 |
| 6.4 LLM 证据提取 | `src/dr/evidence/extractor.py` | Ollama LLM 逐篇分析，提取方向/置信度/机制 |
| 6.5 幻觉检测 | `src/dr/evidence/extractor.py` (detect_hallucination) | 检查 PMID 一致性、药物锚定、机制合理性 |
| 6.6 汇总评分 | step6 脚本内 | 聚合证据 → 计算 confidence → 生成 dossier |

## 工具脚本

| 脚本 | 说明 |
|------|------|
| eval_extraction.py | 评估 LLM 提取质量（支持 holdout split 防过拟合） |
| build_reject_audit_queue.py | 从 Step7 NO-GO/MAYBE-explore 生成人工复核队列（反漏检） |
| test_e2e_sample.py | 端到端验证测试（3 个样本药物） |

## 模块化代码 (`src/dr/`)

| 模块 | 说明 |
|------|------|
| `common/` | 通用工具（日志、HTTP、文件 IO、缓存、验证） |
| `retrieval/` | PubMed 检索（ESearch + EFetch） |
| `evidence/ranker.py` | 排序器（BM25、Hybrid、CrossEncoder、RankingPipeline） |
| `evidence/extractor.py` | LLM 证据提取（重试、JSON 修复、幻觉检测） |
| `scoring/` | 综合评分 |
| `evaluation/` | Gold standard 评估框架 |
| `monitoring/` | Prometheus 指标监控 |
| `config.py` | 配置 dataclass（RankConfig, ExtractorConfig） |
