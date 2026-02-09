#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
import os
import hashlib
import pandas as pd

# ========== 配置 ==========
IN_FILE = "data/poolA_negative_drug_level.csv"

OUT_MASTER  = "data/drug_master.csv"
OUT_ALIAS   = "data/drug_alias_map.csv"
OUT_SUMMARY = "data/negative_drug_summary.csv"
OUT_MANUAL  = "data/manual_alias_review_queue.csv"

# 可选：你人工确认的同药合并表（两列：canonical_from, canonical_to）
OVERRIDE_FILE = "data/manual_alias_overrides.csv"

# canonicalize 时移除的常见词（可以按你习惯增删）
STOP_WORDS = {
    "tablet","tablets","capsule","capsules","injection","injectable","infusion","oral",
    "iv","intravenous","sc","subcutaneous","im","intramuscular","po",
    "qd","bid","tid","qod","qhs",
    "sustained","extended","release","er","sr","xr",
    "solution","suspension","gel","cream","patch","spray","drops","drop",
    "mg","g","mcg","ug","iu","ml",
}

# summary 固定输出 schema（永远有这些列）
SUMMARY_SCHEMA = [
    "drug_id",
    "canonical_name",
    "n_negative_trials",
    "row_count",
    "phases",
    "sponsors",
    "conditions",
    "primary_outcomes",
    "evidence_sources",
    "min_p",
]

# ========== 工具函数 ==========

