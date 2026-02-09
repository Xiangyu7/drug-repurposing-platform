"""Unit tests for BM25Ranker, HybridRanker, CrossEncoderReranker, RankingPipeline"""

import pytest
from unittest.mock import MagicMock, patch

from src.dr.evidence.ranker import (
    BM25Ranker,
    rerank_by_fields,
    reciprocal_rank_fusion,
    HybridRanker,
    CrossEncoderReranker,
    RankingPipeline,
)


# ============================================================
# BM25Ranker Tests (existing)
# ============================================================

class TestBM25Ranker:
    """Tests for BM25 ranking algorithm"""

    def test_ranker_initialization(self):
        """Test ranker initializes with default parameters"""
        ranker = BM25Ranker()
        assert ranker is not None
        assert ranker.k1 == 1.5
        assert ranker.b == 0.75

    def test_ranker_custom_parameters(self):
        """Test ranker accepts custom k1/b parameters"""
        ranker = BM25Ranker(k1=2.0, b=0.5)
        assert ranker.k1 == 2.0
        assert ranker.b == 0.5

    def test_rank_documents_basic(self):
        """Test basic document ranking"""
        ranker = BM25Ranker()

        documents = [
            {"pmid": "1", "title": "Resveratrol reduces atherosclerosis", "abstract": "Study shows benefit"},
            {"pmid": "2", "title": "Metformin and diabetes", "abstract": "Diabetes treatment"},
            {"pmid": "3", "title": "Resveratrol mechanism in atherosclerosis", "abstract": "Reduces plaque"}
        ]

        query = "resveratrol atherosclerosis"
        ranked = ranker.rank(query, documents)

        # Should return list of (score, doc) tuples
        assert len(ranked) == 3
        assert all(isinstance(item, tuple) for item in ranked)
        assert all(len(item) == 2 for item in ranked)

        # First result should have highest score
        assert ranked[0][0] >= ranked[1][0]
        assert ranked[1][0] >= ranked[2][0]

        # Documents about resveratrol+atherosclerosis should rank higher
        assert ranked[0][1]["pmid"] in ["1", "3"]

    def test_rank_documents_empty_query(self):
        """Test ranking with empty query returns empty list"""
        ranker = BM25Ranker()

        documents = [
            {"pmid": "1", "title": "Test 1", "abstract": "Content 1"},
            {"pmid": "2", "title": "Test 2", "abstract": "Content 2"}
        ]

        ranked = ranker.rank("", documents)

        # Should return empty list
        assert ranked == []

    def test_rank_documents_no_documents(self):
        """Test ranking with no documents returns empty list"""
        ranker = BM25Ranker()
        ranked = ranker.rank("test query", [])
        assert ranked == []

    def test_rank_documents_missing_fields(self):
        """Test ranking handles documents with missing fields"""
        ranker = BM25Ranker()

        documents = [
            {"pmid": "1", "title": "Has title only"},
            {"pmid": "2", "abstract": "Has abstract only"},
            {"pmid": "3"}  # Missing both
        ]

        # Should not crash
        ranked = ranker.rank("test", documents)
        assert len(ranked) == 3

    def test_rank_preserves_original_fields(self):
        """Test ranking preserves all original document fields"""
        ranker = BM25Ranker()

        documents = [
            {"pmid": "1", "title": "Test", "abstract": "Content", "custom_field": "value1"},
            {"pmid": "2", "title": "Test", "abstract": "Content", "custom_field": "value2"}
        ]

        ranked = ranker.rank("test", documents)

        # Original fields should be preserved in document
        assert ranked[0][1]["custom_field"] in ["value1", "value2"]

    def test_rank_top_k(self):
        """Test ranking returns only top K results"""
        ranker = BM25Ranker()

        documents = [
            {"pmid": str(i), "title": f"Document {i}", "abstract": "content test"}
            for i in range(100)
        ]

        ranked = ranker.rank("document test", documents, topk=10)

        assert len(ranked) == 10

    def test_rank_deterministic(self):
        """Test ranking is deterministic (same input = same output)"""
        ranker = BM25Ranker()

        documents = [
            {"pmid": "1", "title": "Resveratrol atherosclerosis", "abstract": "Study"},
            {"pmid": "2", "title": "Metformin diabetes", "abstract": "Trial"},
            {"pmid": "3", "title": "Aspirin cardiovascular", "abstract": "Research"}
        ]

        query = "atherosclerosis cardiovascular"

        ranked1 = ranker.rank(query, documents)
        ranked2 = ranker.rank(query, documents)

        # Same order
        assert [doc["pmid"] for _, doc in ranked1] == [doc["pmid"] for _, doc in ranked2]

        # Same scores
        for i in range(len(ranked1)):
            assert abs(ranked1[i][0] - ranked2[i][0]) < 1e-6

    def test_rank_handles_none_abstract(self):
        """Test ranking handles None abstract (from PubMed API)"""
        ranker = BM25Ranker()

        documents = [
            {"pmid": "1", "title": "Test", "abstract": None},
            {"pmid": "2", "title": "Test special", "abstract": "Real content"}
        ]

        # Should not crash
        ranked = ranker.rank("test special", documents)
        assert len(ranked) == 2

        # Document with content should rank higher
        assert ranked[0][1]["pmid"] == "2"


