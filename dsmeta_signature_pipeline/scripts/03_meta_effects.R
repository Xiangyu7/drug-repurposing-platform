#!/usr/bin/env Rscript
# ===========================================================================
# 03_meta_effects.R - Cross-study meta-analysis (industrial grade)
#
# Improvements:
#   - set.seed() for reproducibility
#   - Cross-platform gene overlap validation & warning
#   - SE=0 protection (skip gene, don't crash)
#   - tryCatch around rma.uni (some genes can fail numerically)
#   - I2 > threshold flagging
#   - Summary statistics logged at end
# ===========================================================================
suppressPackageStartupMessages({
  library(optparse)
  library(yaml)
  library(data.table)
  library(metafor)
})

opt_list <- list(
  make_option(c("--config"), type="character", help="config yaml"),
  make_option(c("--workdir"), type="character", default="work", help="work dir")
)
opt <- parse_args(OptionParser(option_list=opt_list))
cfg <- yaml::read_yaml(opt$config)

# --- Reproducibility ---
seed <- as.integer(cfg$project$seed)
if (!is.na(seed)) set.seed(seed)

workdir <- opt$workdir
outdir <- cfg$project$outdir
gse_list <- cfg$geo$gse_list

meta_model <- cfg$meta$model
min_sign <- as.numeric(cfg$meta$min_sign_concordance)
flag_i2 <- as.numeric(cfg$meta$flag_i2_above)

# ===========================================================================
# Load per-study DE results
# ===========================================================================
all_list <- list()
per_gse_genes <- list()

for (gse in gse_list){
  f <- file.path(workdir, "de", gse, "de.tsv")
  if (!file.exists(f)) {
    message("[WARNING] DE file not found for ", gse, ": ", f, " — skipping")
    next
  }
  dt <- fread(f)
  required_cols <- c("feature_id", "gse", "logFC", "se", "sign", "t", "P.Value", "adj.P.Val")
  missing_cols <- setdiff(required_cols, names(dt))
  if (length(missing_cols) > 0) {
    message("[WARNING] ", gse, " DE file missing columns: ", paste(missing_cols, collapse=", "))
    next
  }
  all_list[[gse]] <- dt[, ..required_cols]
  per_gse_genes[[gse]] <- unique(dt$feature_id)
}

if (length(all_list) == 0) stop("No valid DE files found. Cannot run meta-analysis.")
if (length(all_list) < 2) {
  message("[WARNING] Only ", length(all_list), " study available. Meta-analysis requires >= 2 studies.")
  message("         Single-study results will be output as-is (no cross-study stats).")
}

all <- rbindlist(all_list, use.names=TRUE, fill=TRUE)

# ===========================================================================
# Cross-platform gene overlap check (CRITICAL)
# ===========================================================================
if (length(per_gse_genes) >= 2) {
  gene_sets <- per_gse_genes
  pairwise_overlaps <- c()
  gse_names <- names(gene_sets)
  for (i in seq_len(length(gse_names)-1)) {
    for (j in (i+1):length(gse_names)) {
      overlap <- length(intersect(gene_sets[[gse_names[i]]], gene_sets[[gse_names[j]]]))
      total <- length(union(gene_sets[[gse_names[i]]], gene_sets[[gse_names[j]]]))
      pct <- if (total > 0) round(100 * overlap / total, 1) else 0
      pairwise_overlaps <- c(pairwise_overlaps, overlap)
      message("  Gene overlap: ", gse_names[i], " x ", gse_names[j],
              " = ", overlap, " / ", total, " (", pct, "%)")
    }
  }
  if (all(pairwise_overlaps == 0)) {
    message("\n[CRITICAL WARNING] ============================================")
    message("  ZERO gene overlap between studies!")
    message("  This typically means different platforms use different probe IDs.")
    message("  Solution: Map probe IDs to gene symbols before meta-analysis.")
    message("  Meta-analysis will produce empty results.")
    message("================================================================\n")
  } else if (min(pairwise_overlaps) < 1000) {
    message("\n[WARNING] Low gene overlap between some studies (<1000 genes).")
    message("  Consider checking platform ID compatibility.\n")
  }
}

# ===========================================================================
# Sign concordance filter
# ===========================================================================
sign_stats <- all[!is.na(sign), .(
  n = .N,
  pos = sum(sign > 0),
  neg = sum(sign < 0)
), by=feature_id]
sign_stats[, sign_concordance := pmax(pos, neg) / n]
keep_genes <- sign_stats[sign_concordance >= min_sign, feature_id]

message("Sign concordance filter: ", length(keep_genes), " / ", nrow(sign_stats),
        " genes pass (threshold: ", min_sign, ")")

