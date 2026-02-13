"""Unit tests for ClinicalTrials.gov client."""

import pytest
from unittest.mock import Mock, patch

from src.dr.retrieval.ctgov import CTGovClient


class TestCTGovClientSearch:
    @patch("src.dr.retrieval.ctgov.request_with_retries")
    def test_search_by_condition_single_page(self, mock_request):
        mock_response = Mock()
        mock_response.json.return_value = {
            "studies": [
                {"protocolSection": {"identificationModule": {"nctId": "NCT00000001"}}},
                {"protocolSection": {"identificationModule": {"nctId": "NCT00000002"}}},
            ]
        }
        mock_request.return_value = mock_response

        client = CTGovClient(use_cache=False)
        ids = client.search_by_condition("atherosclerosis", max_results=10)

        assert ids == ["NCT00000001", "NCT00000002"]
        call_kwargs = mock_request.call_args.kwargs
        assert call_kwargs["params"]["query.cond"] == "atherosclerosis"
        assert call_kwargs["params"]["pageSize"] == 10

    @patch("src.dr.retrieval.ctgov.request_with_retries")
    def test_search_by_condition_pagination_and_dedup(self, mock_request):
        page1 = Mock()
        page1.json.return_value = {
            "studies": [
                {"protocolSection": {"identificationModule": {"nctId": "NCT00000001"}}},
                {"protocolSection": {"identificationModule": {"nctId": "NCT00000002"}}},
            ],
            "nextPageToken": "token-1",
        }
        page2 = Mock()
        page2.json.return_value = {
            "studies": [
                {"protocolSection": {"identificationModule": {"nctId": "NCT00000002"}}},
                {"nctId": "NCT00000003"},
            ],
        }
        mock_request.side_effect = [page1, page2]

        client = CTGovClient(use_cache=False)
        ids = client.search_by_condition("atherosclerosis", max_results=3)

        assert ids == ["NCT00000001", "NCT00000002", "NCT00000003"]
        assert mock_request.call_count == 2
        second_call_kwargs = mock_request.call_args_list[1].kwargs
        assert second_call_kwargs["params"]["pageToken"] == "token-1"

    @patch("src.dr.retrieval.ctgov.request_with_retries")
    def test_search_by_condition_empty_condition(self, mock_request):
        client = CTGovClient(use_cache=False)
        ids = client.search_by_condition("", max_results=10)
        assert ids == []
        mock_request.assert_not_called()

    @patch("src.dr.retrieval.ctgov.request_with_retries")
    def test_search_by_condition_http_error(self, mock_request):
        mock_request.side_effect = RuntimeError("network down")

        client = CTGovClient(use_cache=False)
        with pytest.raises(RuntimeError) as exc:
            client.search_by_condition("atherosclerosis", max_results=5)

        assert "CT.gov search failed" in str(exc.value)

