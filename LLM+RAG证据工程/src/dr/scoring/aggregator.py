"""药物聚合器

从试验级数据聚合到药物级数据，处理别名、规范化、去重。

主要功能：
1. 药物名称规范化（canonicalize_name）
2. 生成稳定drug_id
3. 别名映射（drug_raw → canonical_name → drug_id）
4. 试验聚合统计
5. 可选的fuzzy matching（手动审查队列）
"""
import os
import hashlib
from typing import Optional, List, Tuple
from pathlib import Path

import pandas as pd

from ..common.text import canonicalize_name, normalize_basic, safe_join_unique, parse_min_pval
from ..logger import get_logger

logger = get_logger(__name__)

# Summary固定输出schema
SUMMARY_SCHEMA = [
    "drug_id",
    "canonical_name",
    "n_negative_trials",
    "row_count",
    "phases",
    "sponsors",
    "conditions",
    "primary_outcomes",
    "evidence_sources",
    "min_p",
]


def stable_drug_id_md5(canonical: str) -> str:
    """生成稳定的药物ID（使用MD5，与旧step5兼容）

    Args:
        canonical: 规范化药物名称

    Returns:
        11字符的药物ID（格式：D + 10位MD5十六进制）

    Notes:
        - 为了与step5原版输出兼容，使用MD5而非SHA1
        - 新代码应使用common.hashing.stable_drug_id（SHA1）
    """
    h = hashlib.md5(canonical.encode("utf-8")).hexdigest()[:10].upper()
    return f"D{h}"


