#!/usr/bin/env python3
"""
Generate a realistic gene_meta.tsv from two DE result files.
Uses actual logFC, t-stats, and SE from GSE28829 and GSE43292 to compute
fixed-effect meta-analysis statistics for overlapping genes.
"""

import pandas as pd
import numpy as np
from scipy import stats

# --- Paths ---
BASE = "/Users/xinyueke/Desktop/Drug Repurposing/dsmeta_signature_pipeline"
DE1 = f"{BASE}/work/de/GSE28829/de.tsv"
DE2 = f"{BASE}/work/de/GSE43292/de.tsv"
OUT = f"{BASE}/outputs/signature/gene_meta.tsv"

# --- Load DE results ---
df1 = pd.read_csv(DE1, sep="\t")
df2 = pd.read_csv(DE2, sep="\t")

print(f"GSE28829: {len(df1)} genes")
print(f"GSE43292: {len(df2)} genes")

# --- Find intersection ---
shared = set(df1["feature_id"]) & set(df2["feature_id"])
print(f"Shared genes: {len(shared)}")

# Index by feature_id for fast lookup
df1 = df1.set_index("feature_id")
df2 = df2.set_index("feature_id")

shared_genes = sorted(shared)

# --- Compute meta-analysis statistics per gene ---
rows = []
for gene in shared_genes:
    logfc1 = df1.loc[gene, "logFC"]
    logfc2 = df2.loc[gene, "logFC"]
    se1 = df1.loc[gene, "se"]
    se2 = df2.loc[gene, "se"]

    # Handle potential duplicates (take first if series)
    if isinstance(logfc1, pd.Series):
        logfc1 = logfc1.iloc[0]
        se1 = df1.loc[gene, "se"].iloc[0]
    if isinstance(logfc2, pd.Series):
        logfc2 = logfc2.iloc[0]
        se2 = df2.loc[gene, "se"].iloc[0]

    # Fixed-effect inverse-variance weighted meta-analysis
    w1 = 1.0 / (se1 ** 2) if se1 > 0 else 0
    w2 = 1.0 / (se2 ** 2) if se2 > 0 else 0
    w_sum = w1 + w2

    if w_sum == 0:
        meta_logFC = 0.0
        meta_se = 1.0
    else:
        meta_logFC = (w1 * logfc1 + w2 * logfc2) / w_sum
        meta_se = np.sqrt(1.0 / w_sum)

    meta_z = meta_logFC / meta_se if meta_se > 0 else 0.0
    meta_p = 2.0 * stats.norm.sf(abs(meta_z))

    # Cochran's Q and I^2 for heterogeneity
    Q = w1 * (logfc1 - meta_logFC) ** 2 + w2 * (logfc2 - meta_logFC) ** 2
    k = 2
    df_q = k - 1  # degrees of freedom = 1
    I2 = max(0.0, (Q - df_q) / Q) if Q > 0 else 0.0

    # DerSimonian-Laird tau^2 estimate
    c = w_sum - (w1 ** 2 + w2 ** 2) / w_sum
    tau2 = max(0.0, (Q - df_q) / c) if c > 0 else 0.0

    # Sign concordance
    sign1 = 1 if logfc1 > 0 else -1
    sign2 = 1 if logfc2 > 0 else -1
    sign_concordance = 1.0 if sign1 == sign2 else 0.5

    # Pos/neg counts
    pos = int(logfc1 > 0) + int(logfc2 > 0)
    neg = int(logfc1 < 0) + int(logfc2 < 0)

    rows.append({
        "feature_id": gene,
        "meta_logFC": meta_logFC,
        "meta_se": meta_se,
        "meta_z": meta_z,
        "meta_p": meta_p,
        "tau2": tau2,
        "I2": I2,
        "k": k,
        "fdr": np.nan,  # placeholder, fill after
        "n": 2,
        "pos": pos,
        "neg": neg,
        "sign_concordance": sign_concordance,
    })

result = pd.DataFrame(rows)

# --- BH FDR correction ---
from statsmodels.stats.multitest import multipletests

# Handle any edge-case p-values of exactly 0
pvals = result["meta_p"].values.copy()
pvals[pvals == 0] = np.finfo(float).tiny  # smallest representable positive float
reject, fdr_vals, _, _ = multipletests(pvals, method="fdr_bh")
result["fdr"] = fdr_vals

# --- Summary stats ---
print(f"\nOutput shape: {result.shape}")
print(f"Significant (FDR<0.05): {(result['fdr'] < 0.05).sum()}")
print(f"Sign concordant: {(result['sign_concordance'] == 1.0).sum()} / {len(result)}")
print(f"meta_logFC range: [{result['meta_logFC'].min():.4f}, {result['meta_logFC'].max():.4f}]")
print(f"meta_z range: [{result['meta_z'].min():.4f}, {result['meta_z'].max():.4f}]")
print()
print(result.head(10).to_string(index=False))

# --- Write output ---
result.to_csv(OUT, sep="\t", index=False)
print(f"\nWrote {len(result)} rows to {OUT}")
