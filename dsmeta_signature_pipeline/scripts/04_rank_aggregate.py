#!/usr/bin/env python3
import argparse, subprocess, sys, os
from pathlib import Path
import yaml
import pandas as pd
import numpy as np
from rich import print

_env_bin = str(Path(sys.executable).resolve().parent)

def sh(cmd):
    print(f"[cyan]$ {' '.join(cmd)}[/cyan]")
    env = os.environ.copy()
    env["PATH"] = _env_bin + os.pathsep + env.get("PATH", "")
    r = subprocess.run(cmd, env=env)
    if r.returncode != 0:
        raise SystemExit(r.returncode)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--workdir", default="work")
    args = ap.parse_args()
    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    outdir = Path(cfg["project"]["outdir"])
    workdir = Path(args.workdir)

    ra = cfg.get("rank_aggregation", {})
    if not ra.get("enable", True):
        print("[yellow]Rank aggregation disabled.[/yellow]")
        return

    gse_list = cfg["geo"]["gse_list"]

    ranks = {}
    for gse in gse_list:
        de_path = workdir / "de" / gse / "de.tsv"
        de = pd.read_csv(de_path, sep="\t")
        de = de[["feature_id","t"]].dropna()
        de["rank_up"] = (-de["t"]).rank(method="average")  # 1 is most up in disease
        ranks[gse] = de.set_index("feature_id")["rank_up"]

    all_genes = sorted(set().union(*[s.index for s in ranks.values()]), key=str)
    M = pd.DataFrame({gse: ranks[gse].reindex(all_genes) for gse in gse_list})
    M = M.apply(lambda col: col.fillna(col.max(skipna=True)), axis=0)

    out_ra = outdir / "signature" / "rank_aggregation"
    out_ra.mkdir(parents=True, exist_ok=True)
    rm_path = out_ra / "rank_matrix.tsv"
    M.to_csv(rm_path, sep="\t", index_label="feature_id")

    method = ra.get("method", "rra").lower()
    if method == "rra":
        sh(["Rscript", "scripts/04b_rra.R", "--rank_matrix", str(rm_path), "--out", str(out_ra / "rra.tsv")])
        rra = pd.read_csv(out_ra / "rra.tsv", sep="\t")
        # columns: Name, Score
        rra = rra.rename(columns={"Name":"feature_id","Score":"rra_score"})
    else:
        mean_rank = M.mean(axis=1)
        rra = pd.DataFrame({"feature_id": mean_rank.index, "rra_score": mean_rank.values})
        rra.to_csv(out_ra / "meanrank.tsv", sep="\t", index=False)

    ensemble = ra.get("ensemble", {}).get("enable", True)
    if ensemble:
        w_meta = float(ra.get("ensemble", {}).get("w_meta", 0.7))
        w_rra = float(ra.get("ensemble", {}).get("w_rra", 0.3))
        meta = pd.read_csv(outdir / "signature" / "gene_meta.tsv", sep="\t")
        meta = meta.dropna(subset=["meta_z"]).copy()
        meta["meta_rank_up"] = (-meta["meta_z"]).rank(method="average")
        meta["meta_rank_up"] = meta["meta_rank_up"] / meta["meta_rank_up"].max()

        rra["rra_rank_up"] = rra["rra_score"].rank(method="average")
        rra["rra_rank_up"] = rra["rra_rank_up"] / rra["rra_rank_up"].max()

        ens = meta.merge(rra[["feature_id","rra_score","rra_rank_up"]], on="feature_id", how="left")
        ens["rra_rank_up"] = ens["rra_rank_up"].fillna(ens["meta_rank_up"])
        ens["ensemble_rank_up"] = w_meta*ens["meta_rank_up"] + w_rra*ens["rra_rank_up"]
        ens.to_csv(outdir / "signature" / "gene_meta_ensemble.tsv", sep="\t", index=False)

    print("[green]Rank aggregation done.[/green]")

if __name__ == "__main__":
    main()
