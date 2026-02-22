#!/usr/bin/env Rscript
# ===========================================================================
# 03_de_analysis.R - DESeq2 differential expression for ARCHS4 counts
#
# Adapted from dsmeta_signature_pipeline/scripts/02_de_deseq2.R
# Simplified: ARCHS4 data already has gene symbols and raw counts.
#
# Input per series:
#   work/{disease}/archs4/{series_id}/counts.tsv   (gene x sample)
#   work/{disease}/archs4/{series_id}/coldata.tsv  (sample metadata)
#
# Output per series:
#   work/{disease}/de/{series_id}/de.tsv
#     Columns: feature_id, logFC, AveExpr, t, P.Value, adj.P.Val, B, gse, se, sign
# ===========================================================================
suppressPackageStartupMessages({
  library(optparse)
  library(yaml)
  library(data.table)
  library(DESeq2)
})

opt_list <- list(
  make_option(c("--config"), type="character", help="config yaml"),
  make_option(c("--workdir"), type="character", default="work", help="work dir")
)
opt <- parse_args(OptionParser(option_list=opt_list))
cfg <- yaml::read_yaml(opt$config)

# --- Reproducibility ---
seed <- cfg$project$seed
if (!is.null(seed)) set.seed(as.integer(seed))

workdir <- opt$workdir
min_count <- cfg$de$min_count
if (is.null(min_count)) min_count <- 10
min_samples_de <- cfg$de$min_samples
if (is.null(min_samples_de)) min_samples_de <- 3

# ===========================================================================
# Find all series directories
# ===========================================================================
archs4_dir <- file.path(workdir, "archs4")
series_dirs <- list.dirs(archs4_dir, recursive = FALSE)
# Filter to only directories that have counts.tsv
series_dirs <- series_dirs[file.exists(file.path(series_dirs, "counts.tsv"))]

if (length(series_dirs) == 0) {
  stop("No series directories found in ", archs4_dir)
}

message("Found ", length(series_dirs), " series to process")

# ===========================================================================
# Process each series
# ===========================================================================
n_success <- 0L
n_fail <- 0L

