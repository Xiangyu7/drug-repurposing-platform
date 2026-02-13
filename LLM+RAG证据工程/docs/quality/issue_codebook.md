# Issue Codebook (v1.0)

## Severity

- `Critical`
  - Could change go/no-go decision or scientific conclusion.
- `Major`
  - Material quality issue, but not immediately decision-flipping.
- `Minor`
  - Cosmetic or low-risk quality issue.

## Core Issue Codes

- `EVID_PMID_MISMATCH`
  - PMID missing/incorrect for claimed evidence.
- `EVID_DRUG_NOT_GROUNDED`
  - Drug claim not grounded in source abstract.
- `EVID_DIRECTION_WRONG`
  - Benefit/harm/neutral label inconsistent with source.
- `EVID_MODEL_WRONG`
  - Human/animal/cell label incorrect.
- `EVID_ENDPOINT_WRONG`
  - Endpoint type inconsistent with source.
- `EVID_OVERCONFIDENCE`
  - Confidence is overstated for weak/ambiguous evidence.
- `SCORE_REPRO_FAIL`
  - Step7 score cannot be reproduced from inputs/config.
- `GATE_REASON_MISMATCH`
  - Gate decision and gate reasons conflict.
- `SHORTLIST_ORDER_ERROR`
  - Step8 ranking/sorting inconsistency.
- `PLAN_NON_ACTIONABLE`
  - Step9 readout/criteria not measurable or actionable.
- `CONTRACT_VIOLATION`
  - Schema/contract mismatch in step outputs.
- `TRACEABILITY_GAP`
  - Missing links from decision to source evidence/run manifest.

## Suggested Severity Mapping

- Usually `Critical`:
  - `EVID_PMID_MISMATCH`
  - `EVID_DIRECTION_WRONG`
  - `SCORE_REPRO_FAIL`
  - `GATE_REASON_MISMATCH`
  - `CONTRACT_VIOLATION`
- Usually `Major`:
  - `EVID_MODEL_WRONG`
  - `EVID_ENDPOINT_WRONG`
  - `PLAN_NON_ACTIONABLE`
  - `TRACEABILITY_GAP`
- Usually `Minor`:
  - Wording/formatting issues without decision impact.

