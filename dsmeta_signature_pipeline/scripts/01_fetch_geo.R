#!/usr/bin/env Rscript
suppressPackageStartupMessages({
  library(optparse)
  library(yaml)
  library(data.table)
  library(GEOquery)
})

# Increase timeout and set retry-friendly options for NCBI downloads
options(timeout = 600)
options(download.file.method = "libcurl")

opt_list <- list(
  make_option(c("--config"), type="character", help="config yaml"),
  make_option(c("--workdir"), type="character", default="work", help="work dir")
)
opt <- parse_args(OptionParser(option_list=opt_list))
cfg <- yaml::read_yaml(opt$config)

workdir <- opt$workdir
dir.create(workdir, recursive=TRUE, showWarnings=FALSE)

gse_list <- cfg$geo$gse_list

collapse_chars <- function(pheno){
  char_cols <- grep("^characteristics_ch1", colnames(pheno), value=TRUE)
  if (length(char_cols) == 0) return(rep("", nrow(pheno)))
  apply(pheno[, char_cols, drop=FALSE], 1, function(x) paste(na.omit(x), collapse="; "))
}

# Helper: retry getGEO up to max_tries on network failure
fetch_with_retry <- function(gse, max_tries = 3) {
  for (attempt in seq_len(max_tries)) {
    result <- tryCatch(
      getGEO(gse, GSEMatrix = TRUE, AnnotGPL = FALSE),
      error = function(e) e
    )
    if (!inherits(result, "error")) return(result)
    message("Attempt ", attempt, "/", max_tries, " failed: ", conditionMessage(result))
    if (attempt < max_tries) {
      Sys.sleep(5 * attempt)
      message("Retrying ...")
    }
  }
  stop("Failed to download ", gse, " after ", max_tries, " attempts.")
}

for (gse in gse_list){
  message("Fetching ", gse, " ...")
  gdir <- file.path(workdir, "geo", gse)
  dir.create(gdir, recursive=TRUE, showWarnings=FALSE)

  # Skip download if data already exists locally
  expr_file <- file.path(gdir, "expr.tsv")
  pheno_file <- file.path(gdir, "pheno.tsv")
  if (file.exists(expr_file) && file.exists(pheno_file)) {
    message("Local cache found, skipping download: ", gdir)
    next
  }

  gset <- fetch_with_retry(gse)
  if (length(gset) > 1){
    ns <- sapply(gset, ncol)
    idx <- which.max(ns)
    message("Multiple platforms detected; using index=", idx, " with n=", ns[idx])
    eset <- gset[[idx]]
  } else {
    eset <- gset[[1]]
  }

  expr <- exprs(eset)
  pheno <- pData(eset)

  pheno_dt <- as.data.table(pheno)
  pheno_dt[, gsm := rownames(pheno)]
  if (!("title" %in% names(pheno_dt))) pheno_dt[, title := ""]
  if (!("source_name_ch1" %in% names(pheno_dt))) pheno_dt[, source_name_ch1 := ""]
  pheno_dt[, characteristics_ch1 := collapse_chars(pheno)]

  expr_dt <- as.data.table(expr)
  expr_dt[, feature_id := rownames(expr)]
  setcolorder(expr_dt, c("feature_id", setdiff(names(expr_dt), "feature_id")))
  fwrite(expr_dt, file=file.path(gdir, "expr.tsv"), sep="\t")
  fwrite(pheno_dt, file=file.path(gdir, "pheno.tsv"), sep="\t")
  message("Saved: ", gdir)
}

message("Done fetch_geo.")
