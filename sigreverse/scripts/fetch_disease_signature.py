#!/usr/bin/env python3
"""Fetch disease gene signature from public databases.

Automatically downloads up/down-regulated gene lists and produces a
disease_signature.json ready for the SigReverse pipeline.

Data sources (tried in order):
    1. CREEDS  — 839 curated disease-vs-normal signatures from GEO
    2. Bulk fallback — downloads all CREEDS disease signatures and searches locally

Output format (compatible with sigreverse):
    {
        "name": "atherosclerosis",
        "up":   ["IL1B", "TNF", ...],
        "down": ["NOS3", "ABCA1", ...],
        "meta": { source, geo_ids, n_signatures_merged, ... }
    }

Usage:
    # Search and download (interactive — shows options)
    python scripts/fetch_disease_signature.py --disease atherosclerosis

    # Auto-select best and write to specific path
    python scripts/fetch_disease_signature.py --disease atherosclerosis \
        --out data/input/disease_signature_auto.json --auto

    # Merge multiple GEO signatures for the same disease (more robust)
    python scripts/fetch_disease_signature.py --disease atherosclerosis \
        --merge --top-n 200 --out data/input/disease_signature_merged.json

    # List available diseases
    python scripts/fetch_disease_signature.py --list
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import Counter
from typing import Dict, List, Optional, Tuple

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("sigreverse.fetch_signature")

CREEDS_BULK = "https://maayanlab.cloud/CREEDS/download/disease_signatures-v1.0.json"
CREEDS_CACHE_PATH = "data/cache/creeds_disease_signatures.json"

DEFAULT_TOP_N = 200  # genes per direction


# ---------------------------------------------------------------------------
# CREEDS bulk download + local search
# ---------------------------------------------------------------------------

def _load_creeds_catalog(cache_path: str = CREEDS_CACHE_PATH) -> List[dict]:
    """Load CREEDS disease signatures — from local cache or bulk download.

    The CREEDS search API is no longer available, but the bulk download
    endpoint still works. We download once and cache locally (~17MB).
    Each entry already includes up_genes and down_genes.
    """
    # Try local cache first
    if os.path.exists(cache_path):
        logger.info(f"Loading CREEDS catalog from cache: {cache_path}")
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Cache read failed, re-downloading: {e}")

    # Download bulk file
    logger.info("Downloading CREEDS disease signature catalog (~17MB)...")
    logger.info("  (this only happens once — cached for future runs)")
    try:
        r = requests.get(CREEDS_BULK, timeout=120)
        r.raise_for_status()
        all_sigs = r.json()
    except Exception as e:
        logger.error(f"Failed to download CREEDS catalog: {e}")
        return []

    # Cache locally
    os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(all_sigs, f, ensure_ascii=False)
        logger.info(f"Cached {len(all_sigs)} signatures → {cache_path}")
    except Exception as e:
        logger.warning(f"Failed to cache: {e}")

    return all_sigs


def creeds_search(disease: str, organism: str = "human") -> List[dict]:
    """Search CREEDS for disease signatures (local search on bulk data).

    Returns list of full signature dicts (including gene lists).
    Matches disease_name using case-insensitive substring match.
    """
    all_sigs = _load_creeds_catalog()
    if not all_sigs:
        return []

    disease_lower = disease.lower()
    results = []
    for sig in all_sigs:
        if sig.get("organism", "").lower() != organism.lower():
            continue
        sig_disease = sig.get("disease_name", "").lower()
        if disease_lower in sig_disease or sig_disease in disease_lower:
            results.append(sig)

    logger.info(f"Found {len(results)} {organism} signatures matching '{disease}'")
    return results


def creeds_get_signature(sig_id: str) -> Optional[dict]:
    """Get a signature by ID from the local catalog.

    Since the bulk download includes gene lists, this just looks it up.
    """
    all_sigs = _load_creeds_catalog()
    for sig in all_sigs:
        if sig.get("id") == sig_id:
            return sig
    logger.warning(f"Signature {sig_id} not found in catalog")
    return None


def creeds_list_diseases() -> List[Tuple[str, int]]:
    """List unique diseases in CREEDS catalog.

    Returns sorted list of (disease_name, count) tuples.
    """
    all_sigs = _load_creeds_catalog()
    if not all_sigs:
        return []

    disease_counts: Counter = Counter()
    for sig in all_sigs:
        if sig.get("organism", "").lower() == "human":
            name = sig.get("disease_name", "unknown")
            disease_counts[name] += 1

    return disease_counts.most_common()


# ---------------------------------------------------------------------------
# Gene list extraction & merging
# ---------------------------------------------------------------------------

def extract_genes(sig: dict) -> Tuple[List[Tuple[str, float]], List[Tuple[str, float]]]:
    """Extract (gene, score) tuples from a CREEDS signature.

    CREEDS format: up_genes = [[gene_symbol, fold_change], ...]
    Returns (up_list, down_list) of (gene, score) tuples.
    """
    up = []
    for item in sig.get("up_genes", []):
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            gene = str(item[0]).strip().upper()
            score = float(item[1])
            if gene:
                up.append((gene, score))

    down = []
    for item in sig.get("down_genes", []):
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            gene = str(item[0]).strip().upper()
            score = float(item[1])
            if gene:
                down.append((gene, score))

    return up, down


def merge_signatures(
    signatures: List[dict],
    top_n: int = 200,
) -> Tuple[List[str], List[str], dict]:
    """Merge multiple CREEDS signatures for the same disease.

    Strategy: rank aggregation by frequency + mean fold change.
    A gene that appears as up-regulated in 3/4 signatures is more
    reliable than one that appears in only 1.

    Args:
        signatures: List of full CREEDS signature dicts.
        top_n: Number of genes to keep per direction.

    Returns:
        (up_genes, down_genes, merge_meta)
    """
    up_scores: Dict[str, List[float]] = {}
    down_scores: Dict[str, List[float]] = {}
    geo_ids = []

    for sig in signatures:
        geo_id = sig.get("geo_id", "unknown")
        geo_ids.append(geo_id)
        up, down = extract_genes(sig)

        for gene, score in up:
            up_scores.setdefault(gene, []).append(score)
        for gene, score in down:
            down_scores.setdefault(gene, []).append(score)

    n_sigs = len(signatures)

    # Score = frequency * mean_abs_score (reward consistency + effect size)
    def rank_score(gene_scores: Dict[str, List[float]]) -> List[Tuple[str, float]]:
        ranked = []
        for gene, scores in gene_scores.items():
            freq = len(scores) / n_sigs
            mean_abs = sum(abs(s) for s in scores) / len(scores)
            # Combined score: frequency × effect size
            combined = freq * mean_abs
            ranked.append((gene, combined))
        ranked.sort(key=lambda x: x[1], reverse=True)
        return ranked

    up_ranked = rank_score(up_scores)
    down_ranked = rank_score(down_scores)

    # Remove genes that appear in both directions (ambiguous)
    up_set = {g for g, _ in up_ranked[:top_n * 2]}
    down_set = {g for g, _ in down_ranked[:top_n * 2]}
    ambiguous = up_set & down_set

    up_genes = [g for g, _ in up_ranked if g not in ambiguous][:top_n]
    down_genes = [g for g, _ in down_ranked if g not in ambiguous][:top_n]

    merge_meta = {
        "n_signatures_merged": n_sigs,
        "geo_ids": geo_ids,
        "n_ambiguous_removed": len(ambiguous),
        "up_candidates_before_filter": len(up_scores),
        "down_candidates_before_filter": len(down_scores),
    }

    return up_genes, down_genes, merge_meta


def single_signature_genes(sig: dict, top_n: int = 200) -> Tuple[List[str], List[str]]:
    """Extract top-N genes from a single CREEDS signature."""
    up, down = extract_genes(sig)

    # Sort by absolute fold change (descending)
    up.sort(key=lambda x: abs(x[1]), reverse=True)
    down.sort(key=lambda x: abs(x[1]), reverse=True)

    up_genes = [g for g, _ in up[:top_n]]
    down_genes = [g for g, _ in down[:top_n]]

    return up_genes, down_genes


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_signature_json(
    path: str,
    disease_name: str,
    up_genes: List[str],
    down_genes: List[str],
    meta: Optional[dict] = None,
):
    """Write disease_signature.json in sigreverse-compatible format."""
    payload = {
        "name": disease_name,
        "up": up_genes,
        "down": down_genes,
        "meta": meta or {},
    }

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    logger.info(f"Wrote: {path}")
    logger.info(f"  {len(up_genes)} up + {len(down_genes)} down genes")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def cmd_list():
    """List all available diseases in CREEDS."""
    diseases = creeds_list_diseases()
    if not diseases:
        print("Failed to fetch disease list.")
        return

    print(f"\nCREEDS disease signatures ({len(diseases)} diseases):\n")
    print(f"{'Disease':<50} {'Signatures':>10}")
    print("-" * 62)
    for name, count in diseases:
        print(f"{name:<50} {count:>10}")


def cmd_fetch(args):
    """Fetch disease signature and write JSON."""
    disease = args.disease
    top_n = args.top_n
    merge = args.merge

    # Step 1: Search CREEDS
    results = creeds_search(disease)

    if not results:
        print(f"\nNo signatures found for '{disease}'.")
        print("Try --list to see available diseases, or use a broader term.")
        sys.exit(1)

    # Show results
    print(f"\nFound {len(results)} signature(s) for '{disease}':\n")
    print(f"  {'#':<4} {'ID':<12} {'GEO':<12} {'Disease':<35} {'Cell Type'}")
    print("  " + "-" * 80)
    for i, sig in enumerate(results):
        print(f"  {i+1:<4} {sig.get('id',''):<12} "
              f"{sig.get('geo_id',''):<12} "
              f"{sig.get('disease_name',''):<35} "
              f"{sig.get('cell_type','')}")

    # Step 2: Fetch full signatures
    if merge and len(results) > 1:
        # Merge mode: download all and merge
        print(f"\nMerging {len(results)} signatures (top {top_n} genes per direction)...")
        full_sigs = []
        for sig_meta in results:
            full = creeds_get_signature(sig_meta["id"])
            if full:
                full_sigs.append(full)
                up, down = extract_genes(full)
                print(f"  {sig_meta['id']}: {len(up)} up + {len(down)} down genes")

        if not full_sigs:
            print("Failed to fetch any signatures.")
            sys.exit(1)

        up_genes, down_genes, merge_meta = merge_signatures(full_sigs, top_n=top_n)

        meta = {
            "source": "CREEDS",
            "method": "multi-signature merge (frequency × effect size)",
            "disease_query": disease,
            "top_n_per_direction": top_n,
            **merge_meta,
        }
    else:
        # Single signature mode
        if args.auto:
            choice = 0
        elif len(results) == 1:
            choice = 0
        else:
            try:
                choice = int(input(f"\nSelect signature (1-{len(results)}): ")) - 1
                if choice < 0 or choice >= len(results):
                    print("Invalid selection.")
                    sys.exit(1)
            except (ValueError, EOFError):
                choice = 0
                print(f"Using first signature: {results[0]['id']}")

        selected = results[choice]
        print(f"\nFetching {selected['id']} ({selected.get('geo_id', '')})...")
        full_sig = creeds_get_signature(selected["id"])
        if not full_sig:
            print("Failed to fetch signature.")
            sys.exit(1)

        up_genes, down_genes = single_signature_genes(full_sig, top_n=top_n)

        meta = {
            "source": "CREEDS",
            "method": "single GEO signature",
            "creeds_id": selected["id"],
            "geo_id": selected.get("geo_id", ""),
            "cell_type": selected.get("cell_type", ""),
            "platform": selected.get("platform", ""),
            "disease_query": disease,
            "top_n_per_direction": top_n,
        }

    # Step 3: Write output
    out_path = args.out
    if not out_path:
        safe_name = disease.replace(" ", "_").replace("/", "_").lower()
        out_path = f"data/input/disease_signature_{safe_name}.json"

    write_signature_json(out_path, disease, up_genes, down_genes, meta)

    print(f"\n{'='*60}")
    print(f"Disease signature ready!")
    print(f"{'='*60}")
    print(f"  Disease:    {disease}")
    print(f"  Up genes:   {len(up_genes)}")
    print(f"  Down genes: {len(down_genes)}")
    print(f"  Output:     {out_path}")
    print(f"\nNext step — run SigReverse:")
    print(f"  python scripts/run.py \\")
    print(f"    --config configs/default.yaml \\")
    print(f"    --in {out_path} \\")
    print(f"    --out data/output_{disease.replace(' ','_')}/")
    print(f"{'='*60}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Fetch disease gene signature from CREEDS for SigReverse",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # List all available diseases
  python scripts/fetch_disease_signature.py --list

  # Download atherosclerosis signature (interactive)
  python scripts/fetch_disease_signature.py --disease atherosclerosis

  # Auto-select best, merge multiple GEO datasets
  python scripts/fetch_disease_signature.py --disease atherosclerosis \\
      --merge --auto --top-n 200

  # Specific disease with custom output path
  python scripts/fetch_disease_signature.py --disease "breast cancer" \\
      --merge --out data/input/breast_cancer_sig.json
        """,
    )

    parser.add_argument("--list", action="store_true",
                        help="List all available diseases in CREEDS")
    parser.add_argument("--disease", "-d", type=str,
                        help="Disease name to search (e.g., 'atherosclerosis', 'breast cancer')")
    parser.add_argument("--merge", "-m", action="store_true",
                        help="Merge all matching GEO signatures (recommended for robustness)")
    parser.add_argument("--auto", "-a", action="store_true",
                        help="Auto-select first result (no interactive prompt)")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N,
                        help=f"Number of genes per direction (default: {DEFAULT_TOP_N})")
    parser.add_argument("--out", "-o", type=str, default=None,
                        help="Output JSON path (default: data/input/disease_signature_<name>.json)")

    args = parser.parse_args()

    if args.list:
        cmd_list()
    elif args.disease:
        cmd_fetch(args)
    else:
        parser.print_help()
        print("\nError: specify --disease <name> or --list")
        sys.exit(1)


if __name__ == "__main__":
    main()
