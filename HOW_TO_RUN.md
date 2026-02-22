# Drug Repurposing Platform -- How to Run

## 1. Project Overview

```
Drug Repurposing/
  ├── dsmeta_signature_pipeline/   # Direction A: GEO microarray 基因签名 (主签名源)
  ├── archs4_signature_pipeline/   # Direction A: ARCHS4 RNA-seq 基因签名 (备选签名源)
  ├── sigreverse/                  # Direction A: LINCS L1000 signature reversal
  ├── kg_explain/                  # Both A & B: Knowledge Graph + V5 Ranking + 靶点结构标注
  ├── LLM+RAG证据工程/              # Both A & B: PubMed RAG + LLM evidence + docking就绪评估
  ├── ops/                         # 用户入口: start.sh, check_status.sh, compare_ab_routes.py
  │   └── internal/                # 底层脚本: runner, env_guard, topn_policy, disease lists
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
bash ops/start.sh setup

# 4. Verify everything
bash ops/start.sh check
```

> **Note:** If you only need Direction B (origin_only), skip steps 1-2 (R is not required).

### Mac (Homebrew) Setup

```bash
brew install python r
bash ops/start.sh setup
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
bash ops/start.sh check
# 或按当前运行模式检查（origin_only 下 A 路问题降级为 warning）
bash ops/start.sh check --mode origin_only --check-scope mode

# Install all dependencies (creates venvs, installs pip packages)
bash ops/start.sh setup

# Full flow: check -> install -> GEO discovery -> launch pipeline
bash ops/start.sh

# Run a single disease only
bash ops/start.sh run atherosclerosis

# Run only Direction B (origin)
bash ops/start.sh start --mode origin_only
```

### start.sh 行为升级（工业级）

1. `--single` 默认会执行：`check -> auto-repair(必要时) -> re-check -> run`。  
2. `--check-only` 默认是全量深检（A+B），并落盘可审计报告 JSON。  
3. dsmeta 解释器解析策略为 `conda dsmeta 优先，.venv 回退`。  
4. 生成产物位置：
   - `runtime/state/env_check_<timestamp>.json`
   - `runtime/state/env_resolved_<timestamp>.env`

可选参数：

```bash
# 关闭自动修复（检查失败即退出）
bash ops/start.sh run atherosclerosis --no-auto-repair

# 自定义报告输出文件（check-only）
bash ops/start.sh check --report-json runtime/state/my_env_check.json
```

### 路径约定（重要）

1. 以疾病级输出路径为主：`kg_explain/output/<disease>/`  
2. 常用产物示例：
   - `kg_explain/output/<disease>/pipeline_manifest.json`
   - `kg_explain/output/<disease>/bridge_origin_reassess.csv`
   - `kg_explain/output/<disease>/bridge_repurpose_cross.csv`
3. `kg_explain/output/*.csv` 仅作为 legacy 回退路径，不建议作为主输入路径。

### 两疾病最小量 Smoke（origin_only / fastest）

```bash
cd /Users/xinyueke/Desktop/Drug\ Repurposing

# 1) 预检（按 origin_only 模式）
bash ops/start.sh check --mode origin_only --check-scope mode

# 2) 生成 day1 前两个疾病临时列表（不改仓库文件）
tmp_list="$(mktemp /tmp/day1_origin_two_XXXXXX.txt)"
awk 'NF && $1 !~ /^#/ {print $1; n++; if (n==2) exit}' ops/internal/disease_list_day1_origin.txt > "$tmp_list"
cat "$tmp_list"

# 3) 最小预算运行
RUN_MODE=origin_only \
SCREEN_MAX_STUDIES=60 \
TOPN_ORIGIN=6 \
TOPN_STAGE2_ENABLE=0 \
bash ops/internal/runner.sh "$tmp_list"

# 4) 监控日志（另开终端）
tail -f "$(ls -t logs/continuous_runner/runner_origin_only_*.log | head -1)"
```

可选快速检查：

```bash
log="$(ls -t logs/continuous_runner/runner_origin_only_*.log | head -1)"
grep -E "\[ERROR\]|\[FAIL\]|=== Disease done|Cycle 1 done" "$log"
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

**Option A: start.sh (recommended)**

`bash ops/start.sh setup` handles this automatically. It creates the venv and installs all Python dependencies.

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

### 4.3b archs4_signature_pipeline (ARCHS4 备选签名源)

```bash
cd archs4_signature_pipeline
# 与 dsmeta 共享同一个 Python 虚拟环境即可
# 需要 R + DESeq2 (用于差异表达分析)