def normalize_basic(x: str) -> str:
    if pd.isna(x) or not str(x).strip():
        return ""
    s = str(x).lower().strip()
    s = re.sub(r"[\(\)\[\]\{\},;:/\\]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def canonicalize_name(x: str) -> str:
    """
    Aggressive canonicalization:
    - lower
    - remove punctuation
    - remove dosage patterns (e.g., 10 mg, 0.5ml, 200mcg)
    - remove route/form words and common stopwords
    """
    s = normalize_basic(x)
    if not s:
        return ""

    # remove dose like "10 mg", "0.5 ml", "200mcg"
    s = re.sub(r"\b\d+(\.\d+)?\s*(mg|g|mcg|ug|iu|ml)\b", " ", s, flags=re.I)

    # remove standalone numbers (often dose without unit)
    s = re.sub(r"\b\d+(\.\d+)?\b", " ", s)

    toks = [t for t in re.split(r"\s+", s) if t]
    toks = [t for t in toks if t not in STOP_WORDS]

    joined = " ".join(toks)
    joined = joined.replace("α", "alpha").replace("β", "beta")
    joined = re.sub(r"\s+", " ", joined).strip()
    return joined

def stable_drug_id(canonical: str) -> str:
    # stable deterministic ID
    h = hashlib.md5(canonical.encode("utf-8")).hexdigest()[:10].upper()
    return f"D{h}"

def parse_min_pval(s) -> float:
    if pd.isna(s):
        return float("nan")
    txt = str(s)
    vals = []
    for m in re.finditer(r"p\s*=?\s*([0-9]*\.?[0-9]+)", txt, flags=re.I):
        try:
            vals.append(float(m.group(1)))
        except:
            pass
    return min(vals) if vals else float("nan")

def join_unique(series, sep="; ", max_chars=1500) -> str:
    vals = [str(v) for v in series.tolist() if pd.notna(v) and str(v).strip()]
    vals = sorted(set(vals))
    out = sep.join(vals)
    return out[:max_chars]

def try_import_rapidfuzz():
    try:
        from rapidfuzz import fuzz  # type: ignore
        return fuzz
    except Exception:
        return None

def apply_overrides(master: pd.DataFrame, alias: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Apply OVERRIDE_FILE if exists:
    canonical_from -> canonical_to
    Both will be canonicalized using canonicalize_name().
    """
    if not os.path.exists(OVERRIDE_FILE):
        return master, alias

    ov = pd.read_csv(OVERRIDE_FILE)
    if not {"canonical_from", "canonical_to"}.issubset(set(ov.columns)):
        raise ValueError(f"{OVERRIDE_FILE} must have columns: canonical_from, canonical_to")

    # map canonical_name -> drug_id
    canon_to_id = dict(zip(master["canonical_name"], master["drug_id"]))

    repl = {}  # from_id -> to_id
    for _, r in ov.iterrows():
        a = canonicalize_name(r["canonical_from"])
        b = canonicalize_name(r["canonical_to"])
        if not a or not b:
            continue
        if a in canon_to_id and b in canon_to_id:
            repl[canon_to_id[a]] = canon_to_id[b]

    if not repl:
        return master, alias

    # replace IDs in alias
    alias["drug_id"] = alias["drug_id"].apply(lambda x: repl.get(x, x))

    # rebuild master by drug_id (keep first canonical)
    master = (alias.groupby("drug_id", as_index=False)
              .agg(canonical_name=("canonical_name", "first")))

    return master, alias

# ========== 主流程 ==========

def main():
    if not os.path.exists(IN_FILE):
        raise FileNotFoundError(f"Input file not found: {IN_FILE}")

    df = pd.read_csv(IN_FILE)

    # 必须要有药名列
    if "drug_raw" in df.columns:
        raw_col = "drug_raw"
    elif "intervention_name" in df.columns:
        raw_col = "intervention_name"
    else:
        raise ValueError("Input must contain 'drug_raw' or 'intervention_name' column.")

    df["drug_raw"] = df[raw_col].astype(str)

    # drug_normalized：如果已有就用，否则自己做 basic normalize
    if "drug_normalized" in df.columns:
        df["drug_normalized"] = df["drug_normalized"].astype(str).apply(normalize_basic)
    else:
        df["drug_normalized"] = df["drug_raw"].apply(normalize_basic)

    # canonical：优先用 aggressive canonical，否则 fallback 到 drug_normalized
    df["canonical_name"] = df["drug_raw"].apply(canonicalize_name)
    df["canonical_name"] = df["canonical_name"].where(df["canonical_name"].str.len() > 0, df["drug_normalized"])

    # master
    canon_list = sorted(set([c for c in df["canonical_name"].tolist() if isinstance(c, str) and c.strip()]))
    master = pd.DataFrame({"canonical_name": canon_list})
    master["drug_id"] = master["canonical_name"].apply(stable_drug_id)
    master = master[["drug_id", "canonical_name"]]

    # alias
    alias = (df[["drug_raw", "drug_normalized", "canonical_name"]]
             .drop_duplicates()
             .merge(master, on="canonical_name", how="left"))
    alias = alias[["drug_id", "canonical_name", "drug_raw", "drug_normalized"]].drop_duplicates()

    # optional overrides
    master, alias = apply_overrides(master, alias)

    # 下沉 drug_id 回 df
    df = df.merge(master, on="canonical_name", how="left")

    # ====== 确保中间列永远存在（不会因为缺字段导致少列）======
    df["_phase"]   = df["phase"].astype(str) if "phase" in df.columns else (df["phases"].astype(str) if "phases" in df.columns else "")
    df["_sponsor"] = df["leadSponsor"].astype(str) if "leadSponsor" in df.columns else ""
    df["_cond"]    = df["conditions"].astype(str) if "conditions" in df.columns else ""
    df["_outcome"] = df["primary_outcome_title"].astype(str) if "primary_outcome_title" in df.columns else ""
    df["_src"]     = df["evidence_source"].astype(str) if "evidence_source" in df.columns else ""

    if "primary_outcome_pvalues" in df.columns:
        df["_min_p"] = df["primary_outcome_pvalues"].apply(parse_min_pval)
    else:
        df["_min_p"] = float("nan")

    # n_negative_trials：优先 nunique(nctId)，否则用行数
    if "nctId" in df.columns:
        n_trials_series = ("nctId", "nunique")
        row_count_series = ("nctId", "size")
    else:
        n_trials_series = ("canonical_name", "size")
        row_count_series = ("canonical_name", "size")

    summary = (df.groupby(["drug_id", "canonical_name"], as_index=False)
               .agg(
                   n_negative_trials=n_trials_series,
                   row_count=row_count_series,
                   phases=("_phase", lambda x: join_unique(x, sep="; ", max_chars=400)),
                   sponsors=("_sponsor", lambda x: join_unique(x, sep="; ", max_chars=400)),
                   conditions=("_cond", lambda x: join_unique(x, sep="; ", max_chars=1500)),
                   primary_outcomes=("_outcome", lambda x: join_unique(x, sep="; ", max_chars=800)),
                   evidence_sources=("_src", lambda x: join_unique(x, sep="; ", max_chars=200)),
                   min_p=("_min_p", "min"),
               ))

    # ====== schema 固定化：缺列就补空（但我们已经保证基本不会缺）======
    for col in SUMMARY_SCHEMA:
        if col not in summary.columns:
            summary[col] = "" if col not in {"n_negative_trials", "row_count", "min_p"} else pd.NA
    summary = summary[SUMMARY_SCHEMA]

    # ====== 相似名队列（可选）======
    fuzz_mod = try_import_rapidfuzz()
    if fuzz_mod is None:
        manual = pd.DataFrame(columns=["name_a", "name_b", "similarity", "hint"])
    else:
        # 为避免 O(n^2) 爆炸，只对 topK 高频 canonical 做 pair
        topK = 200
        if "nctId" in df.columns:
            top = (df.groupby("canonical_name")["nctId"].nunique()
                   .sort_values(ascending=False).head(topK).index.tolist())
        else:
            top = df["canonical_name"].value_counts().head(topK).index.tolist()

        pairs = []
        for i in range(len(top)):
            for j in range(i + 1, len(top)):
                a, b = top[i], top[j]
                if len(a) < 4 or len(b) < 4:
                    continue
                s1 = fuzz_mod.token_set_ratio(a, b)
                s2 = fuzz_mod.partial_ratio(a, b)
                score = max(s1, s2)
                if score >= 92:
                    pairs.append({
                        "name_a": a,
                        "name_b": b,
                        "similarity": score,
                        "hint": "Consider alias merge via manual_alias_overrides.csv"
                    })

        manual = (pd.DataFrame(pairs).sort_values("similarity", ascending=False)
                  if pairs else pd.DataFrame(columns=["name_a","name_b","similarity","hint"]))

    # ====== 输出 ======
    master.to_csv(OUT_MASTER, index=False, encoding="utf-8-sig")
    alias.to_csv(OUT_ALIAS, index=False, encoding="utf-8-sig")
    summary.to_csv(OUT_SUMMARY, index=False, encoding="utf-8-sig")
    manual.to_csv(OUT_MANUAL, index=False, encoding="utf-8-sig")

    print("DONE Step5 v3:")
    print(" -", OUT_MASTER)
    print(" -", OUT_ALIAS)
    print(" -", OUT_SUMMARY, "(cols:", len(summary.columns), ")")
    print(" -", OUT_MANUAL, "(pairs:", len(manual), ")")
    if os.path.exists(OVERRIDE_FILE):
        print(" - overrides applied from:", OVERRIDE_FILE)
    else:
        print(" - optional overrides file (not found):", OVERRIDE_FILE)

if __name__ == "__main__":
    main()
