import pandas as pd
import requests
import time
import re
from typing import Dict, Any, List

SEED_CSV = "data/seed_nct_list.csv"
OUT_TRIALS = "data/poolA_trials.csv"
OUT_DRUG = "data/poolA_drug_level.csv"
OUT_QUEUE = "data/manual_review_queue.csv"

CTGOV_V2 = "https://clinicaltrials.gov/api/v2/studies/{}"
DELAY = 0.35
RETRIES = 3

def norm(s: str) -> str:
    s = str(s).lower().strip()
    s = re.sub(r"[\(\)\[\]\{\},;:/\\]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def fetch_study(nct: str) -> Dict[str, Any]:
    url = CTGOV_V2.format(nct)
    last = None
    for k in range(RETRIES):
        try:
            r = requests.get(url, timeout=40)
            r.raise_for_status()
            time.sleep(DELAY)
            return r.json()
        except Exception as e:
            last = e
            time.sleep(1.0 + k)
    raise RuntimeError(f"Fetch failed for {nct}: {last}")

def get(d: Dict[str, Any], path: List[str], default=""):
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur if cur is not None else default

def main():
    seed = pd.read_csv(SEED_CSV)
    seed["nctId"] = seed["nctId"].astype(str).str.strip()

    trials_rows = []
    drug_rows = []
    queue_rows = []

    for nct in seed["nctId"].tolist():
        data = fetch_study(nct)

        # protocolSection
        ps = data.get("protocolSection", {}) or {}

        brief = get(ps, ["identificationModule", "briefTitle"], "")
        overall = get(ps, ["statusModule", "overallStatus"], "")
        study_type = get(ps, ["designModule", "studyType"], "")
        phases = get(ps, ["designModule", "phases"], "")
        if not phases:
            phases = get(ps, ["designModule", "phaseList"], "")

        lead = get(ps, ["sponsorCollaboratorsModule", "leadSponsor", "name"], "")
        conds = get(ps, ["conditionsModule", "conditions"], [])
        if isinstance(conds, list):
            conditions = "; ".join([str(x) for x in conds if str(x).strip()])
        else:
            conditions = str(conds)

        # interventions for drug-level
        intervs = get(ps, ["armsInterventionsModule", "interventions"], [])
        if not isinstance(intervs, list):
            intervs = []

        # results flag
        has_results = 1 if ("resultsSection" in data and data.get("resultsSection")) else 0

        pass_interventional = 1 if str(study_type).upper() == "INTERVENTIONAL" else 0
        phase_u = str(phases).upper()
        pass_phase23 = 1 if any(p in phase_u for p in ["PHASE2", "PHASE3", "PHASE4"]) else 0

        ctgov_url = f"https://clinicaltrials.gov/study/{nct}"

        trials_rows.append({
            "nctId": nct,
            "briefTitle": brief,
            "overallStatus": overall,
            "studyType": study_type,
            "phases": phases,
            "leadSponsor": lead,
            "conditions": conditions,
            "ctgov_url": ctgov_url,
            "has_resultsSection": has_results,
            "pass_interventional": pass_interventional,
            "pass_phase23": pass_phase23,
        })

        queue_rows.append({
            "nctId": nct,
            "briefTitle": brief,
            "phases": phases,
            "mvp_outcome_label": "UNCLEAR",
            "mvp_outcome_confidence": "LOW",
        })

        # drug-level rows (only DRUG/BIOLOGICAL/GENETIC by default)
        for iv in intervs:
            name = str(iv.get("name", "")).strip()
            itype = str(iv.get("type", "")).strip()
            if not name:
                continue
            if itype and itype.upper() not in ["DRUG", "BIOLOGICAL", "GENETIC"]:
                continue
            drug_rows.append({
                "nctId": nct,
                "briefTitle": brief,
                "phase": phases,
                "leadSponsor": lead,
                "conditions": conditions,
                "ctgov_url": ctgov_url,
                "drug_raw": name,
                "drug_normalized": norm(name),
                "intervention_type": itype,
            })

    pd.DataFrame(trials_rows).to_csv(OUT_TRIALS, index=False, encoding="utf-8-sig")
    pd.DataFrame(drug_rows).to_csv(OUT_DRUG, index=False, encoding="utf-8-sig")
    pd.DataFrame(queue_rows).to_csv(OUT_QUEUE, index=False, encoding="utf-8-sig")

    print("DONE seed build:")
    print(" -", OUT_TRIALS, "rows=", len(trials_rows))
    print(" -", OUT_DRUG, "rows=", len(drug_rows))
    print(" -", OUT_QUEUE, "rows=", len(queue_rows))

if __name__ == "__main__":
    main()
