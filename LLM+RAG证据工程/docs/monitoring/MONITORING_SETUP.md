# LLM+RAG证据工程 Monitoring Setup

This document explains how to set up and use the Prometheus + Grafana monitoring stack for the DR pipeline.

## Overview

The monitoring stack consists of:
- **Prometheus**: Time-series database that scrapes metrics
- **Grafana**: Visualization dashboard for metrics
- **Metrics Server**: HTTP server exposing pipeline metrics at `/metrics` endpoint

## Architecture

```
LLM+RAG证据工程 Scripts
    ↓ (instrumented with metrics)
src.dr.monitoring.metrics
    ↓ (exposes via HTTP)
scripts/metrics_server.py :8000/metrics
    ↓ (scrapes every 15s)
Prometheus :9090
    ↓ (queries)
Grafana :3000 (dashboards)
```

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

This installs `prometheus-client>=0.19.0` for metrics collection.

### 2. Start Metrics Server

In one terminal, start the metrics HTTP server:

```bash
python scripts/metrics_server.py
```

This will:
- Start HTTP server on `http://localhost:8000`
- Expose metrics at `http://localhost:8000/metrics`
- Provide health check at `http://localhost:8000/health`

You can verify it's working:
```bash
curl http://localhost:8000/metrics
curl http://localhost:8000/health
```

### 3. Start Monitoring Stack

In another terminal, start Prometheus and Grafana using Docker Compose:

```bash
docker-compose -f docker-compose.monitoring.yml up -d
```

This will start:
- **Prometheus** on http://localhost:9090
- **Grafana** on http://localhost:3000 (login: admin/admin)

### 4. Access Dashboards

