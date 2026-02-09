"""Unit tests for HTTP utilities"""

import pytest
from unittest.mock import Mock, patch, MagicMock
import requests
from src.dr.common.http import request_with_retries


class TestHTTPRetries:
    """Tests for HTTP request retry logic"""

    def _mock_session(self, mock_session_cls, responses):
        """Helper: configure a mock Session to return given responses."""
        mock_sess = MagicMock()
        if isinstance(responses, list):
            mock_sess.request.side_effect = responses
        else:
            mock_sess.request.return_value = responses
        mock_session_cls.return_value = mock_sess
        return mock_sess

    @patch('src.dr.common.http.requests.Session')
    def test_successful_request(self, mock_session_cls):
        """Test successful request on first try"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.raise_for_status = Mock()
        mock_sess = self._mock_session(mock_session_cls, mock_response)

        response = request_with_retries("GET", "https://example.com")

        assert response == mock_response
        assert mock_sess.request.call_count == 1
        mock_response.raise_for_status.assert_called_once()

    @patch('src.dr.common.http.requests.Session')
    @patch('src.dr.common.http.time.sleep')
    def test_retry_on_failure(self, mock_sleep, mock_session_cls):
        """Test request retries on failure"""
        mock_fail = Mock()
        mock_fail.raise_for_status.side_effect = requests.HTTPError("500 Error")

        mock_success = Mock()
        mock_success.status_code = 200
        mock_success.raise_for_status = Mock()

        mock_sess = self._mock_session(mock_session_cls, [mock_fail, mock_fail, mock_success])

        response = request_with_retries("GET", "https://example.com", max_retries=4)

        assert response == mock_success
        assert mock_sess.request.call_count == 3
        assert mock_sleep.call_count == 2

    @patch('src.dr.common.http.requests.Session')
    @patch('src.dr.common.http.time.sleep')
    def test_exhausted_retries(self, mock_sleep, mock_session_cls):
        """Test exception raised when retries exhausted"""
        mock_response = Mock()
        mock_response.raise_for_status.side_effect = requests.HTTPError("500 Error")
        mock_sess = self._mock_session(mock_session_cls, mock_response)

        with pytest.raises(RuntimeError):
            request_with_retries("GET", "https://example.com", max_retries=2)

        assert mock_sess.request.call_count == 2

    @patch('src.dr.common.http.requests.Session')
    def test_post_request(self, mock_session_cls):
        """Test POST request with data"""
        mock_response = Mock()
        mock_response.status_code = 201
        mock_response.raise_for_status = Mock()
        mock_sess = self._mock_session(mock_session_cls, mock_response)

        response = request_with_retries(
            "POST",
            "https://example.com/api",
            json={"key": "value"}
        )

        assert response == mock_response
        assert mock_sess.request.call_count == 1
        call_kwargs = mock_sess.request.call_args[1]
        assert call_kwargs["json"] == {"key": "value"}

    @patch('src.dr.common.http.requests.Session')
    def test_request_with_timeout(self, mock_session_cls):
        """Test request respects timeout parameter"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.raise_for_status = Mock()
        mock_sess = self._mock_session(mock_session_cls, mock_response)

        request_with_retries("GET", "https://example.com", timeout=10)

        call_kwargs = mock_sess.request.call_args[1]
        assert call_kwargs["timeout"] == 10

    @patch('src.dr.common.http.requests.Session')
    def test_request_with_headers(self, mock_session_cls):
        """Test request includes custom headers"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.raise_for_status = Mock()
        mock_sess = self._mock_session(mock_session_cls, mock_response)

        headers = {"Authorization": "Bearer token123"}
        request_with_retries("GET", "https://example.com", headers=headers)

        call_kwargs = mock_sess.request.call_args[1]
        assert call_kwargs["headers"] == headers

    @patch('src.dr.common.http.requests.Session')
    @patch('src.dr.common.http.time.sleep')
    def test_linear_backoff(self, mock_sleep, mock_session_cls):
        """Test retry delay increases linearly (retry_sleep * attempt)"""
        mock_response = Mock()
        mock_response.raise_for_status.side_effect = requests.HTTPError("500 Error")
        self._mock_session(mock_session_cls, mock_response)

        with pytest.raises(RuntimeError):
            request_with_retries(
                "GET",
                "https://example.com",
                max_retries=3,
                retry_sleep=1.0
            )

        # sleep called after attempts 1 and 2 (not after last attempt)
        assert mock_sleep.call_count == 2
        sleep_calls = [call[0][0] for call in mock_sleep.call_args_list]
        # Linear backoff: 1*1.0=1.0, 2*1.0=2.0
        assert sleep_calls[0] == pytest.approx(1.0)
        assert sleep_calls[1] == pytest.approx(2.0)

    @patch('src.dr.common.http.requests.Session')
    def test_different_http_methods(self, mock_session_cls):
        """Test supports different HTTP methods"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.raise_for_status = Mock()
        mock_sess = self._mock_session(mock_session_cls, mock_response)

        for method in ["GET", "POST", "PUT", "DELETE"]:
            mock_sess.request.reset_mock()
            mock_sess.request.return_value = mock_response
            request_with_retries(method, "https://example.com")
            assert mock_sess.request.call_args[0][0] == method
