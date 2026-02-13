"""Unit tests for LLMEvidenceExtractor with industrial-grade features"""

import pytest
import json
from unittest.mock import Mock, patch
from src.dr.evidence.extractor import (
    LLMEvidenceExtractor,
    EvidenceExtraction,
    BatchResult,
    repair_json,
    validate_extraction,
    coerce_extraction,
    detect_hallucination,
    VALID_DIRECTIONS,
    VALID_MODELS,
    VALID_ENDPOINTS,
    VALID_CONFIDENCES,
)


# ============================================================
# JSON Repair Tests
# ============================================================

class TestRepairJson:
    def test_valid_json_passes_through(self):
        j = '{"direction": "benefit", "model": "animal"}'
        assert repair_json(j) is not None
        data = json.loads(repair_json(j))
        assert data["direction"] == "benefit"

    def test_markdown_wrapped_json(self):
        raw = '```json\n{"direction": "benefit"}\n```'
        result = repair_json(raw)
        assert result is not None
        data = json.loads(result)
        assert data["direction"] == "benefit"

    def test_markdown_no_language(self):
        raw = '```\n{"direction": "harm"}\n```'
        result = repair_json(raw)
        assert result is not None
        data = json.loads(result)
        assert data["direction"] == "harm"

    def test_trailing_comma_object(self):
        raw = '{"direction": "benefit", "model": "animal",}'
        result = repair_json(raw)
        assert result is not None
        data = json.loads(result)
        assert data["model"] == "animal"

    def test_trailing_comma_array(self):
        raw = '["a", "b", "c",]'
        result = repair_json(raw)
        assert result is not None
        data = json.loads(result)
        assert data == ["a", "b", "c"]

    def test_extra_text_before_json(self):
        raw = 'Here is the result:\n{"direction": "neutral"}'
        result = repair_json(raw)
        assert result is not None
        data = json.loads(result)
        assert data["direction"] == "neutral"

    def test_extra_text_after_json(self):
        raw = '{"direction": "harm"}\nI hope this helps!'
        result = repair_json(raw)
        assert result is not None
        data = json.loads(result)
        assert data["direction"] == "harm"

    def test_empty_string(self):
        assert repair_json("") is None
        assert repair_json(None) is None

    def test_no_json_at_all(self):
        assert repair_json("This is just text with no JSON") is None

    def test_nested_json(self):
        raw = '{"outer": {"inner": "value"}, "list": [1, 2]}'
        result = repair_json(raw)
        assert result is not None
        data = json.loads(result)
        assert data["outer"]["inner"] == "value"

    def test_json_with_strings_containing_brackets(self):
        raw = '{"text": "this has {curly} and [square] brackets"}'
        result = repair_json(raw)
        assert result is not None
        data = json.loads(result)
        assert "{curly}" in data["text"]


# ============================================================
# Validation Tests
# ============================================================

class TestValidateExtraction:
    def test_valid_extraction(self):
        data = {
            "direction": "benefit", "model": "animal",
            "endpoint": "PLAQUE_IMAGING", "mechanism": "test", "confidence": "HIGH"
        }
        is_valid, issues = validate_extraction(data)
        assert is_valid
        assert issues == []

    def test_missing_required_field(self):
        data = {"direction": "benefit", "model": "animal", "endpoint": "OTHER", "confidence": "HIGH"}
        is_valid, issues = validate_extraction(data)
        assert not is_valid
        assert any("mechanism" in i for i in issues)

    def test_invalid_direction(self):
        data = {
            "direction": "good", "model": "animal",
            "endpoint": "OTHER", "mechanism": "test", "confidence": "HIGH"
        }
        is_valid, issues = validate_extraction(data)
        assert not is_valid
        assert any("direction" in i for i in issues)

    def test_invalid_model(self):
        data = {
            "direction": "benefit", "model": "fish",
            "endpoint": "OTHER", "mechanism": "test", "confidence": "HIGH"
        }
        is_valid, issues = validate_extraction(data)
        assert not is_valid

    def test_invalid_endpoint(self):
        data = {
            "direction": "benefit", "model": "human",
            "endpoint": "WRONG", "mechanism": "test", "confidence": "HIGH"
        }
        is_valid, issues = validate_extraction(data)
        assert not is_valid

    def test_invalid_confidence(self):
        data = {
            "direction": "benefit", "model": "human",
            "endpoint": "OTHER", "mechanism": "test", "confidence": "VERY_HIGH"
        }
        is_valid, issues = validate_extraction(data)
        assert not is_valid

    def test_all_valid_enum_values(self):
        for d in VALID_DIRECTIONS:
            for m in VALID_MODELS:
                for e in VALID_ENDPOINTS:
                    for c in VALID_CONFIDENCES:
                        data = {"direction": d, "model": m, "endpoint": e,
                                "mechanism": "test", "confidence": c}
                        is_valid, _ = validate_extraction(data)
                        assert is_valid, f"Should be valid: {d}/{m}/{e}/{c}"


