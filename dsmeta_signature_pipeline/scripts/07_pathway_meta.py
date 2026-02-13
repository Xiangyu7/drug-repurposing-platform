#!/usr/bin/env python3
"""
07_pathway_meta.py - Pathway-level meta-analysis (industrial grade)

Improvements:
  - Uses scipy/numpy for BH-FDR (validated implementation)
  - NaN/Inf protection in Stouffer's Z
  - Summary statistics logged
  - Empty result handling
  - Input validation
"""
import argparse, logging
from pathlib import Path

import yaml
import pandas as pd
import numpy as np
from scipy.stats import norm
from rich import print

logger = logging.getLogger("dsmeta.pathway_meta")


def stouffer_signed(pvals, signs, weights=None):
    """Signed Stouffer's Z-score method for combining p-values.

    Args:
        pvals: array of p-values (will be clipped to [1e-300, 1.0])
        signs: array of direction signs (+1 or -1)
        weights: optional array of weights (default: equal weights)

    Returns:
        (combined_z, combined_p) tuple
    """
    pvals = np.clip(pvals, 1e-300, 1.0)  # prevent underflow in isf
    z = signs * norm.isf(pvals / 2.0)

    # Guard against NaN/Inf from extreme p-values
    valid = np.isfinite(z)
    if valid.sum() < 2:
        return np.nan, np.nan

    z = z[valid]
    if weights is not None:
        weights = np.asarray(weights)[valid]
    else:
        weights = np.ones_like(z)

    denom = np.sqrt(np.sum(weights**2))
    if denom < 1e-15:
        return np.nan, np.nan

    zc = np.sum(weights * z) / denom
    p = 2 * norm.sf(abs(zc))
    return float(zc), float(p)


def bh_fdr(p):
    """Benjamini-Hochberg FDR correction.

    Uses the step-up procedure. Equivalent to R's p.adjust(method="BH").
    """
    p = np.asarray(p, dtype=float)
    m = len(p)
    if m == 0:
        return np.array([])

    order = np.argsort(p)
    ranked = p[order]
    q = ranked * m / (np.arange(1, m + 1))
    # Enforce monotonicity (cumulative minimum from the right)
    q = np.minimum.accumulate(q[::-1])[::-1]
    out = np.empty_like(q)
    out[order] = np.clip(q, 0, 1)
    return out


def main():
    ap = argparse.ArgumentParser(description="Pathway-level meta-analysis via Stouffer's Z")
    ap.add_argument("--config", required=True)
    ap.add_argument("--workdir", default="work")
    args = ap.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    workdir = Path(args.workdir)
    outdir = Path(cfg["project"]["outdir"])

    min_conc = float(cfg.get("pathway_meta", {}).get("min_concordance", 0.7))
    gse_list = cfg["geo"]["gse_list"]

    gsea_dir = workdir / "gsea"
    files = list(gsea_dir.glob("*.tsv"))
    if not files:
        raise SystemExit("No GSEA files found. Run scripts/06_gsea_fgsea.R first.")

    # Load all GSEA results
    frames = []
    for f in files:
        try:
            df = pd.read_csv(f, sep="\t")
            if "gse" in df.columns and "lib" in df.columns:
                frames.append(df)
        except Exception as e:
            logger.warning("Failed to read %s: %s", f, e)

    if not frames:
        raise SystemExit("No valid GSEA files could be loaded.")

    gsea = pd.concat(frames, ignore_index=True)
    gsea = gsea[gsea["gse"].isin(gse_list)].copy()

    if gsea.empty:
        raise SystemExit("No GSEA results match the GSE list in config.")

    pdir = outdir / "pathways"
    pdir.mkdir(parents=True, exist_ok=True)

    total_pathways = 0
    total_sig = 0

    for lib in sorted(gsea["lib"].unique()):
        sub = gsea[gsea["lib"] == lib].copy()
        rows = []
        for pw, d in sub.groupby("pathway"):
            d = d.dropna(subset=["pval", "NES"])
            if len(d) < 2:
                continue
            pvals = d["pval"].values.clip(1e-300, 1.0)
            signs = np.sign(d["NES"].values)

            # Direction concordance
            pos = int(np.sum(signs > 0))
            neg = int(np.sum(signs < 0))
            conc = max(pos, neg) / len(signs)
            if conc < min_conc:
                continue

            zc, pmeta = stouffer_signed(pvals, signs)
            if np.isnan(zc):
                continue

            rows.append({
                "pathway": pw,
                "k": len(d),
                "pos": pos,
                "neg": neg,
                "concordance": round(conc, 4),
                "mean_NES": round(float(d["NES"].mean()), 4),
                "meta_z": round(zc, 4),
                "meta_p": float(pmeta),
            })

        out = pd.DataFrame(rows)
        if out.empty:
            print(f"[yellow]No pathways passed concordance for {lib}[/yellow]")
            continue

        out["fdr"] = bh_fdr(out["meta_p"].values)
        out = out.sort_values("meta_p")

        out_path = pdir / f"{lib}_meta.tsv"
        out.to_csv(out_path, sep="\t", index=False)

        n_sig = int((out["fdr"] < 0.05).sum())
        total_pathways += len(out)
        total_sig += n_sig

        print(f"[green]Saved[/green] {out_path} ({len(out)} pathways, {n_sig} FDR<0.05)")

    # Summary
    print(f"\n[bold]Pathway meta summary:[/bold]")
    print(f"  Total pathways passing concordance: {total_pathways}")
    print(f"  Pathways with FDR < 0.05: {total_sig}")


if __name__ == "__main__":
    main()
