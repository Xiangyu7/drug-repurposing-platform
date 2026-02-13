"""Integration tests for end-to-end pipeline"""

import pytest
import pandas as pd


@pytest.mark.integration
class TestPipelineIntegration:
    """Integration tests for full pipeline"""

    def test_step6_simple_pipeline(self, mock_data_dir, mock_output_dir):
        """Test Step6 simple pipeline runs end-to-end"""
        from src.dr.evidence.ranker import BM25Ranker

        ranker = BM25Ranker()
        assert ranker is not None
        ranked = ranker.rank(
            "resveratrol atherosclerosis",
            [{"pmid": "1", "title": "resveratrol and plaque", "abstract": "benefit in animal model"}],
            topk=5,
        )
        assert len(ranked) == 1

    def test_step7_scoring_pipeline(self, mock_data_dir, mock_output_dir, sample_dossier):
        """Test Step7 scoring pipeline runs end-to-end"""
        from src.dr.scoring.scorer import DrugScorer

        scorer = DrugScorer()
        score = scorer.score_drug(sample_dossier)

        assert score["total_score_0_100"] > 0

    def test_data_flow(self, mock_data_dir):
        """Test data flows correctly between steps"""
        master_path = mock_data_dir / "drug_master.csv"
        assert master_path.exists()

        df = pd.read_csv(master_path)
        assert len(df) == 3
        assert "drug_id" in df.columns
        assert "canonical_name" in df.columns
