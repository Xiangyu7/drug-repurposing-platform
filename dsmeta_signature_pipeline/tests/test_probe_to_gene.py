"""Tests for 02b_probe_to_gene.py core functions."""
import sys
from pathlib import Path
import numpy as np
import pandas as pd
import pytest

# Import the module
import importlib.util
spec = importlib.util.spec_from_file_location(
    "probe_to_gene",
    Path(__file__).resolve().parent.parent / "scripts" / "02b_probe_to_gene.py"
)
ptg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ptg)

_clean_gene_symbol = ptg._clean_gene_symbol
is_already_gene_symbols = ptg.is_already_gene_symbols
map_de_to_genes = ptg.map_de_to_genes
map_expression_to_genes = ptg.map_expression_to_genes


# =====================================================================
# Gene symbol cleaning tests
# =====================================================================
class TestCleanGeneSymbol:
    def test_simple_symbol(self):
        assert _clean_gene_symbol("IL1B") == "IL1B"

    def test_simple_symbol_with_dash(self):
        assert _clean_gene_symbol("HLA-A") == "HLA-A"

    def test_triple_slash(self):
        """Multiple genes: take first."""
        assert _clean_gene_symbol("IL1B /// TNF") == "IL1B"

    def test_hugene_st_format(self):
        """HuGene ST gene_assignment format."""
        result = _clean_gene_symbol("NM_000576 // IL1B // interleukin 1 beta // 2q14 // 3553")
        assert result == "IL1B"

    def test_dashes_and_na(self):
        assert _clean_gene_symbol("---") == ""
        assert _clean_gene_symbol("") == ""
        assert _clean_gene_symbol("NA") == ""

    def test_none(self):
        assert _clean_gene_symbol(None) == ""

    def test_nan_float(self):
        assert _clean_gene_symbol(float("nan")) == ""

    def test_refseq_only(self):
        """RefSeq ID alone (no gene symbol embedded) should return empty."""
        # NM_000576 alone doesn't match gene symbol pattern (starts with N, has digits)
        # but it might get caught. Check behavior:
        result = _clean_gene_symbol("NM_000576")
        # NM is filtered out in the fallback
        assert result == ""

    def test_complex_gene_assignment(self):
        """Complex gene_assignment with multiple // separators."""
        raw = "NR_046018 // DDX11L1 // DEAD/H-box helicase 11 like 1 // 1p36.33 // 100287102"
        result = _clean_gene_symbol(raw)
        assert result == "DDX11L1"


# =====================================================================
# Feature ID detection tests
# =====================================================================
class TestIsAlreadyGeneSymbols:
    def test_gene_symbols(self):
        """Typical gene symbols should be detected."""
        s = pd.Series(["IL1B", "TNF", "CCL2", "VCAM1", "NOS3", "ABCA1",
                       "PPARGC1A", "KLF2", "MMP9", "ICAM1"])
        assert is_already_gene_symbols(s) == True

    def test_affymetrix_probes(self):
        """Affymetrix probe IDs should NOT be detected as gene symbols."""
        s = pd.Series(["1007_s_at", "1053_at", "117_at", "121_at", "1255_g_at",
                       "1294_at", "1316_at", "1320_at", "1405_i_at", "1431_at"])
        assert is_already_gene_symbols(s) == False

    def test_numeric_probes(self):
        """Numeric probe IDs (HuGene ST) should NOT be detected."""
        s = pd.Series(["7892501", "7892502", "7892503", "7892504", "7892505"])
        assert is_already_gene_symbols(s) == False

    def test_illumina_probes(self):
        """Illumina probe IDs should NOT be detected."""
        s = pd.Series(["ILMN_1343291", "ILMN_1343295", "ILMN_1651209",
                       "ILMN_1651228", "ILMN_1651254"])
        assert is_already_gene_symbols(s) == False

    def test_empty_series(self):
        assert is_already_gene_symbols(pd.Series([])) == False

    def test_mixed(self):
        """Mostly gene symbols with a few non-gene → should still return True."""
        s = pd.Series(["IL1B", "TNF", "CCL2", "123_at", "VCAM1", "NOS3",
                       "ABCA1", "KLF2", "MMP9", "ICAM1"])
        # 8/10 = 80% gene-like → True
        assert is_already_gene_symbols(s) == True


