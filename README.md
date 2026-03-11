# Drug Repurposing Platform

An end-to-end, explainable AI platform for systematic drug repurposing. Integrates transcriptomic signature reversal, knowledge graph reasoning, LLM-powered evidence extraction, clinical trial analysis, and pharmacovigilance data into a unified scoring framework with built-in novelty detection.

**37+ diseases supported** | **6-layer evidence fusion** | **Dual-route discovery** | **24/7 autonomous runner**

Validated on Rheumatoid Arthritis (expected 83% recall on 6 known drugs) and Atherosclerosis.

---

## Key Differentiators

| Feature | Description |
|---------|-------------|
| **6-layer evidence fusion** | Transcriptomic signatures + Knowledge Graph + PubMed literature + Clinical trials + FAERS safety + Phenotype enrichment -- more evidence sources than any published platform |
| **Explore track with KG waiver** | Novel candidates with no literature are rescued by strong genetic evidence (GWAS-backed KG paths), solving the core paradox of drug repurposing |
| **Dual-route cross-validation** | Direction A (cross-disease) and Direction B (origin reassessment) run in parallel; drugs endorsed by both routes have highest confidence |
| **Full-chain explainability** | Every GO/MAYBE/NO-GO decision is traceable: signature reversal score -> KG mechanism path -> PubMed evidence -> one-pager report |
| **Production-grade ops** | 24/7 continuous runner, per-step timeouts, lock management, automatic config discovery, retention policies |

---

## Architecture

```
  Disease Gene Signature                     Drug Perturbation Matching
  ┌─────────────────────┐                   ┌─────────────────────────┐
  │ dsmeta (GEO array)  │──┐               │      SigReverse         │
  │ ARCHS4 (RNA-seq)    │──┼── signature ──>│  LINCS/CMap 4-stage     │
  │ (auto-fallback)     │  │               │  + cell line weighting  │
  └─────────────────────┘  │               │  + mimicker rescue      │
                           │               └────────────┬────────────┘
                           │                            │
                           v                            v
  ┌──────────────────────────────────────────────────────────────────┐
  │                        KG Explain                                │
  │  Drug → Target → Pathway → Disease mechanistic paths             │
  │  + ChEMBL targets + Reactome pathways + OpenTargets associations │
  │  + FAERS safety (PRR) + Phenotype enrichment + Bootstrap CI      │
  │  + Target structure annotation (PDB / AlphaFold / UniProt)       │
  ├──────────────────────────────────────────────────────────────────┤
  │  Direction A: Cross-disease repurposing (signature-driven)       │
  │  Direction B: Origin disease reassessment (CT.gov-driven)        │
  └──────────────────────────────────────────────────────────────────┘
                           │
                           v
  ┌──────────────────────────────────────────────────────────────────┐
  │                    LLM+RAG Evidence Engine                       │
  │  Step 6: PubMed retrieval + BM25 ranking + LLM extraction       │
  │  Step 7: 5-dimensional scoring (0-100) + GO/MAYBE/NO-GO gating  │
  │          + Explore track (novelty-preserving rescue lane)        │
  │  Step 8: Fusion ranking + release gate + candidate pack          │
  │  Step 9: Validation plan (P1/P2/P3 priority tiers)              │
  └──────────────────────────────────────────────────────────────────┘
                           │
                           v
              A+B Cross-Validation → Human Review → Release
```

---

## Disease Coverage

### Cardiovascular (20 diseases)
Atherosclerosis, coronary artery disease, heart failure, hypertension, myocardial infarction, stroke, cardiomyopathy, myocarditis, abdominal aortic aneurysm, pulmonary arterial hypertension, atrial fibrillation, deep vein thrombosis, venous thromboembolism, angina pectoris, endocarditis, and more.

### Oncology (15 diseases) — `ops/disease_list_oncology.txt`
Prioritised by LINCS L1000 cell line coverage:

| Tier | Diseases | LINCS Cell Lines |
|------|----------|-----------------|
| T1 (core match) | TNBC, NSCLC, Melanoma, Prostate cancer | MCF7, A549, A375, PC3 |
| T2 (secondary) | HCC, Colorectal cancer, AML | HEPG2, HT29, HL60 |
| T3 (indirect) | Glioblastoma, Pancreatic cancer, Ovarian cancer | -- |
| T4 (KG-driven) | RCC, Gastric, HNSCC, Multiple myeloma, Cholangiocarcinoma | -- |

### Commercial Pipeline (17 diseases) — `ops/disease_list_commercial.txt`
- **Metabolic / Liver**: NASH, NAFLD, Metabolic syndrome
- **Autoimmune / Inflammatory**: SLE, Psoriasis, Crohn's disease, Ankylosing spondylitis
- **Neurodegeneration**: Alzheimer's, Parkinson's, ALS, Huntington's
- **Fibrosis**: IPF, Liver fibrosis, Renal fibrosis

---

## Scoring System

### KG V5 Score (mechanistic ranking)
```
final_score = mechanism_score
              × exp(-0.3 × safety_penalty - 0.2 × trial_penalty)
              × (1 + 0.1 × phenotype_boost)
```

| Component | Source | Formula |
|-----------|--------|---------|
| mechanism_score | Drug→Target→Pathway→Disease paths | Base DTPD score |
| safety_penalty | FAERS adverse events | `tanh(log1p(PRR)/5 × confidence × serious_weight)` |
| trial_penalty | Failed clinical trials | `0.1 × log1p(safety_stops) + 0.05 × log1p(eff_stops)` |
| phenotype_boost | Disease phenotype overlap | `avg_pheno × log1p(n_phenotype)` |

### LLM+RAG 5-Dimensional Score (0-100)

| Dimension | Range | Based on |
|-----------|-------|----------|
| Evidence Strength | 0-30 | Benefit/harm/neutral paper counts, PMID coverage |
| Mechanism Plausibility | 0-20 | KG paths + LLM-proposed mechanisms (log1p compressed) |
| Translatability | 0-20 | Clinical stage, human model availability |
| Safety Fit | 0-20 | FAERS AE profile, blacklist status |
| Practicality | 0-10 | Formulation, dosing feasibility |

### Gating: GO / MAYBE / NO-GO
- **GO**: total score >= 60
- **MAYBE**: 40 <= score < 60
- **NO-GO**: score < 40
- **Explore track**: High-novelty candidates (novelty >= 0.20) rescued from NO-GO to MAYBE, with KG evidence waiver for PMID requirement when `log1p(mechanism_score) >= 0.6`

### Novelty Score (0-1.0)
```
novelty = route_coverage (0-0.35) + cross_disease_hits (0-0.25)
        + LLM_mechanisms (0-0.20) + KG_genetic_evidence (0-0.30)
```

---

## Quick Start

### One-command setup
```bash
bash ops/start.sh setup          # Install all environments
bash ops/start.sh check          # Verify
```

### Run a single disease
```bash
bash ops/start.sh run atherosclerosis
bash ops/start.sh run atherosclerosis --mode dual    # Both directions
```

### Run oncology batch (cloud)
```bash
bash ops/start.sh start --list ops/disease_list_oncology.txt
```

### Run commercial diseases
```bash
bash ops/start.sh start --list ops/disease_list_commercial.txt
```

### Monitor
```bash
bash ops/start.sh status                  # Overview
bash ops/start.sh status --failures       # Only failures
bash ops/start.sh status atherosclerosis  # Single disease detail
```

---

## Sub-Projects

| Module | Purpose | Key Technology | Output |
|--------|---------|---------------|--------|
| `dsmeta_signature_pipeline` | GEO microarray meta-analysis | Random-effects DerSimonian-Laird | `disease_signature_meta.json` |
| `archs4_signature_pipeline` | ARCHS4 RNA-seq signature (auto-fallback) | DESeq2 + FDR-weighted meta | `disease_signature_meta.json` |
| `sigreverse` | LINCS/CMap reverse expression matching | 4-stage CMap + cell line weighting + mimicker rescue | `drug_reversal_rank.csv` |
| `kg_explain` | Knowledge graph construction + ranking | DTPD paths + FAERS + Bootstrap CI | `bridge_repurpose_cross.csv` |
| `LLM+RAG Evidence Engine` | Literature evidence + scoring + gating | PubMed RAG + Ollama qwen2.5:7b | `step8_shortlist_topK.csv` |

