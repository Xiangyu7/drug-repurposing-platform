#!/usr/bin/env python3
"""
Generate a small test ARCHS4 H5 file for end-to-end pipeline testing.

Creates a ~5MB file mimicking the real human_gene_v2.4.h5 structure:
  data/expression          (n_genes x n_samples) int32
  meta/genes/gene_symbol   (n_genes,) string
  meta/samples/geo_accession       (n_samples,) string
  meta/samples/series_id           (n_samples,) string
  meta/samples/title               (n_samples,) string
  meta/samples/source_name_ch1     (n_samples,) string
  meta/samples/characteristics_ch1 (n_samples,) string

Simulates 3 series for atherosclerosis with realistic:
  - Real human gene symbols (from HGNC common genes)
  - Realistic count distributions (NB-like)
  - Disease vs control metadata
  - Differential expression for known atherosclerosis genes

Usage:
    python scripts/generate_test_h5.py --out data/archs4/human_gene_v2.4.h5
"""
import argparse
import logging
from pathlib import Path

import h5py
import numpy as np

logger = logging.getLogger("gen_test_h5")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ---- Real gene symbols (curated subset) ----
# 2000 genes including known atherosclerosis-related genes
# The first ~100 are atherosclerosis-relevant (will have DE signal injected)
ATHERO_GENES = [
    # Inflammation / immune
    "TNF", "IL6", "IL1B", "IL18", "IL10", "IFNG", "CCL2", "CCL5", "CXCL8", "CXCL12",
    "TGFB1", "CSF1", "CSF2", "IL1A", "IL33", "IL17A", "IL4", "IL13", "IL12A", "IL23A",
    # Lipid metabolism
    "LDLR", "PCSK9", "APOB", "APOE", "APOA1", "CETP", "LCAT", "LPL", "LIPG", "ABCA1",
    "ABCG1", "SCARB1", "NPC1L1", "HMGCR", "FDFT1", "SQLE", "DHCR7", "MSMO1", "LSS", "CYP51A1",
    # Endothelial / vascular
    "VCAM1", "ICAM1", "SELE", "SELP", "NOS3", "EDN1", "VEGFA", "KDR", "FLT1", "PECAM1",
    "CDH5", "VWF", "THBD", "PROCR", "TEK", "ANGPT1", "ANGPT2", "TIE1", "ESAM", "CLDN5",
    # Matrix / remodeling
    "MMP2", "MMP9", "MMP3", "MMP7", "MMP12", "MMP14", "TIMP1", "TIMP2", "TIMP3", "COL1A1",
    "COL3A1", "FN1", "ELN", "LOX", "LOXL2", "CTSK", "CTSS", "CTSL", "CTSB", "CTSD",
    # Oxidative stress
    "SOD1", "SOD2", "CAT", "GPX1", "NOS2", "NOX4", "CYBB", "NCF1", "NCF2", "MPO",
    # Coagulation / thrombosis
    "F2", "F3", "F7", "F10", "SERPINE1", "PLAT", "PLAU", "THBS1", "GP1BA", "ITGA2B",
]

HOUSEKEEPING_GENES = [
    "GAPDH", "ACTB", "B2M", "HPRT1", "RPL13A", "RPLP0", "TBP", "UBC", "YWHAZ", "SDHA",
    "GUSB", "HMBS", "ALAS1", "PPIA", "PGK1", "TFRC", "RPS18", "RPL19", "EEF1A1", "HSP90AB1",
]