# 数据依赖: 下载 ARCHS4 H5 文件 (43GB)
mkdir -p data/archs4
# 从 https://s3.dev.maayanlab.cloud/archs4/files/human_gene_v2.4.h5 下载
# 或用测试文件代替:
python scripts/generate_test_h5.py --out data/archs4/human_gene_v2.4.h5
```

> **说明**: 默认签名优先级为 dsmeta → ARCHS4 回退（通过 `SIG_PRIORITY=dsmeta` 控制）。
> 设置 `SIG_PRIORITY=archs4` 可切换为 ARCHS4 优先。

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

### 5.2b ARCHS4 Signature Pipeline (备选签名源)

```bash
cd archs4_signature_pipeline

# Full pipeline (5 steps)
python run.py --config configs/atherosclerosis.yaml

# Partial run
python run.py --config configs/atherosclerosis.yaml --from-step 2

# Steps:
#   1. OpenTargets prior   -> 疾病关联基因列表
#   2. ARCHS4 select       -> 从H5中检索疾病GEO series + 提取计数矩阵
#   3. DE analysis (DESeq2) -> 差异表达 (per-series logFC/FDR)
#   3b. Meta-analysis       -> 随机效应 meta-analysis (cross-series)
#   4. Assemble signature   -> OT先验 × DE结果 → top300 up + top300 down

# 使用测试数据 (不需要下载43GB H5文件)
python scripts/generate_test_h5.py --out data/archs4/human_gene_v2.4.h5
python run.py --config configs/atherosclerosis.yaml
```

**Output:** `archs4_signature_pipeline/outputs/<disease>/signature/` (与 dsmeta 格式完全兼容)

**签名源优先级** (runner 自动处理，通过 `SIG_PRIORITY` 环境变量控制):
- `SIG_PRIORITY=dsmeta` (默认): dsmeta 优先 → ARCHS4 回退
- `SIG_PRIORITY=archs4`: ARCHS4 优先 → dsmeta 回退

### 5.3 SigReverse (LINCS L1000)

```bash
cd sigreverse

# SigReverse is called automatically by the runner (runner.sh)
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
  --rank_in data/step6_rank.csv \
  --out output/step6 \
  --target_disease atherosclerosis

# Step 7: 5-dimension scoring + gating (GO/MAYBE/NO-GO)
.venv/bin/python scripts/step7_score_and_gate.py \
  --input output/step6 \
  --out output/step7

# Step 8: Release gate + candidate pack (Excel)
.venv/bin/python scripts/step8_candidate_pack.py \
  --step7_dir output/step7 \
  --outdir output/step8

# Step 9: Validation plan generation
.venv/bin/python scripts/step9_validation_plan.py \
  --step8_dir output/step8 \
  --outdir output/step9
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

Disease lists (in `ops/internal/`):
- `disease_list_day1_dual.txt` -- diseases for both Direction A+B (7 diseases)
- `disease_list_day1_origin.txt` -- all diseases for Direction B (15 diseases)
- `disease_list_b_only.txt` -- diseases only needing Direction B (9 diseases)

User-facing lists (in `ops/`):
- `disease_list.txt` -- master template
- `disease_list_test.txt` -- minimal test set (2 diseases)

### 6.2 Cloud Server (Aliyun/AWS) -- Parallel

```bash
# Start both dual + origin runners in parallel (background)
bash ops/start.sh start --mode dual

# Monitor
tail -f logs/continuous_runner/runner_dual_*.log
tail -f logs/continuous_runner/runner_origin_only_*.log
```

### 6.3 Mac M1 (16GB) -- Serial (saves memory)

```bash
# Serial: first dual (7 diseases), then B-only (9 diseases)
# Estimated ~13 hours total
bash ops/start.sh start                  # background
bash ops/start.sh start --foreground     # foreground (see output)
bash ops/start.sh start --dry-run        # preview only
```

### 6.4 Key Environment Variables

