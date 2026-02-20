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

# ===========================================================================
# Helper functions
# ===========================================================================
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

# Detect if a GEO dataset is RNA-seq based on pheno metadata
detect_rnaseq <- function(pheno) {
  # Check library_strategy column
  if ("library_strategy" %in% names(pheno)) {
    if (any(grepl("RNA-Seq", pheno$library_strategy, ignore.case = TRUE))) return(TRUE)
  }
  # Check type column (SRA = Sequence Read Archive = sequencing data)
  if ("type" %in% names(pheno)) {
    if (any(tolower(pheno$type) == "sra")) return(TRUE)
  }
  # Check known RNA-seq platform IDs
  rnaseq_platforms <- c("GPL20301", "GPL24676", "GPL28038", "GPL18573",
                        "GPL16791", "GPL20795")
  if ("platform_id" %in% names(pheno)) {
    if (any(pheno$platform_id %in% rnaseq_platforms)) return(TRUE)
  }
  return(FALSE)
}

# Validate whether a matrix contains raw integer counts vs normalized values
validate_count_matrix <- function(mat) {
  # Check non-negative
  if (any(mat < 0, na.rm = TRUE)) return(FALSE)
  # Check integer-like: all values should be whole numbers (within tolerance)
  non_na <- mat[!is.na(mat)]
  if (length(non_na) == 0) return(FALSE)
  all(abs(non_na - round(non_na)) < 0.01)
}

# Try to parse a supplementary count file
parse_count_file <- function(fpath, gsm_ids) {
  message("    Trying to parse: ", basename(fpath))

  # Skip tar/binary files
  if (grepl("\\.(tar|cel|idat|bam|sra)(\\.gz)?$", fpath, ignore.case = TRUE)) {
    message("    Skipping binary/archive file")
    return(NULL)
  }

  # Decompress if needed
  if (grepl("\\.gz$", fpath)) {
    tmp <- sub("\\.gz$", "", fpath)
    if (!file.exists(tmp)) {
      tryCatch(R.utils::gunzip(fpath, destname = tmp, remove = FALSE),
               error = function(e) {
                 system2("gunzip", c("-k", "-f", shQuote(fpath)))
               })
    }
    fpath <- tmp
  }

  # Detect and handle UTF-16 encoding
  raw_bytes <- readBin(fpath, "raw", n = 4)
  is_utf16 <- FALSE
  if (length(raw_bytes) >= 2) {
    # UTF-16 LE BOM: FF FE, or detect null bytes pattern
    if ((raw_bytes[1] == as.raw(0xFF) && raw_bytes[2] == as.raw(0xFE)) ||
        (raw_bytes[2] == as.raw(0x00))) {
      is_utf16 <- TRUE
    }
  }

  if (is_utf16) {
    message("    Detected UTF-16 encoding, converting to UTF-8...")
    utf8_path <- paste0(fpath, ".utf8")
    if (!file.exists(utf8_path)) {
      system2("iconv", c("-f", "UTF-16", "-t", "UTF-8", shQuote(fpath)),
              stdout = utf8_path)
    }
    fpath <- utf8_path
  }

  # Read file
  dt <- tryCatch(fread(fpath, header = TRUE, check.names = FALSE),
                 error = function(e) {
                   message("    Failed to read: ", conditionMessage(e))
                   return(NULL)
                 })
  if (is.null(dt) || nrow(dt) == 0) return(NULL)

  # Identify gene ID column: first non-numeric column, or named gene_id/Geneid/Gene/gene_name
  gene_col <- NULL
  gene_col_names <- c("gene_id", "geneid", "gene", "gene_name", "feature_id",
                       "ensembl_gene_id", "symbol", "gene_symbol")
  for (cn in names(dt)) {
    if (tolower(cn) %in% gene_col_names) {
      gene_col <- cn
      break
    }
  }
  if (is.null(gene_col)) {
    # Use first column if it's character/non-numeric
    first_col <- dt[[1]]
    if (is.character(first_col) || is.factor(first_col)) {
      gene_col <- names(dt)[1]
    }
  }
  if (is.null(gene_col)) {
    message("    No gene ID column found")
    return(NULL)
  }

  # Extract gene IDs
  gene_ids <- as.character(dt[[gene_col]])

  # Identify sample columns: match GSM IDs or use all numeric columns
  all_cols <- setdiff(names(dt), gene_col)
  # Try matching GSM IDs in column names
  matched_cols <- c()
  for (gsm in gsm_ids) {
    hits <- grep(gsm, all_cols, value = TRUE)
    if (length(hits) > 0) matched_cols <- c(matched_cols, hits[1])
  }

  if (length(matched_cols) >= 2) {
    sample_cols <- matched_cols
  } else {
    # Fall back to all numeric columns
    sample_cols <- all_cols[sapply(all_cols, function(cn) is.numeric(dt[[cn]]))]
  }

  if (length(sample_cols) < 2) {
    message("    Too few sample columns found (", length(sample_cols), ")")
    return(NULL)
  }

  # Build expression matrix
  expr_mat <- as.matrix(dt[, ..sample_cols])
  rownames(expr_mat) <- gene_ids

  # Clean column names: extract GSM IDs if embedded
  clean_names <- sapply(sample_cols, function(cn) {
    m <- regmatches(cn, regexpr("GSM[0-9]+", cn))
    if (length(m) > 0) m else cn
  })
  colnames(expr_mat) <- clean_names

  # Check if raw counts
  is_raw <- validate_count_matrix(expr_mat)

  # Build data.table with feature_id
  expr_dt <- cbind(data.table(feature_id = gene_ids), as.data.table(expr_mat))

  message("    Parsed: ", nrow(expr_dt), " features x ", length(sample_cols), " samples",
          " (", if (is_raw) "raw counts" else "normalized", ")")

  return(list(expr = expr_dt, is_raw_counts = is_raw))
}