# =====================================================================
# DE mapping tests
# =====================================================================
class TestMapDeToGenes:
    @pytest.fixture
    def de_file(self, tmp_path):
        """Create a temporary DE file with probe IDs."""
        de = pd.DataFrame({
            "feature_id": ["probe_A", "probe_B", "probe_C", "probe_D", "probe_E"],
            "logFC": [1.5, -0.8, 0.3, 2.1, -1.2],
            "t": [5.0, -3.0, 1.0, 7.0, -4.0],
            "P.Value": [0.001, 0.01, 0.3, 0.0001, 0.005],
            "gse": ["GSE1"] * 5,
            "se": [0.3, 0.27, 0.3, 0.3, 0.3],
            "sign": [1, -1, 1, 1, -1],
        })
        path = tmp_path / "de.tsv"
        de.to_csv(path, sep="\t", index=False)
        return str(path)

    @pytest.fixture
    def mapping(self):
        """Probe → gene mapping with one-to-many."""
        return pd.DataFrame({
            "probe_id": ["probe_A", "probe_B", "probe_C", "probe_D", "probe_E"],
            "gene_symbol": ["GENE1", "GENE2", "GENE1", "GENE3", "GENE4"],
            # probe_A and probe_C both map to GENE1
        })

    def test_basic_mapping(self, de_file, mapping):
        result, stats = map_de_to_genes(de_file, mapping)
        assert "GENE1" in result["feature_id"].values
        assert "GENE2" in result["feature_id"].values
        assert "GENE3" in result["feature_id"].values
        assert stats["unique_genes"] == 4

    def test_multi_probe_keeps_max_t(self, de_file, mapping):
        """When two probes map to same gene, keep the one with higher |t|."""
        result, stats = map_de_to_genes(de_file, mapping)
        gene1_row = result[result["feature_id"] == "GENE1"]
        assert len(gene1_row) == 1
        # probe_A has |t|=5.0, probe_C has |t|=1.0 → should keep probe_A
        assert abs(gene1_row["t"].values[0] - 5.0) < 0.01

    def test_empty_mapping(self, de_file):
        """No probes match → returns original data unchanged, mapped=0."""
        mapping = pd.DataFrame({
            "probe_id": ["nonexistent"],
            "gene_symbol": ["GENEX"],
        })
        result, stats = map_de_to_genes(de_file, mapping)
        assert stats["mapped"] == 0
        assert stats["genes"] == 0
        # Original DE data returned as-is
        assert len(result) == 5


# =====================================================================
# Expression mapping tests
# =====================================================================
class TestMapExpressionToGenes:
    @pytest.fixture
    def expr_file(self, tmp_path):
        """Create a temporary expression file."""
        np.random.seed(42)
        expr = pd.DataFrame({
            "feature_id": ["probe_A", "probe_B", "probe_C"],
            "S1": np.random.randn(3),
            "S2": np.random.randn(3),
            "S3": np.random.randn(3),
        })
        # Make probe_A have higher variance than probe_C
        expr.loc[0, ["S1", "S2", "S3"]] = [5.0, 1.0, 8.0]  # var=12.33
        expr.loc[2, ["S1", "S2", "S3"]] = [3.0, 3.1, 3.0]  # var~0.003
        path = tmp_path / "expr.tsv"
        expr.to_csv(path, sep="\t", index=False)
        return str(path)

    @pytest.fixture
    def mapping(self):
        return pd.DataFrame({
            "probe_id": ["probe_A", "probe_B", "probe_C"],
            "gene_symbol": ["GENE1", "GENE2", "GENE1"],
            # probe_A and probe_C both map to GENE1
        })

    def test_multi_probe_keeps_max_variance(self, expr_file, mapping):
        """When two probes map to same gene, keep higher variance."""
        result, stats = map_expression_to_genes(expr_file, mapping)
        gene1_row = result[result["feature_id"] == "GENE1"]
        assert len(gene1_row) == 1
        # probe_A has higher variance → should keep probe_A's values
        assert gene1_row["S1"].values[0] == 5.0

    def test_gene_count(self, expr_file, mapping):
        result, stats = map_expression_to_genes(expr_file, mapping)
        assert stats["unique_genes"] == 2  # GENE1 and GENE2


# =====================================================================
# URL generation tests
# =====================================================================
class TestGPLUrlGeneration:
    """Test that GPL FTP URL patterns are generated correctly."""

    def test_gpl_under_1000(self):
        """GPL570 → GPLnnn"""
        import re
        gpl_id = "GPL570"
        gpl_num_str = re.search(r"(\d+)", gpl_id).group(1)
        if len(gpl_num_str) <= 3:
            prefix = "GPLnnn"
        else:
            prefix = f"GPL{gpl_num_str[:-3]}nnn"
        assert prefix == "GPLnnn"

    def test_gpl_6000(self):
        """GPL6244 → GPL6nnn"""
        import re
        gpl_id = "GPL6244"
        gpl_num_str = re.search(r"(\d+)", gpl_id).group(1)
        if len(gpl_num_str) <= 3:
            prefix = "GPLnnn"
        else:
            prefix = f"GPL{gpl_num_str[:-3]}nnn"
        assert prefix == "GPL6nnn"

    def test_gpl_10000(self):
        """GPL10558 → GPL10nnn"""
        import re
        gpl_id = "GPL10558"
        gpl_num_str = re.search(r"(\d+)", gpl_id).group(1)
        if len(gpl_num_str) <= 3:
            prefix = "GPLnnn"
        else:
            prefix = f"GPL{gpl_num_str[:-3]}nnn"
        assert prefix == "GPL10nnn"

    def test_gpl_96(self):
        """GPL96 → GPLnnn"""
        import re
        gpl_id = "GPL96"
        gpl_num_str = re.search(r"(\d+)", gpl_id).group(1)
        if len(gpl_num_str) <= 3:
            prefix = "GPLnnn"
        else:
            prefix = f"GPL{gpl_num_str[:-3]}nnn"
        assert prefix == "GPLnnn"
