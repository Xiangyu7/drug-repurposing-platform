# Drug Repurposing Platform

Explainable AI platform for systematic drug repurposing via knowledge graph construction, literature evidence extraction, and multi-dimensional scoring.

**Target disease**: Atherosclerosis (extensible to any disease via config)

---

## Architecture Overview

```
  dsmeta_signature_pipeline         archs4_signature_pipeline
  (GEO microarray meta-analysis)    (ARCHS4 RNA-seq, 备选A路签名源)
          |                                   |
          v                                   v
      sigreverse                     disease_signature_meta.json
  (LINCS/CMap matching)              (如dsmeta失败时自动回退)
          |                                   |
          +-----------------------------------+
          v
+------------------------------------------------------------------+
|                         kg_explain                                |
|  CT.gov -> RxNorm -> ChEMBL -> Targets -> Pathways -> Diseases   |
|  + FAERS safety + Phenotype enrichment + Bootstrap CI            |
|  + 靶点结构标注 (PDB/AlphaFold/UniProt)                          |
|  Output: drug_disease_rank.csv                                |
|          + bridge_repurpose_cross.csv  (Direction A: cross-disease)|
+------------------------------------------------------------------+
                               |
              +----------------+----------------+
              v                                 v
   generate_disease_bridge.py        bridge_repurpose_cross.csv
   → bridge_origin_reassess.csv      (Direction A: 跨疾病迁移)
   (Direction B: 原疾病重评估)
              |                                 |
              v                                 v
+------------------------------------------------------------------+
|                      LLM+RAG Evidence Engine                     |
|  Step6: PubMed retrieval + LLM extraction                        |
|  Step7: 5-dim scoring + GO/MAYBE/NO-GO gating                   |
|  Step8: Release Gate + candidate pack + docking就绪列            |
|         + alphafold_structure_id 列 (即使有PDB也展示AF ID)        |
|  Step9: Validation plan                                          |
|                                                                  |
|  Two parallel tracks:                                            |
|    step6-9_repurpose_cross/    (Direction A)                     |
|    step6-9_origin_reassess/    (Direction B)                     |
+------------------------------------------------------------------+
                               |
                               v
                   A+B Cross-Validation (compare_ab_routes.py)
                   → ab_comparison.csv (两路线重叠药物 = 高可信)
                               |
                               v
                    Human Review + Release Decision
```

---

## Data Flow (10-Step Pipeline)

```
Step 1  CT.gov API         Fetch terminated/failed trials for target disease
   |
Step 2  RxNorm API         Normalize drug names (brand -> generic -> canonical)
   |
Step 3  Canonical merge    Deduplicate, split combos (aspirin+ticagrelor -> 2 rows)
   |
Step 4  ChEMBL API         Drug -> Target (mechanism of action, pref_name__iexact first)
   |                       Salt forms -> parent molecule via molecule_hierarchy
   |
Step 5  Ensembl + UniProt  Gene ID cross-references (Ensembl -> UniProt -> ChEMBL)
   |
Step 6  Reactome API       Target -> Pathway relationships
   |
Step 7  OpenTargets GQL    Gene -> Disease associations + Disease -> Phenotype links
   |                       Filter non-disease traits (GO_/MP_ prefixes)
   |
Step 8  FAERS API          Drug adverse event signals (PRR-filtered, serious AE weighted)
   |
Step 9  Edge construction  Build all KG edges + dtpd_paths.jsonl
   |
Step 10 V5 Ranking         final_score = mechanism * exp(-w1*safety - w2*trial) * (1 + w3*log1p(phenotype))
   |                       + Bootstrap CI per pair + bridge_repurpose_cross.csv
   |
Step 10b generate_disease_bridge.py → bridge_origin_reassess.csv (原疾病重评估)
   v
Step 6' PubMed + LLM      BM25 retrieval -> Ollama qwen2.5:7b structured extraction
   |                       (direction / model / endpoint / mechanism / confidence per PMID)
   |
Step 7' Score & Gate       5-dim scoring (0-100) -> GO/MAYBE/NO-GO + Explore track
   |                       ContractEnforcer validates all schemas
   |
Step 8' Candidate Pack     ReleaseGate blocks NO-GO -> shortlist CSV + Excel + one-pagers
   |
Step 9' Validation Plan    Priority tiers (P1/P2/P2E/P3) + stop/go criteria + timeline
```

---

## Sub-Projects

| Sub-project | Purpose | Key output |
|-------------|---------|------------|
| `dsmeta_signature_pipeline` | GEO microarray meta-analysis -> disease gene signature | `disease_signature_meta.json` |
| `archs4_signature_pipeline` | ARCHS4 RNA-seq 替代签名源 (dsmeta 失败时自动回退) | `disease_signature_meta.json` |
| `sigreverse` | LINCS/CMap reverse expression matching -> drug reversal scores | `drug_reversal_rank.csv` |
| `kg_explain` | Knowledge graph construction + mechanistic ranking + 靶点结构标注 | `drug_disease_rank.csv` + bridge CSVs |
| `LLM+RAG Evidence Engine` | Literature evidence extraction + scoring + gating + docking就绪评估 | `step8_shortlist_topK.csv` |
| `ops/compare_ab_routes.py` | Direction A+B 交叉验证 (两路线重叠药物提取) | `ab_comparison.csv` |

