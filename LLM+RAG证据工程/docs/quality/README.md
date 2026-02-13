# Quality Review Pack

This folder contains the minimum human-review package required to run the
pipeline as a governed decision workflow instead of a pure automated script.

## Files

- `INDUSTRIAL_REVIEW_SOP.md`
  - End-to-end human review process and release gates.
- `review_log_template.csv`
  - Row-level review log template for reviewer A/B and final verdict.
- `adjudication_template.md`
  - Disagreement resolution record template.
- `release_decision_template.md`
  - Final release/no-release decision template for each run.
- `issue_codebook.md`
  - Standard issue taxonomy and severity definitions.

## Usage

1. Copy `review_log_template.csv` for each run (e.g. `review_log_<run_id>.csv`).
2. Use `INDUSTRIAL_REVIEW_SOP.md` to execute review and adjudication.
3. Produce one `adjudication_<run_id>.md` and one `release_decision_<run_id>.md`.
4. Archive all artifacts together with run manifests (`step6-9_manifest.json`).