# ===========================================================================
# Per-gene meta-analysis
# ===========================================================================
meta_one <- function(d){
  d <- d[!is.na(se) & is.finite(se) & se > 0]
  if (nrow(d) < 2){
    # Single-study: return raw stats, no meta
    if (nrow(d) == 1) {
      return(list(beta=d$logFC[1], se=d$se[1], z=d$logFC[1]/d$se[1],
                  p=d$P.Value[1], tau2=NA, I2=NA, k=1L))
    }
    return(list(beta=NA, se=NA, z=NA, p=NA, tau2=NA, I2=NA, k=0L))
  }
  yi <- d$logFC
  sei <- d$se

  # tryCatch around rma.uni — some genes can fail numerically
  result <- tryCatch({
    if (meta_model == "fixed"){
      m <- rma.uni(yi=yi, sei=sei, method="FE")
    } else {
      m <- rma.uni(yi=yi, sei=sei, method="DL")
    }
    beta <- as.numeric(m$b)
    se_est <- as.numeric(m$se)
    # Guard against SE=0 from rma
    if (is.na(se_est) || se_est <= 0) {
      z <- NA
      p <- NA
    } else {
      z <- beta / se_est
      p <- 2 * pnorm(-abs(z))
    }
    I2 <- as.numeric(m$I2) / 100
    tau2 <- as.numeric(m$tau2)
    list(beta=beta, se=se_est, z=z, p=p, tau2=tau2, I2=I2, k=nrow(d))
  }, error = function(e) {
    message("  [META WARN] rma.uni failed for a gene: ", conditionMessage(e))
    list(beta=NA, se=NA, z=NA, p=NA, tau2=NA, I2=NA, k=nrow(d))
  })

  return(result)
}

genes <- intersect(unique(all$feature_id), keep_genes)
message("Running meta-analysis for ", length(genes), " genes ...")

res <- vector("list", length(genes))
n_meta_ok <- 0L
n_meta_fail <- 0L
n_single <- 0L

for (i in seq_along(genes)){
  gid <- genes[i]
  d <- all[feature_id == gid]
  m <- meta_one(d)
  res[[i]] <- data.table(
    feature_id = gid,
    meta_logFC = m$beta,
    meta_se = m$se,
    meta_z = m$z,
    meta_p = m$p,
    tau2 = m$tau2,
    I2 = m$I2,
    k = m$k
  )
  if (m$k >= 2 && !is.na(m$z)) n_meta_ok <- n_meta_ok + 1L
  else if (m$k == 1) n_single <- n_single + 1L
  else n_meta_fail <- n_meta_fail + 1L
}

meta_dt <- rbindlist(res, fill=TRUE)

# FDR correction (only on valid p-values)
meta_dt[, fdr := ifelse(is.na(meta_p), NA_real_, p.adjust(meta_p, method="BH"))]
meta_dt <- merge(meta_dt, sign_stats[, .(feature_id, n, pos, neg, sign_concordance)],
                 by="feature_id", all.x=TRUE)

# --- Flag high I2 ---
if (!is.na(flag_i2)) {
  n_high_i2 <- sum(meta_dt$I2 > flag_i2, na.rm=TRUE)
  if (n_high_i2 > 0) {
    message("[INFO] ", n_high_i2, " genes with I2 > ", flag_i2, " (high heterogeneity)")
  }
}

# ===========================================================================
# Save
# ===========================================================================
dir.create(file.path(outdir, "signature"), recursive=TRUE, showWarnings=FALSE)
fwrite(meta_dt[order(fdr)], file=file.path(outdir, "signature", "gene_meta.tsv"), sep="\t")

# --- Summary ---
n_sig <- sum(meta_dt$fdr < 0.05, na.rm=TRUE)
message("\n=== Meta-analysis summary ===")
message("  Total genes in table: ", nrow(meta_dt))
message("  Multi-study meta OK:  ", n_meta_ok)
message("  Single-study only:    ", n_single)
message("  Meta failed:          ", n_meta_fail)
message("  FDR < 0.05:           ", n_sig)
message("  Median I2:            ", round(median(meta_dt$I2, na.rm=TRUE), 3))
message("  Median concordance:   ", round(median(meta_dt$sign_concordance, na.rm=TRUE), 3))
message("Saved: ", file.path(outdir, "signature", "gene_meta.tsv"))

if (n_meta_ok == 0 && n_single > 0) {
  message("\n[WARNING] No genes had multi-study meta-analysis.")
  message("  This usually means studies have non-overlapping feature IDs.")
  message("  Consider: probe-to-gene-symbol mapping before step 03.")
}
