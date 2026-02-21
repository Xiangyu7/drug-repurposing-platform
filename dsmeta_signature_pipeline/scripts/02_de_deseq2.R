#!/usr/bin/env Rscript
# ===========================================================================
# 02_de_deseq2.R - Differential expression via DESeq2 for RNA-seq data
#
# Mirrors 02_de_limma.R in structure and output format but uses DESeq2
# instead of limma. Only processes GSEs with data_type.txt = "rnaseq" or
# "rnaseq_normalized".
#
# Output de.tsv columns (identical to limma output):
#   feature_id, logFC, AveExpr, t, P.Value, adj.P.Val, B, gse, se, sign
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

# --- Reproducibility: set seed from config ---
seed <- as.integer(cfg$project$seed)
if (!is.na(seed)) set.seed(seed)

workdir <- opt$workdir
gse_list <- cfg$geo$gse_list

label_mode <- cfg$labeling$mode
regex_rules <- cfg$labeling$regex_rules
explicit <- cfg$labeling$explicit
covariates <- cfg$de$covariates

# ===========================================================================
# Helper functions (same as limma script for consistency)
# ===========================================================================
assign_groups_regex <- function(pheno_dt, rules){
  fields <- c("title","source_name_ch1","characteristics_ch1","description")
  for (f in fields) if (!(f %in% names(pheno_dt))) pheno_dt[, (f) := ""]
  text <- apply(pheno_dt[, ..fields], 1, function(x) paste(x, collapse=" | "))
  text_l <- tolower(text)

  match_any <- function(patterns){
    if (length(patterns) == 0) return(rep(FALSE, length(text_l)))
    Reduce(`|`, lapply(patterns, function(p) grepl(tolower(p), text_l, perl=TRUE)))
  }
  is_case <- match_any(unlist(rules$case$any))
  is_ctrl <- match_any(unlist(rules$control$any))

  group <- rep(NA_character_, length(text_l))
  group[is_case & !is_ctrl] <- "case"
  group[is_ctrl & !is_case] <- "control"
  return(group)
}

# ===========================================================================
# Main loop
# ===========================================================================
n_success <- 0L
n_fail <- 0L
n_skip <- 0L

