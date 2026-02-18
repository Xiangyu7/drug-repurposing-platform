# ============================================================
# Drug Repurposing Platform — All-in-One Container
# ============================================================
# Base: Miniforge (conda-forge + bioconda pre-configured)
# Provides: Python 3.11, R 4.3+, Bioconductor, all pip deps
#
# Build:  docker compose build
# Run:    docker compose run app bash ops/quickstart.sh --single atherosclerosis
# Shell:  docker compose run app bash
# ============================================================

FROM condaforge/miniforge3:24.7.1-2

LABEL maintainer="Drug Repurposing Team"
LABEL description="Drug Repurposing Platform: kg_explain + sigreverse + dsmeta + LLM+RAG"

# ── Environment ──────────────────────────────────────────────
# No interactive prompts
ENV DEBIAN_FRONTEND=noninteractive
# Python output not buffered (important for Docker logs)
ENV PYTHONUNBUFFERED=1
# UTF-8 locale (needed for Chinese folder name: LLM+RAG证据工程)
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

# ── System packages ──────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        procps \
        coreutils \
        pandoc \
    && rm -rf /var/lib/apt/lists/*

# ── Conda: R + Bioconductor + Python 3.11 ────────────────────
# Install everything into the base environment.
# Runner scripts fallback to `python3` / `Rscript` on PATH when
# .venv is absent, so no activation needed.
RUN mamba install -y -n base --override-channels \
        -c conda-forge \
        -c bioconda \
        # ── Python ──
        python=3.11 \
        pip \
        # ── R core ──
        r-base=4.3 \
        r-data.table \
        r-optparse \
        r-ggplot2 \
        r-jsonlite \
        r-yaml \
        r-rmarkdown \
        r-knitr \
        # ── Bioconductor ──
        bioconductor-geoquery \
        bioconductor-limma \
        bioconductor-fgsea \
        bioconductor-biocparallel \
        # ── CRAN ──
        r-metafor \
        r-robustrankaggreg \
    && mamba clean -afy

# ── Python packages (all 4 modules, no venv needed) ──────────
RUN pip install --no-cache-dir \
        # kg_explain
        pandas numpy pyyaml tqdm requests tenacity python-dotenv networkx \
        # dsmeta (Python parts)
        scipy rich gseapy \
        # LLM+RAG
        openpyxl psutil prometheus-client \
        # testing
        pytest

# ── Application code ─────────────────────────────────────────
WORKDIR /app

COPY ops/                               /app/ops/
COPY kg_explain/                        /app/kg_explain/
COPY sigreverse/                        /app/sigreverse/
COPY dsmeta_signature_pipeline/         /app/dsmeta_signature_pipeline/
COPY LLM+RAG证据工程/                   /app/LLM+RAG证据工程/
COPY HOW_TO_RUN.md                      /app/HOW_TO_RUN.md

# ── LLM+RAG .env setup ──────────────────────────────────────
# Copy example as default; docker-compose env vars override it
# (python-dotenv loads with override=False)
RUN cp /app/LLM+RAG证据工程/.env.example /app/LLM+RAG证据工程/.env

# ── Create runtime directories ───────────────────────────────
RUN mkdir -p /app/runtime/work \
             /app/runtime/results \
             /app/runtime/quarantine \
             /app/runtime/state \
             /app/logs/continuous_runner \
             /app/logs/quickstart \
             /app/data

# ── Make scripts executable ──────────────────────────────────
RUN chmod +x /app/ops/*.sh

# ── Verify all dependencies ──────────────────────────────────
RUN python3 -c "\
import pandas, numpy, requests, networkx, scipy, rich, tqdm, tenacity; \
print('Python packages OK')" \
    && Rscript -e "\
library(GEOquery); library(limma); library(fgsea); library(BiocParallel); \
library(data.table); library(metafor); library(RobustRankAggreg); \
cat('R packages OK\n')" \
    && echo "=== All dependencies verified ==="

# ── Default entrypoint ───────────────────────────────────────
ENTRYPOINT ["bash"]
CMD ["ops/quickstart.sh", "--check-only"]
