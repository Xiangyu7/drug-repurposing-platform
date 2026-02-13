# Drug Repurposing Platform

Explainable AI platform for systematic drug repurposing via knowledge graph construction, literature evidence extraction, and multi-dimensional scoring.

**Target disease**: Atherosclerosis (extensible to any disease via config)

---

## Architecture Overview

```
                        dsmeta_signature_pipeline
                        (GEO meta-analysis)
                               |
                               v
                           sigreverse
                       (LINCS/CMap matching)
                               |
                               v
+------------------------------------------------------------------+
|                         kg_explain                                |
|  CT.gov -> RxNorm -> ChEMBL -> Targets -> Pathways -> Diseases   |
|  + FAERS safety + Phenotype enrichment + Bootstrap CI            |
|  Output: drug_disease_rank_v5.csv                                |
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
|  Step8: Release Gate + candidate pack                            |
|  Step9: Validation plan                                          |
|                                                                  |
|  Two parallel tracks:                                            |
|    step6-9_repurpose_cross/    (Direction A)                     |
|    step6-9_origin_reassess/    (Direction B)                     |
+------------------------------------------------------------------+
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
Step 9  Edge construction  Build all KG edges + evidence_paths_v3.jsonl
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
| `dsmeta_signature_pipeline` | GEO multi-dataset meta-analysis -> disease gene signature | `disease_signature_meta.json` |
| `sigreverse` | LINCS/CMap reverse expression matching -> drug reversal scores | `drug_reversal_rank.csv` |
| `kg_explain` | Knowledge graph construction + mechanistic ranking | `drug_disease_rank_v5.csv` |
| `LLM+RAG Evidence Engine` | Literature evidence extraction + scoring + gating | `step8_shortlist_topK.csv` |

---

## Execution Paths

### Path A: Full Pipeline (recommended)
```
dsmeta -> sigreverse -> kg_explain(signature) -> LLM+RAG Step6-9
```

### Path B: Skip Gene Signatures
```
kg_explain(ctgov) -> LLM+RAG Step6-9
```

### Path C: Evidence Only
```
LLM+RAG Step6-9 (with existing drug pool CSV)
```

### Path D: Origin Disease Reassessment
```
kg_explain V3/V5 → generate_disease_bridge.py → bridge_origin_reassess.csv → LLM+RAG Step6-9
(Reassess whether failed drugs are truly ineffective for their original target disease)
Output: step6/7/8/9_origin_reassess/
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
| `v1.py` | Ranker v1: Drug-Disease direct association (CT.gov conditions) |
| `v2.py` | Ranker v2: Drug-Target-Disease (ChEMBL + OpenTargets) |
| `v3.py` | Ranker v3: Drug-Target-Pathway-Disease (+ Reactome) |
| `v4.py` | Ranker v4: v3 + Evidence Pack (for RAG consumption) |
| `v5.py` | Ranker v5: Full explainable paths + FAERS safety + Phenotype + Bootstrap CI |
| `uncertainty.py` | Bootstrap CI (1000x): `bootstrap_ci()`, `assign_confidence_tier()`, `add_uncertainty_to_ranking()` |

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
| `step8_candidate_pack.py` | ReleaseGate -> shortlist CSV + Excel + one-pager Markdown |
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
| `kg_explain/output/drug_disease_rank_v5.csv` | Ranked drug-disease pairs with CI columns |
| `kg_explain/output/bridge_repurpose_cross.csv` | Direction A: cross-disease repurposing bridge |
| `kg_explain/output/bridge_origin_reassess.csv` | Direction B: origin disease reassessment bridge |
| `kg_explain/output/evidence_pack_v5/*.json` | Per-pair evidence packs |
| `LLM+RAG/output/step7_repurpose_cross/` | Direction A: GO/MAYBE/NO-GO decisions |
| `LLM+RAG/output/step7_origin_reassess/` | Direction B: GO/MAYBE/NO-GO decisions |
| `LLM+RAG/output/step8_*/step8_shortlist_topK.csv` | Final shortlist (Release Gate filtered) |
| `LLM+RAG/output/step9_*/step9_validation_plan.csv` | Prioritized validation plan |

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
| User Guide | `USER_GUIDE.md` |
| Human Review Checklist | `HUMAN_JUDGMENT_CHECKLIST.md` |
| LLM+RAG Module | `LLM+RAG证据工程/README.md` |
| KG Pipeline | `kg_explain/README.md` |
| Signature Pipeline | `dsmeta_signature_pipeline/README.md` |
| Sigreverse | `sigreverse/README.md` |
| Quality Templates | `LLM+RAG证据工程/docs/quality/README.md` |
