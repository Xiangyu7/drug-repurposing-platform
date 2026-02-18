# Drug Repurposing Platform -- How to Run

## 1. Project Overview

```
Drug Repurposing/
  ├── dsmeta_signature_pipeline/   # Direction A: GEO gene signature generation
  ├── sigreverse/                  # Direction A: LINCS L1000 signature reversal
  ├── kg_explain/                  # Both A & B: Knowledge Graph + V5 Ranking
  ├── LLM+RAG证据工程/              # Both A & B: PubMed RAG + LLM evidence extraction
  ├── ops/                         # Operations: runner scripts, disease lists, configs
  └── runtime/                     # Auto-generated: work dirs, results, logs
```

### Two Research Directions

| | Direction A (Cross-Disease) | Direction B (Origin-Disease) |
|---|---|---|
| Scientific Question | Can drugs from other diseases be repurposed? | Should failed trial drugs be re-evaluated? |
| Pipeline | dsmeta -> SigReverse -> kg_explain(signature) -> LLM Step6-9 | screen_drugs(CT.gov) -> kg_explain(ctgov) -> LLM Step6-9 |
| Style | Exploration (high risk, high reward) | Exploitation (low risk, robust) |

---

## 2. Prerequisites

| Software | Required By | Install |
|---|---|---|
| Python 3.10+ | All | `brew install python` / `apt install python3` |
| R 4.1+ | Direction A (dsmeta) | See below |
| Ollama | LLM+RAG | `curl -fsSL https://ollama.com/install.sh \| sh` |
| git | All | pre-installed on most systems |

### Cloud Server (Ubuntu/Debian) One-Line Setup

```bash
# 1. Install system dependencies (Python venv + R + Bioconductor)
sudo apt update && sudo apt install -y python3.10-venv r-base r-base-dev \
    libcurl4-openssl-dev libxml2-dev libssl-dev

# 2. Install R/Bioconductor packages (needed for Direction A / dsmeta)
sudo Rscript -e 'install.packages("BiocManager", repos="https://cloud.r-project.org"); BiocManager::install(c("limma","GEOquery","Biobase","affy","fgsea"))'

# 3. Install all Python venvs + pip packages (4 modules)
bash ops/quickstart.sh --setup-only

# 4. Verify everything
bash ops/quickstart.sh --check-only
```

> **Note:** If you only need Direction B (origin_only), skip steps 1-2 (R is not required).

### Mac (Homebrew) Setup

```bash
brew install python r
bash ops/quickstart.sh --setup-only
```

### Hardware Recommendations

| | Minimum | Recommended |
|---|---|---|
| RAM | 8 GB | 16+ GB |
| Disk | 10 GB | 50+ GB (GEO data is large) |
| GPU | Not required | Helps Ollama speed |

---

## 3. One-Command Quick Start

```bash
# Clone the repo and cd into it
cd /path/to/Drug\ Repurposing

# Check environment prerequisites
bash ops/quickstart.sh --check-only

# Install all dependencies (creates venvs, installs pip packages)
bash ops/quickstart.sh --setup-only

# Full flow: check -> install -> GEO discovery -> launch pipeline
bash ops/quickstart.sh

# Run a single disease only
bash ops/quickstart.sh --single atherosclerosis

# Run only Direction B (origin)
bash ops/quickstart.sh --mode origin_only
```

---

## 4. Step-by-Step Manual Setup

### 4.1 kg_explain

```bash
cd kg_explain
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# Test installation
.venv/bin/python -m pytest tests/ -x -q
```

### 4.2 sigreverse

```bash
cd sigreverse
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# Or install as a package
.venv/bin/pip install -e .
```

### 4.3 dsmeta_signature_pipeline

**Option A: quickstart (recommended)**

`bash ops/quickstart.sh --setup-only` handles this automatically. It creates the venv and installs all Python dependencies.

**Option B: manual setup**
```bash
cd dsmeta_signature_pipeline
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

**R dependencies (required for Direction A):**
```bash
# Ubuntu/Debian
sudo apt install -y r-base r-base-dev libcurl4-openssl-dev libxml2-dev libssl-dev
sudo Rscript -e 'install.packages("BiocManager", repos="https://cloud.r-project.org"); BiocManager::install(c("limma","GEOquery","Biobase","affy","fgsea"))'

