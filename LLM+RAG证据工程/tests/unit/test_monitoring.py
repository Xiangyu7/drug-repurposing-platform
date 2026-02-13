"""Unit tests for monitoring metrics helpers."""

import pytest
import importlib

m = importlib.import_module("src.dr.monitoring.metrics")


class TestMonitoringMetrics:
    def test_record_llm_extraction_success_and_failure(self):
        success_before = m.llm_extractions_total.labels(status="success")._value.get()
        failure_before = m.llm_extractions_total.labels(status="failure")._value.get()
        error_before = m.errors_total.labels(
            module="llm", error_type="attempts_exhausted"
        )._value.get()

        m.record_llm_extraction(success=True, duration_seconds=0.05)
        m.record_llm_extraction(
            success=False, duration_seconds=0.07, error_type="attempts_exhausted"
        )

        assert m.llm_extractions_total.labels(status="success")._value.get() == success_before + 1
        assert m.llm_extractions_total.labels(status="failure")._value.get() == failure_before + 1
        assert m.errors_total.labels(module="llm", error_type="attempts_exhausted")._value.get() == error_before + 1

    def test_track_pipeline_execution_records_success(self):
        success_before = m.pipeline_executions_total.labels(
            pipeline="unit_test_pipeline", status="success"
        )._value.get()
        active_before = m.active_operations.labels(operation="unit_test_pipeline")._value.get()

        with m.track_pipeline_execution("unit_test_pipeline"):
            pass

        assert m.pipeline_executions_total.labels(
            pipeline="unit_test_pipeline", status="success"
        )._value.get() == success_before + 1
        assert m.active_operations.labels(operation="unit_test_pipeline")._value.get() == active_before

    def test_track_pipeline_execution_records_failure(self):
        failure_before = m.pipeline_executions_total.labels(
            pipeline="unit_test_pipeline_fail", status="failure"
        )._value.get()

        with pytest.raises(RuntimeError):
            with m.track_pipeline_execution("unit_test_pipeline_fail"):
                raise RuntimeError("intentional test failure")

        assert m.pipeline_executions_total.labels(
            pipeline="unit_test_pipeline_fail", status="failure"
        )._value.get() == failure_before + 1