```bash
# Example: customize and launch
export RUN_MODE=dual                # dual | origin_only | cross_only
export TOPN_PROFILE=stable          # stable | balanced | recall
export STEP_TIMEOUT=1800            # per-step timeout (seconds)
export DSMETA_CLEANUP=1             # 1=cleanup work dir after dsmeta
bash ops/internal/runner.sh ops/internal/disease_list_day1_dual.txt
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
bash ops/internal/retry_disease.sh atherosclerosis

# Retry both directions
bash ops/internal/retry_disease.sh atherosclerosis --mode dual

# Retry Direction A only
bash ops/internal/retry_disease.sh atherosclerosis --mode cross_only

# Skip cleanup of previous artifacts
bash ops/internal/retry_disease.sh atherosclerosis --no-clean
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
bash ops/internal/cleanup.sh --dry-run --all 7

# Clean everything older than 7 days
bash ops/internal/cleanup.sh --all 7

# Clean only work directories older than 3 days
bash ops/internal/cleanup.sh --work 3

# Clean only quarantine older than 14 days
bash ops/internal/cleanup.sh --quarantine 14

# Clean only logs older than 30 days
bash ops/internal/cleanup.sh --logs 30
```

### 8.4 Restart / Stop Runners

```bash
# Stop all runners
bash ops/internal/restart_runner.sh --stop

# Restart a specific runner
bash ops/internal/restart_runner.sh --runner dual

# Restart all active runners
bash ops/internal/restart_runner.sh
```

---

## 9. Output Structure

```
runtime/
  ├── results/<disease>/<YYYY-MM-DD>/<run_id>/
  │   ├── direction_a/                  # Direction A (跨疾病迁移)
  │   │   ├── dsmeta/                   # dsmeta 基因签名
  │   │   ├── archs4/                   # ARCHS4 签名 (如果dsmeta失败)
  │   │   ├── sigreverse/               # LINCS 反向匹配分数
  │   │   ├── kg/                       # KG ranking (signature mode)
  │   │   │   ├── bridge_repurpose_cross.csv  # ★ 含靶点+PDB/AlphaFold结构标注
  │   │   │   └── drug_disease_rank.csv
  │   │   └── llm/                      # LLM evidence (step6-9)
  │   │       ├── step6/dossiers/       # 每药 PubMed 证据档案
  │   │       ├── step7/                # GO/MAYBE/NO-GO 决策
  │   │       ├── step8/                # ★ 候选包 (CSV+Excel+one-pager)
  │   │       │   ├── step8_shortlist_topK.csv   # ★★★ 最终候选 (含docking列+alphafold_structure_id)
  │   │       │   └── step8_candidate_pack.xlsx  # Excel 报告
  │   │       └── step9/                # 验证计划
  │   ├── direction_b/                  # Direction B (原疾病重评估)
  │   │   ├── kg/
  │   │   │   └── bridge_origin_reassess.csv
  │   │   └── llm/step6-9/
  │   └── ab_comparison.csv             # ★ A+B 交叉验证 (两路线重叠药物)
  ├── work/<disease>/                   # 中间文件 (自动清理)
  ├── quarantine/<disease>/             # 失败运行隔离
  └── state/                            # PID, lock, env check 报告
```

### 最终输出文件优先级（按重要性排序）

| 优先级 | 文件 | 用途 |
|--------|------|------|
| ★★★ | `ab_comparison.csv` | A+B 交叉验证: 两路线都推荐的药物 = 最高可信度 |
| ★★★ | `step8_shortlist_topK.csv` | 最终候选药列表 (含靶点/结构/docking/AlphaFold) |
| ★★ | `step8_candidate_pack.xlsx` | Excel 候选报告 (每药独立 Sheet) |
| ★★ | `step9_validation_plan.csv` | 实验验证计划 (P1/P2/P3 优先级) |
| ★ | `bridge_*.csv` | KG 中间排名 + 靶点结构信息 |
| | `step7_gating_decision.csv` | 全部药物 GO/MAYBE/NO-GO 决策 |
| | `step6/dossiers/*.json` | PubMed 证据原文 (调试/溯源用) |

---

## 10. Common Issues

