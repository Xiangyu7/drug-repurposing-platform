# dsmeta-signature: Multi-GSE meta disease signature + pathway concordance

This repo builds a **robust disease signature** from multiple GEO Series (GSE) datasets using:
- **Late integration meta-analysis** (per-GSE DE → cross-study meta)
- **Rank aggregation** (optional; robust across noisy studies)
- **Pathway-level concordance** (Reactome/KEGG/WikiPathways via GSEA/fgsea)

Outputs are designed to plug into LINCS/CMap connectivity scoring.

---

## What you get (key outputs)

Under `outputs/`:

- `signature/gene_meta.tsv`  
  Per-gene meta logFC, SE, z, p, FDR, heterogeneity (I2), sign-concordance, etc.

- `signature/up_genes.txt`, `signature/down_genes.txt`  
  Ready for CMap-style up/down list connectivity.

- `signature/disease_signature_meta.json`  
  A simple JSON payload with up/down genes + per-gene weights.

- `pathways/reactome_meta.tsv` (and optional `kegg_meta.tsv`, `wikipathways_meta.tsv`)  
  Meta NES + concordance per pathway.

- `reports/qc_summary.html`  
  QC summary (lightweight HTML) + top pathways.

---

## Minimal manual steps (everything else is automated)

**You must provide:**
1) which GSE datasets to use  
2) how to label samples as case vs control (regex rules OR explicit GSM lists)

Everything else (download, preprocessing, DE, meta, GSEA, reporting) runs automatically.

---

## Quick start

### 1) Create environment (recommended: mamba)
```bash
mamba env create -f environment.yml
conda activate dsmeta
```

### 2) Copy and edit a config
```bash
cp configs/template.yaml configs/athero.yaml
# edit configs/athero.yaml
```

### 3) Run
```bash
python run.py --config configs/athero.yaml
```

---

## Notes on gene sets (Reactome/KEGG)

This pipeline supports three sources:
- **Reactome (open)**: recommended default
- **WikiPathways (open)**: good optional addition
- **KEGG**: may have licensing constraints depending on usage/source

By default, the pipeline downloads **Reactome + WikiPathways** gene sets automatically.
If you want KEGG, set `genesets.enable_kegg: true` and provide `work/genesets/kegg.gmt`.

---

## Troubleshooting

- If a GSE contains raw CEL files only (no series matrix), you may need
  a custom preprocessing step. This repo supports a "series-matrix-first"
  path. For CEL-based preprocessing, see `docs/cel_workflow.md`.

- If your groups cannot be reliably inferred from GEO metadata,
  use the explicit GSM list mode in config.

