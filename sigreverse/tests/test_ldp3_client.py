"""Unit tests for sigreverse.ldp3_client module.

Tests cover:
    - Custom exception hierarchy
    - HTTP status code classification (429, 404, 4xx, 5xx)
    - Retry logic (exponential backoff, Retry-After)
    - Response schema validation
    - Input validation (empty lists, invalid symbols)
    - Request statistics tracking
"""
import json
import pytest
from unittest.mock import patch, MagicMock

from sigreverse.ldp3_client import (
    LDP3Client,
    LDP3Error,
    LDP3RateLimitError,
    LDP3NotFoundError,
    LDP3ServerError,
    LDP3ValidationError,
    _parse_retry_after,
)


# ===== Fixtures =====

@pytest.fixture
def client():
    """Create a client with minimal retry for fast tests."""
    return LDP3Client(
        metadata_api="http://test.api/metadata/",
        data_api="http://test.api/data/",
        timeout_sec=5,
        retries=2,
        backoff_sec=0.01,  # Very short for testing
        max_backoff_sec=0.05,
    )


def _mock_response(status_code=200, json_data=None, text="", headers=None):
    """Create a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.text = text or json.dumps(json_data or {})
    resp.headers = headers or {}
    resp.json.return_value = json_data
    return resp


# ===== Exception hierarchy =====

class TestExceptionHierarchy:
    def test_base_error(self):
        e = LDP3Error("test", status_code=400, url="http://x")
        assert e.status_code == 400
        assert e.url == "http://x"
        assert "test" in str(e)

    def test_rate_limit_is_ldp3_error(self):
        assert issubclass(LDP3RateLimitError, LDP3Error)

    def test_not_found_is_ldp3_error(self):
        assert issubclass(LDP3NotFoundError, LDP3Error)

    def test_server_error_is_ldp3_error(self):
        assert issubclass(LDP3ServerError, LDP3Error)

    def test_validation_error_is_ldp3_error(self):
        assert issubclass(LDP3ValidationError, LDP3Error)


# ===== HTTP status classification =====

class TestHTTPStatusClassification:
    @patch("sigreverse.ldp3_client.requests.Session")
    def test_success_200(self, mock_session_cls, client):
        """200 OK should return parsed JSON."""
        mock_session = MagicMock()
        client.session = mock_session
        mock_session.post.return_value = _mock_response(200, json_data=[{"id": "1"}])

        result = client._post("http://test/api", {"key": "val"})
        assert result == [{"id": "1"}]

    @patch("sigreverse.ldp3_client.requests.Session")
    def test_404_raises_not_found(self, mock_session_cls, client):
        """404 should raise LDP3NotFoundError immediately (no retry)."""
        mock_session = MagicMock()
        client.session = mock_session
        mock_session.post.return_value = _mock_response(404, text="Not Found")

        with pytest.raises(LDP3NotFoundError) as exc_info:
            client._post("http://test/api", {})
        assert exc_info.value.status_code == 404
        # Should NOT retry on 404
        assert mock_session.post.call_count == 1

    @patch("sigreverse.ldp3_client.requests.Session")
    def test_400_raises_client_error(self, mock_session_cls, client):
        """400 Bad Request should raise LDP3Error immediately (no retry)."""
        mock_session = MagicMock()
        client.session = mock_session
        mock_session.post.return_value = _mock_response(400, text="Bad Request")

        with pytest.raises(LDP3Error) as exc_info:
            client._post("http://test/api", {})
        assert exc_info.value.status_code == 400
        assert mock_session.post.call_count == 1

    @patch("sigreverse.ldp3_client.requests.Session")
    def test_429_retries_then_raises(self, mock_session_cls, client):
        """429 should retry then raise LDP3RateLimitError."""
        mock_session = MagicMock()
        client.session = mock_session
        mock_session.post.return_value = _mock_response(429, text="Too Many Requests")

        with pytest.raises(LDP3RateLimitError):
            client._post("http://test/api", {})
        # Should retry (retries=2)
        assert mock_session.post.call_count == 2

    @patch("sigreverse.ldp3_client.requests.Session")
    def test_500_retries_then_raises(self, mock_session_cls, client):
        """500 should retry with backoff then raise LDP3ServerError."""
        mock_session = MagicMock()
        client.session = mock_session
        mock_session.post.return_value = _mock_response(500, text="Internal Server Error")

        with pytest.raises(LDP3ServerError):
            client._post("http://test/api", {})
        assert mock_session.post.call_count == 2

    @patch("sigreverse.ldp3_client.requests.Session")
    def test_500_then_success(self, mock_session_cls, client):
        """500 on first attempt, 200 on second should succeed."""
        mock_session = MagicMock()
        client.session = mock_session
        mock_session.post.side_effect = [
            _mock_response(500, text="Error"),
            _mock_response(200, json_data={"ok": True}),
        ]

        result = client._post("http://test/api", {})
        assert result == {"ok": True}
        assert mock_session.post.call_count == 2


# ===== Retry-After header =====

class TestRetryAfter:
    def test_parse_numeric(self):
        resp = _mock_response(429, headers={"Retry-After": "5"})
        assert _parse_retry_after(resp) == 5.0

    def test_parse_float(self):
        resp = _mock_response(429, headers={"Retry-After": "2.5"})
        assert _parse_retry_after(resp) == 2.5

    def test_missing_header(self):
        resp = _mock_response(429)
        assert _parse_retry_after(resp) is None

    def test_invalid_header(self):
        resp = _mock_response(429, headers={"Retry-After": "not-a-number"})
        assert _parse_retry_after(resp) is None


# ===== Response validation =====

class TestResponseValidation:
    def test_validate_list_valid(self, client):
        result = client._validate_list_response([{"id": "1"}], "http://test")
        assert len(result) == 1

    def test_validate_list_invalid_dict(self, client):
        with pytest.raises(LDP3ValidationError):
            client._validate_list_response({"not": "a list"}, "http://test")

    def test_validate_list_invalid_string(self, client):
        with pytest.raises(LDP3ValidationError):
            client._validate_list_response("string", "http://test")

    def test_validate_enrichment_valid(self, client):
        data = {"results": [{"uuid": "1"}]}
        result = client._validate_enrichment_response(data, "http://test")
        assert "results" in result

    def test_validate_enrichment_missing_results(self, client):
        with pytest.raises(LDP3ValidationError, match="missing 'results'"):
            client._validate_enrichment_response({"data": []}, "http://test")

    def test_validate_enrichment_not_dict(self, client):
        with pytest.raises(LDP3ValidationError):
            client._validate_enrichment_response([1, 2, 3], "http://test")

    def test_validate_enrichment_results_not_list(self, client):
        with pytest.raises(LDP3ValidationError, match="should be a list"):
            client._validate_enrichment_response({"results": "wrong"}, "http://test")


# ===== Input validation =====

class TestInputValidation:
    def test_entities_empty_symbols_raises(self, client):
        with pytest.raises(ValueError, match="must not be empty"):
            client.entities_find_by_symbols([])

    def test_signatures_empty_uuids_raises(self, client):
        with pytest.raises(ValueError, match="must not be empty"):
            client.signatures_find_metadata([])

    def test_enrich_empty_up_raises(self, client):
        with pytest.raises(ValueError, match="up_entities must not be empty"):
            client.enrich_ranktwosided([], ["id1"], 100, "l1000_cp")

    def test_enrich_empty_down_raises(self, client):
        with pytest.raises(ValueError, match="down_entities must not be empty"):
            client.enrich_ranktwosided(["id1"], [], 100, "l1000_cp")

    def test_enrich_invalid_limit_raises(self, client):
        with pytest.raises(ValueError, match="limit must be >= 1"):
            client.enrich_ranktwosided(["id1"], ["id2"], 0, "l1000_cp")

    @patch("sigreverse.ldp3_client.requests.Session")
    def test_entities_filters_invalid_symbols(self, mock_session_cls, client):
        """Non-string or empty symbols should be filtered with warning."""
        mock_session = MagicMock()
        client.session = mock_session
        mock_session.post.return_value = _mock_response(200, json_data=[])

        # Mix of valid and invalid
        result = client.entities_find_by_symbols(["BRCA1", "", "  ", "TP53"])
        # Should have been called (after filtering, still has BRCA1 and TP53)
        assert mock_session.post.call_count == 1


# ===== Statistics tracking =====

class TestStatistics:
    @patch("sigreverse.ldp3_client.requests.Session")
    def test_stats_track_requests(self, mock_session_cls, client):
        mock_session = MagicMock()
        client.session = mock_session
        mock_session.post.return_value = _mock_response(200, json_data=[])

        client._post("http://test/api", {})
        assert client.stats["total_requests"] == 1

    @patch("sigreverse.ldp3_client.requests.Session")
    def test_stats_track_retries(self, mock_session_cls, client):
        mock_session = MagicMock()
        client.session = mock_session
        mock_session.post.return_value = _mock_response(500, text="Error")

        try:
            client._post("http://test/api", {})
        except LDP3ServerError:
            pass
        assert client.stats["retries"] >= 1
        assert client.stats["errors_5xx"] >= 1

    def test_initial_stats_zero(self, client):
        stats = client.stats
        assert all(v == 0 for v in stats.values())
