"""测试文本处理工具函数"""
import pytest
from src.dr.common.text import (
    normalize_basic,
    canonicalize_name,
    safe_filename,
    normalize_pmid,
    safe_join_unique,
    parse_min_pval,
)


class TestNormalizeBasic:
    """测试基础标准化"""

    def test_lowercase(self):
        assert normalize_basic("ASPIRIN") == "aspirin"

    def test_remove_parens(self):
        assert normalize_basic("drug (100mg)") == "drug 100mg"
        assert normalize_basic("drug [extended]") == "drug extended"

    def test_collapse_spaces(self):
        assert normalize_basic("drug  multiple   spaces") == "drug multiple spaces"

    def test_empty(self):
        assert normalize_basic("") == ""
        assert normalize_basic("   ") == ""


class TestCanonicalizeName:
    """测试药物名称规范化"""

    def test_remove_dosage(self):
        assert canonicalize_name("aspirin 100mg") == "aspirin"
        assert canonicalize_name("drug 50 ug injection") == "drug"

    def test_remove_stop_words(self):
        assert canonicalize_name("aspirin tablet") == "aspirin"
        assert canonicalize_name("drug oral capsule") == "drug"

    def test_greek_letters(self):
        assert canonicalize_name("interferon-α") == "interferon alpha"
        assert canonicalize_name("TNF-β inhibitor") == "tnf beta inhibitor"

    def test_complex_case(self):
        assert canonicalize_name("Aspirin 100mg Extended Release Tablet") == "aspirin"

    def test_empty_input(self):
        assert canonicalize_name("") == ""
        assert canonicalize_name("   ") == ""

    @pytest.mark.parametrize("input,expected", [
        ("DRUG (Parenteral)", "drug parenteral"),
        ("Drug  Multiple   Spaces", "drug multiple spaces"),
        ("α-interferon 2b 100ug/ml", "alpha interferon 2b"),  # 2b是变体名，保留
    ])
    def test_edge_cases(self, input, expected):
        assert canonicalize_name(input) == expected


class TestSafeFilename:
    """测试文件名安全化"""

    def test_special_chars(self):
        assert safe_filename("drug/name") == "drug_name"
        assert safe_filename("drug (100mg)") == "drug_100mg"

    def test_long_name(self):
        long = "a" * 100
        result = safe_filename(long, max_len=80)
        assert len(result) == 80

    def test_empty(self):
        assert safe_filename("") == "drug"
        assert safe_filename("!!!") == "drug"

    def test_lowercase(self):
        assert safe_filename("ASPIRIN") == "aspirin"


class TestNormalizePMID:
    """测试PMID标准化"""

    def test_valid_pmid(self):
        assert normalize_pmid("12345678") == "12345678"
        assert normalize_pmid("123456") == "123456"  # 6位也有效

    def test_with_prefix(self):
        assert normalize_pmid("PMID: 12345678") == "12345678"
        assert normalize_pmid("Found in PMID 23456789 study") == "23456789"

    def test_invalid_pmid(self):
        assert normalize_pmid("abc") == ""
        assert normalize_pmid("123") == ""  # 太短（<6位）
        assert normalize_pmid("1234567890") == ""  # 太长（>9位）
        assert normalize_pmid(None) == ""

    def test_multiple_pmids(self):
        # 返回第一个
        assert normalize_pmid("12345678 and 23456789") == "12345678"


class TestSafeJoinUnique:
    """测试唯一值连接"""

    def test_basic(self):
        assert safe_join_unique(["a", "b", "c"]) == "a; b; c"

    def test_dedup(self):
        assert safe_join_unique(["a", "b", "a", "c"]) == "a; b; c"

    def test_none_filter(self):
        assert safe_join_unique(["a", None, "b", None, "c"]) == "a; b; c"

    def test_empty_filter(self):
        assert safe_join_unique(["a", "", "b", "  ", "c"]) == "a; b; c"

    def test_sorted(self):
        assert safe_join_unique(["c", "a", "b"]) == "a; b; c"

    def test_max_chars(self):
        long_list = ["a" * 100 for _ in range(20)]
        result = safe_join_unique(long_list, max_chars=200)
        assert len(result) <= 200


class TestParseMinPval:
    """测试p-value解析"""

    def test_basic(self):
        assert parse_min_pval("0.05") == 0.05

    def test_multiple(self):
        assert parse_min_pval("0.05, 0.01, 0.2") == 0.01

    def test_with_text(self):
        assert parse_min_pval("p<0.001") == 0.001
        assert parse_min_pval("p=0.05") == 0.05

    def test_invalid(self):
        assert parse_min_pval("n/a") == 1.0
        assert parse_min_pval("") == 1.0
        assert parse_min_pval(None) == 1.0

    def test_out_of_range(self):
        assert parse_min_pval("1.5") == 1.0  # >1不是有效p值
        assert parse_min_pval("-0.01") == 1.0  # <0不是有效p值