for (sdir in series_dirs) {
  series_id <- basename(sdir)
  message("\n=== DE (DESeq2) for ", series_id, " ===")

  tryCatch({
    counts_path <- file.path(sdir, "counts.tsv")
    coldata_path <- file.path(sdir, "coldata.tsv")

    if (!file.exists(counts_path)) stop("counts.tsv not found")
    if (!file.exists(coldata_path)) stop("coldata.tsv not found")

    # --- Load data ---
    counts_dt <- fread(counts_path)
    coldata_dt <- fread(coldata_path)

    # First column is feature_id (gene symbol)
    feature_id <- counts_dt[[1]]
    count_mat <- as.matrix(counts_dt[, -1, with=FALSE])
    rownames(count_mat) <- feature_id

    message("  Count matrix: ", nrow(count_mat), " genes x ", ncol(count_mat), " samples")

    # --- Match with coldata ---
    # coldata has gsm, group columns
    if (!"group" %in% names(coldata_dt)) stop("coldata.tsv missing 'group' column")
    if (!"gsm" %in% names(coldata_dt)) stop("coldata.tsv missing 'gsm' column")

    # Match columns to coldata rows — STRICT GSM matching only
    # SAFETY: Never use position-based matching — it can silently swap case/control labels
    common <- intersect(colnames(count_mat), coldata_dt$gsm)
    if (length(common) < 4) {
      stop("FATAL: Cannot match count columns to coldata GSMs. ",
           "Only ", length(common), " common IDs found. ",
           "count_mat cols: ", paste(head(colnames(count_mat), 3), collapse=", "),
           " ... coldata GSMs: ", paste(head(coldata_dt$gsm, 3), collapse=", "),
           ". This indicates a data integrity issue in ARCHS4 extraction.")
    }

    if (length(common) < ncol(count_mat)) {
      message("  [WARN] ", ncol(count_mat) - length(common),
              " samples in counts.tsv not found in coldata — dropping them")
    }

    # Subset and align using strict GSM key matching
    count_mat <- count_mat[, common, drop=FALSE]
    setkey(coldata_dt, gsm)
    coldata_dt <- coldata_dt[common]

    n_case <- sum(coldata_dt$group == "case")
    n_ctrl <- sum(coldata_dt$group == "control")
    message("  Samples: ", n_case, " case, ", n_ctrl, " control")

    if (n_case < 2) stop("Need at least 2 case samples, found ", n_case)
    if (n_ctrl < 2) stop("Need at least 2 control samples, found ", n_ctrl)

    # --- Ensure integer counts ---
    count_mat[is.na(count_mat)] <- 0L
    count_mat[count_mat < 0] <- 0L
    storage.mode(count_mat) <- "integer"

    # --- Remove zero-count genes ---
    keep_genes <- rowSums(count_mat) > 0
    n_zero <- sum(!keep_genes)
    if (n_zero > 0) {
      message("  [QC] Removing ", n_zero, " zero-count genes")
      count_mat <- count_mat[keep_genes, , drop=FALSE]
    }

    # --- Pre-filter low-count genes ---
    min_samp <- min(as.integer(min_samples_de), floor(ncol(count_mat) / 2))
    keep_filt <- rowSums(count_mat >= as.integer(min_count)) >= min_samp
    n_low <- sum(!keep_filt)
    if (n_low > 0) {
      message("  [QC] Removing ", n_low, " low-count genes (< ", min_count,
              " in < ", min_samp, " samples)")
      count_mat <- count_mat[keep_filt, , drop=FALSE]
    }

    if (nrow(count_mat) < 10) stop("Too few genes after filtering: ", nrow(count_mat))

    message("  After filtering: ", nrow(count_mat), " genes x ", ncol(count_mat), " samples")

    # --- DESeq2 ---
    col_data <- data.frame(
      group = factor(coldata_dt$group, levels = c("control", "case")),
      row.names = coldata_dt$gsm
    )

    dds <- DESeqDataSetFromMatrix(
      countData = count_mat,
      colData = col_data,
      design = ~ group
    )

    message("  Running DESeq2...")
    dds <- DESeq(dds, quiet = TRUE)
    res <- results(dds, contrast = c("group", "case", "control"),
                   independentFiltering = TRUE)

    # --- Convert to standard output format ---
    res_df <- as.data.frame(res)
    res_df$feature_id <- rownames(res_df)

    tab_dt <- data.table(
      feature_id = res_df$feature_id,
      logFC      = res_df$log2FoldChange,
      AveExpr    = log2(res_df$baseMean + 1),
      t          = res_df$stat,
      P.Value    = res_df$pvalue,
      adj.P.Val  = res_df$padj,
      B          = NA_real_,
      gse        = series_id,
      se         = res_df$lfcSE,
      sign       = sign(res_df$log2FoldChange)
    )

    # Remove NAs
    n_before <- nrow(tab_dt)
    tab_dt <- tab_dt[!is.na(logFC) & !is.na(P.Value)]
    n_removed <- n_before - nrow(tab_dt)
    if (n_removed > 0) {
      message("  [QC] Removed ", n_removed, " genes with NA results")
    }
    tab_dt[is.na(adj.P.Val), adj.P.Val := 1]

    # --- Save ---
    out_dir <- file.path(workdir, "de", series_id)
    dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)
    fwrite(tab_dt, file = file.path(out_dir, "de.tsv"), sep = "\t")

    # Also save labeled coldata for reference
    fwrite(coldata_dt, file = file.path(out_dir, "pheno_labeled.tsv"), sep = "\t")

    message("  Saved: ", out_dir, " (", nrow(tab_dt), " genes)")
    n_success <- n_success + 1L

  }, error = function(e) {
    message("[ERROR] ", series_id, ": ", conditionMessage(e))
    n_fail <<- n_fail + 1L
  })
}

message("\nDone DESeq2 DE: ", n_success, "/", n_success + n_fail, " series succeeded",
        if (n_fail > 0) paste0(" (", n_fail, " FAILED)") else "")

if (n_success == 0) {
  stop("All series failed DE analysis. Cannot continue.")
}