for (gse in gse_list){
  message("\n=== DE (DESeq2) for ", gse, " ===")

  tryCatch({
    gdir <- file.path(workdir, "geo", gse)

    # --- Check data type: only process RNA-seq ---
    dtype_file <- file.path(gdir, "data_type.txt")
    if (!file.exists(dtype_file)) {
      message("  Skipping ", gse, " (no data_type.txt, assumed microarray)")
      n_skip <- n_skip + 1L
      next
    }
    dtype <- trimws(readLines(dtype_file, n = 1))
    if (!startsWith(dtype, "rnaseq")) {
      message("  Skipping ", gse, " (data_type=", dtype, ", not RNA-seq)")
      n_skip <- n_skip + 1L
      next
    }

    expr_path <- file.path(gdir, "expr.tsv")
    pheno_path <- file.path(gdir, "pheno.tsv")

    # --- Input validation ---
    if (!file.exists(expr_path)) stop("Expression file not found: ", expr_path)
    if (!file.exists(pheno_path)) stop("Phenotype file not found: ", pheno_path)

    expr_dt <- fread(expr_path)
    pheno_dt <- fread(pheno_path)

    # --- Extract feature IDs ---
    if ("feature_id" %in% names(expr_dt)) {
      feature_id <- expr_dt$feature_id
      expr_dt[, feature_id := NULL]
    } else {
      message("  [WARN] No feature_id column, using row indices")
      feature_id <- as.character(seq_len(nrow(expr_dt)))
    }

    # --- Build count matrix ---
    count_mat <- as.matrix(expr_dt)
    rownames(count_mat) <- feature_id

    # Handle normalized data: round to integers with warning
    if (dtype == "rnaseq_normalized") {
      message("  [WARN] Normalized data detected. Rounding to pseudo-counts for DESeq2.")
      count_mat <- round(count_mat)
    }

    # Ensure non-negative integers
    count_mat[is.na(count_mat)] <- 0L
    count_mat[count_mat < 0] <- 0L
    storage.mode(count_mat) <- "integer"

    message("  Count matrix: ", nrow(count_mat), " features x ", ncol(count_mat), " samples")

    # --- Remove genes with zero total counts ---
    keep_genes <- rowSums(count_mat) > 0
    n_zero <- sum(!keep_genes)
    if (n_zero > 0) {
      message("  [QC] Removing ", n_zero, " zero-count features")
      count_mat <- count_mat[keep_genes, , drop = FALSE]
    }

    # --- Match pheno to expression ---
    expr_cols <- colnames(count_mat)

    # Strategy 1: direct GSM match
    matched <- FALSE
    if ("gsm" %in% names(pheno_dt)) {
      common <- intersect(pheno_dt$gsm, expr_cols)
      if (length(common) >= 2) {
        setkey(pheno_dt, gsm)
        pheno_dt <- pheno_dt[common]
        count_mat <- count_mat[, common, drop = FALSE]
        matched <- TRUE
      }
    }

    # Strategy 2: map via title field (e.g. title="case [1240831p]" -> expr col="X1240831p")
    if (!matched && "title" %in% names(pheno_dt)) {
      # Extract sample IDs from title brackets or use whole title
      extracted <- gsub(".*\\[(.+)\\].*", "\\1", pheno_dt$title)
      # R prepends X to numeric-starting colnames
      extracted_x <- paste0("X", extracted)
      # Try both with and without X prefix
      if (sum(extracted_x %in% expr_cols) >= 2) {
        pheno_dt[, expr_col := extracted_x]
        keep_match <- pheno_dt$expr_col %in% expr_cols
        pheno_dt <- pheno_dt[keep_match]
        count_mat <- count_mat[, pheno_dt$expr_col, drop = FALSE]
        # Rename columns to gsm for downstream consistency
        colnames(count_mat) <- pheno_dt$gsm
        matched <- TRUE
        message("  [INFO] Matched samples via title field (", nrow(pheno_dt), " samples)")
      } else if (sum(extracted %in% expr_cols) >= 2) {
        pheno_dt[, expr_col := extracted]
        keep_match <- pheno_dt$expr_col %in% expr_cols
        pheno_dt <- pheno_dt[keep_match]
        count_mat <- count_mat[, pheno_dt$expr_col, drop = FALSE]
        colnames(count_mat) <- pheno_dt$gsm
        matched <- TRUE
        message("  [INFO] Matched samples via title field (", nrow(pheno_dt), " samples)")
      }
    }

    # Strategy 3: positional match (same number of samples)
    if (!matched) {
      pheno_all <- fread(pheno_path)
      if (nrow(pheno_all) == ncol(count_mat)) {
        if ("geo_accession" %in% names(pheno_all)) {
          pheno_dt <- pheno_all
          pheno_dt[, gsm := geo_accession]
        } else {
          pheno_dt <- pheno_all
          pheno_dt[, gsm := expr_cols]
        }
        colnames(count_mat) <- pheno_dt$gsm
        matched <- TRUE
        message("  [INFO] Matched samples by position (", nrow(pheno_dt), " samples)")
      } else {
        stop("Cannot match pheno (", nrow(pheno_all), " rows) to expression (",
             ncol(count_mat), " cols) for ", gse)
      }
    }

    # --- Assign groups ---
    if (label_mode == "explicit"){
      m <- explicit[[gse]]
      if (is.null(m)) stop("Missing explicit labels for ", gse)
      group <- rep(NA_character_, nrow(pheno_dt))
      group[pheno_dt$gsm %in% m$case_gsm] <- "case"
      group[pheno_dt$gsm %in% m$control_gsm] <- "control"
    } else {
      rules <- regex_rules[[gse]]
      if (is.null(rules)) stop("Missing regex_rules for ", gse)
      group <- assign_groups_regex(pheno_dt, rules)
    }

    pheno_dt[, group := group]
    keep <- which(!is.na(pheno_dt$group))

    n_case <- sum(group[keep] == "case", na.rm = TRUE)
    n_ctrl <- sum(group[keep] == "control", na.rm = TRUE)
    message("  Labeled: ", n_case, " case, ", n_ctrl, " control, ",
            sum(is.na(group)), " unlabeled")

    # --- Validate sample counts ---
    if (n_case < 2) stop(gse, ": Need at least 2 case samples, found ", n_case)
    if (n_ctrl < 2) stop(gse, ": Need at least 2 control samples, found ", n_ctrl)

    pheno_dt <- pheno_dt[keep]
    count_mat <- count_mat[, pheno_dt$gsm, drop = FALSE]

    # --- Pre-filter low-count genes (speeds up DESeq2) ---
    min_samples <- min(3, floor(ncol(count_mat) / 2))
    keep_filt <- rowSums(count_mat >= 10) >= min_samples
    n_low <- sum(!keep_filt)
    if (n_low > 0) {
      message("  [QC] Removing ", n_low, " low-count features (< 10 counts in < ",
              min_samples, " samples)")
      count_mat <- count_mat[keep_filt, , drop = FALSE]
    }

    if (nrow(count_mat) < 10) stop(gse, ": Too few features after filtering (", nrow(count_mat), ")")

    message("  After filtering: ", nrow(count_mat), " features x ", ncol(count_mat), " samples")

    # --- DESeq2 pipeline ---
    col_data <- data.frame(
      group = factor(pheno_dt$group, levels = c("control", "case")),
      row.names = pheno_dt$gsm
    )

    # Add covariates if present
    fml <- "~ group"
    for (cov in covariates) {
      if (cov %in% names(pheno_dt)) {
        col_data[[cov]] <- pheno_dt[[cov]]
        fml <- paste0(fml, " + ", cov)
      }
    }

    dds <- DESeqDataSetFromMatrix(
      countData = count_mat,
      colData = col_data,
      design = as.formula(fml)
    )

    message("  Running DESeq2...")
    dds <- DESeq(dds, quiet = TRUE)
    res <- results(dds, contrast = c("group", "case", "control"),
                   independentFiltering = TRUE)

    # --- Convert to limma-compatible output format ---
    res_df <- as.data.frame(res)
    res_df$feature_id <- rownames(res_df)

    tab_dt <- data.table(
      feature_id = res_df$feature_id,
      logFC      = res_df$log2FoldChange,
      AveExpr    = log2(res_df$baseMean + 1),  # log2 scale for comparability
      t          = res_df$stat,                  # Wald statistic
      P.Value    = res_df$pvalue,
      adj.P.Val  = res_df$padj,
      B          = NA_real_,                     # no B-statistic in DESeq2
      gse        = gse,
      se         = res_df$lfcSE,
      sign       = sign(res_df$log2FoldChange)
    )

    # Remove genes with NA results (DESeq2 outlier/independent filtering)
    n_before <- nrow(tab_dt)
    tab_dt <- tab_dt[!is.na(logFC) & !is.na(P.Value)]
    n_removed <- n_before - nrow(tab_dt)
    if (n_removed > 0) {
      message("  [QC] Removed ", n_removed, " features with NA results (outlier/low-count)")
    }

    # Fill remaining NA adj.P.Val with 1
    tab_dt[is.na(adj.P.Val), adj.P.Val := 1]

    # --- Save outputs ---
    out_gdir <- file.path(workdir, "de", gse)
    dir.create(out_gdir, recursive = TRUE, showWarnings = FALSE)
    fwrite(pheno_dt, file = file.path(out_gdir, "pheno_labeled.tsv"), sep = "\t")
    fwrite(tab_dt, file = file.path(out_gdir, "de.tsv"), sep = "\t")

    message("  Saved: ", out_gdir, " (", nrow(tab_dt), " features)")
    n_success <- n_success + 1L

  }, error = function(e) {
    message("[ERROR] ", gse, ": ", conditionMessage(e))
    n_fail <<- n_fail + 1L
  })
}

n_total <- n_success + n_fail
message("\nDone DESeq2 DE. RNA-seq processed: ", n_success, "/", n_total,
        if (n_fail > 0) paste0(" (", n_fail, " FAILED)") else "",
        " (", n_skip, " non-RNA-seq skipped)")
