#!/usr/bin/env Rscript
# ===========================================================================
# 02_de_limma.R - Differential expression via limma (industrial grade)
#
# Improvements over original:
#   - set.seed() from config for reproducibility
#   - Input validation: min samples, NA%, log-scale detection
#   - Informative warnings for edge cases
#   - tryCatch around per-GSE processing (continue on soft errors)
# ===========================================================================
suppressPackageStartupMessages({
  library(optparse)
  library(yaml)
  library(data.table)
  library(limma)
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

qc_remove_outliers <- isTRUE(cfg$de$qc$remove_outliers)
pca_outlier_z <- as.numeric(cfg$de$qc$pca_outlier_z)

# ===========================================================================
# Helper functions
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

pca_outliers <- function(expr_mat){
  if (ncol(expr_mat) < 4) {
    message("  [QC] Too few samples for PCA outlier detection, skipping")
    return(integer(0))
  }
  x <- t(expr_mat)
  # Remove zero-variance features before PCA
  v <- apply(x, 2, var, na.rm=TRUE)
  x <- x[, which(v > 0), drop=FALSE]
  if (ncol(x) < 2) {
    message("  [QC] Not enough variable features for PCA, skipping outlier detection")
    return(integer(0))
  }
  x <- scale(x)
  # Replace any remaining NaN from scaling with 0
  x[is.nan(x)] <- 0
  n_pcs <- min(2, ncol(x), nrow(x) - 1)
  pc <- prcomp(x, center=TRUE, scale.=FALSE)
  scores <- pc$x[, seq_len(n_pcs), drop=FALSE]
  d <- sqrt(rowSums(scores^2))
  z <- (d - mean(d, na.rm=TRUE)) / sd(d, na.rm=TRUE)
  which(z > pca_outlier_z)
}

detect_log_scale <- function(expr_mat) {
  # Heuristic: if max > 30, data is likely NOT log-transformed
  mx <- max(expr_mat, na.rm=TRUE)
  if (mx > 30) {
    return(FALSE)
  }
  return(TRUE)
}

# ===========================================================================
# Main loop
# ===========================================================================
n_success <- 0L
n_fail <- 0L

for (gse in gse_list){
  message("\n=== DE (limma) for ", gse, " ===")

  tryCatch({
    gdir <- file.path(workdir, "geo", gse)
    expr_path <- file.path(gdir, "expr.tsv")
    pheno_path <- file.path(gdir, "pheno.tsv")

    # --- Input validation ---
    if (!file.exists(expr_path)) stop("Expression file not found: ", expr_path)
    if (!file.exists(pheno_path)) stop("Phenotype file not found: ", pheno_path)

    expr_dt <- fread(expr_path)
    pheno_dt <- fread(pheno_path)

    feature_id <- expr_dt$feature_id
    expr_dt[, feature_id := NULL]
    expr <- as.matrix(expr_dt)
    rownames(expr) <- feature_id
    colnames(expr) <- colnames(expr_dt)

    # --- Validate expression matrix ---
    na_frac <- sum(is.na(expr)) / length(expr)
    if (na_frac > 0.5) {
      warning(gse, ": >50% NA in expression matrix (", round(na_frac*100,1), "%). Results may be unreliable.")
    }
    message("  Expression matrix: ", nrow(expr), " features x ", ncol(expr), " samples")
    message("  NA fraction: ", round(na_frac*100,2), "%")

    # --- Log-scale detection ---
    if (!detect_log_scale(expr)) {
      message("  [WARNING] Data may not be log-transformed (max=", round(max(expr,na.rm=TRUE),1),
              "). Consider log2-transforming. Proceeding anyway.")
    }

    # --- Match pheno to expression ---
    setkey(pheno_dt, gsm)
    pheno_dt <- pheno_dt[colnames(expr), nomatch=0]
    if (nrow(pheno_dt) != ncol(expr)){
      stop("Pheno-sample mismatch for ", gse,
           " (pheno:", nrow(pheno_dt), " vs expr:", ncol(expr), ")")
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

    n_case <- sum(group[keep] == "case", na.rm=TRUE)
    n_ctrl <- sum(group[keep] == "control", na.rm=TRUE)
    message("  Labeled: ", n_case, " case, ", n_ctrl, " control, ",
            sum(is.na(group)), " unlabeled")

    # --- Validate sample counts ---
    if (n_case < 2) stop(gse, ": Need at least 2 case samples, found ", n_case)
    if (n_ctrl < 2) stop(gse, ": Need at least 2 control samples, found ", n_ctrl)
    if (length(keep) < 6) {
      warning(gse, ": Only ", length(keep), " labeled samples. Results may be underpowered.")
    }

    pheno_dt <- pheno_dt[keep]
    expr <- expr[, keep, drop=FALSE]

    # --- PCA outlier removal ---
    if (qc_remove_outliers){
      out_idx <- pca_outliers(expr)
      if (length(out_idx) > 0){
        message("  [QC] Removing PCA outliers: ", paste(pheno_dt$gsm[out_idx], collapse=", "))
        keep2 <- setdiff(seq_len(nrow(pheno_dt)), out_idx)
        # Check we still have enough samples after removal
        remaining_case <- sum(pheno_dt$group[keep2] == "case")
        remaining_ctrl <- sum(pheno_dt$group[keep2] == "control")
        if (remaining_case < 2 || remaining_ctrl < 2) {
          warning(gse, ": Outlier removal would leave too few samples. Keeping all samples.")
        } else {
          pheno_dt <- pheno_dt[keep2]
          expr <- expr[, keep2, drop=FALSE]
        }
      }
    }

    # --- Remove zero-variance genes (limma can choke on these) ---
    gene_var <- apply(expr, 1, var, na.rm=TRUE)
    zero_var <- which(is.na(gene_var) | gene_var == 0)
    if (length(zero_var) > 0) {
      message("  [QC] Removing ", length(zero_var), " zero-variance features")
      expr <- expr[-zero_var, , drop=FALSE]
      feature_id <- rownames(expr)
    }

    # --- limma DE ---
    pheno_dt[, group := factor(group, levels=c("control","case"))]
    covs <- covariates
    covs <- covs[covs %in% names(pheno_dt)]
    fml <- if (length(covs) == 0) {
      "~ 0 + group"
    } else {
      paste0("~ 0 + group + ", paste(covs, collapse=" + "))
    }
    design <- model.matrix(as.formula(fml), data=pheno_dt)
    colnames(design) <- make.names(colnames(design))

    fit <- lmFit(expr, design)
    cn_case <- grep("^groupcase$", colnames(design), value=TRUE)
    cn_ctrl <- grep("^groupcontrol$", colnames(design), value=TRUE)
    if (length(cn_case)!=1 || length(cn_ctrl)!=1) {
      stop("Group columns not found in design matrix for ", gse,
           ". Columns: ", paste(colnames(design), collapse=", "))
    }

    cont <- makeContrasts(contrasts=paste0(cn_case, "-", cn_ctrl), levels=design)
    fit2 <- eBayes(contrasts.fit(fit, cont))

    tab <- topTable(fit2, number=Inf, sort.by="none")
    tab_dt <- as.data.table(tab, keep.rownames="feature_id")
    tab_dt[, gse := gse]
    tab_dt[, se := ifelse(t == 0, NA_real_, abs(logFC / t))]
    tab_dt[, sign := sign(logFC)]

    # --- Save outputs ---
    out_gdir <- file.path(workdir, "de", gse)
    dir.create(out_gdir, recursive=TRUE, showWarnings=FALSE)
    fwrite(pheno_dt, file=file.path(out_gdir, "pheno_labeled.tsv"), sep="\t")
    fwrite(tab_dt, file=file.path(out_gdir, "de.tsv"), sep="\t")

    message("  Saved: ", out_gdir, " (", nrow(tab_dt), " features)")
    n_success <- n_success + 1L

  }, error = function(e) {
    message("[ERROR] ", gse, ": ", conditionMessage(e))
    n_fail <<- n_fail + 1L
  })
}

message("\nDone DE. Success: ", n_success, "/", length(gse_list),
        if (n_fail > 0) paste0(" (", n_fail, " FAILED)") else "")
if (n_success == 0) stop("All GSE datasets failed DE. Cannot proceed.")