# ============================================================
# Coercion Tests
# ============================================================

class TestCoerceExtraction:
    def test_coerce_direction_variants(self):
        assert coerce_extraction({"direction": "beneficial"})["direction"] == "benefit"
        assert coerce_extraction({"direction": "harmful"})["direction"] == "harm"
        assert coerce_extraction({"direction": "positive"})["direction"] == "benefit"
        assert coerce_extraction({"direction": "unknown"})["direction"] == "unclear"

    def test_coerce_model_variants(self):
        assert coerce_extraction({"model": "mouse"})["model"] == "animal"
        assert coerce_extraction({"model": "mice"})["model"] == "animal"
        assert coerce_extraction({"model": "in vitro"})["model"] == "cell"
        assert coerce_extraction({"model": "patient"})["model"] == "human"
        assert coerce_extraction({"model": "review"})["model"] == "unclear"

    def test_coerce_endpoint_case(self):
        assert coerce_extraction({"endpoint": "plaque_imaging"})["endpoint"] == "PLAQUE_IMAGING"
        assert coerce_extraction({"endpoint": "cv_events"})["endpoint"] == "CV_EVENTS"

    def test_coerce_confidence_variants(self):
        assert coerce_extraction({"confidence": "high"})["confidence"] == "HIGH"
        assert coerce_extraction({"confidence": "MEDIUM"})["confidence"] == "MED"
        assert coerce_extraction({"confidence": "low"})["confidence"] == "LOW"

    def test_coerce_preserves_valid_values(self):
        data = {"direction": "benefit", "model": "human", "endpoint": "CV_EVENTS"}
        coerced = coerce_extraction(data)
        assert coerced["direction"] == "benefit"
        assert coerced["model"] == "human"
        assert coerced["endpoint"] == "CV_EVENTS"


# ============================================================
# Hallucination Detection Tests
# ============================================================

class TestDetectHallucination:
    def test_no_hallucination(self):
        data = {"pmid": "123", "mechanism": "Resveratrol reduces plaque inflammation"}
        warnings = detect_hallucination(
            data, "123",
            "Resveratrol reduces atherosclerotic plaque through inflammation pathways",
            "resveratrol"
        )
        assert len(warnings) == 0

    def test_pmid_mismatch(self):
        data = {"pmid": "999", "mechanism": "test"}
        warnings = detect_hallucination(data, "123", "abstract", "drug")
        assert any("pmid_mismatch" in w for w in warnings)

    def test_pmid_empty_no_warning(self):
        data = {"pmid": "", "mechanism": "test"}
        warnings = detect_hallucination(data, "123", "abstract", "drug")
        assert not any("pmid_mismatch" in w for w in warnings)

    def test_drug_not_grounded(self):
        data = {"mechanism": "affects the pathway"}
        warnings = detect_hallucination(
            data, "123",
            "This study examines cardiovascular outcomes",
            "resveratrol"
        )
        assert any("drug_not_grounded" in w for w in warnings)

    def test_drug_grounded_exact(self):
        data = {"mechanism": "test"}
        warnings = detect_hallucination(
            data, "123",
            "Resveratrol was administered to mice",
            "resveratrol"
        )
        assert not any("drug_not_grounded" in w for w in warnings)

    def test_drug_grounded_token(self):
        data = {"mechanism": "test"}
        warnings = detect_hallucination(
            data, "123",
            "The compound nicotinamide was tested",
            "nicotinamide riboside"
        )
        assert not any("drug_not_grounded" in w for w in warnings)

    def test_mechanism_unanchored(self):
        data = {"mechanism": "Activates the xyzzynol pathway through zymotransferase regulation"}
        warnings = detect_hallucination(
            data, "123",
            "This study examines resveratrol in atherosclerosis mouse models",
            "resveratrol"
        )
        assert any("mechanism_unanchored" in w for w in warnings)

    def test_mechanism_well_anchored(self):
        data = {"mechanism": "Reduces atherosclerotic plaque through inflammation suppression"}
        warnings = detect_hallucination(
            data, "123",
            "We studied inflammation suppression and atherosclerotic plaque reduction",
            "resveratrol"
        )
        assert not any("mechanism_unanchored" in w for w in warnings)


