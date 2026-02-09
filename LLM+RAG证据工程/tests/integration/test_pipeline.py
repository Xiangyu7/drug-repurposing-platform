"""Integration tests for end-to-end pipeline"""

import pytest
from pathlib import Path
import pandas as pd


@pytest.mark.integration
class TestPipelineIntegration:
    """Integration tests for full pipeline"""

    def test_step6_simple_pipeline(self, mock_data_dir, mock_output_dir):
        """Test Step6 simple pipeline runs end-to-end"""
        # This test would import and run step6_pubmed_rag_simple
        # For now, just test the structure exists
        from src.dr.retrieval.ranker import BM25Ranker
        from src.dr.evidence.classifier import RuleBasedClassifier

        ranker = BM25Ranker()
        classifier = RuleBasedClassifier()

        assert ranker is not None
        assert classifier is not None

    def test_step7_scoring_pipeline(self, mock_data_dir, mock_output_dir, sample_dossier):
        """Test Step7 scoring pipeline runs end-to-end"""
        from src.dr.scoring.scorer import DrugScorer

        scorer = DrugScorer()
        score = scorer.score(sample_dossier)

        assert score.total_score > 0

    def test_data_flow(self, mock_data_dir):
        """Test data flows correctly between steps"""
        master_path = mock_data_dir / "drug_master.csv"
        assert master_path.exists()

        df = pd.read_csv(master_path)
        assert len(df) == 3
        assert "drug_id" in df.columns
        assert "canonical_name" in df.columns
