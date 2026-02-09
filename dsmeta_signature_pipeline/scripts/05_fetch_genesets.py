#!/usr/bin/env python3
import argparse
from pathlib import Path
import yaml
import requests
from rich import print

REACTOME_GMT_URLS = [
    "https://reactome.org/download/current/ReactomePathways.gmt",
]
WIKIPATHWAYS_GMT_URLS = [
    "https://data.wikipathways.org/current/gmt/wikipathways-Homo_sapiens.gmt",
    "https://wikipathways-data.wmcloud.org/current/gmt/wikipathways-Homo_sapiens.gmt",
    "https://data.wikipathways.org/current/gmt/wikipathways.gmt",
    "https://wikipathways-data.wmcloud.org/current/gmt/wikipathways.gmt",
]

def download_first(urls, out_path: Path):
    last = None
    for url in urls:
        try:
            print(f"[cyan]Downloading {url}[/cyan]")
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            out_path.write_bytes(r.content)
            return
        except Exception as e:
            last = e
            print(f"[yellow]Failed {url}: {e}[/yellow]")
    raise RuntimeError(f"All downloads failed for {out_path.name}. Last error: {last}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--workdir", default="work")
    args = ap.parse_args()
    cfg = yaml.safe_load(open(args.config, "r", encoding="utf-8"))

    workdir = Path(args.workdir)
    gdir = workdir / "genesets"
    gdir.mkdir(parents=True, exist_ok=True)

    gs = cfg.get("genesets", {})
    if gs.get("enable_reactome", True):
        out = gdir / "reactome.gmt"
        if not out.exists():
            download_first(REACTOME_GMT_URLS, out)
        else:
            print("[green]reactome.gmt already exists[/green]")

    if gs.get("enable_wikipathways", True):
        out = gdir / "wikipathways.gmt"
        if not out.exists():
            try:
                download_first(WIKIPATHWAYS_GMT_URLS, out)
            except RuntimeError as e:
                print(f"[yellow]WARNING: {e}. WikiPathways will be skipped.[/yellow]")
        else:
            print("[green]wikipathways.gmt already exists[/green]")

    if gs.get("enable_kegg", False):
        print("[yellow]KEGG enabled. Provide work/genesets/kegg.gmt manually (source/license dependent).[/yellow]")

    print("[green]Gene sets ready.[/green]")

if __name__ == "__main__":
    main()
