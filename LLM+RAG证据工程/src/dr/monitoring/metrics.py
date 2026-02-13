"""Prometheus metrics for LLM+RAG证据工程 monitoring"""

from prometheus_client import Counter, Histogram, Gauge, Info
import time
from contextlib import contextmanager

from ..logger import get_logger

logger = get_logger(__name__)

# Pipeline Metrics
pipeline_executions_total = Counter(
    'dr_pipeline_executions_total',
    'Total pipeline executions',
    ['pipeline', 'status']
)

pipeline_duration_seconds = Histogram(
    'dr_pipeline_duration_seconds',
    'Pipeline execution duration',
    ['pipeline'],
    buckets=[1, 5, 10, 30, 60, 120, 300, 600]
)

# PubMed Metrics
pubmed_requests_total = Counter(
    'dr_pubmed_requests_total',
    'Total PubMed requests',
    ['operation', 'status']
)

pubmed_request_duration_seconds = Histogram(
    'dr_pubmed_request_duration_seconds',
    'PubMed request duration',
    ['operation'],
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30]
)

# LLM Metrics
llm_extractions_total = Counter(
    'dr_llm_extractions_total',
    'Total LLM extractions',
    ['status']
)

llm_extraction_duration_seconds = Histogram(
    'dr_llm_extraction_duration_seconds',
    'LLM extraction duration',
    buckets=[0.5, 1, 2, 5, 10, 30, 60]
)

# Scoring Metrics
drug_scores = Histogram(
    'dr_drug_scores',
    'Drug total scores',
    buckets=[0, 20, 40, 50, 60, 70, 80, 90, 100]
)

gating_decisions_total = Counter(
    'dr_gating_decisions_total',
    'Gating decisions',
    ['decision']
)

# System Metrics
errors_total = Counter(
    'dr_errors_total',
    'Total errors',
    ['module', 'error_type']
)

active_operations = Gauge(
    'dr_active_operations',
    'Active operations',
    ['operation']
)

system_info = Info('dr_system_info', 'System information')

class MetricsTracker:
    def __init__(self):
        logger.info("Metrics tracker initialized")
        system_info.info({
            'version': '1.0.0',
            'python_version': '3.11'
        })

@contextmanager
def track_pipeline_execution(pipeline: str):
    start_time = time.time()
    active_operations.labels(operation=pipeline).inc()
    try:
        yield
        duration = time.time() - start_time
        pipeline_executions_total.labels(pipeline=pipeline, status='success').inc()
        pipeline_duration_seconds.labels(pipeline=pipeline).observe(duration)
    except Exception as e:
        pipeline_executions_total.labels(pipeline=pipeline, status='failure').inc()
        errors_total.labels(module=pipeline, error_type=type(e).__name__).inc()
        raise
    finally:
        active_operations.labels(operation=pipeline).dec()

@contextmanager
def track_pubmed_request(operation: str):
    start_time = time.time()
    try:
        yield
        duration = time.time() - start_time
        pubmed_requests_total.labels(operation=operation, status='success').inc()
        pubmed_request_duration_seconds.labels(operation=operation).observe(duration)
    except Exception as e:
        pubmed_requests_total.labels(operation=operation, status='error').inc()
        errors_total.labels(module='pubmed', error_type=type(e).__name__).inc()
        raise

@contextmanager
def track_llm_extraction():
    start_time = time.time()
    try:
        yield
        duration = time.time() - start_time
        llm_extractions_total.labels(status='success').inc()
        llm_extraction_duration_seconds.observe(duration)
    except Exception as e:
        llm_extractions_total.labels(status='failure').inc()
        errors_total.labels(module='llm', error_type=type(e).__name__).inc()
        raise

def track_drug_scoring(scores: dict):
    total = scores.get('total_score_0_100', 0)
    drug_scores.observe(total)

def track_gating_decision(decision: str, gate_reasons: list = None):
    gating_decisions_total.labels(decision=decision).inc()


def record_llm_extraction(success: bool, duration_seconds: float, error_type: str = "unknown"):
    """Record one LLM extraction attempt outcome.

    This helper is useful in code paths that do not raise on failure
    (e.g., returning None after retries), where context managers alone
    cannot capture failure counts.
    """
    status = "success" if success else "failure"
    llm_extractions_total.labels(status=status).inc()
    llm_extraction_duration_seconds.observe(max(0.0, float(duration_seconds)))
    if not success:
        errors_total.labels(module="llm", error_type=str(error_type or "unknown")).inc()

metrics = MetricsTracker()


def collect_summary() -> dict[str, float]:
    """Collect current metric values as a flat dict for alerting.

    Reads Prometheus metric values and returns a dictionary suitable
    for passing to AlertEngine.evaluate().

    Returns:
        Dictionary of metric names to current values.
    """
    summary: dict[str, float] = {}

    # Pipeline metrics
    try:
        success = pipeline_executions_total.labels(pipeline="main", status="success")._value.get()
        failure = pipeline_executions_total.labels(pipeline="main", status="failure")._value.get()
        total = success + failure
        if total > 0:
            summary["pipeline_fail_rate"] = failure / total
    except (AttributeError, TypeError):
        pass

    # LLM metrics
    try:
        llm_success = llm_extractions_total.labels(status="success")._value.get()
        llm_failure = llm_extractions_total.labels(status="failure")._value.get()
        llm_total = llm_success + llm_failure
        if llm_total > 0:
            summary["llm_fail_rate"] = llm_failure / llm_total
    except (AttributeError, TypeError):
        pass

    # Gating decisions
    try:
        go = gating_decisions_total.labels(decision="GO")._value.get()
        nogo = gating_decisions_total.labels(decision="NO-GO")._value.get()
        maybe = gating_decisions_total.labels(decision="MAYBE")._value.get()
        total_decisions = go + nogo + maybe
        if total_decisions > 0:
            summary["nogo_ratio"] = nogo / total_decisions
    except (AttributeError, TypeError):
        pass

    return summary