---

## Disease List Format

Pipe-separated, 4 columns:
```
disease_key|disease_query|origin_disease_ids(optional)|inject_yaml(optional)
```

Example:
```
atherosclerosis|atherosclerosis|EFO_0003914,MONDO_0021661|kg_explain/configs/inject_atherosclerosis.yaml
nsclc|non-small cell lung carcinoma|EFO_0003060|
```

### Adding a new disease
1. Add a line to your disease list file
2. Validate ARCHS4 keywords: `bash ops/start.sh check-keywords --list your_list.txt`
3. If keywords insufficient: add to `EXTRA_KEYWORDS` in `archs4_signature_pipeline/scripts/auto_generate_config.py`
4. Run: `bash ops/start.sh run your_disease`

---

## Execution Paths

### Direction A: Cross-Disease Repurposing (exploration)
```
signature (dsmeta/ARCHS4) → SigReverse → KG Explain → LLM+RAG Step 6-9
```
Scientific question: Can drugs from other diseases be repositioned to the target disease?

### Direction B: Origin Disease Reassessment (exploitation)
```
CT.gov failed trials → KG Explain → LLM+RAG Step 6-9
```
Scientific question: Are failed clinical trial drugs truly ineffective? Can they work with different endpoints or populations?

### Dual Mode (recommended for production)
Both directions run in parallel. `compare_ab_routes.py` identifies drugs endorsed by both routes (highest confidence).

---

## Key Outputs

| Priority | File | Content |
|----------|------|---------|
| ★★★ | `step8_shortlist_topK.csv` | Final drug candidates with targets, PDB/AlphaFold, docking readiness |
| ★★★ | `ab_comparison.csv` | Cross-validated drugs (both routes agree) |
| ★★ | `step8_fusion_rank_report.xlsx` | Excel report with per-drug sheets |
| ★★ | `step9_validation_plan.csv` | Validation plan (P1/P2/P3 priority) |
| ★ | `bridge_*.csv` | KG ranking with target structure details |
| ★ | `step7_gating_decision.csv` | GO/MAYBE/NO-GO decisions with scores |

Output directory: `runtime/results/<disease>/<date>/<run_id>/`

---

## Quality Assurance