# Additional common genes to fill out to ~2000
FILLER_GENES = [
    "TP53", "EGFR", "BRAF", "KRAS", "MYC", "PTEN", "RB1", "AKT1", "PIK3CA", "MTOR",
    "BRCA1", "BRCA2", "ATM", "CHEK2", "CDK4", "CDK6", "CCND1", "CCNE1", "E2F1", "MDM2",
    "BCL2", "BAX", "CASP3", "CASP8", "CASP9", "XIAP", "BIRC5", "MCL1", "BID", "PARP1",
    "STAT3", "JAK2", "NFKB1", "RELA", "MAPK1", "MAPK3", "RAF1", "MAP2K1", "SRC", "ABL1",
    "ERBB2", "ERBB3", "MET", "ALK", "RET", "FGFR1", "FGFR2", "PDGFRA", "KIT", "FLT3",
    "NOTCH1", "WNT1", "CTNNB1", "APC", "GSK3B", "AXIN1", "DKK1", "LEF1", "TCF7", "FZD1",
    "SMAD2", "SMAD3", "SMAD4", "SMAD7", "BMP2", "BMP4", "BMPR1A", "BMPR2", "GDF15", "INHBA",
    "HIF1A", "EPAS1", "VHL", "PHD2", "FIH1", "ARNT", "EP300", "CREBBP", "SIRT1", "HDAC1",
    "DNMT1", "DNMT3A", "DNMT3B", "TET1", "TET2", "IDH1", "IDH2", "EZH2", "KDM5A", "KMT2A",
    "SWI5", "ARID1A", "SMARCA4", "SMARCB1", "CHD4", "BRD4", "TRIM28", "CBX5", "HP1A", "SUV39H1",
]


def generate_gene_list(n_genes: int = 2000) -> list[str]:
    """Generate a list of gene symbols."""
    genes = list(ATHERO_GENES)  # 100 athero genes
    genes.extend(HOUSEKEEPING_GENES)  # 20 housekeeping
    genes.extend(FILLER_GENES)  # 100 filler

    # Fill remaining with synthetic but realistic gene names
    existing = set(genes)
    prefixes = [
        "ABCC", "ADAM", "ALDH", "ANXA", "ATP", "CACNA", "CALM", "CASP",
        "CDH", "CEBP", "CLU", "COX", "CRK", "CUL", "CYP", "DAB",
        "DDR", "DUSP", "EPHA", "FAM", "FBN", "FOXO", "GJA", "GNG",
        "GRB", "GRIN", "GST", "HLA", "HOXA", "HSP", "IGF", "ILK",
        "ITGA", "KCNJ", "LAMB", "LRP", "MAP3K", "MECP", "MIR", "NEDD",
        "NR3C", "NTRK", "PAK", "PDE", "PDLIM", "PIAS", "PKD", "PLA2G",
        "PPARG", "PRDM", "PTPN", "RAB", "RASA", "RHOA", "RNF", "ROCK",
        "RPS", "RUNX", "SERPINA", "SLC", "SNAI", "SOX", "SPHK", "STAM",
        "SYK", "TAB", "TCF", "TERT", "TGFBR", "TNFRSF", "TRAF", "TRPM",
        "UBE2", "USP", "VASP", "WASF", "XBP", "YAP", "ZEB", "ZFP",
    ]
    idx = 1
    while len(genes) < n_genes:
        prefix = prefixes[idx % len(prefixes)]
        name = f"{prefix}{idx}"
        if name not in existing:
            genes.append(name)
            existing.add(name)
        idx += 1

    return genes[:n_genes]