# Additional CRAN packages (optional, for full dsmeta pipeline)
sudo Rscript -e 'install.packages(c("data.table","optparse","ggplot2","jsonlite","yaml","metafor","RobustRankAggreg"), repos="https://cloud.r-project.org")'
```

**Option C: conda (includes R + Bioconductor in one command)**
```bash
cd dsmeta_signature_pipeline
mamba env create -f environment.yml
conda activate dsmeta
```

### 4.4 LLM+RAG

```bash
cd LLM+RAG证据工程
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# Copy and edit config
cp .env.example .env
# Edit .env: fill in NCBI_API_KEY (optional but recommended)
```

### 4.5 Ollama (for LLM+RAG)

```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Pull required models
ollama pull qwen2.5:7b-instruct
ollama pull nomic-embed-text

# Verify
ollama list
```

---

## 5. Running Individual Pipelines

### 5.1 KG Pipeline (kg_explain)

```bash
cd kg_explain

# Run full pipeline for a single disease
.venv/bin/python -m src.kg_explain.cli pipeline --disease atherosclerosis --version v5

# Or use the shell script
bash scripts/run_pipeline.sh atherosclerosis v5

# Subcommands available:
.venv/bin/python -m src.kg_explain.cli rank --disease atherosclerosis --version v5
.venv/bin/python -m src.kg_explain.cli benchmark --disease atherosclerosis --version v5
```

**Output:** `kg_explain/output/<disease>/rank_v5.csv`, `evidence_pack/`, `bridge_*.csv`

### 5.2 dsmeta Signature Pipeline

```bash
cd dsmeta_signature_pipeline

# Full pipeline (step 1-9)
python run.py --config configs/atherosclerosis.yaml

# Partial run (e.g., only step 3-9, skip GEO download)
python run.py --config configs/atherosclerosis.yaml --from-step 3 --to-step 9

# Steps:
#   1. GEO download
#   2. Sample labeling
#   3. Differential expression (R/limma)
#   4. QC checks
#   5. Gene set fetch (Reactome, WikiPathways)
#   6. GSEA (R/fgsea)
#   7. Meta-analysis (R/metafor)
#   8. Rank aggregation
#   9. Output gene signature JSON
```

**Output:** `dsmeta_signature_pipeline/outputs/<disease>/signature/`

### 5.3 SigReverse (LINCS L1000)

```bash
cd sigreverse

# SigReverse is called automatically by the runner (run_24x7_all_directions.sh)
# Manual run:
.venv/bin/python -m sigreverse.cli \
  --signature ../dsmeta_signature_pipeline/outputs/<disease>/signature/sigreverse_input.json \
  --out-dir output/<disease>/
```

### 5.4 LLM+RAG Evidence Engineering

```bash
cd LLM+RAG证据工程

# Make sure Ollama is running
ollama serve &

# Step 6: PubMed RAG + LLM evidence extraction
.venv/bin/python scripts/step6_evidence_extraction.py \
  --input data/step6_rank.csv \
  --output-dir data/step6_output \
  --disease atherosclerosis

# Step 7: 5-dimension scoring + gating (GO/MAYBE/NO-GO)
.venv/bin/python scripts/step7_score_and_gate.py \
  --dossiers data/step6_output \
  --scores data/step7_scores.csv \
  --gating data/step7_gating.csv

# Step 8: Release gate + candidate pack (Excel)
.venv/bin/python scripts/step8_candidate_pack.py \
  --step7_dir data/ \
  --bridge data/step6_rank.csv \
  --outdir data/step8_output

# Step 9: Validation plan generation
.venv/bin/python scripts/step9_validation_plan.py \
  --step8_dir data/step8_output \
  --outdir data/step9_output
```

**Output:** `data/step8_output/` (Excel), `data/step9_output/` (validation plans)

---

## 6. Running the Full Automated Pipeline

### 6.1 Disease List Format

Files in `ops/`:

```
# disease_key|disease_query|origin_disease_ids(optional)|inject_yaml(optional)
atherosclerosis|atherosclerosis|EFO_0003914,MONDO_0021661|kg_explain/configs/inject_atherosclerosis.yaml
heart_failure|heart failure|EFO_0003144|
stroke|stroke|EFO_0000712|
```

Three lists provided:
- `disease_list_day1_dual.txt` -- diseases for both Direction A+B (7 diseases)
- `disease_list_day1_origin.txt` -- all diseases for Direction B (15 diseases)
- `disease_list_b_only.txt` -- diseases only needing Direction B (9 diseases)

### 6.2 Cloud Server (Aliyun/AWS) -- Parallel

```bash
# Start both dual + origin runners in parallel (background)
bash ops/start_day1_aliyun.sh

