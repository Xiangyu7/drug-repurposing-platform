#!/usr/bin/env Rscript
# ===========================================================================
# 03b_meta_effects.R - Cross-series meta-analysis
#
# Adapted from dsmeta_signature_pipeline/scripts/03_meta_effects.R
# Uses per-series DE results from step 03 (work/{disease}/de/{series_id}/de.tsv)
# and produces gene_meta.tsv for signature assembly.
#
# Output: outdir/signature/gene_meta.tsv
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
seed <- cfg$project$seed
if (!is.null(seed)) set.seed(as.integer(seed))

workdir <- opt$workdir
outdir <- cfg$project$outdir

# Meta config with defaults
meta_cfg <- cfg$meta
if (is.null(meta_cfg)) meta_cfg <- list()
meta_model <- if (!is.null(meta_cfg$model)) meta_cfg$model else "DL"
min_sign <- if (!is.null(meta_cfg$min_sign_concordance)) as.numeric(meta_cfg$min_sign_concordance) else 0.8
flag_i2 <- if (!is.null(meta_cfg$flag_i2_above)) as.numeric(meta_cfg$flag_i2_above) else 0.75

# ===========================================================================
# Load per-series DE results
# ===========================================================================
de_dir <- file.path(workdir, "de")
series_dirs <- list.dirs(de_dir, recursive = FALSE)

all_list <- list()
per_series_genes <- list()

for (sdir in series_dirs) {
  series_id <- basename(sdir)
  f <- file.path(sdir, "de.tsv")
  if (!file.exists(f)) {
    message("[WARNING] DE file not found for ", series_id, ": ", f, " — skipping")
    next
  }
  dt <- fread(f)
  required_cols <- c("feature_id", "gse", "logFC", "se", "sign", "t", "P.Value", "adj.P.Val")
  missing_cols <- setdiff(required_cols, names(dt))
  if (length(missing_cols) > 0) {
    message("[WARNING] ", series_id, " DE file missing columns: ", paste(missing_cols, collapse=", "))
    next
  }
  all_list[[series_id]] <- dt[, ..required_cols]
  per_series_genes[[series_id]] <- unique(dt$feature_id)
}

if (length(all_list) == 0) stop("No valid DE files found. Cannot run meta-analysis.")

if (length(all_list) < 2) {
  message("[WARNING] Only ", length(all_list), " series available. No cross-study meta needed.")
  message("         Single-series results will be output as-is.")
}

all <- rbindlist(all_list, use.names=TRUE, fill=TRUE)

# ===========================================================================
# Cross-series gene overlap check
# ===========================================================================
if (length(per_series_genes) >= 2) {
  gene_sets <- per_series_genes
  series_names <- names(gene_sets)
  for (i in seq_len(length(series_names)-1)) {
    for (j in (i+1):length(series_names)) {
      overlap <- length(intersect(gene_sets[[series_names[i]]], gene_sets[[series_names[j]]]))
      total <- length(union(gene_sets[[series_names[i]]], gene_sets[[series_names[j]]]))
      pct <- if (total > 0) round(100 * overlap / total, 1) else 0
      message("  Gene overlap: ", series_names[i], " x ", series_names[j],
              " = ", overlap, " / ", total, " (", pct, "%)")
    }
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
    if (nrow(d) == 1) {
      return(list(beta=d$logFC[1], se=d$se[1], z=d$logFC[1]/d$se[1],
                  p=d$P.Value[1], tau2=NA, I2=NA, k=1L))
    }
    return(list(beta=NA, se=NA, z=NA, p=NA, tau2=NA, I2=NA, k=0L))
  }
  yi <- d$logFC
  sei <- d$se

  result <- tryCatch({
    if (meta_model == "fixed"){
      m <- rma.uni(yi=yi, sei=sei, method="FE")
    } else {
      m <- rma.uni(yi=yi, sei=sei, method="DL")
    }
    beta <- as.numeric(m$b)
    se_est <- as.numeric(m$se)
    if (is.na(se_est) || se_est <= 0) {
      z <- NA; p <- NA
    } else {
      z <- beta / se_est
      p <- 2 * pnorm(-abs(z))
    }
    I2 <- as.numeric(m$I2) / 100
    tau2 <- as.numeric(m$tau2)
    list(beta=beta, se=se_est, z=z, p=p, tau2=tau2, I2=I2, k=nrow(d))
  }, error = function(e) {
    message("  [META WARN] rma.uni failed: ", conditionMessage(e))
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

# FDR correction
meta_dt[, fdr := ifelse(is.na(meta_p), NA_real_, p.adjust(meta_p, method="BH"))]
meta_dt <- merge(meta_dt, sign_stats[, .(feature_id, n, pos, neg, sign_concordance)],
                 by="feature_id", all.x=TRUE)

# Flag high I2
if (!is.na(flag_i2)) {
  n_high_i2 <- sum(meta_dt$I2 > flag_i2, na.rm=TRUE)
  if (n_high_i2 > 0) {
    message("[INFO] ", n_high_i2, " genes with I2 > ", flag_i2)
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
message("  Total genes: ", nrow(meta_dt))
message("  Multi-series meta OK: ", n_meta_ok)
message("  Single-series only: ", n_single)
message("  Meta failed: ", n_meta_fail)
message("  FDR < 0.05: ", n_sig)
if (n_meta_ok > 0) {
  message("  Median I2: ", round(median(meta_dt$I2, na.rm=TRUE), 3))
}
message("  Median concordance: ", round(median(meta_dt$sign_concordance, na.rm=TRUE), 3))
message("Saved: ", file.path(outdir, "signature", "gene_meta.tsv"))