def generate_samples() -> dict:
    """
    Generate sample metadata for 3 atherosclerosis-related GEO series.

    Returns dict with arrays of metadata fields.
    """
    rng = np.random.RandomState(42)
    samples = []

    # ---- Series 1: GSE100927 — Human carotid atherosclerotic plaques ----
    # 12 case + 11 control = 23 samples
    for i in range(12):
        samples.append({
            "gsm": f"GSM2697{100+i}",
            "series_id": "GSE100927",
            "title": f"atherosclerotic carotid plaque patient {i+1}",
            "source": "carotid artery plaque tissue",
            "characteristics": f"tissue: atherosclerotic plaque; disease state: atherosclerosis; patient_id: P{i+1:03d}",
        })
    for i in range(11):
        samples.append({
            "gsm": f"GSM2697{200+i}",
            "series_id": "GSE100927",
            "title": f"normal carotid artery control {i+1}",
            "source": "normal carotid artery tissue",
            "characteristics": f"tissue: normal artery; disease state: healthy control; patient_id: C{i+1:03d}",
        })

    # ---- Series 2: GSE28829 — Early/advanced atherosclerotic lesions ----
    # 8 case + 8 control = 16 samples
    for i in range(8):
        samples.append({
            "gsm": f"GSM7134{10+i}",
            "series_id": "GSE28829",
            "title": f"advanced atherosclerotic lesion {i+1}",
            "source": "aortic atherosclerotic lesion",
            "characteristics": f"tissue: aorta; lesion type: advanced atherosclerotic plaque; age: {55+rng.randint(0,20)}",
        })
    for i in range(8):
        samples.append({
            "gsm": f"GSM7134{30+i}",
            "series_id": "GSE28829",
            "title": f"control intimal tissue {i+1}",
            "source": "normal intimal tissue",
            "characteristics": f"tissue: aorta; lesion type: normal intima; age: {50+rng.randint(0,20)}",
        })

    # ---- Series 3: GSE43292 — Carotid endarterectomy ----
    # 10 case + 10 control = 20 samples
    for i in range(10):
        samples.append({
            "gsm": f"GSM1060{10+i}",
            "series_id": "GSE43292",
            "title": f"atheroma carotid plaque sample {i+1}",
            "source": "carotid atherosclerotic plaque",
            "characteristics": f"tissue: carotid artery; condition: atherosclerosis stenosis; gender: {'M' if rng.random() > 0.4 else 'F'}",
        })
    for i in range(10):
        samples.append({
            "gsm": f"GSM1060{40+i}",
            "series_id": "GSE43292",
            "title": f"normal macroscopically intact tissue {i+1}",
            "source": "normal carotid artery",
            "characteristics": f"tissue: carotid artery; condition: healthy control; gender: {'M' if rng.random() > 0.4 else 'F'}",
        })

    # ---- Noise: unrelated series (should NOT be selected) ----
    # Series with cancer samples, heart failure, etc.
    for i in range(15):
        samples.append({
            "gsm": f"GSM5555{10+i}",
            "series_id": "GSE55555",
            "title": f"breast cancer cell line sample {i+1}",
            "source": "MCF7 cell line",
            "characteristics": f"cell line: MCF7; treatment: {'vehicle' if i < 7 else 'tamoxifen'}",
        })
    for i in range(8):
        samples.append({
            "gsm": f"GSM6666{10+i}",
            "series_id": "GSE66666",
            "title": f"heart failure myocardial biopsy {i+1}",
            "source": "left ventricle",
            "characteristics": f"tissue: heart; condition: heart failure; EF: {25+rng.randint(0,15)}%",
        })

    return samples


def generate_expression(
    n_genes: int,
    n_samples: int,
    n_athero_genes: int,
    case_indices: list[int],
    control_indices: list[int],
    rng: np.random.RandomState,
) -> np.ndarray:
    """
    Generate a realistic count matrix with DE signal for atherosclerosis genes.

    Injects log2FC ~ N(1.5, 0.5) for up-regulated athero genes (first 60)
    and log2FC ~ N(-1.2, 0.4) for down-regulated athero genes (next 40).
    """
    # Base expression: NB-like counts
    # Mean expression per gene (log-normal distributed)
    gene_means = rng.lognormal(mean=3.0, sigma=1.5, size=n_genes)
    gene_means = np.clip(gene_means, 0.5, 5000)

    # Generate counts
    counts = np.zeros((n_genes, n_samples), dtype=np.int32)
    for g in range(n_genes):
        mu = gene_means[g]
        # Overdispersion parameter
        r = max(1, mu / 3)
        p = r / (r + mu)
        for s in range(n_samples):
            counts[g, s] = int(rng.negative_binomial(max(1, int(r)), min(0.99, max(0.01, p))))

    # Inject DE signal for atherosclerosis genes
    n_up = 60  # First 60 athero genes are up-regulated
    n_down = 40  # Next 40 athero genes are down-regulated

    for g in range(min(n_up, n_athero_genes)):
        # Up-regulated in disease vs control
        fc = 2 ** rng.normal(1.5, 0.5)  # ~2.8x fold change
        fc = max(1.5, min(fc, 8.0))
        for s in case_indices:
            counts[g, s] = max(1, int(counts[g, s] * fc))

    for g in range(n_up, min(n_up + n_down, n_athero_genes)):
        # Down-regulated in disease vs control
        fc = 2 ** rng.normal(-1.2, 0.4)  # ~0.43x fold change
        fc = max(0.1, min(fc, 0.8))
        for s in case_indices:
            counts[g, s] = max(0, int(counts[g, s] * fc))

    return counts