# Monitor
tail -f logs/day1_aliyun/dual_*.log
tail -f logs/day1_aliyun/origin_*.log
```

### 6.3 Mac M1 (16GB) -- Serial (saves memory)

```bash
# Serial: first dual (7 diseases), then B-only (9 diseases)
# Estimated ~13 hours total
bash ops/start_m1_serial.sh              # background
bash ops/start_m1_serial.sh --foreground # foreground (see output)
bash ops/start_m1_serial.sh --dry-run    # preview only
```

### 6.4 Key Environment Variables

```bash
# Example: customize and launch
export RUN_MODE=dual                # dual | origin_only | cross_only
export MAX_CYCLES=1                 # 0=infinite loop, 1=run once
export SLEEP_SECONDS=300            # interval between cycles
export TOPN_PROFILE=stable          # stable | balanced | recall
export STEP_TIMEOUT=1800            # per-step timeout (seconds)
export DSMETA_CLEANUP=1             # 1=cleanup work dir after dsmeta
bash ops/run_24x7_all_directions.sh ops/disease_list_day1_dual.txt
```

---

## 7. Monitoring & Debugging

```bash
# Overall status dashboard
bash ops/check_status.sh

# Single disease detail
bash ops/check_status.sh atherosclerosis

# Only failures
bash ops/check_status.sh --failures

# Check Ollama status
bash ops/check_status.sh --ollama

# Check disk usage
bash ops/check_status.sh --disk

# All checks
bash ops/check_status.sh --all

# Tail live logs
tail -f logs/continuous_runner/runner_dual_*.log

# Track progress markers (shows which step each disease is on)
grep "PROGRESS" logs/continuous_runner/runner_dual_*.log

# View results for a specific disease
bash ops/show_results.sh atherosclerosis

# View all diseases with results
bash ops/show_results.sh
```

---

## 8. Operations Scripts

### 8.1 Retry a Failed Disease

```bash
# Retry Direction B (default)
bash ops/retry_disease.sh atherosclerosis

# Retry both directions
bash ops/retry_disease.sh atherosclerosis --mode dual

# Retry Direction A only
bash ops/retry_disease.sh atherosclerosis --mode cross_only

# Skip cleanup of previous artifacts
bash ops/retry_disease.sh atherosclerosis --no-clean
```

### 8.2 View Results

```bash
# List all diseases with results
bash ops/show_results.sh

# Show detail for one disease
bash ops/show_results.sh atherosclerosis

# Copy results to another directory
bash ops/show_results.sh atherosclerosis --copy /tmp/export
```

### 8.3 Cleanup Disk Space

```bash
# Preview what would be cleaned (dry run)
bash ops/cleanup.sh --dry-run --all 7

# Clean everything older than 7 days
bash ops/cleanup.sh --all 7

# Clean only work directories older than 3 days
bash ops/cleanup.sh --work 3

# Clean only quarantine older than 14 days
bash ops/cleanup.sh --quarantine 14

# Clean only logs older than 30 days
bash ops/cleanup.sh --logs 30
```

### 8.4 Restart / Stop Runners

```bash
# Stop all runners
bash ops/restart_runner.sh --stop

# Restart a specific runner
bash ops/restart_runner.sh --runner dual

# Restart all active runners
bash ops/restart_runner.sh
```

---

## 9. Output Structure

```
runtime/
  ├── results/<disease>/
  │   ├── direction_a/          # Direction A results
  │   │   ├── dsmeta/           # Gene signatures
  │   │   ├── sigreverse/       # Signature reversal scores
  │   │   ├── kg/               # KG ranking (signature mode)
  │   │   └── llm/              # LLM evidence (step6-9)
  │   └── direction_b/          # Direction B results
  │       ├── kg/               # KG ranking (ctgov mode)
  │       └── llm/              # LLM evidence (step6-9)
  ├── work/<disease>/           # Intermediate files (auto-cleaned)
  ├── quarantine/<disease>/     # Failed runs moved here
  └── state/                    # PID files, lock files
