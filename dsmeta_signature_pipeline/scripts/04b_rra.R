#!/usr/bin/env Rscript
suppressPackageStartupMessages({
  library(optparse)
  library(data.table)
  library(RobustRankAggreg)
})

opt_list <- list(
  make_option(c("--rank_matrix"), type="character", help="tsv with feature_id as first column, ranks per study"),
  make_option(c("--out"), type="character", help="output tsv")
)
opt <- parse_args(OptionParser(option_list=opt_list))

M <- fread(opt$rank_matrix)
gene <- M[[1]]
M[[1]] <- NULL

ranked_lists <- list()
for (nm in names(M)){
  r <- M[[nm]]
  o <- order(r, decreasing=FALSE, na.last=TRUE)
  ranked_lists[[nm]] <- gene[o]
}

res <- aggregateRanks(ranked_lists)
fwrite(as.data.table(res), file=opt$out, sep="\t")