def main():
    ap = argparse.ArgumentParser(description="Generate test ARCHS4 H5 file")
    ap.add_argument("--out", default="data/archs4/human_gene_v2.4.h5",
                    help="Output H5 file path")
    ap.add_argument("--n-genes", type=int, default=2000,
                    help="Number of genes (default: 2000)")
    ap.add_argument("--seed", type=int, default=42, help="Random seed")
    args = ap.parse_args()

    rng = np.random.RandomState(args.seed)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Generate gene list
    genes = generate_gene_list(args.n_genes)
    n_genes = len(genes)
    logger.info("Generated %d gene symbols (first 100 = athero-relevant)", n_genes)

    # Generate sample metadata
    all_samples = generate_samples()
    n_samples = len(all_samples)
    logger.info("Generated %d samples across %d series",
                n_samples, len(set(s["series_id"] for s in all_samples)))

    # Identify case/control indices across all athero series
    case_indices = []
    control_indices = []
    athero_series = {"GSE100927", "GSE28829", "GSE43292"}
    for i, s in enumerate(all_samples):
        if s["series_id"] in athero_series:
            text = f"{s['title']} {s['source']} {s['characteristics']}".lower()
            if any(kw in text for kw in ["atherosclerotic", "atherosclerosis", "atheroma", "plaque", "lesion", "stenosis"]):
                if not any(kw in text for kw in ["normal", "healthy", "control", "intact"]):
                    case_indices.append(i)
                    continue
            if any(kw in text for kw in ["normal", "healthy", "control", "intact"]):
                control_indices.append(i)

    logger.info("Case indices: %d, Control indices: %d", len(case_indices), len(control_indices))

    # Generate expression matrix
    logger.info("Generating expression matrix (%d genes x %d samples)...", n_genes, n_samples)
    expression = generate_expression(n_genes, n_samples, len(ATHERO_GENES),
                                     case_indices, control_indices, rng)

    # Write H5 file
    logger.info("Writing H5 file: %s", out_path)
    with h5py.File(str(out_path), "w") as h5:
        # data/expression (n_genes x n_samples)
        h5.create_dataset("data/expression", data=expression, dtype="int32",
                          chunks=(min(500, n_genes), min(50, n_samples)),
                          compression="gzip", compression_opts=4)

        # meta/genes/gene_symbol
        dt = h5py.string_dtype(encoding='utf-8')
        h5.create_dataset("meta/genes/gene_symbol", data=genes, dtype=dt)

        # meta/samples/*
        fields = {
            "geo_accession": [s["gsm"] for s in all_samples],
            "series_id": [s["series_id"] for s in all_samples],
            "title": [s["title"] for s in all_samples],
            "source_name_ch1": [s["source"] for s in all_samples],
            "characteristics_ch1": [s["characteristics"] for s in all_samples],
        }
        for field_name, values in fields.items():
            h5.create_dataset(f"meta/samples/{field_name}", data=values, dtype=dt)

    file_size = out_path.stat().st_size
    logger.info("Done! File size: %.1f MB", file_size / 1024 / 1024)
    logger.info("  Genes: %d (%d athero-relevant with DE signal)", n_genes, len(ATHERO_GENES))
    logger.info("  Samples: %d total", n_samples)
    logger.info("  Series: GSE100927 (23), GSE28829 (16), GSE43292 (20), GSE55555 (15), GSE66666 (8)")
    logger.info("  DE signal: 60 up-regulated + 40 down-regulated athero genes in disease vs control")
    logger.info("")
    logger.info("To run pipeline:")
    logger.info("  python run.py --config configs/atherosclerosis.yaml")


if __name__ == "__main__":
    main()