1. Open Grafana at http://localhost:3000
2. Login with `admin/admin` (you'll be prompted to change password)
3. Navigate to Dashboards → Browse
4. Open "LLM+RAG证据工程 Monitoring" dashboard

## Available Metrics

### Pipeline Metrics
- `dr_pipeline_executions_total{pipeline, status}` - Total pipeline executions (counter)
- `dr_pipeline_duration_seconds{pipeline}` - Pipeline execution duration (histogram)
- `dr_active_operations{operation}` - Currently active operations (gauge)

### PubMed Metrics
- `dr_pubmed_requests_total{operation, status}` - Total PubMed requests (counter)
- `dr_pubmed_request_duration_seconds{operation}` - PubMed request duration (histogram)

### LLM Metrics
- `dr_llm_extractions_total{status}` - Total LLM extractions (counter)
- `dr_llm_extraction_duration_seconds` - LLM extraction duration (histogram)

### Scoring Metrics
- `dr_drug_scores` - Drug total scores distribution (histogram)
- `dr_gating_decisions_total{decision}` - Gating decisions (GO/MAYBE/NO-GO) (counter)

### System Metrics
- `dr_errors_total{module, error_type}` - Total errors by module and type (counter)
- `dr_system_info` - System metadata (version, Python version) (info)

## Instrumenting Your Code

### Using Context Managers (Recommended)

The easiest way to add monitoring is using context managers:

```python
from src.dr.monitoring import (
    track_pipeline_execution,
    track_pubmed_request,
    track_llm_extraction,
    track_drug_scoring,
    track_gating_decision
)

# Track entire pipeline execution
def run_step6_pipeline(drug_name: str):
    with track_pipeline_execution('step6'):
        # Your pipeline code here
        results = process_drug(drug_name)
        return results

# Track PubMed requests
def fetch_pubmed_articles(query: str):
    with track_pubmed_request('search'):
        # Your PubMed API call
        results = pubmed_client.search(query)
        return results

# Track LLM extractions
def extract_with_llm(text: str):
    with track_llm_extraction():
        # Your LLM extraction code
        extracted = llm.extract(text)
        return extracted
```

### Tracking Scores and Decisions

```python
from src.dr.monitoring import track_drug_scoring, track_gating_decision

# After scoring a drug
scores = {
    'total_score_0_100': 75.5,
    'mechanism_score': 25,
    'safety_score': 30,
    # ... other scores
}
track_drug_scoring(scores)

# After gating decision
track_gating_decision('GO', gate_reasons=['High score', 'Strong evidence'])
```

### Example: Instrumenting Step6 Pipeline

```python
# scripts/step6_run.py
from src.dr.monitoring import track_pipeline_execution, track_drug_scoring
from src.dr.logger import get_logger

logger = get_logger(__name__)

def main():
    drugs = load_drugs()

    with track_pipeline_execution('step6'):
        for drug in drugs:
            logger.info(f"Processing {drug.name}")

            # Your existing code...
            scores = score_drug(drug)

            # Track the scores
            track_drug_scoring(scores)

            logger.info(f"Completed {drug.name}: {scores['total_score_0_100']}")

if __name__ == '__main__':
    main()
```

## Dashboard Panels

The LLM+RAG证据工程 Monitoring dashboard includes:

1. **Pipeline Execution Rate** - Executions per second by pipeline and status
2. **Active Operations** - Currently running operations
3. **Pipeline Duration (p50, p95, p99)** - Performance percentiles
4. **PubMed Request Rate** - API requests per second
5. **PubMed Request Duration** - API latency
6. **LLM Extraction Success Rate** - Percentage of successful extractions
7. **LLM Extraction Duration** - Time spent on LLM calls
8. **Drug Score Distribution** - Heatmap of score ranges
9. **Gating Decisions** - Pie chart of GO/MAYBE/NO-GO decisions
10. **Error Rate by Module** - Errors per second (with alerting)
11. **Total Pipeline Executions** - Cumulative count
12. **Success Rate** - Overall pipeline success percentage
13. **Total LLM Extractions** - Cumulative LLM calls
14. **Total Errors** - Error count

## Useful Prometheus Queries

### Pipeline Performance
```promql
# Average pipeline duration over 5 minutes
rate(dr_pipeline_duration_seconds_sum[5m]) / rate(dr_pipeline_duration_seconds_count[5m])

# 95th percentile pipeline duration
histogram_quantile(0.95, rate(dr_pipeline_duration_seconds_bucket[5m]))

# Pipeline success rate
sum(rate(dr_pipeline_executions_total{status="success"}[5m])) / sum(rate(dr_pipeline_executions_total[5m]))
```

### PubMed Monitoring
```promql
# PubMed request rate by operation
rate(dr_pubmed_requests_total[5m])

# PubMed error rate
rate(dr_pubmed_requests_total{status="error"}[5m])
```

### LLM Monitoring
```promql
# LLM extraction throughput
rate(dr_llm_extractions_total[5m])

# LLM failure rate
rate(dr_llm_extractions_total{status="failure"}[5m]) / rate(dr_llm_extractions_total[5m])
```

### Error Monitoring
```promql
# Total error rate
sum(rate(dr_errors_total[5m]))

# Errors by module
sum(rate(dr_errors_total[5m])) by (module)

# Top error types
topk(5, sum(rate(dr_errors_total[5m])) by (error_type))
```

## Alerting

The dashboard includes a pre-configured alert on high error rates (>0.1 errors/sec). To receive notifications:

1. Configure notification channels in Grafana (Settings → Notification channels)
2. Add email, Slack, or webhook integrations
3. Link alerts to notification channels

Example Slack webhook:
1. Grafana → Alerting → Notification channels → New channel
2. Type: Slack
3. Webhook URL: `https://hooks.slack.com/services/YOUR/WEBHOOK/URL`
4. Edit alert → Notifications → Select your Slack channel

## Troubleshooting

### Metrics server won't start
- Check if port 8000 is already in use: `lsof -i :8000`
- Kill existing process or change PORT in `scripts/metrics_server.py`

### Prometheus can't scrape metrics
- Verify metrics server is running: `curl http://localhost:8000/metrics`
- Check Prometheus targets: http://localhost:9090/targets
- If using Docker Desktop on Mac, ensure `host.docker.internal` resolves
- Check Prometheus logs: `docker logs dr-prometheus`

### Grafana shows "No Data"
- Verify Prometheus is receiving metrics: http://localhost:9090/graph
- Run query: `dr_pipeline_executions_total`
- Check datasource configuration: Grafana → Configuration → Data sources
- Ensure Prometheus URL is `http://prometheus:9090`

### No metrics appearing
- Ensure you've instrumented your code with tracking context managers
- Run your pipeline to generate metrics
- Check metrics endpoint shows your metrics: `curl http://localhost:8000/metrics | grep dr_`

## Stopping the Stack

```bash
# Stop monitoring containers
docker-compose -f docker-compose.monitoring.yml down

# Stop and remove volumes (deletes all metrics data)
docker-compose -f docker-compose.monitoring.yml down -v

# Stop metrics server
# Press Ctrl+C in the terminal running metrics_server.py
```

## Data Retention

By default:
- Prometheus retains data for 15 days
- Grafana retains dashboard state in `grafana-data` volume

To change Prometheus retention, edit `docker-compose.monitoring.yml`:
```yaml
command:
  - '--storage.tsdb.retention.time=30d'  # Keep 30 days
```

## Production Considerations

For production deployments:

1. **Security**:
   - Change Grafana admin password
   - Enable HTTPS for Grafana
   - Restrict network access to monitoring ports

2. **Scalability**:
   - Consider remote storage for Prometheus (e.g., Thanos, Cortex)
   - Use Grafana Cloud for managed dashboards

3. **High Availability**:
   - Run multiple Prometheus replicas
   - Use Prometheus federation

4. **Backup**:
   - Backup Prometheus data volume: `prometheus-data`
   - Backup Grafana dashboards via API or volume: `grafana-data`

## Next Steps

1. Run a pipeline with monitoring enabled
2. Watch metrics appear in real-time on Grafana dashboard
3. Set up alerts for critical errors
4. Create custom dashboards for specific use cases
5. Tune scrape intervals based on your needs

## References

- [Prometheus Documentation](https://prometheus.io/docs/)
- [Grafana Documentation](https://grafana.com/docs/)
- [Prometheus Python Client](https://github.com/prometheus/client_python)
