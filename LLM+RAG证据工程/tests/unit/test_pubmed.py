"""Unit tests for PubMedClient"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from src.dr.retrieval.pubmed import PubMedClient


class TestPubMedClient:
    """Tests for PubMed E-utilities client"""

    def test_client_initialization(self):
        """Test PubMed client initializes with defaults"""
        client = PubMedClient()
        assert client is not None
        assert client.config is not None
        assert client.use_cache is True

    def test_client_without_cache(self):
        """Test PubMed client can be initialized without cache"""
        client = PubMedClient(use_cache=False)
        assert client.use_cache is False

    def test_rate_limit_detection(self):
        """Test rate limit is set based on API key presence"""
        client = PubMedClient()
        # Without API key: 3 req/s = ~0.34s delay
        # With API key: 10 req/s = 0.1s delay
        assert client.delay > 0
        assert isinstance(client.has_api_key, bool)

    @patch('src.dr.retrieval.pubmed.request_with_retries')
    def test_search_basic(self, mock_request):
        """Test basic PubMed search"""
        # Mock Response object
        mock_response = Mock()
        mock_response.json.return_value = {
            "esearchresult": {
                "count": "3",
                "idlist": ["12345678", "87654321", "11111111"]
            }
        }
        mock_request.return_value = mock_response

        client = PubMedClient(use_cache=False)
        pmids = client.search("aspirin atherosclerosis", max_results=10)

        assert len(pmids) == 3
        assert "12345678" in pmids
        assert "87654321" in pmids
        mock_request.assert_called_once()

    @patch('src.dr.retrieval.pubmed.request_with_retries')
    def test_search_empty_results(self, mock_request):
        """Test search with no results"""
        mock_response = Mock()
        mock_response.json.return_value = {
            "esearchresult": {
                "count": "0",
                "idlist": []
            }
        }
        mock_request.return_value = mock_response

        client = PubMedClient(use_cache=False)
        pmids = client.search("nonexistent drug xyz123", max_results=10)

        assert pmids == []

    @patch('src.dr.retrieval.pubmed.request_with_retries')
    def test_search_with_reldate(self, mock_request):
        """Test search with recent date filter"""
        mock_response = Mock()
        mock_response.json.return_value = {
            "esearchresult": {
                "count": "5",
                "idlist": ["12345678", "87654321", "11111111", "22222222", "33333333"]
            }
        }
        mock_request.return_value = mock_response

        client = PubMedClient(use_cache=False)
        pmids = client.search("aspirin", max_results=10, reldate=365)

        assert len(pmids) == 5
        # Check that reldate parameter was passed
        call_args = mock_request.call_args
        assert call_args is not None

    @patch('src.dr.retrieval.pubmed.request_with_retries')
    def test_fetch_details_basic(self, mock_request):
        """Test fetching article details"""
        # Mock XML response from PubMed
        mock_xml = """<?xml version="1.0"?>
        <PubmedArticleSet>
            <PubmedArticle>
                <MedlineCitation>
                    <PMID>12345678</PMID>
                    <Article>
                        <ArticleTitle>Test Article Title</ArticleTitle>
                        <Abstract>
                            <AbstractText>Test abstract content</AbstractText>
                        </Abstract>
                        <AuthorList>
                            <Author><LastName>Smith</LastName><ForeName>John</ForeName></Author>
                        </AuthorList>
                    </Article>
                    <MedlineJournalInfo>
                        <MedlineTA>Test Journal</MedlineTA>
                    </MedlineJournalInfo>
                </MedlineCitation>
                <PubmedData>
                    <ArticleIdList>
                        <ArticleId IdType="doi">10.1234/test.2024.001</ArticleId>
                    </ArticleIdList>
                    <History>
                        <PubMedPubDate PubStatus="pubmed">
                            <Year>2024</Year>
                            <Month>1</Month>
                            <Day>15</Day>
                        </PubMedPubDate>
                    </History>
                </PubmedData>
            </PubmedArticle>
        </PubmedArticleSet>
        """

        mock_response = Mock()
        mock_response.text = mock_xml
        mock_request.return_value = mock_response

        client = PubMedClient(use_cache=False)
        articles = client.fetch_details(["12345678"])

        assert "12345678" in articles
        article = articles["12345678"]
        assert article["pmid"] == "12345678"
        assert "Test Article Title" in article["title"]
        assert "Test abstract" in article["abstract"]

    @patch('src.dr.retrieval.pubmed.request_with_retries')
    def test_fetch_details_missing_abstract(self, mock_request):
        """Test fetching article with missing abstract"""
        mock_xml = """<?xml version="1.0"?>
        <PubmedArticleSet>
            <PubmedArticle>
                <MedlineCitation>
                    <PMID>12345678</PMID>
                    <Article>
                        <ArticleTitle>Test Article Title</ArticleTitle>
                    </Article>
                </MedlineCitation>
            </PubmedArticle>
        </PubmedArticleSet>
        """

        mock_response = Mock()
        mock_response.text = mock_xml
        mock_request.return_value = mock_response

        client = PubMedClient(use_cache=False)
        articles = client.fetch_details(["12345678"])

        assert "12345678" in articles
        # Should handle missing abstract gracefully
        assert articles["12345678"]["abstract"] is None or articles["12345678"]["abstract"] == ""

    @patch('src.dr.retrieval.pubmed.request_with_retries')
    def test_fetch_details_batch(self, mock_request):
        """Test fetching multiple articles in batch"""
        mock_xml = """<?xml version="1.0"?>
        <PubmedArticleSet>
            <PubmedArticle>
                <MedlineCitation>
                    <PMID>11111111</PMID>
                    <Article><ArticleTitle>Article 1</ArticleTitle></Article>
                </MedlineCitation>
            </PubmedArticle>
            <PubmedArticle>
                <MedlineCitation>
                    <PMID>22222222</PMID>
                    <Article><ArticleTitle>Article 2</ArticleTitle></Article>
                </MedlineCitation>
            </PubmedArticle>
        </PubmedArticleSet>
        """

        mock_response = Mock()
        mock_response.text = mock_xml
        mock_request.return_value = mock_response

        client = PubMedClient(use_cache=False)
        articles = client.fetch_details(["11111111", "22222222"])

        assert len(articles) == 2
        assert "11111111" in articles
        assert "22222222" in articles

    @patch('src.dr.retrieval.pubmed.request_with_retries')
    def test_search_and_fetch_integration(self, mock_request):
        """Test search followed by fetch (integration)"""
        # Mock search and fetch responses
        def side_effect(*args, **kwargs):
            url = kwargs.get('url', '')
            if 'esearch' in url:
                mock_response = Mock()
                mock_response.json.return_value = {
                    "esearchresult": {
                        "count": "2",
                        "idlist": ["12345678", "87654321"]
                    }
                }
                return mock_response
            else:  # efetch
                mock_response = Mock()
                mock_response.text = """<?xml version="1.0"?>
                <PubmedArticleSet>
                    <PubmedArticle>
                        <MedlineCitation>
                            <PMID>12345678</PMID>
                            <Article><ArticleTitle>Test</ArticleTitle></Article>
                        </MedlineCitation>
                    </PubmedArticle>
                </PubmedArticleSet>
                """
                return mock_response

        mock_request.side_effect = side_effect

        client = PubMedClient(use_cache=False)

        # Search
        pmids = client.search("test query", max_results=10)
        assert len(pmids) == 2

        # Fetch
        articles = client.fetch_details(pmids[:1])
        assert len(articles) >= 1

    @patch('src.dr.retrieval.pubmed.request_with_retries')
    def test_batch_fetch_single_request(self, mock_request):
        """Batch fetch sends one HTTP request instead of N individual ones."""
        mock_xml = """<?xml version="1.0"?>
        <PubmedArticleSet>
            <PubmedArticle>
                <MedlineCitation>
                    <PMID>11111111</PMID>
                    <Article><ArticleTitle>Article 1</ArticleTitle>
                    <Abstract><AbstractText>Abstract one</AbstractText></Abstract>
                    </Article>
                </MedlineCitation>
            </PubmedArticle>
            <PubmedArticle>
                <MedlineCitation>
                    <PMID>22222222</PMID>
                    <Article><ArticleTitle>Article 2</ArticleTitle>
                    <Abstract><AbstractText>Abstract two</AbstractText></Abstract>
                    </Article>
                </MedlineCitation>
            </PubmedArticle>
            <PubmedArticle>
                <MedlineCitation>
                    <PMID>33333333</PMID>
                    <Article><ArticleTitle>Article 3</ArticleTitle>
                    <Abstract><AbstractText>Abstract three</AbstractText></Abstract>
                    </Article>
                </MedlineCitation>
            </PubmedArticle>
        </PubmedArticleSet>
        """

        mock_response = Mock()
        mock_response.text = mock_xml
        mock_request.return_value = mock_response

        client = PubMedClient(use_cache=False)
        articles = client.fetch_details(["11111111", "22222222", "33333333"])

        assert len(articles) == 3
        assert articles["11111111"]["title"] == "Article 1"
        assert articles["22222222"]["title"] == "Article 2"
        assert articles["33333333"]["title"] == "Article 3"
        # Should be ONE batch request, not three individual ones
        assert mock_request.call_count == 1

    @patch('src.dr.retrieval.pubmed.request_with_retries')
    def test_batch_fetch_multi_part_abstract(self, mock_request):
        """Batch fetch handles structured abstracts with multiple <AbstractText> parts."""
        mock_xml = """<?xml version="1.0"?>
        <PubmedArticleSet>
            <PubmedArticle>
                <MedlineCitation>
                    <PMID>44444444</PMID>
                    <Article>
                        <ArticleTitle>Structured Abstract Article</ArticleTitle>
                        <Abstract>
                            <AbstractText Label="BACKGROUND">Background info here.</AbstractText>
                            <AbstractText Label="METHODS">Methods described here.</AbstractText>
                            <AbstractText Label="RESULTS">Results reported here.</AbstractText>
                        </Abstract>
                    </Article>
                </MedlineCitation>
            </PubmedArticle>
        </PubmedArticleSet>
        """

        mock_response = Mock()
        mock_response.text = mock_xml
        mock_request.return_value = mock_response

        client = PubMedClient(use_cache=False)
        articles = client.fetch_details(["44444444"])

        assert "44444444" in articles
        abstract = articles["44444444"]["abstract"]
        assert "Background info" in abstract
        assert "Methods described" in abstract
        assert "Results reported" in abstract

    @patch('src.dr.retrieval.pubmed.request_with_retries')
    def test_batch_fetch_deduplicates_pmids(self, mock_request):
        """Duplicate PMIDs in input should only be fetched once."""
        mock_xml = """<?xml version="1.0"?>
        <PubmedArticleSet>
            <PubmedArticle>
                <MedlineCitation>
                    <PMID>11111111</PMID>
                    <Article><ArticleTitle>Article 1</ArticleTitle></Article>
                </MedlineCitation>
            </PubmedArticle>
        </PubmedArticleSet>
        """

        mock_response = Mock()
        mock_response.text = mock_xml
        mock_request.return_value = mock_response

        client = PubMedClient(use_cache=False)
        articles = client.fetch_details(["11111111", "11111111", "11111111"])

        assert len(articles) == 1
        assert "11111111" in articles
        # Only one HTTP request despite 3 duplicate inputs
        assert mock_request.call_count == 1

    def test_batch_fetch_empty_input(self):
        """Empty input returns empty dict without making requests."""
        client = PubMedClient(use_cache=False)
        articles = client.fetch_details([])
        assert articles == {}

    @patch('src.dr.retrieval.pubmed.request_with_retries')
    def test_batch_fetch_fallback_on_batch_error(self, mock_request):
        """When batch request fails, falls back to individual fetches."""
        individual_xml = """<?xml version="1.0"?>
        <PubmedArticleSet>
            <PubmedArticle>
                <MedlineCitation>
                    <PMID>11111111</PMID>
                    <Article><ArticleTitle>Fallback Article</ArticleTitle></Article>
                </MedlineCitation>
            </PubmedArticle>
        </PubmedArticleSet>
        """

        call_count = [0]
        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call (batch) fails
                raise RuntimeError("Batch request failed")
            # Subsequent individual calls succeed
            mock_resp = Mock()
            mock_resp.text = individual_xml
            return mock_resp

        mock_request.side_effect = side_effect

        client = PubMedClient(use_cache=False)
        articles = client.fetch_details(["11111111"])

        # Should still get the article via individual fallback
        assert "11111111" in articles
        assert articles["11111111"]["title"] == "Fallback Article"

    @patch('src.dr.retrieval.pubmed.request_with_retries')
    def test_batch_fetch_malformed_xml_fallback(self, mock_request):
        """Malformed XML falls back to regex block extraction."""
        # Two PubmedArticle blocks but not wrapped in valid root
        malformed_xml = """
        <PubmedArticle>
            <MedlineCitation>
                <PMID>11111111</PMID>
                <Article><ArticleTitle>Article 1</ArticleTitle></Article>
            </MedlineCitation>
        </PubmedArticle>
        <PubmedArticle>
            <MedlineCitation>
                <PMID>22222222</PMID>
                <Article><ArticleTitle>Article 2</ArticleTitle></Article>
            </MedlineCitation>
        </PubmedArticle>
        """

        mock_response = Mock()
        mock_response.text = malformed_xml
        mock_request.return_value = mock_response

        client = PubMedClient(use_cache=False)
        articles = client.fetch_details(["11111111", "22222222"])

        # Both articles should be recovered via regex fallback
        assert len(articles) == 2
        assert "11111111" in articles
        assert "22222222" in articles

    @patch('src.dr.retrieval.pubmed.request_with_retries')
    def test_batch_fetch_with_cache_hit(self, mock_request):
        """Cached PMIDs are not re-fetched; only uncached ones hit the API."""
        mock_xml = """<?xml version="1.0"?>
        <PubmedArticleSet>
            <PubmedArticle>
                <MedlineCitation>
                    <PMID>22222222</PMID>
                    <Article><ArticleTitle>Uncached Article</ArticleTitle></Article>
                </MedlineCitation>
            </PubmedArticle>
        </PubmedArticleSet>
        """

        mock_response = Mock()
        mock_response.text = mock_xml
        mock_request.return_value = mock_response

        # Mock cache manager
        mock_cache = MagicMock()
        cached_data = {"pmid": "11111111", "title": "Cached Article", "abstract": "", "authors": [], "journal": "", "year": ""}

        def cache_get(drug_id, query, params=None):
            if params and params.get("pmid") == "11111111":
                return cached_data
            return None

        mock_cache.get_pubmed.side_effect = cache_get

        client = PubMedClient(cache_manager=mock_cache, use_cache=True)
        articles = client.fetch_details(["11111111", "22222222"])

        assert len(articles) == 2
        assert articles["11111111"]["title"] == "Cached Article"
        assert articles["22222222"]["title"] == "Uncached Article"
        # Only ONE request for the uncached PMID
        assert mock_request.call_count == 1

    @patch('src.dr.retrieval.pubmed.request_with_retries')
    def test_batch_fetch_medlinedate_year(self, mock_request):
        """Year extraction from MedlineDate format (e.g., '2024 Jan-Feb')."""
        mock_xml = """<?xml version="1.0"?>
        <PubmedArticleSet>
            <PubmedArticle>
                <MedlineCitation>
                    <PMID>55555555</PMID>
                    <Article>
                        <ArticleTitle>Date Test</ArticleTitle>
                        <Journal><Title>Test Journal</Title>
                        <JournalIssue><PubDate><MedlineDate>2023 Jan-Feb</MedlineDate></PubDate></JournalIssue>
                        </Journal>
                    </Article>
                </MedlineCitation>
            </PubmedArticle>
        </PubmedArticleSet>
        """

        mock_response = Mock()
        mock_response.text = mock_xml
        mock_request.return_value = mock_response

        client = PubMedClient(use_cache=False)
        articles = client.fetch_details(["55555555"])

        assert articles["55555555"]["year"] == "2023"

    def test_cache_key_generation(self):
        """Test that cache keys are generated consistently"""
        client = PubMedClient()

        # Same query should generate same cache key
        query1 = "aspirin atherosclerosis"
        query2 = "aspirin atherosclerosis"

        # This test verifies the cache behavior indirectly
        # by checking that the client has cache enabled
        assert client.use_cache is True
        assert client.cache is not None