| Feature | Description |
|---------|-------------|
| Bootstrap CI | 1000x resampling per drug-disease pair, HIGH/MEDIUM/LOW confidence tiers |
| Schema enforcement | Strict contract validation for Steps 6-9 outputs |
| Release gate | Automatic NO-GO blocking + GO ratio check |
| Audit log | Append-only SHA256 hash chain with tamper detection |
| Data leakage audit | Drug/disease/pair overlap detection between train/test |
| Human review | Kill rate, miss rate, IRR (Cohen's Kappa) computation |
| Monitoring alerts | Configurable threshold rules with JSONL dispatch |

---

## Environment Requirements

| Software | Purpose | Required for |
|----------|---------|-------------|
| Python 3.10+ | All modules | Everything |
| R 4.1+ | dsmeta signature pipeline | Direction A only |
| Ollama | LLM inference | LLM+RAG (Step 6) |

```bash
# Ollama models
ollama pull qwen2.5:7b-instruct
ollama pull nomic-embed-text
```

ARCHS4 data: `archs4_signature_pipeline/data/archs4/human_gene_v2.5.h5` (43GB, download from https://archs4.org)

---

## Test Suite

| Location | Tests | Scope |
|----------|-------|-------|
| `kg_explain/tests/` | 353 | KG construction, ranking, evaluation, governance |
| `LLM+RAG Evidence Engine/tests/` | 522 | Evidence extraction, scoring, gating, contracts |
| `sigreverse/tests/` | 302 | Signature reversal, drug scoring, fusion |
| `tests/integration/` | 12 | Cross-project schema compatibility |
| **Total** | **1189+** | |

---

## Documentation

| Document | Description |
|----------|-------------|
| [HOW_TO_RUN.md](HOW_TO_RUN.md) | Step-by-step operations manual (Chinese) |
| [RUN_REVIEW_CHECKLIST.md](RUN_REVIEW_CHECKLIST.md) | Post-run review checklist |
| [HUMAN_JUDGMENT_CHECKLIST.md](HUMAN_JUDGMENT_CHECKLIST.md) | Parameters requiring human judgment |
| [sigreverse/README.md](sigreverse/README.md) | SigReverse module documentation |
| [kg_explain/README.md](kg_explain/README.md) | KG Explain module documentation |
| [LLM+RAG Evidence Engine README](LLM+RAG%E8%AF%81%E6%8D%AE%E5%B7%A5%E7%A8%8B/README.md) | Evidence engine documentation |

---

## Technical Details

<details>
<summary>Data sources and APIs (click to expand)</summary>

| Source | Purpose | Module |
|--------|---------|--------|
| LINCS L1000 (LDP3 API) | Drug perturbation signatures | SigReverse |
| ChEMBL | Drug-target mapping, MoA | KG Explain |
| Reactome | Target-pathway relationships | KG Explain |
| OpenTargets (GraphQL) | Gene-disease associations | KG Explain, ARCHS4 |
| FAERS (openFDA) | Adverse event signals | KG Explain |
| PubMed (E-utilities) | Literature evidence | LLM+RAG |
| ClinicalTrials.gov (v2) | Trial outcomes | KG Explain, LLM+RAG |
| PubChem / UniChem | Drug name standardization | SigReverse |
| GEO / ARCHS4 | Disease gene expression | dsmeta, ARCHS4 pipeline |

</details>

<details>
<summary>SigReverse: Cell line weighting and mimicker rescue (click to expand)</summary>

LINCS L1000 data is ~60% cancer cell lines. Disease-specific cell line weights mitigate this bias:

| Config | Diseases | Strategy |
|--------|----------|----------|
| `cell_line_weights_autoimmune.csv` | RA, Lupus, Psoriasis, etc. | JURKAT=1.0, THP1=0.95, MCF7=0.25 |
| `cell_line_weights_atherosclerosis.csv` | Atherosclerosis, CAD | HUVEC=1.0, THP1=0.90, MCF7=0.30 |

**Mimicker rescue**: JAK inhibitors appear as "mimickers" in cancer cell lines (opposite transcriptomic effect). The rescue mechanism recovers these with a penalized positive score for autoimmune diseases.

Auto-routing in `sigreverse/scripts/run.py` detects disease type from name keywords and applies appropriate weights.

</details>

<details>
<summary>Non-coding RNA filter (click to expand)</summary>

ARCHS4 signature assembly (`04_assemble_signature.py`) filters pseudogenes, lncRNAs, IG/TCR segments, snoRNAs, and ENSEMBL-only IDs before top-N gene selection. This prevents non-coding genes from displacing druggable targets in disease signatures.

Filtered categories: `LINC*`, `MIR*`, `SNOR*`, `IGH*/IGK*/IGL*`, `TR[ABDG][VDJ]*`, `LOC*`, `RNA5S/18S/28S/45S`, ribosomal protein pseudogenes (`RPL*P`, `RPS*P`).

</details>

<details>
<summary>Environment variables (click to expand)</summary>

```bash
RUN_MODE=dual             # dual | origin_only | cross_only
SIG_PRIORITY=archs4       # archs4 | dsmeta
TOPN_PROFILE=stable       # stable | balanced | recall
STRICT_CONTRACT=1         # Schema enforcement
STEP_TIMEOUT=3600         # Per-step timeout (seconds)
RETENTION_DAYS=7          # Auto-cleanup for work/quarantine dirs
```

Module-level timeout overrides:
```bash
TIMEOUT_CROSS_ARCHS4=3600
TIMEOUT_CROSS_SIGREVERSE=3600
TIMEOUT_CROSS_KG_SIGNATURE=3600
TIMEOUT_LLM_STEP6=3600
TIMEOUT_LLM_STEP7=3600
TIMEOUT_LLM_STEP8=3600
TIMEOUT_LLM_STEP9=3600
```

</details>

---

## License

This project is proprietary. All rights reserved.
