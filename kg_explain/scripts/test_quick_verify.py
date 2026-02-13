"""
Quick verification script: test fixes with limited data
1. Fetch phenotype data for a small sample of diseases (verify GraphQL fix)
2. Re-run V3 ranking (verify Fix 2: deduplication, Fix 3: non-drug filter)
3. Re-run V5 ranking (verify Fix 5: safety gradient, phenotype boost)
4. Print first 100 rows for inspection
"""
import sys
import os

# 确保项目根目录在 path
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ".")

import logging
import pandas as pd
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
logger = logging.getLogger("quick_verify")

from src.kg_explain.config import load_config, ensure_dir
from src.kg_explain.cache import HTTPCache
from src.kg_explain.utils import read_csv
from src.kg_explain import datasources, rankers

# 加载配置
cfg = load_config(
    base_path="configs/base.yaml",
    version_path="configs/versions/v5.yaml",
)
cfg.raw["mode"] = "v5"

data_dir = cfg.data_dir
cache = HTTPCache(cfg.cache_dir, max_workers=4, ttl_seconds=3600*24*7)

# ====== Step 1: Test phenotype fetch ======
logger.info("=" * 60)
logger.info("Step 1: 测试 phenotype 数据获取 (验证 GraphQL query 修复)")
logger.info("=" * 60)

# 取 50 个不同前缀的 disease ID 来测试
diseases_df = read_csv(data_dir / "edge_target_disease_ot.csv", dtype=str)
all_diseases = diseases_df["diseaseId"].dropna().unique().tolist()

# 从不同前缀各取一些
test_diseases = []
prefixes_seen = {}
for d in all_diseases:
    prefix = d.split("_")[0] if "_" in d else "other"
    if prefix not in prefixes_seen:
        prefixes_seen[prefix] = 0
    if prefixes_seen[prefix] < 10:
        test_diseases.append(d)
        prefixes_seen[prefix] += 1
    if len(test_diseases) >= 50:
        break

logger.info("测试 %d 个 disease ID (前缀分布: %s)", len(test_diseases),
            {k: v for k, v in prefixes_seen.items()})

# Fetch phenotypes for test sample
result = datasources.fetch_disease_phenotypes(
    data_dir, cache, test_diseases,
    min_score=0.3,
    max_phenotypes=30,
)

# 检查结果
phe_path = Path(result)
if phe_path.exists() and phe_path.stat().st_size > 10:
    phe_df = read_csv(phe_path, dtype=str)
    logger.info("Phenotype 结果: %d 行, %d 个疾病有表型", len(phe_df),
                phe_df["diseaseId"].nunique() if not phe_df.empty else 0)

    if not phe_df.empty:
        logger.info("样例:")
        print(phe_df.head(10).to_string())
        print()
        # 统计各前缀命中率
        for prefix in sorted(prefixes_seen.keys()):
            prefix_diseases = [d for d in test_diseases if d.startswith(prefix)]
            hits = phe_df[phe_df["diseaseId"].isin(prefix_diseases)]["diseaseId"].nunique()
            logger.info("  %s 前缀: %d/%d 有表型 (%.0f%%)", prefix, hits, len(prefix_diseases),
                        100 * hits / max(len(prefix_diseases), 1))
    else:
        logger.warning("Phenotype DataFrame 为空!")
else:
    logger.warning("Phenotype 文件为空或不存在!")

# ====== Step 2: 重跑 V3 + V5 排序 ======
logger.info("=" * 60)
logger.info("Step 2: 重跑 V5 排序 (验证 Fix 2/3/5)")
logger.info("=" * 60)

result = rankers.run_pipeline(cfg)

# ====== Step 3: 检查输出质量 ======
logger.info("=" * 60)
logger.info("Step 3: 输出质量检查")
logger.info("=" * 60)

# V3
v3_df = read_csv(cfg.output_dir / "drug_disease_rank_v3.csv")
logger.info("V3 排序: %d 行, %d 个药物", len(v3_df), v3_df["drug_normalized"].nunique())

# 检查非药物条目
non_drugs = ["cells", "placebo", "saline", "sodium chloride"]
for term in non_drugs:
    matches = v3_df[v3_df["drug_normalized"].str.contains(term, case=False, na=False)]
    if not matches.empty:
        logger.warning("⚠ V3 仍含非药物 '%s': %d 行", term, len(matches))
    else:
        logger.info("✓ V3 无 '%s' 条目", term)

# 检查路径重复
v3_paths = pd.read_json(cfg.output_dir / "evidence_paths_v3.jsonl", lines=True)
logger.info("V3 evidence paths: %d 行", len(v3_paths))

# 找一个药物看是否有重复路径
if not v3_paths.empty:
    sample_drug = v3_paths["drug"].value_counts().index[0]
    sample_paths = v3_paths[v3_paths["drug"] == sample_drug]
    sample_disease = sample_paths["diseaseId"].value_counts().index[0]
    pair_paths = sample_paths[sample_paths["diseaseId"] == sample_disease]
    logger.info("样例路径 (%s, %s): %d 条", sample_drug, sample_disease, len(pair_paths))

# V5
v5_df = read_csv(cfg.output_dir / "drug_disease_rank_v5.csv")
logger.info("\nV5 排序: %d 行, %d 个药物", len(v5_df), v5_df["drug_normalized"].nunique())

# 检查 safety_penalty 梯度
sp = pd.to_numeric(v5_df["safety_penalty"], errors="coerce")
logger.info("safety_penalty 统计:")
logger.info("  min=%.4f, max=%.4f, mean=%.4f, median=%.4f", sp.min(), sp.max(), sp.mean(), sp.median())
logger.info("  唯一值数量: %d", sp.nunique())
sp_values = sp.value_counts().head(10)
logger.info("  top 值分布:\n%s", sp_values.to_string())

# 检查 phenotype_boost
pb = pd.to_numeric(v5_df["phenotype_boost"], errors="coerce")
logger.info("\nphenotype_boost 统计:")
logger.info("  min=%.4f, max=%.4f, mean=%.4f", pb.min(), pb.max(), pb.mean())
logger.info("  非零行: %d/%d (%.1f%%)", (pb > 0).sum(), len(pb), 100 * (pb > 0).sum() / max(len(pb), 1))

# 检查 trial_penalty
tp = pd.to_numeric(v5_df["trial_penalty"], errors="coerce")
logger.info("\ntrial_penalty 统计:")
logger.info("  min=%.4f, max=%.4f, mean=%.4f", tp.min(), tp.max(), tp.mean())
logger.info("  非零行: %d/%d", (tp > 0).sum(), len(tp))

# ====== 打印前 100 行 ======
logger.info("=" * 60)
logger.info("前 100 行 V5 排序结果 (按 final_score 降序)")
logger.info("=" * 60)

v5_sorted = v5_df.sort_values("final_score", ascending=False).head(100)
pd.set_option("display.max_columns", None)
pd.set_option("display.width", 200)
pd.set_option("display.max_colwidth", 30)
print(v5_sorted.to_string(index=False))

logger.info("\n完成! 共 %d 行 V5 输出", len(v5_df))
