"""Integration tests for Step6 pipeline"""

import pytest
from unittest.mock import Mock, patch


@pytest.mark.integration
class TestStep6Pipeline:
    """End-to-end tests for Step6 PubMed RAG pipeline"""

    @patch('src.dr.retrieval.pubmed.request_with_retries')
    def test_step6_simple_pipeline_single_drug(self, mock_request, temp_dir):
        """Test Step6 simple pipeline processes one drug end-to-end"""
        from src.dr.retrieval.pubmed import PubMedClient
        from src.dr.evidence.ranker import BM25Ranker
        
        # Mock PubMed search
        mock_search_response = Mock()
        mock_search_response.json.return_value = {
            "esearchresult": {
                "count": "3",
                "idlist": ["12345678", "87654321", "11111111"]
            }
        }
        
        # Mock PubMed fetch
        mock_fetch_response = Mock()
        mock_fetch_response.text = """<?xml version="1.0"?>
        <PubmedArticleSet>
            <PubmedArticle>
                <MedlineCitation>
                    <PMID>12345678</PMID>
                    <Article>
                        <ArticleTitle>Resveratrol reduces atherosclerosis in mice</ArticleTitle>
                        <Abstract>
                            <AbstractText>Treatment significantly reduced plaque by 45%</AbstractText>
                        </Abstract>
                    </Article>
                </MedlineCitation>
            </PubmedArticle>
        </PubmedArticleSet>
        """
        
        mock_request.side_effect = [mock_search_response, mock_fetch_response]
        
        # Run pipeline components
        client = PubMedClient(use_cache=False)
        ranker = BM25Ranker()
        
        # Search
        pmids = client.search("resveratrol atherosclerosis", max_results=10)
        assert len(pmids) == 3
        
        # Fetch
        articles = client.fetch_details(pmids)
        assert len(articles) >= 1
        
        # Rank
        docs = [{"pmid": pmid, **meta} for pmid, meta in articles.items()]
        ranked = ranker.rank("resveratrol atherosclerosis", docs, topk=20)
        
        assert len(ranked) >= 1
        assert ranked[0][0] > 0  # Has BM25 score

    def test_bm25_ranking_quality(self):
        """Test BM25 ranking produces sensible results"""
        from src.dr.evidence.ranker import BM25Ranker
        
        ranker = BM25Ranker()
        
        # Create documents with varying relevance
        docs = [
            {
                "pmid": "1",
                "title": "Resveratrol reduces atherosclerotic plaque in ApoE mice",
                "abstract": "Resveratrol treatment significantly reduced atherosclerosis and plaque formation"
            },
            {
                "pmid": "2",
                "title": "Diabetes treatment with metformin",
                "abstract": "Metformin improves glucose control in diabetes patients"
            },
            {
                "pmid": "3",
                "title": "Effects of resveratrol on cardiovascular health",
                "abstract": "Resveratrol shows promise for atherosclerosis prevention"
            }
        ]
        
        query = "resveratrol atherosclerosis"
        ranked = ranker.rank(query, docs, topk=10)
        
        # Most relevant documents should rank higher
        assert ranked[0][1]["pmid"] in ["1", "3"]
        assert ranked[0][0] > ranked[1][0]  # Scores decrease
        
        # Irrelevant document should rank lower
        pmids_in_order = [doc["pmid"] for _, doc in ranked]
        assert pmids_in_order.index("2") > 0  # Metformin paper not first

    def test_evidence_classification_consistency(self):
        """Test that classification is consistent and deterministic"""
        from src.dr.evidence.ranker import BM25Ranker
        
        ranker = BM25Ranker()
        
        docs = [
            {"pmid": "1", "title": "Drug reduces plaque", "abstract": "Significant reduction observed"},
            {"pmid": "2", "title": "Drug increases risk", "abstract": "Adverse effects noted"},
            {"pmid": "3", "title": "No effect observed", "abstract": "No significant changes"}
        ]
        
        query = "drug atherosclerosis"
        
        # Run ranking twice
        ranked1 = ranker.rank(query, docs, topk=10)
        ranked2 = ranker.rank(query, docs, topk=10)
        
        # Should produce identical results
        assert len(ranked1) == len(ranked2)
        for i in range(len(ranked1)):
            assert ranked1[i][1]["pmid"] == ranked2[i][1]["pmid"]
            assert abs(ranked1[i][0] - ranked2[i][0]) < 1e-6

    @patch('src.dr.evidence.extractor.OllamaClient')
    def test_llm_extraction_pipeline(self, mock_ollama_class):
        """Test LLM extraction integrates with pipeline"""
        from src.dr.evidence.extractor import LLMEvidenceExtractor
        
        # Mock LLM responses
        mock_client = Mock()
        mock_ollama_class.return_value = mock_client
        mock_client.generate.return_value = '{"direction": "benefit", "model": "animal", "endpoint": "PLAQUE_IMAGING", "mechanism": "Reduces plaque", "confidence": "HIGH"}'
        
        extractor = LLMEvidenceExtractor()
        
        papers = [
            {
                "pmid": "12345",
                "title": "Resveratrol reduces atherosclerosis",
                "abstract": "Treatment reduced plaque by 45%"
            },
            {
                "pmid": "67890",
                "title": "Another study on resveratrol",
                "abstract": "Similar benefits observed"
            }
        ]
        
        results = extractor.extract_batch(papers, drug_name="resveratrol", max_papers=10)

        assert results.success == 2
        assert len(results.extractions) == 2
        assert all(r.direction == "benefit" for r in results.extractions)
        assert all(r.pmid in ["12345", "67890"] for r in results.extractions)

    def test_dossier_structure(self, sample_dossier):
        """Test dossier has expected structure for scoring"""
        # Verify dossier has all required fields
        required_fields = [
            "drug_id",
            "canonical_name",
            "total_pmids",
            "evidence_count"
        ]
        
        for field in required_fields:
            assert field in sample_dossier, f"Missing required field: {field}"
        
        # Verify evidence_count structure
        evidence_count = sample_dossier["evidence_count"]
        assert "benefit" in evidence_count
        assert "harm" in evidence_count
        assert "neutral" in evidence_count
        
        # Verify counts are non-negative
        assert evidence_count["benefit"] >= 0
        assert evidence_count["harm"] >= 0