# ============================================================
# EvidenceExtraction Dataclass Tests
# ============================================================

class TestEvidenceExtraction:
    def test_to_dict(self):
        extraction = EvidenceExtraction(
            pmid="12345678", direction="benefit", model="animal",
            endpoint="PLAQUE_IMAGING", mechanism="Test mechanism", confidence="HIGH"
        )
        result = extraction.to_dict()
        assert isinstance(result, dict)
        assert result["pmid"] == "12345678"
        assert "raw_response" not in result

    def test_to_dict_with_warnings(self):
        extraction = EvidenceExtraction(
            pmid="123", direction="benefit", model="animal",
            endpoint="OTHER", mechanism="test", confidence="HIGH",
            warnings=["pmid_mismatch: test"]
        )
        result = extraction.to_dict()
        assert "warnings" in result
        assert len(result["warnings"]) == 1

    def test_to_dict_without_warnings(self):
        extraction = EvidenceExtraction(
            pmid="123", direction="benefit", model="animal",
            endpoint="OTHER", mechanism="test", confidence="HIGH"
        )
        result = extraction.to_dict()
        assert "warnings" not in result

    def test_has_warnings_property(self):
        e1 = EvidenceExtraction("1", "benefit", "animal", "OTHER", "m", "HIGH", warnings=["w"])
        e2 = EvidenceExtraction("2", "benefit", "animal", "OTHER", "m", "HIGH")
        assert e1.has_warnings is True
        assert e2.has_warnings is False


class TestBatchResult:
    def test_success_rate(self):
        r = BatchResult(extractions=[], total=10, success=7, failed=3)
        assert r.success_rate == 0.7

    def test_success_rate_zero(self):
        r = BatchResult(extractions=[], total=0)
        assert r.success_rate == 0.0

    def test_summary(self):
        r = BatchResult(extractions=[], total=5, success=3, failed=1, skipped=1, hallucination_warnings=1)
        s = r.summary()
        assert "3/5" in s
        assert "60%" in s


# ============================================================
# LLMEvidenceExtractor Tests
# ============================================================