```

---

## 10. Common Issues

| Problem | Cause | Solution |
|---|---|---|
| `No module named 'xxx'` | venv not activated / not created | Run `bash ops/quickstart.sh --setup-only` |
| `python3-venv not available` | Missing system package | `sudo apt install python3.10-venv` (auto-detected by quickstart) |
| R packages missing | dsmeta needs Bioconductor | `sudo Rscript -e 'BiocManager::install(c("limma","GEOquery","fgsea"))'` |
| R not found | Direction A needs R | `sudo apt install -y r-base r-base-dev` |
| Ollama connection refused | Ollama not running | `ollama serve &` then `ollama list` |
| Ollama slow (~5min/drug) | CPU-only inference | Normal for 7B model on CPU; use GPU if available |
| WikiPathways 404 | GMT download URL outdated | Auto-skipped; Reactome still works |
| step8 ValueError all NO-GO | All candidates gated out | Expected when test data has irrelevant drugs |
| Disk full | GEO data accumulates | Set `DSMETA_CLEANUP=1` or `RETENTION_DAYS=7` |
| API rate limit | NCBI/ChEMBL throttling | Auto-retried; set `NCBI_API_KEY` in `.env` |
| Permission denied on .sh | Scripts not executable | `chmod +x ops/*.sh` |

---

## 11. Running Tests

```bash
# kg_explain tests (287 tests)
cd kg_explain && .venv/bin/python -m pytest tests/ -x -q

# dsmeta tests
cd dsmeta_signature_pipeline && python -m pytest tests/ -x -q

# LLM+RAG tests
cd LLM+RAG证据工程 && .venv/bin/python -m pytest tests/ -x -q
```

---

## 12. Docker Deployment (Recommended for Cloud Servers)

Docker packages the entire environment (Python + R + Bioconductor + all dependencies) into a single image, eliminating environment setup issues on cloud servers.

### Prerequisites

Install Docker on the server:
```bash
# Ubuntu / Debian
curl -fsSL https://get.docker.com | sh

# Verify
docker --version
docker compose version
```

### Quick Start

```bash
# Clone the repo
git clone https://github.com/Xiangyu7/drug-repurposing-platform.git
cd drug-repurposing-platform

# First run: build image + start Ollama + pull models (~15 min)
docker compose up --build

# Run a single disease (Direction B only)
docker compose run app bash ops/quickstart.sh --single atherosclerosis

# Run a single disease (Direction A only)
docker compose run app bash ops/quickstart.sh --single atherosclerosis --mode cross_only

# Run a single disease (Direction A + B)
docker compose run app bash ops/quickstart.sh --single atherosclerosis --mode dual

# Check environment
docker compose run app bash ops/quickstart.sh --check-only
```

### Background / 24x7 Mode

```bash
# Start in background
docker compose up -d app

# View live logs
docker compose logs -f app

# Stop (keeps all data)
docker compose down

# Stop and delete all data
docker compose down -v
```

### Useful Commands

```bash
# Interactive shell (debug inside container)
docker compose run app bash

# Check Ollama models
docker compose exec ollama ollama list

# View pipeline status inside container
docker compose run app bash ops/check_status.sh

# Rebuild image after code changes
docker compose build
docker compose up -d app
```

### Edit Disease Lists

Disease lists are text files. Edit them before running, or bind-mount them in `docker-compose.yml`:

```bash
# Edit on the server, then run
vi ops/disease_list_day1_origin.txt
docker compose run app bash ops/quickstart.sh --run-only
```

Or uncomment the bind-mount lines in `docker-compose.yml` to live-edit configs:
```yaml
volumes:
  - ./ops/disease_list_day1_origin.txt:/app/ops/disease_list_day1_origin.txt
  - ./dsmeta_signature_pipeline/configs:/app/dsmeta_signature_pipeline/configs
```

### GPU Support (NVIDIA)

Uncomment the GPU section in `docker-compose.yml` under the `ollama` service:
```yaml
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          count: all
          capabilities: [gpu]
```

### Architecture

```
docker-compose.yml
  ├── app (main)        → Python 3.11 + R 4.3 + all 4 modules (~3.5 GB)
  ├── ollama (LLM)      → qwen2.5:7b-instruct + nomic-embed-text
  └── ollama-init       → auto-pulls models on first run, then exits
```

---

## 13. Environment Variables Reference

### Core Variables

| Variable | Default | Description |
|---|---|---|
| `RUN_MODE` | `dual` | Run mode: `dual` (A+B), `origin_only` (B), `cross_only` (A) |
| `MAX_CYCLES` | `0` | Max cycles (0=infinite, 1=run once) |
| `SLEEP_SECONDS` | `300` | Seconds between cycles |
| `STEP_TIMEOUT` | `1800` | Per-step timeout in seconds |

### TopN / Quality Control

| Variable | Default | Description |
|---|---|---|
| `TOPN_PROFILE` | `stable` | Profile: `stable`, `balanced`, `recall` |
| `TOPN_CROSS` | `auto` | Direction A bridge topn: `auto` or integer |
| `TOPN_ORIGIN` | `auto` | Direction B bridge topn: `auto` or integer |
| `TOPN_STAGE2_ENABLE` | `1` | Allow second-stage expansion if quality low |
| `TOPN_MAX_EXPAND_ROUNDS` | `1` | Max expansion rounds |
| `STRICT_CONTRACT` | `1` | Strict data contract enforcement |

### Disk & Cleanup

| Variable | Default | Description |
|---|---|---|
| `DISK_MIN_GB` | `5` | Min free disk space (GB) before cycle starts |
| `DSMETA_DISK_MIN_GB` | `8` | Min free disk space (GB) before dsmeta runs |
| `DSMETA_CLEANUP` | `1` | Auto-clean dsmeta workdir after each disease |
| `RETENTION_DAYS` | `7` | Work/quarantine retention (days) |
| `LOG_RETENTION_DAYS` | `30` | Log retention (days) |
| `CACHE_RETENTION_DAYS` | `1` | KG HTTP cache retention (days) |

### API / External Services

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama API endpoint |
| `NCBI_API_KEY` | _(empty)_ | NCBI API key for faster PubMed access |
| `API_BACKOFF_SECONDS` | `600` | Wait time when all APIs are down |
| `SCREEN_MAX_STUDIES` | `500` | Max CT.gov studies to screen per disease |

### LLM Step6 Budget

| Variable | Default | Description |
|---|---|---|
| `STEP6_PUBMED_RETMAX` | `120` | Max PubMed articles to retrieve |
| `STEP6_PUBMED_PARSE_MAX` | `60` | Max articles to parse |
| `STEP6_MAX_RERANK_DOCS` | `40` | Max documents for reranking |
| `STEP6_MAX_EVIDENCE_DOCS` | `12` | Max evidence documents per drug |

---

## 14. Quick Reference

```bash
# === MOST COMMON COMMANDS ===

# First time setup
bash ops/quickstart.sh --setup-only

# Run everything for one disease (Direction B)
bash ops/quickstart.sh --single atherosclerosis

# Run everything for one disease (Direction A + B)
bash ops/quickstart.sh --single atherosclerosis --mode dual

# Run everything for one disease (Direction A only)
bash ops/quickstart.sh --single atherosclerosis --mode cross_only

# Run full pipeline on cloud server
bash ops/start_day1_aliyun.sh

# Run full pipeline on Mac (serial, saves memory)
bash ops/start_m1_serial.sh

# Check what's happening
bash ops/check_status.sh

# === OPERATIONS ===

# Retry a failed disease
bash ops/retry_disease.sh atherosclerosis --mode dual

# View results
bash ops/show_results.sh atherosclerosis

# Stop all runners
bash ops/restart_runner.sh --stop

# Clean up disk (dry run first)
bash ops/cleanup.sh --dry-run --all 7
bash ops/cleanup.sh --all 7

# Track progress
grep "PROGRESS" logs/continuous_runner/runner_*.log

# === INDIVIDUAL MODULES ===

# Run only KG for one disease
cd kg_explain && .venv/bin/python -m src.kg_explain.cli pipeline --disease atherosclerosis --version v5

# Run only dsmeta for one disease
cd dsmeta_signature_pipeline && python run.py --config configs/atherosclerosis.yaml

# Run only LLM evidence for one disease (needs Ollama running)
cd LLM+RAG证据工程 && .venv/bin/python scripts/step6_evidence_extraction.py --input data/step6_rank.csv --output-dir data/step6_output --disease atherosclerosis
```