---

## Execution Paths

### Direction A: Cross-Disease Repurposing（跨疾病迁移，探索型）
```
dsmeta/archs4 -> sigreverse -> kg_explain(signature) -> LLM+RAG Step6-9
  签名源优先级: dsmeta (GEO microarray) > archs4 (ARCHS4 RNA-seq) > OT-only (仅OpenTargets基因)
  科学问题: 其他疾病的药物能否重新定位到目标疾病？
```

### Direction B: Origin Disease Reassessment（原疾病重评估，稳健型）
```
screen_drugs(CT.gov) -> kg_explain(ctgov) -> generate_disease_bridge.py -> LLM+RAG Step6-9
  科学问题: 失败的临床试验药物是否真的无效？换终点/人群能否翻盘？
```

### Dual Mode: A+B Parallel（推荐生产模式）
```
同时跑 Direction A + Direction B
  → 最后用 compare_ab_routes.py 做 A+B 交叉验证
  → 两路线都推荐的药物 = 最高可信度
```

### Path C: Evidence Only（仅文献证据）
```
LLM+RAG Step6-9 (with existing drug pool CSV)
```

---

## 24/7 Continuous Runner (Dual Route)

Use start.sh as the single entry point:

```bash
# Run a single disease (recommended first test)
bash ops/start.sh run atherosclerosis

# Start production pipeline (background)
bash ops/start.sh start

# Check status
bash ops/check_status.sh
```

`LOCK_NAME` is mode-scoped by default (`dual` / `origin_only`) so two processes can run in parallel without lock conflicts.

### Disease list format (`ops/disease_list.txt`)

Pipe-separated 4 columns:

```text
disease_key|disease_query|origin_disease_ids(optional)|inject_yaml(optional)
```

Examples:

```text
atherosclerosis|atherosclerosis|EFO_0003914,MONDO_0021661|kg_explain/configs/inject_atherosclerosis.yaml
type2_diabetes|type 2 diabetes|EFO_0001360|
heart_failure|heart failure||
```

Disease lists (internal, managed by start.sh):

- `ops/internal/disease_list_day1_dual.txt` (GEO-ready diseases for dual mode)
- `ops/internal/disease_list_day1_origin.txt` (all CV diseases for origin mode)
- `ops/disease_list.txt` (master template)
- `ops/disease_list_test.txt` (minimal test set)

### Cross input path auto-discovery

The runner no longer requires `signature_meta_path` / `sigreverse_input_path` in disease list.
For each `disease_key`, it resolves:

1. `dsmeta_signature_pipeline/outputs/<disease_key>/signature/disease_signature_meta.json`
2. `dsmeta_signature_pipeline/outputs/<disease_key>/signature/sigreverse_input.json`
3. fallback to legacy `dsmeta_signature_pipeline/outputs/signature/*.json`

### Hard gate on KG manifest

After each KG pipeline run, runner checks:

- `kg_explain/output/pipeline_manifest.json`
- `drug_source` matches expected route (`signature` for cross, `ctgov` for origin)
- no `step_timings[].status == "error"`

If any check fails, current disease is marked failed and skipped, then runner continues to next disease.

### Runtime directories

- Working runs: `runtime/work/<disease_key>/<run_id>/`
- Final deliverables: `runtime/results/<disease_key>/<YYYY-MM-DD>/<run_id>/`
- Failure isolation: `runtime/quarantine/<disease_key>/<run_id>/`

### Retention and cleanup

- `runtime/work` and `runtime/quarantine` are cleaned by `RETENTION_DAYS` (default `7`).
- `runtime/results` is not auto-deleted.
- `kg_explain/output/dtpd_paths.jsonl` is deleted only after origin bridge is generated and archived.

### Environment defaults

```bash
STRICT_CONTRACT=1
TOPN_PROFILE=stable       # stable | balanced | recall
TOPN_CROSS=auto
TOPN_ORIGIN=auto
TOPN_STAGE2_ENABLE=1
RETENTION_DAYS=7
RUN_MODE=dual             # dual | origin_only | cross_only
```

`topn` policy semantics:
- `TOPN_ORIGIN/TOPN_CROSS=auto`: score-driven budget control (recommended)
- Stage1: compute `n50` then clamp by route bounds, and enforce `topn >= topk + 2`
- Stage2: trigger only when shortlist quality fails; expand by `score >= 0.30 * top_score`; max one round
- Manual compatibility: `TOPN_ORIGIN/TOPN_CROSS=<int>` still works (`<=0` means full bridge rows)

### Start.sh (recommended entry point)

```bash
# First-time setup
bash ops/start.sh setup

# Run single disease (foreground, recommended for testing)
bash ops/start.sh run atherosclerosis

# Run both directions for a disease
bash ops/start.sh run atherosclerosis --mode dual

# Start production pipeline (background)
bash ops/start.sh start

# Check environment only
bash ops/start.sh check
```

> **Note:** Legacy scripts (`runner.sh`, etc.) have been moved to `ops/internal/` and are called internally by `start.sh`. You should not need to invoke them directly.

