"""Unit tests for Ollama client."""

import json
from unittest.mock import Mock, patch

from src.dr.evidence.ollama import OllamaClient


class _StreamResponse:
    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self, decode_unicode=True):
        for line in self._lines:
            if decode_unicode:
                yield line
            else:
                yield line.encode("utf-8")


class TestOllamaChat:
    @patch("src.dr.evidence.ollama.request_with_retries")
    def test_chat_non_stream(self, mock_request):
        mock_response = Mock()
        mock_response.json.return_value = {
            "message": {"role": "assistant", "content": '{"ok": true}'}
        }
        mock_request.return_value = mock_response

        client = OllamaClient()
        out = client.chat(
            messages=[{"role": "user", "content": "hello"}],
            format="json",
            stream=False,
        )

        assert out is not None
        assert out["message"]["content"] == '{"ok": true}'
        payload = mock_request.call_args.kwargs["json"]
        assert payload["stream"] is False

    @patch("src.dr.evidence.ollama.request_with_retries")
    def test_chat_stream_aggregates_chunks(self, mock_request):
        lines = [
            json.dumps({"message": {"role": "assistant", "content": "Hello "}, "done": False}),
            json.dumps({"message": {"role": "assistant", "content": "world"}, "done": False}),
            json.dumps({"done": True, "eval_count": 12, "total_duration": 456}),
        ]
        mock_request.return_value = _StreamResponse(lines)

        client = OllamaClient()
        out = client.chat(
            messages=[{"role": "user", "content": "hello"}],
            stream=True,
        )

        assert out is not None
        assert out["message"]["content"] == "Hello world"
        assert out["done"] is True
        assert out["eval_count"] == 12
        assert out["total_duration"] == 456
        payload = mock_request.call_args.kwargs["json"]
        assert payload["stream"] is True

    @patch("src.dr.evidence.ollama.request_with_retries")
    def test_chat_stream_skips_invalid_chunks(self, mock_request):
        lines = [
            "not-json-line",
            json.dumps({"message": {"role": "assistant", "content": "A"}, "done": False}),
            json.dumps({"done": True}),
        ]
        mock_request.return_value = _StreamResponse(lines)

        client = OllamaClient()
        out = client.chat(messages=[{"role": "user", "content": "x"}], stream=True)

        assert out is not None
        assert out["message"]["content"] == "A"
        assert out["done"] is True

    @patch("src.dr.evidence.ollama.request_with_retries")
    def test_chat_stream_request_failure_returns_none(self, mock_request):
        mock_request.side_effect = RuntimeError("connect failed")

        client = OllamaClient()
        out = client.chat(messages=[{"role": "user", "content": "x"}], stream=True)

        assert out is None

