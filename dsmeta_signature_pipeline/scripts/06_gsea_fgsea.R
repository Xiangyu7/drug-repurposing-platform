#!/usr/bin/env Rscript
# ===========================================================================
# 06_gsea_fgsea.R - Per-study GSEA via fgsea (industrial grade)
#
# Improvements:
#   - set.seed() for reproducibility (fgsea uses permutations)
#   - Input validation (check DE files, gene stats)
#   - Duplicate gene name handling
#   - tryCatch per GSE/library (don't fail entire pipeline)
#   - Summary stats logged
# ===========================================================================
suppressPackageStartupMessages({
  library(optparse)
  library(yaml)
  library(data.table)
  library(fgsea)
})

opt_list <- list(
  make_option(c("--config"), type="character", help="config yaml"),
  make_option(c("--workdir"), type="character", default="work", help="work dir")
)
opt <- parse_args(OptionParser(option_list=opt_list))
cfg <- yaml::read_yaml(opt$config)

# --- Reproducibility: set seed (critical for fgsea permutations) ---
seed <- as.integer(cfg$project$seed)
if (!is.na(seed)) set.seed(seed)

workdir <- opt$workdir
gse_list <- cfg$geo$gse_list

minSize <- as.integer(cfg$gsea$min_size)
maxSize <- as.integer(cfg$gsea$max_size)
nperm <- as.integer(cfg$gsea$nperm)

# --- Validate GSEA params ---
if (is.na(nperm) || nperm < 100) {
  message("[WARNING] nperm=", nperm, " is very low. Recommend >= 1000. Using 1000.")
  nperm <- 1000L
}

# --- Load gene sets ---
gs_dir <- file.path(workdir, "genesets")
genesets <- list()
if (isTRUE(cfg$genesets$enable_reactome) && file.exists(file.path(gs_dir, "reactome.gmt"))){
  genesets[["reactome"]] <- fgsea::gmtPathways(file.path(gs_dir, "reactome.gmt"))
  message("Loaded Reactome: ", length(genesets[["reactome"]]), " pathways")
}
if (isTRUE(cfg$genesets$enable_kegg) && file.exists(file.path(gs_dir, "kegg.gmt"))){
  genesets[["kegg"]] <- fgsea::gmtPathways(file.path(gs_dir, "kegg.gmt"))
  message("Loaded KEGG: ", length(genesets[["kegg"]]), " pathways")
}
if (length(genesets) == 0) stop("No gene sets available (GMT). Run 05_fetch_genesets.py first.")

dir.create(file.path(workdir, "gsea"), recursive=TRUE, showWarnings=FALSE)

n_success <- 0L
n_fail <- 0L

for (gse in gse_list){
  for (lib in names(genesets)){
    message("GSEA: ", gse, " / ", lib)

    tryCatch({
      de_path <- file.path(workdir, "de", gse, "de.tsv")
      if (!file.exists(de_path)) {
        message("  [SKIP] DE file not found: ", de_path)
        next
      }
      de <- fread(de_path)
      de <- de[!is.na(t)]

      if (nrow(de) < 100) {
        message("  [SKIP] Too few genes with valid t-stat: ", nrow(de))
        next
      }

      stats <- de$t
      names(stats) <- de$feature_id

      # Handle duplicate gene names (take max abs t-stat)
      if (any(duplicated(names(stats)))) {
        n_dup <- sum(duplicated(names(stats)))
        message("  [QC] ", n_dup, " duplicate feature IDs. Keeping max |t| per ID.")
        dt_tmp <- data.table(gene=names(stats), t=stats)
        dt_tmp[, abs_t := abs(t)]
        dt_tmp <- dt_tmp[dt_tmp[, .I[which.max(abs_t)], by=gene]$V1]
        stats <- dt_tmp$t
        names(stats) <- dt_tmp$gene
      }

      stats <- sort(stats, decreasing=TRUE)

      pathways <- genesets[[lib]]

      # Reset seed before each fgsea call for exact reproducibility
      if (!is.na(seed)) set.seed(seed)

      fg <- fgsea(pathways=pathways, stats=stats, minSize=minSize,
                  maxSize=maxSize, nPermSimple=nperm)
      fg_dt <- as.data.table(fg)
      fg_dt[, gse := gse]
      fg_dt[, lib := lib]

      out_path <- file.path(workdir, "gsea", paste0(gse, "__", lib, ".tsv"))
      fwrite(fg_dt, file=out_path, sep="\t")
      message("  Saved: ", out_path, " (", nrow(fg_dt), " pathways)")
      n_success <- n_success + 1L

    }, error = function(e) {
      message("  [ERROR] ", gse, "/", lib, ": ", conditionMessage(e))
      n_fail <<- n_fail + 1L
    })
  }
}

message("\nDone GSEA. Success: ", n_success, " / ", n_success + n_fail)
if (n_success == 0) stop("All GSEA runs failed. Cannot proceed to pathway meta.")
