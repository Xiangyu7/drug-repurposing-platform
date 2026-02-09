"""文本处理工具函数

消除跨脚本的代码重复：
- canonicalize_name: 5份重复 → 1份
- normalize_basic: 7份重复 → 1份
- safe_filename: 3份重复 → 1份
"""
import re
from typing import Optional

# 停用词（从configs/stop_words.yaml加载，暂时硬编码）
STOP_WORDS = {
    "tablet", "tablets", "capsule", "capsules", "injection", "injectable",
    "infusion", "oral", "iv", "intravenous", "sc", "subcutaneous",
    "im", "intramuscular", "po", "qd", "bid", "tid", "qod", "qhs",
    "sustained", "extended", "release", "er", "sr", "xr",
    "solution", "suspension", "gel", "cream", "patch", "spray",
    "drops", "drop", "mg", "g", "mcg", "ug", "iu", "ml"
}

PMID_DIGITS_RE = re.compile(r"\b(\d{6,9})\b")


def normalize_basic(x: str) -> str:
    """基础标准化：小写、去标点、去多余空格

    Args:
        x: 输入字符串

    Returns:
        标准化后的字符串

    Example:
        >>> normalize_basic("Drug (100mg)")
        'drug 100mg'
        >>> normalize_basic("DRUG  Multiple   Spaces")
        'drug multiple spaces'
    """
    s = str(x).lower().strip()
    s = re.sub(r"[\(\)\[\]\{\},;:/\\]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def canonicalize_name(x: str) -> str:
    """药物名称规范化：去剂量、去停用词、统一希腊字母

    这是DR管道中最关键的函数之一，用于跨数据源匹配药物名称。

    Args:
        x: 原始药物名称（可能包含剂量、剂型等）

    Returns:
        规范化后的药物名称（小写、无剂量、无停用词）

    Example:
        >>> canonicalize_name("Aspirin 100mg Tablet")
        'aspirin'
        >>> canonicalize_name("Interferon-α 2b Injection")
        'interferon alpha 2b'
        >>> canonicalize_name("Drug (Extended Release) 50 ug")
        'drug'

    Notes:
        - 去除剂量（如100mg, 50ug, 2ml）
        - 去除数字（如2b中的2）
        - 去除停用词（tablet, injection, etc.）
        - 希腊字母统一为英文（α→alpha, β→beta）
        - 结果为小写、单空格分隔
    """
    s = normalize_basic(x)
    if not s:
        return ""

    # 先统一希腊字母（在分词之前，保持连续性）
    s = s.replace("α", "alpha").replace("β", "beta")

    # 去剂量（如100mg, 50ug, 2.5ml）
    s = re.sub(r"\b\d+(\.\d+)?\s*(mg|g|mcg|ug|iu|ml)\b", " ", s, flags=re.I)

    # 去所有数字（避免"aspirin 100"变成"aspirin"）
    s = re.sub(r"\b\d+(\.\d+)?\b", " ", s)

    # 去连字符（interferon-alpha → interferon alpha）
    s = s.replace("-", " ")

    # 分词
    toks = [t for t in re.split(r"\s+", s) if t]

    # 去停用词
    toks = [t for t in toks if t not in STOP_WORDS]

    # 最终清理
    joined = " ".join(toks)
    joined = re.sub(r"\s+", " ", joined).strip()

    return joined


def safe_filename(s: str, max_len: int = 80) -> str:
    """转换为安全的文件名（用于缓存路径、dossier文件名）

    Args:
        s: 原始字符串
        max_len: 最大长度（默认80字符）

    Returns:
        安全的文件名（仅包含字母数字、下划线、连字符）

    Example:
        >>> safe_filename("drug/name (100mg)")
        'drug_name_100mg_'
        >>> safe_filename("α-interferon")
        '_-interferon'
        >>> safe_filename("")
        'drug'

    Notes:
        - 非字母数字字符转换为下划线
        - 多个连续下划线合并为一个
        - 空字符串返回"drug"（防止空文件名）
    """
    s = re.sub(r"[^a-zA-Z0-9\-_]+", "_", str(s).strip().lower())
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:max_len] or "drug"


def normalize_pmid(v: Optional[str]) -> str:
    """提取标准PMID（6-9位数字）

    Args:
        v: 原始PMID字符串（可能包含前缀"PMID: "等）

    Returns:
        纯数字PMID，失败返回空字符串

    Example:
        >>> normalize_pmid("12345678")
        '12345678'
        >>> normalize_pmid("PMID: 12345678")
        '12345678'
        >>> normalize_pmid("Found in 23456789 study")
        '23456789'
        >>> normalize_pmid("123")  # 太短
        ''
        >>> normalize_pmid("abc")
        ''

    Notes:
        - PMID通常为8位数字，范围6-9位
        - 多个PMID时返回第一个
    """
    if v is None:
        return ""
    s = str(v).strip()
    if not s:
        return ""
    m = PMID_DIGITS_RE.search(s)
    return m.group(1) if m else ""


def safe_join_unique(vals: list, sep: str = "; ", max_chars: int = 1200) -> str:
    """安全地连接唯一值（用于CSV字段）

    Args:
        vals: 值列表
        sep: 分隔符（默认"; "）
        max_chars: 最大字符数（默认1200）

    Returns:
        去重、排序后的连接字符串

    Example:
        >>> safe_join_unique(["a", "b", "a", None, "c"])
        'a; b; c'
        >>> safe_join_unique([1, 2, 1, 3])
        '1; 2; 3'
    """
    vv = [str(v) for v in vals if v is not None and str(v).strip()]
    vv = sorted(set(vv))
    out = sep.join(vv)
    return out[:max_chars]


def parse_min_pval(pvalues_str: str) -> float:
    """从p-values字符串中提取最小值（用于step5）

    Args:
        pvalues_str: p-values字符串（如"0.05, 0.01, 0.2"）

    Returns:
        最小p值，解析失败返回1.0

    Example:
        >>> parse_min_pval("0.05, 0.01, 0.2")
        0.01
        >>> parse_min_pval("p<0.001")
        0.001
        >>> parse_min_pval("n/a")
        1.0
    """
    if not pvalues_str or (isinstance(pvalues_str, float) and pvalues_str != pvalues_str):  # NaN check
        return 1.0

    s = str(pvalues_str).lower()

    # 提取所有数字（包括科学计数法和负号）
    nums = re.findall(r"(-?\d+\.?\d*(?:e-?\d+)?)", s)
    if not nums:
        return 1.0

    try:
        # 过滤有效p值范围[0, 1]
        pvals = []
        for n in nums:
            val = float(n)
            if 0 <= val <= 1:
                pvals.append(val)
        return min(pvals) if pvals else 1.0
    except ValueError:
        return 1.0