class DrugAggregator:
    """药物聚合器

    将试验级数据（每行一个试验）聚合为药物级数据。

    Example:
        >>> aggregator = DrugAggregator()
        >>> master, alias, summary, manual_review = aggregator.process(
        ...     input_path="data/poolA_negative_drug_level.csv",
        ...     override_path="data/manual_alias_overrides.csv"
        ... )
        >>> aggregator.save_outputs(
        ...     master, alias, summary, manual_review,
        ...     output_dir="data"
        ... )
    """

    def __init__(self, use_rapidfuzz: bool = True):
        """初始化聚合器

        Args:
            use_rapidfuzz: 是否使用rapidfuzz进行相似度检测（需要安装）
        """
        self.use_rapidfuzz = use_rapidfuzz
        self.fuzz = self._try_import_rapidfuzz() if use_rapidfuzz else None

    def _try_import_rapidfuzz(self):
        """尝试导入rapidfuzz（可选依赖）"""
        try:
            from rapidfuzz import fuzz
            logger.info("rapidfuzz available - will generate manual review queue")
            return fuzz
        except ImportError:
            logger.warning("rapidfuzz not installed - skipping similarity detection")
            return None

    def process(
        self,
        input_path: str | Path,
        override_path: Optional[str | Path] = None
    ) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """处理输入数据，生成master/alias/summary/manual_review

        Args:
            input_path: 输入CSV路径（试验级数据）
            override_path: 可选的手动别名覆盖文件

        Returns:
            (master, alias, summary, manual_review)四个DataFrame

        Raises:
            FileNotFoundError: 输入文件不存在
            ValueError: 缺少必需列
        """
        input_path = Path(input_path)
        if not input_path.exists():
            raise FileNotFoundError(f"Input file not found: {input_path}")

        logger.info("Loading input: %s", input_path)
        df = pd.read_csv(input_path)

        # 1. 规范化药物名称
        df = self._normalize_drug_names(df)

        # 2. 生成master/alias
        master, alias = self._build_master_and_alias(df)

        # 3. 应用手动覆盖
        if override_path and Path(override_path).exists():
            logger.info("Applying overrides from: %s", override_path)
            master, alias = self._apply_overrides(master, alias, override_path)
        else:
            logger.info("No overrides applied")

        # 4. 下沉drug_id回df
        df = df.merge(master, on="canonical_name", how="left")

        # 5. 聚合统计
        summary = self._aggregate_summary(df)

        # 6. 生成手动审查队列（fuzzy matching）
        manual_review = self._generate_manual_review_queue(df)

        logger.info("Aggregation complete: %d drugs, %d aliases", len(master), len(alias))

        return master, alias, summary, manual_review

    def _normalize_drug_names(self, df: pd.DataFrame) -> pd.DataFrame:
        """规范化药物名称列

        Args:
            df: 输入DataFrame

        Returns:
            添加drug_raw, drug_normalized, canonical_name列的DataFrame
        """
        # 找到原始药物名称列
        if "drug_raw" in df.columns:
            raw_col = "drug_raw"
        elif "intervention_name" in df.columns:
            raw_col = "intervention_name"
        else:
            raise ValueError("Input must contain 'drug_raw' or 'intervention_name' column")

        df["drug_raw"] = df[raw_col].astype(str)

        # drug_normalized（基础标准化）
        if "drug_normalized" in df.columns:
            df["drug_normalized"] = df["drug_normalized"].astype(str).apply(normalize_basic)
        else:
            df["drug_normalized"] = df["drug_raw"].apply(normalize_basic)

        # canonical_name（激进规范化）
        df["canonical_name"] = df["drug_raw"].apply(canonicalize_name)

        # 如果canonical为空，回退到drug_normalized
        df["canonical_name"] = df["canonical_name"].where(
            df["canonical_name"].str.len() > 0,
            df["drug_normalized"]
        )

        return df

    def _build_master_and_alias(
        self,
        df: pd.DataFrame
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """构建master表（drug_id → canonical_name）和alias表（映射）

        Args:
            df: 包含canonical_name的DataFrame

        Returns:
            (master, alias)
        """
        # 提取所有不重复的canonical名称
        canon_list = sorted(set([
            c for c in df["canonical_name"].tolist()
            if isinstance(c, str) and c.strip()
        ]))

        # 创建master表
        master = pd.DataFrame({"canonical_name": canon_list})
        master["drug_id"] = master["canonical_name"].apply(stable_drug_id_md5)
        master = master[["drug_id", "canonical_name"]]

        # 创建alias表（drug_raw → canonical_name → drug_id）
        alias = (
            df[["drug_raw", "drug_normalized", "canonical_name"]]
            .drop_duplicates()
            .merge(master, on="canonical_name", how="left")
        )
        alias = alias[["drug_id", "canonical_name", "drug_raw", "drug_normalized"]].drop_duplicates()

        return master, alias

    def _apply_overrides(
        self,
        master: pd.DataFrame,
        alias: pd.DataFrame,
        override_path: str | Path
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """应用手动别名覆盖

        覆盖文件格式：
        - canonical_from, canonical_to
        - 将所有canonical_from合并到canonical_to

        Args:
            master: master表
            alias: alias表
            override_path: 覆盖文件路径

        Returns:
            更新后的(master, alias)
        """
        override_path = Path(override_path)
        ov = pd.read_csv(override_path)

        if not {"canonical_from", "canonical_to"}.issubset(set(ov.columns)):
            raise ValueError(f"{override_path} must have columns: canonical_from, canonical_to")

        # 构建canonical_name → drug_id映射
        canon_to_id = dict(zip(master["canonical_name"], master["drug_id"]))

        # 构建ID替换映射
        repl = {}  # from_id -> to_id
        for _, r in ov.iterrows():
            a = canonicalize_name(r["canonical_from"])
            b = canonicalize_name(r["canonical_to"])

            if not a or not b:
                continue

            if a in canon_to_id and b in canon_to_id:
                repl[canon_to_id[a]] = canon_to_id[b]
                logger.debug("Override: %s -> %s", a, b)

        if not repl:
            logger.info("No valid overrides found")
            return master, alias

        logger.info("Applying %d overrides", len(repl))

        # 在alias中替换drug_id
        alias["drug_id"] = alias["drug_id"].apply(lambda x: repl.get(x, x))

        # 重建master（保留每个drug_id的第一个canonical_name）
        master = (
            alias.groupby("drug_id", as_index=False)
            .agg(canonical_name=("canonical_name", "first"))
        )

        return master, alias

    def _aggregate_summary(self, df: pd.DataFrame) -> pd.DataFrame:
        """聚合统计（按drug_id）

        Args:
            df: 包含drug_id的试验级数据

        Returns:
            Summary DataFrame（符合SUMMARY_SCHEMA）
        """
        # 准备中间列（确保存在）
        df["_phase"] = df["phase"].astype(str) if "phase" in df.columns else (
            df["phases"].astype(str) if "phases" in df.columns else ""
        )
        df["_sponsor"] = df["leadSponsor"].astype(str) if "leadSponsor" in df.columns else ""
        df["_cond"] = df["conditions"].astype(str) if "conditions" in df.columns else ""
        df["_outcome"] = df["primary_outcome_title"].astype(str) if "primary_outcome_title" in df.columns else ""
        df["_src"] = df["evidence_source"].astype(str) if "evidence_source" in df.columns else ""

        if "primary_outcome_pvalues" in df.columns:
            df["_min_p"] = df["primary_outcome_pvalues"].apply(parse_min_pval)
        else:
            df["_min_p"] = float("nan")

        # n_negative_trials计数策略
        if "nctId" in df.columns:
            n_trials_series = ("nctId", "nunique")
            row_count_series = ("nctId", "size")
        else:
            n_trials_series = ("canonical_name", "size")
            row_count_series = ("canonical_name", "size")

        # 聚合
        summary = (
            df.groupby(["drug_id", "canonical_name"], as_index=False)
            .agg(
                n_negative_trials=n_trials_series,
                row_count=row_count_series,
                phases=("_phase", lambda x: safe_join_unique(x.tolist(), sep="; ", max_chars=400)),
                sponsors=("_sponsor", lambda x: safe_join_unique(x.tolist(), sep="; ", max_chars=400)),
                conditions=("_cond", lambda x: safe_join_unique(x.tolist(), sep="; ", max_chars=1500)),
                primary_outcomes=("_outcome", lambda x: safe_join_unique(x.tolist(), sep="; ", max_chars=800)),
                evidence_sources=("_src", lambda x: safe_join_unique(x.tolist(), sep="; ", max_chars=200)),
                min_p=("_min_p", "min"),
            )
        )

        # 确保schema一致（缺列补空）
        for col in SUMMARY_SCHEMA:
            if col not in summary.columns:
                summary[col] = "" if col not in {"n_negative_trials", "row_count", "min_p"} else pd.NA

        summary = summary[SUMMARY_SCHEMA]

        return summary

    def _generate_manual_review_queue(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成手动审查队列（相似药物名）

        使用fuzzy matching找出可能是同一药物但名称略有差异的候选。

        Args:
            df: 包含canonical_name的DataFrame

        Returns:
            相似对DataFrame（name_a, name_b, similarity, hint）
        """
        if self.fuzz is None:
            logger.info("Fuzzy matching disabled - empty manual review queue")
            return pd.DataFrame(columns=["name_a", "name_b", "similarity", "hint"])

        # 为避免O(n²)爆炸，只对topK高频药物做pair
        topK = 200

        if "nctId" in df.columns:
            top = (
                df.groupby("canonical_name")["nctId"]
                .nunique()
                .sort_values(ascending=False)
                .head(topK)
                .index.tolist()
            )
        else:
            top = df["canonical_name"].value_counts().head(topK).index.tolist()

        logger.info("Fuzzy matching top %d drugs (n=%d pairs)", topK, len(top) * (len(top) - 1) // 2)

        pairs = []
        for i in range(len(top)):
            for j in range(i + 1, len(top)):
                a, b = top[i], top[j]

                if len(a) < 4 or len(b) < 4:
                    continue

                s1 = self.fuzz.token_set_ratio(a, b)
                s2 = self.fuzz.partial_ratio(a, b)
                score = max(s1, s2)

                if score >= 92:
                    pairs.append({
                        "name_a": a,
                        "name_b": b,
                        "similarity": score,
                        "hint": "Consider alias merge via manual_alias_overrides.csv"
                    })

        manual = (
            pd.DataFrame(pairs).sort_values("similarity", ascending=False)
            if pairs
            else pd.DataFrame(columns=["name_a", "name_b", "similarity", "hint"])
        )

        logger.info("Found %d similar pairs for manual review", len(manual))

        return manual

    def save_outputs(
        self,
        master: pd.DataFrame,
        alias: pd.DataFrame,
        summary: pd.DataFrame,
        manual_review: pd.DataFrame,
        output_dir: str | Path = "data",
        prefix: str = ""
    ):
        """保存所有输出文件

        Args:
            master: master表
            alias: alias表
            summary: summary表
            manual_review: 手动审查队列
            output_dir: 输出目录
            prefix: 文件名前缀（可选）
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        master_path = output_dir / f"{prefix}drug_master.csv"
        alias_path = output_dir / f"{prefix}drug_alias_map.csv"
        summary_path = output_dir / f"{prefix}negative_drug_summary.csv"
        manual_path = output_dir / f"{prefix}manual_alias_review_queue.csv"

        master.to_csv(master_path, index=False, encoding="utf-8-sig")
        alias.to_csv(alias_path, index=False, encoding="utf-8-sig")
        summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
        manual_review.to_csv(manual_path, index=False, encoding="utf-8-sig")

        logger.info("Saved outputs to %s:", output_dir)
        logger.info("  - %s (%d drugs)", master_path.name, len(master))
        logger.info("  - %s (%d aliases)", alias_path.name, len(alias))
        logger.info("  - %s (%d drugs, %d cols)", summary_path.name, len(summary), len(summary.columns))
        logger.info("  - %s (%d pairs)", manual_path.name, len(manual_review))
