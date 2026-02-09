# LLM+RAG 证据工程

基于 LLM+RAG 的自动化证据工程管道，靶向**动脉粥样硬化**药物再利用，整合 ClinicalTrials.gov 数据、PubMed 文献挖掘和 LLM 辅助证据提取。

## Directory Structure

```
LLM+RAG证据工程/
├── scripts/          # All pipeline scripts (step0 → step9)
│   └── metrics_server.py        # Prometheus metrics HTTP server
├── src/dr/           # Core pipeline modules
│   ├── monitoring/   # Prometheus metrics & tracking
│   ├── scoring/      # Scoring & gating engine
│   ├── retrieval/    # PubMed & CT.gov retrieval
│   └── common/       # Utilities
├── tests/            # Test suite (70% coverage)
│   ├── unit/         # Unit tests
│   └── integration/  # Integration tests
├── monitoring/       # Monitoring stack configuration
│   ├── prometheus/   # Prometheus config
│   └── grafana/      # Grafana dashboards & datasources
├── data/             # Input/output CSV & Excel data files
├── output/           # Pipeline step outputs
│   ├── step6/        # PubMed RAG dossiers & rank CSV
│   ├── step7/        # Hypothesis cards & gating decisions
│   ├── step8/        # Candidate shortlist & one-pagers
│   └── step9/        # Validation plans
├── cache/            # Regenerable caches (gitignored)
│   ├── ctgov/        # ClinicalTrials.gov API cache
│   └── pubmed/       # PubMed XML/embedding cache
├── archive/          # Old script versions & backups
├── docker-compose.monitoring.yml  # Prometheus + Grafana stack
└── README.md
```

## Pipeline Steps

| Step | Script | Description |
|------|--------|-------------|
| 0 | `step0_build_pool_from_seed_ncts.py` | Build trial pool from seed NCT IDs |
| 4 | `step4_pipeline_best.py` | AI labeling pipeline (CT.gov + PubMed) |
| 5 | `step5_drug_normalize_and_aggregate_v3.py` | Drug name normalization & aggregation |
| 6 | `step6_pubmed_rag_ollama_evidence_v2.py` | PubMed RAG + Ollama LLM evidence extraction |
| 7 | `step7_build_from_step6.py` | Hypothesis card generation & gating |
| 8 | `step8_candidate_pack_from_step7.py` | Top-K candidate shortlisting |
| 9 | `step9_validation_plan_from_step8.py` | Validation plan generation |

## Quick Start

```bash
# From project root:
cd /path/to/LLM+RAG证据工程

# Step 6: PubMed RAG (requires Ollama running)
python scripts/step6_pubmed_rag_ollama_evidence_v2.py \
  --rank_in data/step6_rank.csv \
  --neg data/poolA_negative_drug_level.csv \
  --out output/step6

# Step 7: Build hypothesis cards
python scripts/step7_build_from_step6.py \
  --rank output/step6/step6_rank_v2.csv \
  --neg data/poolA_negative_drug_level.csv \
  --outdir output/step7

# Step 8: Candidate pack
python scripts/step8_candidate_pack_from_step7.py \
  --step7_dir output/step7 \
  --neg data/poolA_negative_drug_level.csv \
  --outdir output/step8
```

## Requirements

- Python 3.10+
- `requests`, `pandas`, `tqdm`, `prometheus-client`
- [Ollama](https://ollama.ai/) (for Step 6 LLM/embedding)
- Docker & Docker Compose (optional, for monitoring stack)

## Monitoring (NEW!)

The pipeline now includes **production-grade monitoring** with Prometheus + Grafana:

- **Real-time metrics**: Track pipeline executions, PubMed requests, LLM extractions, drug scores, gating decisions
- **Performance monitoring**: Monitor latency, throughput, error rates
- **Visual dashboards**: Pre-built Grafana dashboard with 14 panels
- **Alerting**: Automatic alerts on high error rates

### Quick Start Monitoring

```bash
# 1. Start metrics server
python scripts/metrics_server.py

# 2. Start Prometheus + Grafana
docker-compose -f docker-compose.monitoring.yml up -d

# 3. Generate test metrics
python scripts/test_monitoring.py

# 4. Access dashboards
# Grafana: http://localhost:3000 (admin/admin)
# Prometheus: http://localhost:9090
```

**See `MONITORING_QUICKSTART.md` for detailed setup guide.**

### Available Metrics

- `dr_pipeline_executions_total` - Pipeline runs (success/failure)
- `dr_pipeline_duration_seconds` - Execution time
- `dr_pubmed_requests_total` - PubMed API calls
- `dr_llm_extractions_total` - LLM extraction calls
- `dr_drug_scores` - Drug score distribution
- `dr_gating_decisions_total` - GO/MAYBE/NO-GO decisions
- `dr_errors_total` - Error counts by module and type

### Instrumented Examples

See `scripts/step7_score_and_gate_monitored.py` for an example of a pipeline script instrumented with monitoring.

## Testing

Test suite with 304 passing tests (70% coverage):

```bash
# Run all tests
pytest

# Run with coverage report
pytest --cov=src --cov-report=term-missing

# Run specific test module
pytest tests/unit/test_scorer.py
```

**Coverage by module:**
- Extractor: 96%
- Text Utils: 95%
- Scorer: 91%
- Gating: 87%
- Ranker: 79%
- PubMed: 72%
