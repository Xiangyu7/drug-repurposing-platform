# Monitoring Quick Start Guide

Get the DR pipeline monitoring stack running in 5 minutes.

## Prerequisites

- Docker and Docker Compose installed
- Python 3.11+ with dependencies installed (`pip install -r requirements.txt`)

## Step-by-Step Setup

### Step 1: Start the Metrics Server

Open a terminal and start the metrics HTTP server:

```bash
cd /Users/xinyueke/Desktop/DR
python scripts/metrics_server.py
```

You should see:
```
[INFO] Starting metrics server on http://localhost:8000
[INFO] Metrics available at http://localhost:8000/metrics
[INFO] Health check at http://localhost:8000/health
```

**Keep this terminal open** - the server needs to keep running.

### Step 2: Verify Metrics Server

Open a new terminal and test the metrics endpoint:

```bash
# Health check
curl http://localhost:8000/health
# Should return: {"status":"healthy"}

# Check metrics
curl http://localhost:8000/metrics
# Should return Prometheus format metrics
```

### Step 3: Start Prometheus and Grafana

In the new terminal, start the monitoring stack:

```bash
cd /Users/xinyueke/Desktop/DR
docker-compose -f docker-compose.monitoring.yml up -d
```

This will:
- Pull Prometheus and Grafana Docker images (first time only)
- Start Prometheus on port 9090
- Start Grafana on port 3000

Verify containers are running:
```bash
docker ps
```

You should see `dr-prometheus` and `dr-grafana` containers.

### Step 4: Verify Prometheus is Scraping

1. Open Prometheus in your browser: http://localhost:9090
2. Go to **Status → Targets** (http://localhost:9090/targets)
3. You should see `dr-pipeline` target with state **UP**

If the target is down:
- Check that metrics_server.py is still running
- On Mac/Windows, verify `host.docker.internal` resolves
- Check Prometheus logs: `docker logs dr-prometheus`

### Step 5: Access Grafana

1. Open Grafana: http://localhost:3000
2. Login with:
   - Username: `admin`
   - Password: `admin`
3. You'll be prompted to change the password (you can skip this for now)

### Step 6: Open the Dashboard

1. Click on **Dashboards** (left sidebar, compass icon)
2. Click **Browse**
3. You should see **LLM+RAG证据工程 Monitoring** dashboard
4. Click on it to open

**Note:** The dashboard will be empty at first because no pipeline has run yet.

### Step 7: Generate Test Metrics

Open a new terminal and run the test script:

```bash
cd /Users/xinyueke/Desktop/DR
python scripts/test_monitoring.py
```

This will simulate pipeline operations and generate metrics. You should see:
```
[INFO] Monitoring Test Script
[INFO] Simulating Step6 pipeline...
[INFO]   PubMed search completed
[INFO]   Processing article 1/5
...
```

**Leave it running for a few minutes** to generate interesting data.

### Step 8: Watch Metrics Appear

1. Go back to the Grafana dashboard (http://localhost:3000)
2. Refresh the page
3. You should now see:
   - **Pipeline Execution Rate** showing activity
   - **Active Operations** showing currently running pipelines
   - **Drug Score Distribution** showing score ranges
   - **Gating Decisions** pie chart showing GO/MAYBE/NO-GO splits
   - **Error Rate** graph (should be low)

4. The dashboard auto-refreshes every 10 seconds

### Step 9: Explore Prometheus Queries

Go to Prometheus (http://localhost:9090) and try some queries:

**Pipeline success rate:**
```promql
sum(rate(dr_pipeline_executions_total{status="success"}[5m])) / sum(rate(dr_pipeline_executions_total[5m]))
```

**Average drug score:**
```promql
rate(dr_drug_scores_sum[5m]) / rate(dr_drug_scores_count[5m])
```

**Total gating decisions:**
```promql
dr_gating_decisions_total
```

**LLM extraction duration (95th percentile):**
```promql
histogram_quantile(0.95, rate(dr_llm_extraction_duration_seconds_bucket[5m]))
```

## Quick Reference

### URLs
- Metrics Server: http://localhost:8000/metrics
- Prometheus: http://localhost:9090
- Grafana: http://localhost:3000
- Grafana Dashboard: http://localhost:3000/d/dr-pipeline/dr-pipeline-monitoring

### Default Credentials
- Grafana: admin/admin

### Commands

```bash
# Start metrics server
python scripts/metrics_server.py

# Start monitoring stack
docker-compose -f docker-compose.monitoring.yml up -d

# View logs
docker logs dr-prometheus
docker logs dr-grafana

# Stop monitoring stack
docker-compose -f docker-compose.monitoring.yml down

# Stop and remove all data
docker-compose -f docker-compose.monitoring.yml down -v

# Generate test metrics
python scripts/test_monitoring.py
```

## Using Monitoring in Your Scripts

### Basic Example

```python
from src.dr.monitoring import track_pipeline_execution, track_drug_scoring, track_gating_decision

def run_step7():
    # Wrap entire pipeline
    with track_pipeline_execution('step7'):
        # Your pipeline code
        for drug in drugs:
            scores = score_drug(drug)
            track_drug_scoring(scores)  # Track scores

            decision = gate_drug(drug, scores)
            track_gating_decision(decision)  # Track decision
```

### Real Example

See `scripts/step7_score_and_gate_monitored.py` for a complete example of an instrumented pipeline script.

## Troubleshooting

### "Connection refused" when accessing Prometheus/Grafana
- Check containers are running: `docker ps`
- Restart: `docker-compose -f docker-compose.monitoring.yml restart`

### Metrics not appearing in dashboard
- Verify metrics server is running on port 8000
- Check Prometheus targets are UP: http://localhost:9090/targets
- Run test script to generate metrics: `python scripts/test_monitoring.py`
- Wait 15-30 seconds for Prometheus to scrape

### Dashboard shows "No Data"
- Change the time range in Grafana (top right) to "Last 5 minutes"
- Ensure you've generated some metrics by running a pipeline or test script
- Check that Prometheus datasource is configured: Grafana → Configuration → Data sources

### Port 8000/3000/9090 already in use
- Find and kill the process: `lsof -i :8000` (or :3000, :9090)
- Or change the port in the respective config file

## Next Steps

1. **Read the full guide:** See `MONITORING_SETUP.md` for detailed documentation
2. **Instrument your pipelines:** Add monitoring to your actual Step6/Step7 scripts
3. **Set up alerts:** Configure Slack/email notifications for critical errors
4. **Customize dashboards:** Create additional panels for your specific metrics

## Clean Up

When you're done testing:

```bash
# Stop test script (Ctrl+C in its terminal)
# Stop metrics server (Ctrl+C in its terminal)

# Stop and remove monitoring containers
docker-compose -f docker-compose.monitoring.yml down

# Optional: Remove volumes (deletes all stored metrics)
docker-compose -f docker-compose.monitoring.yml down -v
```

## Success Checklist

- [x] Metrics server running on port 8000
- [x] Prometheus scraping metrics (check /targets)
- [x] Grafana accessible on port 3000
- [x] Dashboard showing data from test script
- [x] Able to query metrics in Prometheus

If all checkboxes are ticked, your monitoring stack is fully operational! 🎉