---

## Ops Toolchain (2026-02-16)

Tools for automating the GEO dataset discovery and pipeline configuration workflow.

### GEO Auto-Discovery

Searches NCBI GEO for suitable expression datasets, auto-detects case/control groups, and generates candidate dsmeta configs. Pure rule-based (no LLM needed).

```bash
# Single disease
python ops/internal/auto_discover_geo.py --disease "heart failure" --write-yaml --out-dir ops/internal/geo_curation

# Batch mode (all 15 CV diseases)
python ops/internal/auto_discover_geo.py --batch ops/internal/disease_list_day1_origin.txt --write-yaml --out-dir ops/internal/geo_curation
```

Output per disease:
- `ops/internal/geo_curation/<disease>/candidates.tsv` — all candidate GSE with scores
- `ops/internal/geo_curation/<disease>/selected.tsv` — top-K selected
- `ops/internal/geo_curation/<disease>/candidate_config.yaml` — ready-to-review dsmeta config
- `ops/internal/geo_curation/<disease>/discovery_log.txt` — detailed search report
- `ops/internal/geo_curation/<disease>/route_recommendation.txt` — Direction A/B route advice

**Route recommendation** (auto-generated per disease):

| GSE Found | Route | Meaning |
|-----------|-------|---------|
| 0 | `DIRECTION_B_ONLY` | No GEO data — skip Direction A, use Direction B only |
| 1 | `DIRECTION_A_LOW_CONFIDENCE` | Can run but single-study, no cross-validation |
| 2-3 (high conf) | `DIRECTION_A_GOOD` | Meta-analysis viable with cross-study validation |
| 2-3 (mixed conf) | `DIRECTION_A_MODERATE` | Viable but review case/control regex carefully |
| ≥4 | `DIRECTION_A_IDEAL` | Robust meta-analysis with strong cross-study validation |

Batch mode also outputs `batch_summary.tsv` with a `route_recommendation` column for all diseases.

### Batch Config Generation

Reads auto-discovery results and generates final dsmeta YAML configs.
Only diseases with **≥2 GSE and no TODO placeholders** are added to the dual disease list.

```bash
python ops/internal/generate_dsmeta_configs.py \
    --geo-dir ops/internal/geo_curation \
    --config-dir dsmeta_signature_pipeline/configs \
    --update-disease-list
```

### Adding a New Disease to Direction A

```bash
# 1. Auto-discover GEO datasets (~1 min)
python ops/internal/auto_discover_geo.py --disease "heart failure" --write-yaml --out-dir ops/internal/geo_curation

# 2. Check route recommendation
cat ops/internal/geo_curation/heart_failure/route_recommendation.txt

# 3. AI-assisted review: feed discovery_log.txt to LLM for quality check
cat ops/internal/geo_curation/heart_failure/discovery_log.txt
#    → Give this to Claude/ChatGPT and ask: "review these GSE selections for heart failure"

# 4. Apply review: edit YAML to remove disqualified GSEs
#    If <2 GSE remain → remove disease from ops/internal/disease_list_day1_dual.txt

# 5. Generate config + update disease list
python ops/internal/generate_dsmeta_configs.py --geo-dir ops/internal/geo_curation --config-dir dsmeta_signature_pipeline/configs --update-disease-list

# 6. Validate (runs dsmeta Step 1-2)
bash ops/start.sh check --mode dual

# 7. Restart runner
bash ops/start.sh start --mode dual
```

**AI Review Checklist** — what to ask the LLM to check in `discovery_log.txt`:

| Check | Disqualify if... | Example |
|-------|-------------------|---------|
| Data type | Not standard mRNA expression profiling | 16S rRNA, exosomal circRNA, tRNA-derived small RNA |
| Disease match | GSE studies a different disease than intended | PAH datasets in a "hypertension" search |
| Tissue/cell type | Uses cell lines or non-disease-relevant cells | Fibroblasts instead of cardiac tissue |
| Case/control logic | Regex matches wrong groups | Comparing two subtypes instead of disease vs healthy |
| Duplicates | Same dataset appears twice (with/without GSE prefix) | GSE48060 and '48060' |
| Study design | Not a case-control transcriptomics study | Potency test assay, drug treatment experiment |

### Ops File Reference

**用户可见（ops/）**:

| File | Purpose |
|------|---------|
| `ops/start.sh` | **唯一入口** — setup, single-disease run, production launch |
| `ops/check_status.sh` | Pipeline status dashboard — per-disease detail, failures, Ollama health, disk |
| `ops/show_results.sh` | 查看/导出结果 |
| `ops/compare_ab_routes.py` | Direction A+B 交叉验证: 输出 ab_comparison.csv |
| `ops/disease_list.txt` | 疾病列表模板 |
| `ops/disease_list_test.txt` | 最小测试列表 (2 diseases) |

**底层脚本（ops/internal/）— 由 start.sh 内部调用**:

| File | Purpose |
|------|---------|
| `ops/internal/runner.sh` | 24/7 continuous runner (dual/origin modes) |
| `ops/internal/env_guard.py` | 环境预检 + 自动修复 |
| `ops/internal/topn_policy.py` | TopN 自动调控策略 |
| `ops/internal/auto_discover_geo.py` | GEO auto-discovery |
| `ops/internal/generate_dsmeta_configs.py` | dsmeta config generator |
| `ops/internal/cleanup.sh` | 磁盘空间清理 |
| `ops/internal/retry_disease.sh` | 重试失败疾病 |
| `ops/internal/restart_runner.sh` | 停止/重启 runner |
| `ops/internal/disease_list_day1_*.txt` | 按模式分的疾病子列表 |

### Pipeline Status Monitoring

Run `bash ops/check_status.sh` at any time to see what's happening:

```bash
# Overview table of all diseases + status
bash ops/check_status.sh

# Deep-dive into a single disease
bash ops/check_status.sh coronary_artery_disease

# Only show failures
bash ops/check_status.sh --failures

# Check Ollama service + models + inference test
bash ops/check_status.sh --ollama

# Disk usage breakdown
bash ops/check_status.sh --disk

# All checks at once
bash ops/check_status.sh --all
```

---

## Quality Assurance Features