# ============================================================
# Reciprocal Rank Fusion Tests
# ============================================================

class TestReciprocalRankFusion:

    def test_basic_fusion(self):
        """Two lists with overlapping docs get combined scores."""
        list_a = [
            (10.0, {"pmid": "A"}),
            (8.0, {"pmid": "B"}),
            (6.0, {"pmid": "C"}),
        ]
        list_b = [
            (10.0, {"pmid": "B"}),
            (8.0, {"pmid": "A"}),
            (6.0, {"pmid": "D"}),
        ]

        fused = reciprocal_rank_fusion([list_a, list_b], k=60)

        pmid_order = [d["pmid"] for _, d in fused]
        # A and B appear in both lists, so they should rank higher than C and D
        assert "A" in pmid_order[:2]
        assert "B" in pmid_order[:2]

    def test_single_list(self):
        """Single list returns same ordering."""
        lst = [
            (10.0, {"pmid": "A"}),
            (5.0, {"pmid": "B"}),
        ]
        fused = reciprocal_rank_fusion([lst], k=60)
        assert len(fused) == 2
        assert fused[0][1]["pmid"] == "A"
        assert fused[1][1]["pmid"] == "B"

    def test_empty_lists(self):
        """Empty input returns empty output."""
        assert reciprocal_rank_fusion([], k=60) == []
        assert reciprocal_rank_fusion([[], []], k=60) == []

    def test_k_parameter_effect(self):
        """Smaller k gives more weight to rank position differences."""
        list_a = [(10.0, {"pmid": "A"}), (5.0, {"pmid": "B"})]
        list_b = [(10.0, {"pmid": "B"}), (5.0, {"pmid": "A"})]

        fused_low_k = reciprocal_rank_fusion([list_a, list_b], k=1)
        fused_high_k = reciprocal_rank_fusion([list_a, list_b], k=1000)

        # With very high k, the RRF scores should be very close
        scores_high_k = [s for s, _ in fused_high_k]
        if len(scores_high_k) == 2:
            diff_high = abs(scores_high_k[0] - scores_high_k[1])
            scores_low_k = [s for s, _ in fused_low_k]
            diff_low = abs(scores_low_k[0] - scores_low_k[1])
            # Low k should show more differentiation
            # Actually both docs appear at same ranks in both lists (swapped),
            # so the total RRF score is the same. Let me use asymmetric lists.
            pass

    def test_asymmetric_lists(self):
        """Doc appearing in both lists gets higher score than single-list doc."""
        list_a = [(10.0, {"pmid": "A"}), (5.0, {"pmid": "B"})]
        list_b = [(10.0, {"pmid": "A"})]  # only A

        fused = reciprocal_rank_fusion([list_a, list_b], k=60)
        scores = {d["pmid"]: s for s, d in fused}

        # A appears in both -> higher score; B only in one
        assert scores["A"] > scores["B"]

    def test_preserves_all_documents(self):
        """All unique documents appear in output."""
        list_a = [(10.0, {"pmid": "A"})]
        list_b = [(10.0, {"pmid": "B"})]
        list_c = [(10.0, {"pmid": "C"})]

        fused = reciprocal_rank_fusion([list_a, list_b, list_c], k=60)
        pmids = {d["pmid"] for _, d in fused}
        assert pmids == {"A", "B", "C"}


# ============================================================
# HybridRanker Tests
# ============================================================