class TestLLMEvidenceExtractor:
    def test_initialization(self):
        extractor = LLMEvidenceExtractor()
        assert extractor.model == "qwen2.5:7b-instruct"
        assert len(extractor.temperatures) == 3

    def test_custom_init(self):
        extractor = LLMEvidenceExtractor(
            model="test-model",
            temperatures=[0.5, 0.0],
            retry_base_delay=0.0,
            hallucination_check=False,
        )
        assert extractor.model == "test-model"
        assert len(extractor.temperatures) == 2
        assert extractor.hallucination_check is False

    @patch('src.dr.evidence.extractor.OllamaClient')
    def test_extract_success(self, mock_ollama_class):
        mock_client = Mock()
        mock_ollama_class.return_value = mock_client
        mock_client.generate.return_value = json.dumps({
            "direction": "benefit", "model": "animal",
            "endpoint": "PLAQUE_IMAGING",
            "mechanism": "Reduces atherosclerotic plaque via SIRT1",
            "confidence": "HIGH"
        })

        extractor = LLMEvidenceExtractor(retry_base_delay=0)
        result = extractor.extract("12345", "Title about resveratrol",
                                   "Resveratrol reduces plaque", "resveratrol")

        assert result is not None
        assert result.direction == "benefit"
        assert result.model == "animal"

    @patch('src.dr.evidence.extractor.OllamaClient')
    def test_extract_retry_on_empty(self, mock_ollama_class):
        """First attempt returns empty, second succeeds"""
        mock_client = Mock()
        mock_ollama_class.return_value = mock_client
        mock_client.generate.side_effect = [
            None,  # First attempt empty
            json.dumps({
                "direction": "benefit", "model": "human",
                "endpoint": "CV_EVENTS", "mechanism": "test", "confidence": "MED"
            })
        ]

        extractor = LLMEvidenceExtractor(retry_base_delay=0)
        result = extractor.extract("111", "Title", "Abstract", "drug")

        assert result is not None
        assert result.direction == "benefit"
        assert mock_client.generate.call_count == 2

    @patch('src.dr.evidence.extractor.OllamaClient')
    def test_extract_retry_on_bad_json(self, mock_ollama_class):
        """First attempt bad JSON, second succeeds"""
        mock_client = Mock()
        mock_ollama_class.return_value = mock_client
        mock_client.generate.side_effect = [
            "not json at all {{{{",
            json.dumps({
                "direction": "harm", "model": "animal",
                "endpoint": "BIOMARKER", "mechanism": "test", "confidence": "LOW"
            })
        ]

        extractor = LLMEvidenceExtractor(retry_base_delay=0)
        result = extractor.extract("111", "Title", "Abstract", "drug")

        assert result is not None
        assert result.direction == "harm"

    @patch('src.dr.evidence.extractor.OllamaClient')
    def test_extract_all_retries_fail(self, mock_ollama_class):
        mock_client = Mock()
        mock_ollama_class.return_value = mock_client
        mock_client.generate.return_value = None

        extractor = LLMEvidenceExtractor(retry_base_delay=0)
        result = extractor.extract("111", "Title", "Abstract", "drug")

        assert result is None
        assert mock_client.generate.call_count == 3  # default 3 temperatures

    @patch('src.dr.evidence.extractor.OllamaClient')
    def test_extract_json_repair_works(self, mock_ollama_class):
        """Test that JSON repair handles markdown-wrapped response"""
        mock_client = Mock()
        mock_ollama_class.return_value = mock_client
        mock_client.generate.return_value = '```json\n{"direction": "benefit", "model": "animal", "endpoint": "PLAQUE_IMAGING", "mechanism": "test", "confidence": "HIGH"}\n```'

        extractor = LLMEvidenceExtractor(retry_base_delay=0)
        result = extractor.extract("111", "Title", "Abstract", "drug")

        assert result is not None
        assert result.direction == "benefit"

    @patch('src.dr.evidence.extractor.OllamaClient')
    def test_extract_coercion_works(self, mock_ollama_class):
        """Test that near-valid values get coerced"""
        mock_client = Mock()
        mock_ollama_class.return_value = mock_client
        mock_client.generate.return_value = json.dumps({
            "direction": "beneficial",  # -> benefit
            "model": "mice",           # -> animal
            "endpoint": "BIOMARKER",
            "mechanism": "test",
            "confidence": "MEDIUM"     # -> MED
        })

        extractor = LLMEvidenceExtractor(retry_base_delay=0)
        result = extractor.extract("111", "Title", "Abstract", "drug")

        assert result is not None
        assert result.direction == "benefit"
        assert result.model == "animal"
        assert result.confidence == "MED"

    @patch('src.dr.evidence.extractor.OllamaClient')
    def test_extract_hallucination_warning(self, mock_ollama_class):
        mock_client = Mock()
        mock_ollama_class.return_value = mock_client
        mock_client.generate.return_value = json.dumps({
            "direction": "benefit", "model": "animal",
            "endpoint": "PLAQUE_IMAGING",
            "mechanism": "Activates xyzzynol pathway through zymotransferase",
            "confidence": "HIGH"
        })

        extractor = LLMEvidenceExtractor(retry_base_delay=0, hallucination_check=True)
        result = extractor.extract(
            "111", "Title about resveratrol",
            "Resveratrol reduces atherosclerosis in mice via inflammation",
            "resveratrol"
        )

        assert result is not None
        assert result.has_warnings  # mechanism terms not in abstract

    @patch('src.dr.evidence.extractor.OllamaClient')
    def test_extract_batch_returns_batch_result(self, mock_ollama_class):
        mock_client = Mock()
        mock_ollama_class.return_value = mock_client
        mock_client.generate.return_value = json.dumps({
            "direction": "benefit", "model": "animal",
            "endpoint": "PLAQUE_IMAGING", "mechanism": "test", "confidence": "HIGH"
        })

        extractor = LLMEvidenceExtractor(retry_base_delay=0)
        papers = [
            {"pmid": "111", "title": "Paper 1", "abstract": "Abstract 1"},
            {"pmid": "222", "title": "Paper 2", "abstract": "Abstract 2"},
        ]
        result = extractor.extract_batch(papers, drug_name="test_drug")

        assert isinstance(result, BatchResult)
        assert result.total == 2
        assert result.success == 2
        assert len(result.extractions) == 2

    @patch('src.dr.evidence.extractor.OllamaClient')
    def test_extract_batch_with_failures(self, mock_ollama_class):
        mock_client = Mock()
        mock_ollama_class.return_value = mock_client
        mock_client.generate.side_effect = [
            json.dumps({"direction": "benefit", "model": "animal", "endpoint": "PLAQUE_IMAGING", "mechanism": "Test", "confidence": "HIGH"}),
            None, None, None,  # 3 retries for paper 2, all fail
            json.dumps({"direction": "harm", "model": "cell", "endpoint": "BIOMARKER", "mechanism": "Test", "confidence": "MED"})
        ]

        extractor = LLMEvidenceExtractor(retry_base_delay=0)
        papers = [
            {"pmid": "111", "title": "P1", "abstract": "A1"},
            {"pmid": "222", "title": "P2", "abstract": "A2"},
            {"pmid": "333", "title": "P3", "abstract": "A3"},
        ]
        result = extractor.extract_batch(papers, drug_name="test")

        assert result.success == 2
        assert result.failed == 1
        assert len(result.extractions) == 2

    @patch('src.dr.evidence.extractor.OllamaClient')
    def test_extract_batch_skips_empty(self, mock_ollama_class):
        mock_client = Mock()
        mock_ollama_class.return_value = mock_client
        mock_client.generate.return_value = json.dumps({
            "direction": "benefit", "model": "animal",
            "endpoint": "OTHER", "mechanism": "test", "confidence": "HIGH"
        })

        extractor = LLMEvidenceExtractor(retry_base_delay=0)
        papers = [
            {"pmid": "111", "title": "", "abstract": ""},  # skip
            {"pmid": "222", "title": "Title", "abstract": "Abstract"},
        ]
        result = extractor.extract_batch(papers, drug_name="test")

        assert result.total == 2
        assert result.skipped == 1
        assert result.success == 1

    @patch('src.dr.evidence.extractor.OllamaClient')
    def test_extract_batch_max_papers(self, mock_ollama_class):
        mock_client = Mock()
        mock_ollama_class.return_value = mock_client
        mock_client.generate.return_value = json.dumps({
            "direction": "benefit", "model": "animal",
            "endpoint": "OTHER", "mechanism": "test", "confidence": "HIGH"
        })

        extractor = LLMEvidenceExtractor(retry_base_delay=0)
        papers = [{"pmid": str(i), "title": f"P{i}", "abstract": "A"} for i in range(50)]
        result = extractor.extract_batch(papers, drug_name="test", max_papers=10)

        assert result.total == 10
        assert result.success == 10

    @patch('src.dr.evidence.extractor.OllamaClient')
    def test_extract_records_monitoring_success(self, mock_ollama_class, monkeypatch):
        """Successful extraction should report success metric once."""
        mock_client = Mock()
        mock_ollama_class.return_value = mock_client
        mock_client.generate.return_value = json.dumps({
            "direction": "benefit",
            "model": "animal",
            "endpoint": "PLAQUE_IMAGING",
            "mechanism": "Reduces atherosclerotic plaque",
            "confidence": "HIGH",
        })

        calls = []

        def fake_record(success: bool, duration_seconds: float, error_type: str = "unknown"):
            calls.append((success, duration_seconds, error_type))

        monkeypatch.setattr("src.dr.evidence.extractor.record_llm_extraction", fake_record)

        extractor = LLMEvidenceExtractor(retry_base_delay=0)
        result = extractor.extract("12345", "Title", "Resveratrol reduces plaque", "resveratrol")

        assert result is not None
        assert len(calls) == 1
        assert calls[0][0] is True

    @patch('src.dr.evidence.extractor.OllamaClient')
    def test_extract_records_monitoring_failure(self, mock_ollama_class, monkeypatch):
        """Exhausted retries should report failure metric once."""
        mock_client = Mock()
        mock_ollama_class.return_value = mock_client
        mock_client.generate.return_value = None

        calls = []

        def fake_record(success: bool, duration_seconds: float, error_type: str = "unknown"):
            calls.append((success, duration_seconds, error_type))

        monkeypatch.setattr("src.dr.evidence.extractor.record_llm_extraction", fake_record)

        extractor = LLMEvidenceExtractor(retry_base_delay=0)
        result = extractor.extract("12345", "Title", "Abstract", "resveratrol")

        assert result is None
        assert len(calls) == 1
        assert calls[0][0] is False
        assert calls[0][2] == "attempts_exhausted"
