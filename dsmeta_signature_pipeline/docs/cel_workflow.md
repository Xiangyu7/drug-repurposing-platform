# CEL/raw microarray workflow (optional)

Some GEO series provide only raw CEL files (no Series Matrix).
This repo's default path uses **Series Matrix / ExpressionSet**.

If you need CEL processing:
1) Download CEL files (GEO supplementary)
2) Preprocess with R packages:
   - affy / oligo (platform-specific)
   - rma normalization
3) Export expression matrix (log2)
4) Drop into `work/geo/GSEXXXX/expr.tsv` and `pheno.tsv` format

Then you can run the rest of the pipeline starting from DE/meta.

This is intentionally separated because it is platform-dependent and can be heavy.