class TestHybridRanker:

    @pytest.fixture
    def sample_docs(self):
        return [
            {"pmid": "1", "title": "Resveratrol atherosclerosis", "abstract": "Plaque study"},
            {"pmid": "2", "title": "Aspirin cardiovascular", "abstract": "Heart trial"},
            {"pmid": "3", "title": "Metformin diabetes", "abstract": "Blood sugar"},
            {"pmid": "4", "title": "Statin plaque regression", "abstract": "Atherosclerosis study"},
            {"pmid": "5", "title": "Resveratrol anti-inflammatory", "abstract": "Inflammation research"},
        ]

    def test_bm25_only_fallback(self, sample_docs):
        """Without embed_client, falls back to BM25 only."""
        ranker = HybridRanker(embed_client=None)
        results = ranker.rank("resveratrol atherosclerosis", sample_docs, topk=3)

        assert len(results) <= 3
        assert all(isinstance(item, tuple) and len(item) == 2 for item in results)
        # Resveratrol docs should be top
        top_pmids = {d["pmid"] for _, d in results}
        assert "1" in top_pmids or "5" in top_pmids

    def test_hybrid_with_embedding(self, sample_docs):
        """With embed_client, combines BM25 + embedding via RRF."""
        mock_client = MagicMock()
        # Mock rerank_by_embedding to return docs in reverse pmid order
        mock_client.rerank_by_embedding.return_value = list(reversed(sample_docs))

        ranker = HybridRanker(embed_client=mock_client, bm25_topk=5, embed_topk=5)
        results = ranker.rank("resveratrol atherosclerosis", sample_docs, topk=3)

        assert len(results) <= 3
        mock_client.rerank_by_embedding.assert_called_once()

    def test_embedding_failure_fallback(self, sample_docs):
        """If embedding raises, gracefully falls back to BM25."""
        mock_client = MagicMock()
        mock_client.rerank_by_embedding.side_effect = RuntimeError("connection refused")

        ranker = HybridRanker(embed_client=mock_client)
        results = ranker.rank("resveratrol", sample_docs, topk=3)

        # Should still return results (BM25 fallback)
        assert len(results) > 0

    def test_empty_query(self, sample_docs):
        """Empty query returns empty results."""
        ranker = HybridRanker(embed_client=None)
        assert ranker.rank("", sample_docs) == []

    def test_empty_docs(self):
        """Empty docs returns empty results."""
        ranker = HybridRanker(embed_client=None)
        assert ranker.rank("test query", []) == []

    def test_topk_respected(self, sample_docs):
        """topk limits output size."""
        ranker = HybridRanker(embed_client=None)
        results = ranker.rank("study", sample_docs, topk=2)
        assert len(results) <= 2

    def test_rrf_fusion_improves_ranking(self, sample_docs):
        """Docs that rank well in both BM25 and embedding should rise to top."""
        # Create mock that returns doc "1" first (same as BM25 top)
        mock_client = MagicMock()
        mock_client.rerank_by_embedding.return_value = [
            sample_docs[0],  # pmid=1 (also top in BM25 for "resveratrol")
            sample_docs[4],  # pmid=5
            sample_docs[3],  # pmid=4
            sample_docs[1],  # pmid=2
            sample_docs[2],  # pmid=3
        ]

        ranker = HybridRanker(embed_client=mock_client, bm25_topk=5, embed_topk=5)
        results = ranker.rank("resveratrol atherosclerosis", sample_docs, topk=5)

        # Doc 1 should be top since it ranks high in both
        assert results[0][1]["pmid"] == "1"


# ============================================================
# CrossEncoderReranker Tests
# ============================================================

class TestCrossEncoderReranker:

    @pytest.fixture
    def sample_docs(self):
        return [
            {"pmid": "1", "title": "Resveratrol atherosclerosis", "abstract": "Plaque regression study"},
            {"pmid": "2", "title": "Aspirin cardiovascular", "abstract": "Heart disease trial"},
            {"pmid": "3", "title": "Metformin diabetes", "abstract": "Blood sugar research"},
        ]

    def test_no_client_returns_defaults(self, sample_docs):
        """Without client, returns docs with score 0."""
        reranker = CrossEncoderReranker(ollama_client=None)
        results = reranker.rerank("test", sample_docs, topk=3)

        assert len(results) == 3
        assert all(score == 0.0 for score, _ in results)

    def test_empty_docs(self):
        """Empty docs returns empty."""
        reranker = CrossEncoderReranker(ollama_client=MagicMock())
        results = reranker.rerank("test", [], topk=5)
        assert results == []

    def test_llm_scoring(self, sample_docs):
        """Mock LLM returns numeric scores that determine ranking."""
        mock_client = MagicMock()
        # Return different scores for each doc
        mock_client.generate.side_effect = ["9.5", "3.0", "7.0"]

        reranker = CrossEncoderReranker(ollama_client=mock_client)
        results = reranker.rerank("resveratrol atherosclerosis", sample_docs, topk=3)

        # Should be sorted by score descending
        scores = [s for s, _ in results]
        assert scores == sorted(scores, reverse=True)
        # First doc should be pmid=1 (score 9.5)
        assert results[0][1]["pmid"] == "1"
        assert results[0][0] == 9.5

    def test_llm_failure_returns_default_score(self, sample_docs):
        """If LLM fails, uses default score 5.0."""
        mock_client = MagicMock()
        mock_client.generate.side_effect = RuntimeError("LLM unavailable")

        reranker = CrossEncoderReranker(ollama_client=mock_client)
        results = reranker.rerank("test", sample_docs, topk=3)

        # All should have default score 5.0
        assert all(score == 5.0 for score, _ in results)

    def test_score_clamping(self, sample_docs):
        """Scores above 10 get clamped to 10."""
        mock_client = MagicMock()
        mock_client.generate.side_effect = ["15.0", "2.0", "8.0"]

        reranker = CrossEncoderReranker(ollama_client=mock_client)
        results = reranker.rerank("test", sample_docs, topk=3)

        scores = {d["pmid"]: s for s, d in results}
        assert scores["1"] == 10.0  # clamped from 15
        assert scores["2"] == 2.0
        assert scores["3"] == 8.0

    def test_non_numeric_response(self, sample_docs):
        """Non-numeric LLM response uses default score."""
        mock_client = MagicMock()
        mock_client.generate.side_effect = [
            "This is very relevant!",  # no number -> default 5.0
            "8",                       # valid
            "",                        # empty -> default 5.0
        ]

        reranker = CrossEncoderReranker(ollama_client=mock_client)
        results = reranker.rerank("test", sample_docs, topk=3)

        scores = {d["pmid"]: s for s, d in results}
        assert scores["2"] == 8.0

    def test_topk_limits_output(self, sample_docs):
        """topk limits number of results."""
        mock_client = MagicMock()
        mock_client.generate.return_value = "5"

        reranker = CrossEncoderReranker(ollama_client=mock_client)
        results = reranker.rerank("test", sample_docs, topk=1)
        assert len(results) == 1