| Problem | Cause | Solution |
|---|---|---|
| `No module named 'xxx'` | venv not activated / not created | Run `bash ops/start.sh setup` |
| `python3-venv not available` | Missing system package | `sudo apt install python3.10-venv` (auto-detected by start.sh) |
| R packages missing | dsmeta needs Bioconductor | `sudo Rscript -e 'BiocManager::install(c("limma","GEOquery","fgsea"))'` |
| R not found | Direction A needs R | `sudo apt install -y r-base r-base-dev` |
| Ollama connection refused | Ollama not running | `ollama serve &` then `ollama list` |
| Ollama slow (~5min/drug) | CPU-only inference | Normal for 7B model on CPU; use GPU if available |
| WikiPathways 404 | GMT download URL outdated | Auto-skipped; Reactome still works |
| step8 ValueError all NO-GO | All candidates gated out | Expected when test data has irrelevant drugs |
| Disk full | GEO data accumulates | Set `DSMETA_CLEANUP=1` or `RETENTION_DAYS=7` |
| API rate limit | NCBI/ChEMBL throttling | Auto-retried; set `NCBI_API_KEY` in `.env` |
| Permission denied on .sh | Scripts not executable | `chmod +x ops/*.sh` |
| ARCHS4 H5 file not found | 未下载 43GB 数据文件 | 用 `generate_test_h5.py` 生成测试文件，或下载真实 H5 |
| numpy int64 JSON error | h5py 返回 numpy 类型 | 已修复: `02_archs4_select.py` 含 NumpyEncoder |
| ARCHS4 空签名 | 疾病在 ARCHS4 无数据 | runner 自动回退到 dsmeta 或 OT-only |

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
docker compose run app bash ops/start.sh run atherosclerosis

# Run a single disease (Direction A only)
docker compose run app bash ops/start.sh run atherosclerosis --mode cross_only

# Run a single disease (Direction A + B)
docker compose run app bash ops/start.sh run atherosclerosis --mode dual

# Check environment
docker compose run app bash ops/start.sh check
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
vi ops/internal/disease_list_day1_origin.txt
docker compose run app bash ops/start.sh start
```

Or uncomment the bind-mount lines in `docker-compose.yml` to live-edit configs:
```yaml
volumes:
  - ./ops/internal/disease_list_day1_origin.txt:/app/ops/internal/disease_list_day1_origin.txt
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
| `STEP_TIMEOUT` | `1800` | Per-step timeout in seconds |

### TopN / Quality Control

| Variable | Default | Description |
|---|---|---|
| `TOPN_PROFILE` | `stable` | Profile: `stable`, `balanced`, `recall` |
| `TOPN_CROSS` | `auto` | Direction A bridge topn: `auto` or integer |
| `TOPN_ORIGIN` | `auto` | Direction B bridge topn: `auto` or integer |
| `TOPN_STAGE2_ENABLE` | `1` | Allow second-stage expansion if quality low |
| `STRICT_CONTRACT` | `1` | Strict data contract enforcement |

### Disk & Cleanup

| Variable | Default | Description |
|---|---|---|
| `DISK_MIN_GB` | `5` | Min free disk space (GB) before pipeline starts |
| `DSMETA_DISK_MIN_GB` | `8` | Min free disk space (GB) before dsmeta runs |
| `DSMETA_CLEANUP` | `1` | Auto-clean dsmeta workdir after each disease |
| `SIG_PRIORITY` | `dsmeta` | Signature source priority: `dsmeta` or `archs4` |
| `RETENTION_DAYS` | `7` | Work/quarantine retention (days) |
| `LOG_RETENTION_DAYS` | `30` | Log retention (days) |
| `CACHE_RETENTION_DAYS` | `1` | KG HTTP cache retention (days) |

### API / External Services

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama API endpoint |
| `NCBI_API_KEY` | _(empty)_ | NCBI API key for faster PubMed access |
| `SCREEN_MAX_STUDIES` | `500` | Max CT.gov studies to screen per disease |

---

## 14. Quick Reference

```bash
# === MOST COMMON COMMANDS ===

# First time setup
bash ops/start.sh setup

# Run everything for one disease (Direction B)
bash ops/start.sh run atherosclerosis

# Run everything for one disease (Direction A + B)
bash ops/start.sh run atherosclerosis --mode dual

# Run everything for one disease (Direction A only)
bash ops/start.sh run atherosclerosis --mode cross_only

# Run full pipeline on cloud server
bash ops/start.sh start --mode dual

# Run full pipeline on Mac (serial, saves memory)
bash ops/start.sh start

# Check what's happening
bash ops/check_status.sh

# === OPERATIONS ===

# Retry a failed disease
bash ops/internal/retry_disease.sh atherosclerosis --mode dual

# View results
bash ops/show_results.sh atherosclerosis

# Stop all runners
bash ops/internal/restart_runner.sh --stop

# Clean up disk (dry run first)
bash ops/internal/cleanup.sh --dry-run --all 7
bash ops/internal/cleanup.sh --all 7

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
