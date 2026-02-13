# Industrial Human Review SOP (v1.0)

## 1. Objective

Define a repeatable human-review process for Step6-9 outputs so release
decisions are auditable, reproducible, and risk-controlled.

Important:
- This SOP is a necessary condition for industrial readiness.
- It is not the only condition. CI, monitoring, security, and data governance
  are still required.

## 2. Scope

- In scope: `output/step6`, `output/step7`, `output/step8`, `output/step9`.
- Per-run artifacts:
  - `step6_manifest.json`
  - `step7_manifest.json`
  - `step8_manifest.json`
  - `step9_manifest.json`
  - `review_log_<run_id>.csv`
  - `adjudication_<run_id>.md`
  - `release_decision_<run_id>.md`

## 3. Roles

- Reviewer A:
  - Independent review (no access to Reviewer B labels during first pass).
- Reviewer B:
  - Independent review.
- Adjudicator (senior):
  - Resolves disagreements and sets final verdict.
- Release owner:
  - Signs go/no-go for run-level release.

## 4. Sampling Policy

- `GO/MAYBE` candidates: 100% mandatory human review.
- `NO-GO` candidates: >=20% random sample.
- Global sentinel sample: >=5% random sample across all records.
- If any `Critical` defect is found in sample:
  - Expand to 100% review for that defect cluster.

## 5. Review Workflow

1. Run lock:
  - Freeze inputs and manifests.
  - Record `run_id`, git commit, model versions.
2. First-pass independent review:
  - Reviewer A and B fill `review_log_<run_id>.csv` independently.
3. Disagreement extraction:
  - Filter rows where `verdict_a != verdict_b` or severity differs.
4. Adjudication:
  - Senior reviewer resolves each disagreement in
    `adjudication_<run_id>.md`.
5. Defect metrics:
  - Compute `Critical/Major/Minor` counts and rates.
  - Compute inter-rater agreement (`kappa` recommended).
6. Release decision:
  - Fill `release_decision_<run_id>.md`.
  - Release owner signs.
7. Archive:
  - Save review artifacts next to run manifests.

## 6. Hard Gates (Release Blocking)

- `Critical defects == 0`
- `Major defect rate < 2%` in reviewed scope
- Reviewer agreement target: `kappa >= 0.75`
- No unresolved adjudication items
- All output contracts valid (`step6-9`)

If any gate fails:
- Status = `NO-RELEASE`
- Required CAPA (Corrective and Preventive Actions) must be documented.

## 7. Step-Specific Checklist

## 7.1 Step6 (Evidence extraction)

- PMID exists and points to the claimed paper.
- Drug mention is anchored in text (no cross-drug contamination).
- `direction/model/endpoint` consistent with abstract.
- Confidence value is calibrated and not over-claimed.

## 7.2 Step7 (Scoring and gate)

- Input-output consistency with Step6 counts.
- Score components are reproducible from rules/config.
- `gate_decision` matches `gate_reasons` and thresholds.

## 7.3 Step8 (Shortlist and pack)

- Ranking order reproducible from rank key.
- Top-K contains no contract/schema violation.
- Candidate pack links to valid dossier evidence.

## 7.4 Step9 (Validation plan)

- Proposed readouts and stop/go criteria are measurable.
- Risk statements reflect evidence balance (support vs harm).
- Owner/timeline fields are explicit and realistic.

## 8. Defect Severity

- `Critical`: May lead to wrong scientific or portfolio decision.
- `Major`: Material quality issue with moderate decision impact.
- `Minor`: Formatting, wording, or non-decision-impact issue.

See `issue_codebook.md` for standardized issue codes.

## 9. SLA and Cadence

- First-pass review: within 1 business day after run completion.
- Adjudication: within next 1 business day.
- Weekly calibration meeting:
  - Review recurring issue codes.
  - Update examples and reviewer guidance.

## 10. Minimum Tooling Requirement

- Keep reviewer logs in versioned storage.
- Keep manifests and review artifacts immutable after release decision.
- Every release decision must link to exact run artifacts.