# Fetch RNA-seq count data from GEO supplementary files
fetch_rnaseq_counts <- function(gse, gdir, pheno) {
  supp_dir <- file.path(gdir, "supp")
  dir.create(supp_dir, recursive = TRUE, showWarnings = FALSE)

  gsm_ids <- rownames(pheno)
  if (is.null(gsm_ids)) gsm_ids <- pheno$geo_accession

  # === Strategy 1: Series-level supplementary files ===
  message("  Trying series-level supplementary files...")
  supp_files <- tryCatch(
    getGEOSuppFiles(gse, makeDirectory = FALSE, baseDir = supp_dir),
    error = function(e) {
      message("  getGEOSuppFiles failed: ", conditionMessage(e))
      NULL
    }
  )

  if (!is.null(supp_files) && nrow(supp_files) > 0) {
    fnames <- rownames(supp_files)

    # Exclude obviously non-count files
    exclude_pat <- "fpkm|tpm|rpkm|normalized|readme|md5|soft|miniml|annotation"
    fnames <- fnames[!grepl(exclude_pat, basename(fnames), ignore.case = TRUE)]

    # Prefer files with "count" or "raw" in the name
    count_pat <- "count|raw|featurecount|readcount|gene_count"
    count_files <- fnames[grepl(count_pat, basename(fnames), ignore.case = TRUE)]

    # If no count-specific, try any text/csv/tsv files
    if (length(count_files) == 0) {
      count_files <- fnames[grepl("\\.(txt|tsv|csv|tab)(\\.gz)?$",
                                   basename(fnames), ignore.case = TRUE)]
    }

    # Also try xlsx (requires readxl), but deprioritize
    xlsx_files <- fnames[grepl("\\.xlsx?(\\.gz)?$", basename(fnames), ignore.case = TRUE)]

    # Try count files first, then generic text files
    for (cf in count_files) {
      result <- tryCatch(parse_count_file(cf, gsm_ids), error = function(e) NULL)
      if (!is.null(result) && nrow(result$expr) > 100) return(result)
    }

    # Try xlsx as last resort
    for (xf in xlsx_files) {
      result <- tryCatch({
        if (!requireNamespace("readxl", quietly = TRUE)) return(NULL)
        # Decompress if .gz
        if (grepl("\\.gz$", xf)) {
          tmp <- sub("\\.gz$", "", xf)
          if (!file.exists(tmp)) system2("gunzip", c("-k", "-f", shQuote(xf)))
          xf <- tmp
        }
        xl_dt <- as.data.table(readxl::read_excel(xf))
        # Write as temp TSV and re-parse
        tmp_tsv <- file.path(supp_dir, "temp_xlsx.tsv")
        fwrite(xl_dt, tmp_tsv, sep = "\t")
        parse_count_file(tmp_tsv, gsm_ids)
      }, error = function(e) NULL)
      if (!is.null(result) && nrow(result$expr) > 100) return(result)
    }
  }

  # === Strategy 2: Per-sample files (from tar extraction or individual downloads) ===
  message("  Trying per-sample files...")

  # Scan supp directory for per-sample files (GSM* pattern)
  all_supp_files <- list.files(supp_dir, pattern = "^GSM.*\\.(txt|tsv|csv)(\\.gz)?$",
                                full.names = TRUE, ignore.case = TRUE)
  # Also check for files without GSM prefix but with .txt extension (from tar)
  if (length(all_supp_files) < 2) {
    all_supp_files <- list.files(supp_dir, pattern = "\\.(txt|tsv|csv)$",
                                  full.names = TRUE, ignore.case = TRUE)
    # Exclude temp files
    all_supp_files <- all_supp_files[!grepl("temp_|data_type", basename(all_supp_files))]
  }

  # If no local files found, try downloading from pheno URLs
  if (length(all_supp_files) < 2) {
    supp_col <- NULL
    for (cn in c("supplementary_file", "supplementary_file_1")) {
      if (cn %in% names(pheno) && !all(is.na(pheno[[cn]]))) {
        supp_col <- cn
        break
      }
    }
    if (!is.null(supp_col)) {
      urls <- pheno[[supp_col]]
      urls <- urls[!is.na(urls) & urls != ""]
      for (url in urls) {
        local_file <- file.path(supp_dir, basename(url))
        if (!file.exists(local_file)) {
          tryCatch(download.file(url, local_file, mode = "wb", quiet = TRUE),
                   error = function(e) NULL)
        }
      }
      # Re-scan
      all_supp_files <- list.files(supp_dir, pattern = "\\.(txt|tsv|csv)(\\.gz)?$",
                                    full.names = TRUE, ignore.case = TRUE)
    }
  }

  if (length(all_supp_files) >= 2) {
    message("  Found ", length(all_supp_files), " per-sample files, merging...")
    sample_dfs <- list()

    for (sf in all_supp_files) {
      # Try to extract GSM ID from filename
      gsm_match <- regmatches(basename(sf), regexpr("GSM[0-9]+", basename(sf)))
      gsm <- if (length(gsm_match) > 0) gsm_match else basename(sf)

      # Read the file (handle gz + UTF-16)
      sdt <- tryCatch({
        f <- sf
        # Decompress gz
        if (grepl("\\.gz$", f)) {
          tmp <- sub("\\.gz$", "", f)
          if (!file.exists(tmp)) {
            tryCatch(R.utils::gunzip(f, destname = tmp, remove = FALSE),
                     error = function(e) system2("gunzip", c("-k", "-f", shQuote(f))))
          }
          f <- tmp
        }
        # Handle UTF-16
        raw_bytes <- readBin(f, "raw", n = 4)
        if (length(raw_bytes) >= 2 &&
            ((raw_bytes[1] == as.raw(0xFF) && raw_bytes[2] == as.raw(0xFE)) ||
             raw_bytes[2] == as.raw(0x00))) {
          utf8_path <- paste0(f, ".utf8")
          if (!file.exists(utf8_path)) {
            system2("iconv", c("-f", "UTF-16", "-t", "UTF-8", shQuote(f)),
                    stdout = utf8_path)
          }
          f <- utf8_path
        }
        fread(f, header = TRUE, check.names = FALSE)
      }, error = function(e) NULL)

      if (is.null(sdt) || nrow(sdt) < 10 || ncol(sdt) < 2) next

      # First column = gene ID, find first numeric column = value
      gene_col_name <- names(sdt)[1]
      val_col_name <- NULL
      for (j in 2:ncol(sdt)) {
        if (is.numeric(sdt[[j]])) { val_col_name <- names(sdt)[j]; break }
      }
      if (is.null(val_col_name)) next

      sdf <- data.table(gene_id = as.character(sdt[[gene_col_name]]),
                         value = as.numeric(sdt[[val_col_name]]))
      # Aggregate duplicate gene IDs (e.g. multiple transcripts per gene)
      sdf <- sdf[, .(value = max(value, na.rm = TRUE)), by = gene_id]
      setnames(sdf, "value", gsm)
      sample_dfs[[gsm]] <- sdf
    }

    if (length(sample_dfs) >= 2) {
      # Merge all samples by gene_id
      merged <- sample_dfs[[1]]
      for (k in 2:length(sample_dfs)) {
        merged <- merge(merged, sample_dfs[[k]], by = "gene_id", all = TRUE)
      }
      gene_ids <- merged$gene_id
      sample_cols <- setdiff(names(merged), "gene_id")
      expr_mat <- as.matrix(merged[, ..sample_cols])
      expr_mat[is.na(expr_mat)] <- 0

      # Clean column names: extract GSM IDs if embedded
      clean_names <- sapply(sample_cols, function(cn) {
        m <- regmatches(cn, regexpr("GSM[0-9]+", cn))
        if (length(m) > 0) m else cn
      })
      colnames(expr_mat) <- clean_names

      is_raw <- validate_count_matrix(expr_mat)
      expr_dt <- cbind(data.table(feature_id = gene_ids), as.data.table(expr_mat))
      message("    Merged: ", nrow(expr_dt), " features x ", length(sample_cols), " samples",
              " (", if (is_raw) "raw counts" else "normalized", ")")
      return(list(expr = expr_dt, is_raw_counts = is_raw))
    }
  }

  stop("No parseable count matrix found for ", gse)
}