| Feature | Module | Description |
|---------|--------|-------------|
| **Uncertainty Quantification** | `rankers/uncertainty.py` | Bootstrap CI (1000x resampling) per drug-disease pair, HIGH/MEDIUM/LOW tiers |
| **Data Leakage Audit** | `evaluation/leakage_audit.py` | Drug/disease/pair overlap detection between train/test splits |
| **Temporal Validation** | `evaluation/temporal_split.py` | Time-based split (pre/post cutoff year) with integrated leakage audit |
| **External Benchmark** | `evaluation/external_benchmarks.py` | Hetionet CtD gold standard + multi-metric (MRR/MAP/AUROC/Hit@K/P@K/NDCG@K) |
| **Schema Enforcement** | `contracts_enforcer.py` | Strict/soft mode schema validation for Steps 6-9 outputs |
| **Release Gate** | `scoring/release_gate.py` | NO-GO blocking + GO ratio check + human review quality gate (kill/miss rate, IRR) |
| **Audit Log** | `common/audit_log.py` | Append-only SHA256 hash chain, tamper detection |
| **Model Registry** | `governance/registry.py` | Config hash + data hash + metrics snapshot per model version |
| **Quality Gate** | `governance/quality_gate.py` | Metric threshold enforcement + regression tolerance check |
| **Human Review** | `evaluation/human_review.py` | Kill rate, miss rate, IRR (Cohen's Kappa) computation |
| **Stratified Sampling** | `evaluation/stratified_sampling.py` | Balanced review queue across score tiers and gate decisions |
| **Monitoring Alerts** | `monitoring/alerts.py` | Configurable threshold rules with JSONL dispatch |

---

## File Reference

### kg_explain/src/kg_explain/

#### Core
| File | Description |
|------|-------------|
| `cli.py` | Command-line interface, pipeline orchestration with timing |
| `config.py` | YAML config loader with inheritance (base -> disease -> version) |
| `cache.py` | HTTP response caching with stats and retry logic |
| `graph.py` | NetworkX KG layer: graph build, path enumeration, GraphML export |
| `orchestrator.py` | DAG pipeline orchestrator with hash-based step skipping |
| `utils.py` | File I/O, concurrent execution, NaN-safe serialization |

#### Datasources (`datasources/`)
| File | Description |
|------|-------------|
| `ctgov.py` | ClinicalTrials.gov API v2: fetch terminated/failed trials by disease |
| `rxnorm.py` | RxNorm API: drug name normalization (brand -> generic -> canonical) |
| `chembl.py` | ChEMBL API: drug -> target mapping, MoA, parent molecule hierarchy |
| `reactome.py` | Reactome API: protein -> pathway relationships |
| `opentargets.py` | OpenTargets GraphQL: gene-disease associations, disease-phenotype links |
| `faers.py` | FDA FAERS API: adverse event signals with PRR filtering |
| `signature.py` | Gene signature driver for cross-disease drug discovery |

#### Builders (`builders/`)
| File | Description |
|------|-------------|
| `edges.py` | Edge construction: gene-pathway, pathway-disease, trial-AE relations |

#### Rankers (`rankers/`)
| File | Description |
|------|-------------|
| `base.py` | Shared scoring utilities (hub penalty, path aggregation) |
| `dtpd.py` | DTPD 基础路径评分: Drug→Target→Pathway→Disease (被 ranker 内部调用) |
| `ranker.py` | **完整排名器**: DTPD 路径 + FAERS 安全惩罚 + 表型加成 + Bootstrap CI + 靶点结构标记 |
| `uncertainty.py` | Bootstrap CI (1000x): `bootstrap_ci()`, `assign_confidence_tier()`, `add_uncertainty_to_ranking()` |

> v1 (Drug-Disease 直连)、v2 (Drug-Target-Disease)、v4 (v3+Evidence Pack) 已删除。v5 包含全部功能。

#### Evaluation (`evaluation/`)
| File | Description |
|------|-------------|
| `metrics.py` | Hit@K, MRR, P@K, AP, NDCG@K, AUROC computation |
| `benchmark.py` | Gold-standard benchmark runner + report formatter (with CI + leakage sections) |
| `external_benchmarks.py` | Hetionet CtD edge download + EFO mapping + benchmark |
| `temporal_split.py` | Time-based train/test split with integrated leakage audit |
| `leakage_audit.py` | Drug/disease/pair overlap audit between splits |

#### Governance (`governance/`)
| File | Description |
|------|-------------|
| `quality_gate.py` | Metric threshold enforcement + regression tolerance check |
| `registry.py` | Model version registry (config hash, data hash, metrics) |
| `regression.py` | Regression test suite with fixed input/output fixtures |

---

### LLM+RAG Evidence Engine (`LLM+RAG证据工程/src/dr/`)

#### Core
| File | Description |
|------|-------------|
| `config.py` | Environment configuration (API keys, model names, paths) |
| `contracts.py` | Data contract definitions + validators for Steps 6-9 schemas |
| `contracts_enforcer.py` | Schema enforcement wrapper: strict (raise) / soft (warn) modes |
| `logger.py` | Unified logging configuration |

#### Evidence (`evidence/`)
| File | Description |
|------|-------------|
| `extractor.py` | LLM-based structured extraction from PubMed abstracts (direction/model/endpoint/mechanism/confidence) |
| `ollama.py` | Ollama client: qwen2.5:7b for generation, nomic-embed-text for embeddings |
| `ranker.py` | BM25 retrieval ranker for literature pre-filtering |

#### Retrieval (`retrieval/`)
| File | Description |
|------|-------------|
| `pubmed.py` | PubMed E-utilities: ESearch + EFetch, rate limiting, 4-layer caching |
| `ctgov.py` | ClinicalTrials.gov API v2: trial metadata + outcome extraction |
| `cache.py` | Unified 4-layer cache manager |

#### Scoring (`scoring/`)
| File | Description |
|------|-------------|
| `scorer.py` | 5-dimensional drug scorer: Evidence(0-30) + Mechanism(0-20) + Translatability(0-20) + Safety(0-20) + Practicality(0-10) = Total(0-100) |
| `gating.py` | Gating engine: hard gates (disqualify) + soft gates (thresholds) -> GO/MAYBE/NO-GO + Explore track |
| `cards.py` | Hypothesis card builder (JSON + Markdown with evidence summaries) |
| `validation.py` | Validation plan generator for GO/MAYBE candidates |
| `aggregator.py` | Trial-to-drug aggregation with canonicalization + fuzzy matching |
| `release_gate.py` | Release gate: NO-GO blocking, GO ratio, human review quality (kill/miss rate, IRR Kappa) |

#### Evaluation (`evaluation/`)
| File | Description |
|------|-------------|
| `gold_standard.py` | Gold-standard management for extraction accuracy evaluation |
| `metrics.py` | Evaluation metrics (precision, recall, F1 per extraction field) |
| `annotation.py` | Inter-Annotator Agreement: Cohen's Kappa, confusion matrices |
| `human_review.py` | Human review metrics: kill rate, miss rate, IRR, accuracy calibration |
| `stratified_sampling.py` | Stratified sampling across score tiers and gate decisions |

#### Common (`common/`)
| File | Description |
|------|-------------|
| `file_io.py` | Atomic file I/O (read/write consistency) |
| `hashing.py` | Content integrity hashing |
| `http.py` | HTTP helpers with retry |
| `provenance.py` | Run provenance manifests (git hash, input/output hashes, config snapshot) |
| `text.py` | Text normalization (canonicalize_name, safe_filename) |
| `audit_log.py` | Append-only audit log with SHA256 hash chain, tamper detection |

#### Monitoring (`monitoring/`)
| File | Description |
|------|-------------|
| `metrics.py` | Prometheus-compatible metrics for pipeline monitoring |
| `alerts.py` | Configurable alert rules with JSONL dispatch |

---

### Pipeline Scripts (`LLM+RAG证据工程/scripts/`)

| Script | Description |
|--------|-------------|
| `step0_build_pool.py` | Build initial drug pool from CT.gov (trial -> drug level aggregation) |
| `step1_3_fetch_failed_drugs.py` | Fetch failed/terminated drugs from CT.gov seed list |
| `step4_label_trials.py` | Label trials with conditions and outcomes |
| `step5_normalize_drugs.py` | Drug name normalization |
| `step6_evidence_extraction.py` | PubMed retrieval + LLM structured extraction -> dossier JSONs |
| `step7_score_and_gate.py` | 5-dim scoring + gating (ContractEnforcer validates outputs) |
| `step8_candidate_pack.py` | ReleaseGate -> shortlist CSV + Excel (含靶点结构表) + one-pager Markdown (含靶点/PDB) |
| `step9_validation_plan.py` | Priority tiers + stop/go criteria + timeline (ContractEnforcer validates) |
| `eval_extraction.py` | Evaluate extraction accuracy vs gold standard |
| `screen_drugs.py` | Filter/screen drugs by configurable criteria |
| `screen_drugs_extended.py` | Extended screening with additional filters |
| `build_reject_audit_queue.py` | Build audit queue for rejected candidates (anti-miss review) |

---

### dsmeta_signature_pipeline/

| File | Description |
|------|-------------|
| `run.py` | Pipeline orchestrator with step caching + manifest generation |
| `scripts/02b_probe_to_gene.py` | Microarray probe -> gene ID mapping |
| `scripts/04_rank_aggregate.py` | Multi-dataset rank aggregation |
| `scripts/05_fetch_genesets.py` | Biological gene set download |
| `scripts/07_pathway_meta.py` | Pathway-level meta-analysis |
| `scripts/08_make_signature_json.py` | Generate disease signature JSON |
| `scripts/09_make_report.py` | Analysis report generation |

---

### archs4_signature_pipeline/ (2026-02-21 新增)

ARCHS4 RNA-seq 替代签名管线，在 dsmeta (GEO microarray) 失败时自动回退使用。

| File | Description |
|------|-------------|
| `run.py` | 5步管线编排器 (Step1-4 + meta)，含步骤缓存 + manifest |
| `scripts/01_opentargets_prior.py` | Step1: OpenTargets 疾病关联基因 (先验知识) |
| `scripts/02_archs4_select.py` | Step2: 从 human_gene_v2.4.h5 中检索疾病相关 GEO series |
| `scripts/03_de_analysis.R` | Step3: DESeq2 差异表达分析 (per-series) |
| `scripts/03b_meta_effects.R` | Step3b: 随机效应 meta-analysis (cross-series) |
| `scripts/04_assemble_signature.py` | Step4: OT 先验 × DE 结果 → top300 up + top300 down 签名 |
| `scripts/generate_test_h5.py` | 生成测试用小型 H5 文件 (0.3MB，用于本地调试) |
| `scripts/auto_generate_config.py` | 自动生成疾病配置 YAML |
| `configs/*.yaml` | 17个心血管疾病配置文件 |

**数据依赖**: 需要 `data/archs4/human_gene_v2.4.h5` (43GB，从 ARCHS4 官网下载)。
测试时可用 `generate_test_h5.py` 生成 0.3MB 替代文件。

**输出**: `outputs/<disease>/signature/disease_signature_meta.json` (与 dsmeta 格式完全兼容)

---

### sigreverse/sigreverse/

| File | Description |
|------|-------------|
| `cmap_algorithms.py` | LINCS/CMap connectivity score algorithms |
| `ldp3_client.py` | L1000 CDS2 API client |
| `scoring.py` | Signature matching and drug scoring |
| `fusion.py` | Multi-signature fusion and aggregation |
| `dose_response.py` | Dose-response relationship analysis |
| `drug_standardization.py` | Drug name standardization |
| `robustness.py` | Prediction robustness analysis |
| `statistics.py` | Statistical inference utilities |
| `qc.py` | Quality control and validation |
| `cache.py` | API response caching |
| `io.py` | File I/O operations |

---

### Test Suite

| Location | Tests | Scope |
|----------|-------|-------|
| `kg_explain/tests/` | 335 | KG construction, ranking, evaluation, governance |
| `LLM+RAG证据工程/tests/` | 501 | Evidence extraction, scoring, gating, contracts, monitoring |
| `tests/integration/` | 12 | Cross-project schema compatibility, end-to-end data flow |
| **Total** | **848+** | **All passing** |

Run all tests:
```bash
# KG pipeline
cd kg_explain && python -m pytest tests/ -v

# LLM+RAG pipeline
cd "LLM+RAG证据工程" && python -m pytest tests/ -v

# Cross-project integration
python -m pytest tests/integration/ -v
```

---

## Environment Setup

### kg_explain
```bash
cd kg_explain
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
mkdir -p data output cache
```

### LLM+RAG Evidence Engine
```bash
cd "LLM+RAG证据工程"
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Ollama (required for Step6)
ollama serve
ollama pull qwen2.5:7b-instruct
ollama pull nomic-embed-text
```

### dsmeta_signature_pipeline
```bash
cd dsmeta_signature_pipeline
mamba env create -f environment.yml
conda activate dsmeta
```

### sigreverse
```bash
cd sigreverse
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

---

## Quick Start (Atherosclerosis)

### One-Command Start (recommended)
```bash
# Fast verification (foreground, origin route)
bash ops/start.sh run atherosclerosis

# Full A+B routes for one disease (foreground)
bash ops/start.sh run atherosclerosis --mode dual

# Disable auto-repair if you only want strict check-and-stop
bash ops/start.sh run atherosclerosis --no-auto-repair
```

### Full Pipeline (Path A)
```bash
# 1. Disease signature
cd dsmeta_signature_pipeline
python run.py --config configs/athero_example.yaml

# 2. Reverse expression matching
cd ../sigreverse
python scripts/run.py \
  --config configs/default.yaml \
  --in ../dsmeta_signature_pipeline/outputs/signature/sigreverse_input.json \
  --out data/output_atherosclerosis

# 3. KG mechanistic ranking
cd ../kg_explain
python -m src.kg_explain.cli pipeline \
  --disease atherosclerosis --version v5 \
  --drug-source signature \
  --signature-path ../dsmeta_signature_pipeline/outputs/signature/disease_signature_meta.json

# 4. Evidence extraction + scoring + gating
cd "../LLM+RAG证据工程"
# Direction A: Cross-disease repurposing
python scripts/step6_evidence_extraction.py \
  --rank_in ../kg_explain/output/bridge_repurpose_cross.csv \
  --neg data/poolA_negative_drug_level.csv \
  --out output/step6_repurpose_cross --target_disease atherosclerosis --topn 50

python scripts/step7_score_and_gate.py \
  --input output/step6_repurpose_cross --out output/step7_repurpose_cross --strict_contract 1

python scripts/step8_candidate_pack.py \
  --step7_dir output/step7_repurpose_cross --outdir output/step8_repurpose_cross \
  --target_disease atherosclerosis --topk 5 \
  --include_explore 1 --strict_contract 1

python scripts/step9_validation_plan.py \
  --step8_dir output/step8_repurpose_cross --step7_dir output/step7_repurpose_cross \
  --outdir output/step9_repurpose_cross --target_disease atherosclerosis

# Direction B: Origin disease reassessment
cd ../kg_explain
python scripts/generate_disease_bridge.py \
  --disease atherosclerosis \
  --inject configs/inject_atherosclerosis.yaml \
  --out output/bridge_origin_reassess.csv

cd "../LLM+RAG证据工程"
python scripts/step6_evidence_extraction.py \
  --rank_in ../kg_explain/output/bridge_origin_reassess.csv \
  --neg data/poolA_negative_drug_level.csv \
  --out output/step6_origin_reassess --target_disease atherosclerosis --topn 83

python scripts/step7_score_and_gate.py \
  --input output/step6_origin_reassess --out output/step7_origin_reassess --strict_contract 1

python scripts/step8_candidate_pack.py \
  --step7_dir output/step7_origin_reassess --outdir output/step8_origin_reassess \
  --target_disease atherosclerosis --topk 10 --include_explore 1

python scripts/step9_validation_plan.py \
  --step8_dir output/step8_origin_reassess --step7_dir output/step7_origin_reassess \
  --outdir output/step9_origin_reassess --target_disease atherosclerosis
```

### Key Outputs
| File | Content |
|------|---------|
| `kg_explain/output/drug_disease_rank.csv` | Ranked drug-disease pairs with CI columns |
| `kg_explain/output/bridge_repurpose_cross.csv` | Direction A: cross-disease repurposing bridge (含靶点 + 结构来源) |
| `kg_explain/output/bridge_origin_reassess.csv` | Direction B: origin disease reassessment bridge (含靶点 + 结构来源) |
| `kg_explain/output/evidence_pack/*.json` | Per-pair evidence packs |
| `LLM+RAG/output/step7_repurpose_cross/` | Direction A: GO/MAYBE/NO-GO decisions |
| `LLM+RAG/output/step7_origin_reassess/` | Direction B: GO/MAYBE/NO-GO decisions |
| `LLM+RAG/output/step8_*/step8_shortlist_topK.csv` | Final shortlist (含靶点/UniProt/PDB/AlphaFold + docking就绪字段) |
| `LLM+RAG/output/step8_*/step8_candidate_pack_from_step7.xlsx` | Excel 候选报告 (每药 Sheet 含靶点结构表) |
| `LLM+RAG/output/step9_*/step9_validation_plan.csv` | Prioritized validation plan |

### Bridge CSV 靶点列 (2026-02-16 新增)

Bridge 文件新增两列，用于分子对接准备:
- **`targets`**: 人类可读靶点摘要，格式: `TargetName (CHEMBL_ID) [UniProt:ACCESSION] [PDB+AlphaFold] — MoA`
- **`target_details`**: JSON 数组，每个靶点包含:
  - `target_chembl_id`, `target_name`, `mechanism_of_action`, `uniprot`
  - `pdb_ids` (top 5 实验 PDB ID), `pdb_count` (PDB 条目总数)
  - `has_alphafold` (是否有 AlphaFold 预测)
  - `structure_source`: `PDB+AlphaFold` | `PDB` | `AlphaFold_only` | `none`

### Step8 Docking 就绪列 (2026-02-17 新增, 2026-02-21 更新)

`step8_shortlist_topK.csv` 在保留 `targets`/`target_details` 的同时新增:
- `docking_primary_target_chembl_id`, `docking_primary_target_name`, `docking_primary_uniprot`
- `docking_primary_structure_source`, `docking_primary_structure_provider`, `docking_primary_structure_id`
- `alphafold_structure_id`: **AlphaFold 结构 ID** (格式 `AF-{UniProt}-F1`，即使有PDB也会展示)
- `docking_backup_targets_json` (默认主靶1 + 备选2)
- `docking_feasibility_tier`: `READY_PDB` | `READY_AF` | `LIMITED` | `BLOCKED`
- `docking_target_selection_score` (0-1), `docking_risk_flags`, `docking_policy_version`

**AlphaFold ID 来源说明**: ChEMBL API 返回靶点交叉引用 (`target_component_xrefs`)，
若 `xref_src_db == "AlphaFoldDB"` 则标记 `has_alphafold=True`，
然后按 AlphaFold 官方命名规则拼接: `AF-{UniProt}-F1`。

默认策略: `PDB优先 + AlphaFold回退`，无PDB时不阻断流程，仅打降级标记。

### 跑完一轮后检查清单

**第一优先 — 看结论:**
1. 打开 `step8_candidate_pack_from_step7.xlsx` → Shortlist sheet → 确认候选药数量和 gate 分布
2. 看每个药的 Sheet → 检查靶点结构表 (Structure Source 列)
   - `PDB+AlphaFold` → 可直接做分子对接，选实验 PDB
   - `AlphaFold_only` → 对接结果需谨慎解读
3. 看 `step8_shortlist_topK.csv` 的 docking 列
   - 优先筛选 `docking_feasibility_tier=READY_PDB`
   - 对 `AF_FALLBACK/NO_STRUCTURE` 使用 `docking_risk_flags` 做降级处理
4. 看 `step9_validation_plan.csv` → P1 优先级的药重点关注
5. 对比 cross vs origin 两条路线 → 有重叠候选药 = 更可信

**第二优先 — 判断可信度:**
- `step7_cards.json` → 每药 GO/MAYBE/NO-GO 决策 + 5 维打分
- `step8_one_pagers_topK.md` → 候选药 Markdown 报告 (含靶点/PDB 链接)
- `bridge_*.csv` → KG 排名 + 靶点信息

**第三优先 — 排查问题:**
- `drug_disease_rank.csv` → 某药排名高/低的原因
- `poolA_drug_level.csv` → CT.gov 拉到了哪些药
- `manual_review_queue.csv` → 需人工确认的试验
- `step6 dossiers/*.json` → PubMed 证据原文
- `run_summary.json` → 运行是否有步骤失败

---

## Human Review & Release

1. Run pipeline (Steps 6-9), all outputs include `*_manifest.json` for provenance
2. `ReleaseGate` auto-blocks any NO-GO drugs from shortlist
3. `ContractEnforcer` validates all output schemas (strict mode = fail on violation)
4. Reviewer A/B independently review top-K candidates
5. Compute IAA (Cohen's Kappa) via `evaluation/annotation.py`
6. Record adjudication via `adjudication_template.md`
7. Final RELEASE/NO-RELEASE decision via `release_decision_template.md`

Templates: `LLM+RAG证据工程/docs/quality/`

---

## V5 Scoring Formula

```
final_score = mechanism_score
              * exp(-w1 * safety_penalty - w2 * trial_penalty)
              * (1 + w3 * log1p(n_phenotype))
```

| Component | Source | Weight |
|-----------|--------|--------|
| `mechanism_score` | V3 path aggregation (Drug-Target-Pathway-Disease) | base |
| `safety_penalty` | FAERS AE signals (PRR filtered, serious AE 2x weighted) | w1=0.3 |
| `trial_penalty` | Failed trials (safety stop 0.1, efficacy stop 0.05 per trial) | w2=0.2 |
| `phenotype_boost` | Disease phenotype count (log1p, max 10) | w3=0.1 |

After scoring, Bootstrap CI is computed per pair (1000x resampling of evidence path scores).

---

## Scoring Dimensions (LLM+RAG, 0-100)

| Dimension | Range | Based on |
|-----------|-------|----------|
| Evidence Strength | 0-30 | Unique PMIDs, benefit/harm ratio, confidence scores |
| Mechanism Plausibility | 0-20 | Known targets, pathway coverage, MoA clarity |
| Translatability | 0-20 | Clinical stage, human model availability |
| Safety Fit | 0-20 | AE profile, blacklist status, therapeutic window |
| Practicality | 0-10 | Formulation, dosing feasibility, IP landscape |

---

## Configuration

Main config: `kg_explain/configs/versions/v5.yaml`

Config inheritance: `base.yaml` -> `diseases/atherosclerosis.yaml` -> `versions/v5.yaml`

Key parameters:
- `safety_penalty_weight`: 0.3 (FAERS weight)
- `trial_failure_penalty`: 0.2 (failed trial weight)
- `phenotype_overlap_boost`: 0.1 (phenotype enrichment weight)
- `topk_pairs_per_drug`: 50
- `topk_paths_per_pair`: 10
- `faers.min_prr`: PRR signal threshold

---

## Documentation

| Document | Location |
|----------|----------|
| This README | `README.md` |
| How to Run (详细启动指南) | `HOW_TO_RUN.md` |
| User Guide (中文用户手册) | `USER_GUIDE.md` |
| Project Overview (中文 HTML) | `项目全览.html` |
| Human Review Checklist | `HUMAN_JUDGMENT_CHECKLIST.md` |
| LLM+RAG Module | `LLM+RAG证据工程/README.md` |
| KG Pipeline | `kg_explain/README.md` |
| Signature Pipeline (GEO) | `dsmeta_signature_pipeline/README.md` |
| ARCHS4 Pipeline (RNA-seq) | `archs4_signature_pipeline/` (见本 README archs4 部分) |
| Sigreverse | `sigreverse/README.md` |
| Quality Templates | `LLM+RAG证据工程/docs/quality/README.md` |
| Ops Toolchain | `ops/start.sh`, `ops/auto_discover_geo.py`, `ops/generate_dsmeta_configs.py` |
