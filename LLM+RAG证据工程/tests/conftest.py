"""Pytest configuration and shared fixtures for DR project"""

import pytest
from pathlib import Path
from typing import Dict, Any
import pandas as pd

# ============================================================
# Fixtures: Temp directory
# ============================================================

@pytest.fixture
def temp_dir(tmp_path):
    """Provide a temporary directory that is cleaned up after the test."""
    return tmp_path


@pytest.fixture
def mock_data_dir(temp_dir, sample_drug_master) -> Path:
    """Create a mock data directory with minimal pipeline inputs."""
    data_dir = temp_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    sample_drug_master.to_csv(data_dir / "drug_master.csv", index=False)
    return data_dir


@pytest.fixture
def mock_output_dir(temp_dir) -> Path:
    """Create a mock output directory for integration tests."""
    out_dir = temp_dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


# ============================================================
# Fixtures: Test Data
# ============================================================

@pytest.fixture
def sample_drug_master() -> pd.DataFrame:
    """Sample drug_master.csv for testing"""
    return pd.DataFrame({
        "drug_id": ["D001", "D002", "D003"],
        "canonical_name": ["resveratrol", "metformin", "aspirin"],
        "aliases": [
            "resveratrol|trans-resveratrol",
            "metformin|glucophage",
            "aspirin|acetylsalicylic acid"
        ],
        "mechanism_keywords": [
            "antioxidant,anti-inflammatory",
            "metformin,ampk,insulin",
            "antiplatelet,cox inhibitor"
        ],
        "benefit_count": [15, 8, 25],
        "harm_count": [2, 1, 5],
        "neutral_count": [3, 2, 8],
        "total_pmids": [100, 50, 200]
    })


@pytest.fixture
def sample_pubmed_paper() -> Dict[str, Any]:
    """Sample PubMed paper for testing"""
    return {
        "pmid": "12345678",
        "title": "Resveratrol reduces atherosclerotic plaque in ApoE-/- mice",
        "abstract": (
            "Background: Resveratrol is a polyphenol with anti-inflammatory properties. "
            "Methods: We treated ApoE-/- mice with resveratrol (10 mg/kg) for 12 weeks. "
            "Results: Resveratrol significantly reduced atherosclerotic plaque area by 45% "
            "(p<0.001) compared to control. LDL cholesterol decreased by 20%. "
            "Conclusion: Resveratrol shows anti-atherogenic effects in mice."
        ),
        "authors": ["Smith J", "Doe A"],
        "journal": "Atherosclerosis",
        "pub_date": "2024",
        "doi": "10.1016/test.2024.001"
    }


@pytest.fixture
def sample_dossier() -> Dict[str, Any]:
    """Sample drug dossier for testing"""
    return {
        "drug_id": "D001",
        "canonical_name": "resveratrol",
        "total_pmids": 100,
        "top_papers": [
            {"pmid": "11111111", "bm25_score": 8.5},
            {"pmid": "22222222", "bm25_score": 7.2}
        ],
        "evidence_count": {
            "benefit": 15,
            "harm": 2,
            "neutral": 3,
            "unknown": 5
        },
        "evidence_extractions": [
            {
                "pmid": "11111111",
                "direction": "benefit",
                "model": "animal",
                "endpoint": "PLAQUE_IMAGING",
                "mechanism": "Reduces plaque via SIRT1 activation",
                "confidence": "HIGH"
            }
        ],
        "mechanism_keywords": ["antioxidant", "sirt1", "anti-inflammatory"],
        "safety_concerns": [],
        "metadata": {
            "generated_at": "2024-02-08T07:00:00",
            "pipeline_version": "1.0"
        }
    }


@pytest.fixture
def sample_gold_standard():
    """Sample gold-standard records for evaluation testing"""
    from src.dr.evaluation.gold_standard import GoldStandardRecord
    return [
        GoldStandardRecord("12345678", "resveratrol", "benefit", "animal", "PLAQUE_IMAGING", "HIGH"),
        GoldStandardRecord("22222222", "resveratrol", "harm", "animal", "BIOMARKER", "MED"),
        GoldStandardRecord("33333333", "aspirin", "benefit", "human", "CV_EVENTS", "HIGH"),
    ]