# Helper: process and save pheno data
save_pheno <- function(pheno, gdir) {
  pheno_rn <- rownames(pheno)
  pheno_dt <- as.data.table(pheno)
  if (!is.null(pheno_rn) && length(pheno_rn) == nrow(pheno_dt)) {
    pheno_dt[, gsm := pheno_rn]
  }
  if (!("title" %in% names(pheno_dt))) pheno_dt[, title := ""]
  if (!("source_name_ch1" %in% names(pheno_dt))) pheno_dt[, source_name_ch1 := ""]
  pheno_dt[, characteristics_ch1 := collapse_chars(pheno)]
  fwrite(pheno_dt, file = file.path(gdir, "pheno.tsv"), sep = "\t")
  return(pheno_dt)
}

# ===========================================================================
# Main loop
# ===========================================================================
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

  # === RNA-seq path: expression matrix is empty ===
  if (nrow(expr) == 0) {
    if (!detect_rnaseq(pheno)) {
      message("  [ERROR] ", gse, ": expression matrix is empty and not detected as RNA-seq. Skipping.")
      next
    }

    message("  [INFO] ", gse, ": RNA-seq detected (empty series matrix). Attempting supplementary file download...")

    # Save pheno first (always available from series matrix)
    save_pheno(pheno, gdir)

    # Try to fetch count data from supplementary files
    count_result <- tryCatch(
      fetch_rnaseq_counts(gse, gdir, pheno),
      error = function(e) {
        message("  [ERROR] Failed to fetch RNA-seq counts: ", conditionMessage(e))
        NULL
      }
    )

    if (is.null(count_result)) {
      message("  [ERROR] ", gse, ": Could not retrieve RNA-seq count matrix. Skipping.")
      writeLines("rnaseq_failed", file.path(gdir, "data_type.txt"))
      next
    }

    # Write data type marker
    dtype <- if (count_result$is_raw_counts) "rnaseq" else "rnaseq_normalized"
    writeLines(dtype, file.path(gdir, "data_type.txt"))

    # Write expression data
    fwrite(count_result$expr, file = file.path(gdir, "expr.tsv"), sep = "\t")
    message("  Saved RNA-seq data: ", gdir, " (", nrow(count_result$expr),
            " features, type=", dtype, ")")
    next
  }

  # === Microarray path: expression matrix has data ===
  writeLines("microarray", file.path(gdir, "data_type.txt"))

  # Save pheno
  save_pheno(pheno, gdir)

  # Save expression with feature_id
  expr_rn <- rownames(expr)
  expr_dt <- as.data.table(expr)
  if (!is.null(expr_rn) && length(expr_rn) == nrow(expr_dt)) {
    expr_dt <- cbind(data.table(feature_id = expr_rn), expr_dt)
  } else {
    message("  [WARN] No rownames on expression matrix, using row indices as feature_id")
    expr_dt <- cbind(data.table(feature_id = as.character(seq_len(nrow(expr_dt)))), expr_dt)
  }
  fwrite(expr_dt, file = file.path(gdir, "expr.tsv"), sep = "\t")
  message("Saved: ", gdir)
}

message("Done fetch_geo.")