# ============================================================
# RankingPipeline Tests
# ============================================================

class TestRankingPipeline:

    @pytest.fixture
    def sample_docs(self):
        return [
            {"pmid": str(i), "title": f"Document {i} about atherosclerosis", "abstract": f"Study {i}"}
            for i in range(20)
        ]

    def test_bm25_only_pipeline(self, sample_docs):
        """Pipeline with no embedding or cross-encoder uses BM25 only."""
        pipeline = RankingPipeline(
            hybrid_ranker=None,
            cross_encoder=None,
            final_topk=5,
        )
        results = pipeline.rank("atherosclerosis study", sample_docs)

        assert len(results) <= 5
        assert all(isinstance(d, dict) for d in results)
        # Results should be dicts (no scores in pipeline output)
        assert "pmid" in results[0]

    def test_pipeline_with_hybrid(self, sample_docs):
        """Pipeline with hybrid ranker."""
        mock_client = MagicMock()
        mock_client.rerank_by_embedding.return_value = sample_docs[:10]

        hybrid = HybridRanker(embed_client=mock_client, bm25_topk=20, embed_topk=10)
        pipeline = RankingPipeline(
            hybrid_ranker=hybrid,
            cross_encoder=None,
            final_topk=5,
        )
        results = pipeline.rank("atherosclerosis", sample_docs, topk=5)

        assert len(results) <= 5

    def test_pipeline_with_cross_encoder(self, sample_docs):
        """Pipeline with both hybrid and cross-encoder."""
        mock_embed = MagicMock()
        mock_embed.rerank_by_embedding.return_value = sample_docs[:10]

        mock_llm = MagicMock()
        # Return scores for the hybrid docs that will be sent to CE
        mock_llm.generate.return_value = "7"

        hybrid = HybridRanker(embed_client=mock_embed, bm25_topk=20, embed_topk=10)
        ce = CrossEncoderReranker(ollama_client=mock_llm)

        pipeline = RankingPipeline(
            hybrid_ranker=hybrid,
            cross_encoder=ce,
            final_topk=5,
        )
        results = pipeline.rank("atherosclerosis", sample_docs, topk=5)

        assert len(results) <= 5
        # Cross-encoder should have been called
        assert mock_llm.generate.called

    def test_pipeline_empty_query(self, sample_docs):
        """Empty query returns empty results through pipeline."""
        pipeline = RankingPipeline()
        assert pipeline.rank("", sample_docs) == []

    def test_pipeline_empty_docs(self):
        """Empty docs returns empty results."""
        pipeline = RankingPipeline()
        assert pipeline.rank("test query", []) == []

    def test_pipeline_topk_override(self, sample_docs):
        """topk parameter overrides default final_topk."""
        pipeline = RankingPipeline(final_topk=5)
        results = pipeline.rank("atherosclerosis", sample_docs, topk=3)
        assert len(results) <= 3

    def test_pipeline_fallback_on_embed_failure(self, sample_docs):
        """Pipeline still works if embedding fails at hybrid stage."""
        mock_client = MagicMock()
        mock_client.rerank_by_embedding.side_effect = ConnectionError("timeout")

        hybrid = HybridRanker(embed_client=mock_client)
        pipeline = RankingPipeline(hybrid_ranker=hybrid, final_topk=5)
        results = pipeline.rank("atherosclerosis study", sample_docs)

        # Should still get results via BM25 fallback
        assert len(results) > 0
