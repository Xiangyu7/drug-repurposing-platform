#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Step6 (v2): PubMed RAG + Evidence Engineering with:
1) Evidence blocks (not single sentences) with required PMID + direction/model/endpoint fields
2) Endpoint-driven topic gating (plaque/PAD/events) rather than one-size-fits-all "atherosclerosis"
3) Two-stage retrieval: broad PubMed -> BM25 pre-rank -> (optional) Ollama embedding rerank
4) Negative evidence extraction & counting (CT.gov + abstract "no difference"/harm language)

Designed to be drop-in upgrade for pipelines using step6_rank.csv + dossier_json used by step7_score_and_gate.py.

Run (example):
  OLLAMA_HOST=http://localhost:11434 OLLAMA_EMBED_MODEL=nomic-embed-text OLLAMA_LLM_MODEL=qwen2.5:7b-instruct \
  python step6_evidence_extraction.py \
    --rank_in step6_rank.csv --neg poolA_negative_drug_level.csv --out step6_v2_out --target_disease atherosclerosis

Outputs:
  - {out}/step6_rank_v2.csv  (updated dossier paths + evidence counts)
  - {out}/dossiers/{drug_id}__{canonical}.json
  - {out}/dossiers/{drug_id}__{canonical}.md
  - {out}/cache/pubmed/... (pmids + xml + parsed docs + embeddings cache)

Notes:
- Requires: requests, pandas, tqdm (and a running Ollama if you want embedding/LLM)
- Network access needed for PubMed E-utilities.
"""

import os, re, json, math, time, hashlib, argparse, sys, logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import pandas as pd
from tqdm import tqdm

# ---------------------------
# Structured logging (replaces bare print)
# ---------------------------
_logger = logging.getLogger("dr.step6")
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
    ))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)

# ---- Modular imports from src/dr ----
# Ensure src/ is on path for modular imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.dr.evidence.ranker import BM25Ranker, HybridRanker, RankingPipeline
from src.dr.evidence.extractor import repair_json as modular_repair_json
from src.dr.evidence.extractor import validate_extraction, coerce_extraction, detect_hallucination
from src.dr.config import Config as _Config

# ---------------------------
# Config / Env
# ---------------------------
NCBI_EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
NCBI_API_KEY = os.getenv("NCBI_API_KEY", "").strip()
NCBI_DELAY = float(os.getenv("NCBI_DELAY", "0.6"))  # polite default

PUBMED_TIMEOUT = float(os.getenv("PUBMED_TIMEOUT", "30"))
PUBMED_EFETCH_CHUNK = int(os.getenv("PUBMED_EFETCH_CHUNK", "20"))  # smaller batches reduce SSL/EOF issues
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "600"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "30"))  # kept for backward compat
DISABLE_EMBED = os.getenv("DISABLE_EMBED", "0") == "1"
DISABLE_LLM = os.getenv("DISABLE_LLM", "0") == "1"

CROSS_DRUG_FILTER = os.getenv("CROSS_DRUG_FILTER", "1") == "1"
PMID_STRICT = os.getenv("PMID_STRICT", "1") == "1"
SUPPORT_COUNT_MODE = os.getenv("SUPPORT_COUNT_MODE", "unique_pmids")  # unique_pmids | sentences

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "4"))
RETRY_SLEEP = float(os.getenv("RETRY_SLEEP", "2"))

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
EMBED_BATCH_SIZE = int(os.getenv("EMBED_BATCH_SIZE", "16"))
MAX_RERANK_DOCS = int(os.getenv("MAX_RERANK_DOCS", "60"))
OLLAMA_LLM_MODEL = os.getenv("OLLAMA_LLM_MODEL", "qwen2.5:7b-instruct")

OLLAMA_CHAT_FORMAT = os.getenv("OLLAMA_CHAT_FORMAT", "json")  # json or schema
USE_CHAT_SCHEMA = os.getenv("USE_CHAT_SCHEMA", "1") == "1"


FORCE_REBUILD = os.getenv("FORCE_REBUILD", "0") == "1"
REFRESH_EMPTY_CACHE = os.getenv("REFRESH_EMPTY_CACHE", "1") == "1"  # default to refresh empties in v2

# ---------------------------
# Utilities
# ---------------------------
def log(msg: str, level: str = "info") -> None:
    """Structured log via Python logging module."""
    getattr(_logger, level, _logger.info)(msg)

def safe_filename(s: str, max_len: int = 80) -> str:
    s = re.sub(r"[^a-zA-Z0-9\-_]+", "_", str(s).strip().lower())
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:max_len] or "drug"

def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

def request_with_retries(method: str, url: str, **kwargs) -> requests.Response:
    """
    Robust HTTP helper with retries.
    IMPORTANT: do not mutate the caller kwargs across retries (so timeout stays consistent).
    """
    last = None
    timeout_default = kwargs.get("timeout", REQUEST_TIMEOUT)
    trust_env_default = kwargs.get("trust_env", True)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # copy kwargs per attempt so we don't lose timeout/trust_env
            kw = dict(kwargs)
            timeout = kw.pop("timeout", timeout_default)
            trust_env = kw.pop("trust_env", trust_env_default)

            if trust_env is False:
                sess = requests.Session()
                sess.trust_env = False
                r = sess.request(method, url, timeout=timeout, **kw)
            else:
                r = requests.request(method, url, timeout=timeout, **kw)

            r.raise_for_status()
            return r
        except Exception as e:
            last = e
            log(f"[HTTP] {method} {url} attempt {attempt}/{MAX_RETRIES} failed: {type(e).__name__}: {e}", "warning")
            time.sleep(RETRY_SLEEP * attempt)

    raise RuntimeError(f"HTTP failed after {MAX_RETRIES} retries: {last}")

def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))

def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

def write_text(path: Path, txt: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(txt, encoding="utf-8")
    tmp.replace(path)

def is_empty(path: Path) -> bool:
    return (not path.exists()) or path.stat().st_size == 0


PMID_DIGITS_RE = re.compile(r"\b(\d{6,9})\b")

def normalize_pmid(v: Any) -> str:
    """Extract a clean numeric PMID (6-9 digits). Return '' if none."""
    if v is None:
        return ""
    s = str(v).strip()
    if not s:
        return ""
    m = PMID_DIGITS_RE.search(s)
    return m.group(1) if m else ""

def build_other_drug_markers(all_drugs_lower: List[str], current_lower: str) -> List[str]:
    """Markers used to detect cross-drug leakage in extracted claims."""
    markers = []
    for d in all_drugs_lower:
        dd = (d or "").strip().lower()
        if not dd or dd == current_lower:
            continue
        # avoid very short strings that cause false positives
        if len(dd) < 4:
            continue
        markers.append(dd)
    # longer first reduces accidental matches
    markers.sort(key=len, reverse=True)
    return markers

def contains_other_drug(text: str, other_markers: List[str]) -> bool:
    t = (text or "").lower()
    for m in other_markers:
        if m in t:
            return True
    return False


# ---------------------------
# Endpoint classification + topic gate
# ---------------------------
def classify_endpoint(primary_outcome_title: str, conditions: str) -> str:
    s = f"{primary_outcome_title} {conditions}".lower()
    if any(k in s for k in ["plaque", "atheroma", "cta", "ivus", "carotid", "intima-media", "non-calcified", "noncalcified"]):
        return "PLAQUE_IMAGING"
    if any(k in s for k in ["six-minute walk", "6-minute walk", "6mw", "claudication", "walking distance", "treadmill", "limb ischemia", "perfusion", "pad", "peripheral artery"]):
        return "PAD_FUNCTION"
    if any(k in s for k in ["mace", "major adverse", "myocardial infarction", "stroke", "cv death", "revascularization", "acute coronary syndrome"]):
        return "CV_EVENTS"
    return "OTHER"

TOPIC_KEYWORDS = {
    "PLAQUE_IMAGING": [
        "atherosclerosis","plaque","atheroma","noncalcified","non-calcified","cta","ivus","carotid","coronary","intima-media","foam cell","oxldl","ldlr","apoe"
    ],
    "PAD_FUNCTION": [
        "peripheral artery disease","pad","claudication","limb ischemia","ischemic limb","walking","six-minute walk","6-minute walk","treadmill","ankle brachial","perfusion","collateral"
    ],
    "CV_EVENTS": [
        "myocardial infarction","mi","acute coronary","acs","mace","stroke","cv death","revascularization","coronary heart disease","chd"
    ],
    "OTHER": [
        "atherosclerosis","cardiovascular","vascular","inflammation","endothelial","lipid","cholesterol"
    ]
}

ENDPOINT_QUERY = {
    "PLAQUE_IMAGING": '(atherosclerosis OR plaque OR atheroma OR "noncalcified plaque" OR "coronary plaque" OR "computed tomography angiography" OR CTA OR IVUS OR "carotid intima-media")',
    "PAD_FUNCTION": '("peripheral artery disease" OR PAD OR claudication OR "six-minute walk" OR treadmill OR "limb ischemia" OR perfusion)',
    "CV_EVENTS": '("myocardial infarction" OR MI OR "acute coronary syndrome" OR ACS OR MACE OR stroke OR revascularization)',
    "OTHER": '(atherosclerosis OR cardiovascular OR vascular OR endothelial OR inflammation)'
}

def topic_match_ratio(text: str, endpoint_type: str) -> float:
    kws = TOPIC_KEYWORDS.get(endpoint_type, TOPIC_KEYWORDS["OTHER"])
    t = (text or "").lower()
    if not kws: return 0.0
    hit = sum(1 for k in kws if k.lower() in t)
    return hit / float(len(kws))

def pubmed_esearch(term: str, retmax: int = 200) -> List[str]:
    params = {"db":"pubmed","term":term,"retmode":"json","retmax":str(retmax),"sort":"relevance"}
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY
    url = f"{NCBI_EUTILS}/esearch.fcgi"
    r = request_with_retries("GET", url, params=params, timeout=PUBMED_TIMEOUT)
    data = r.json()
    time.sleep(NCBI_DELAY)
    return (data.get("esearchresult", {}) or {}).get("idlist", []) or []

def pubmed_efetch_xml(pmids: List[str]) -> str:
    """Fetch PubMed XML in small batches to reduce SSL/EOF failures."""
    if not pmids:
        return ""
    out = []
    step = max(1, int(PUBMED_EFETCH_CHUNK))
    for i in range(0, len(pmids), step):
        batch = pmids[i:i+step]
        params = {"db": "pubmed", "id": ",".join(batch), "retmode": "xml"}
        if NCBI_API_KEY:
            params["api_key"] = NCBI_API_KEY
        url = f"{NCBI_EUTILS}/efetch.fcgi"
        r = request_with_retries("GET", url, params=params, timeout=PUBMED_TIMEOUT)
        time.sleep(NCBI_DELAY)
        out.append(r.text)
    return "\n".join(out)

def parse_pubmed_xml(xml_text: str, max_articles: int = 200) -> List[Dict[str, Any]]:
    """Parse PubMed EFetch XML into list of article dicts.

    Handles concatenated XML from multiple batches by extracting
    individual <PubmedArticle> blocks via regex.

    Returns:
        List of {"pmid": str, "title": str, "abstract": str, "year": str}
    """
    import xml.etree.ElementTree as ET

    if not xml_text or not xml_text.strip():
        return []

    docs = []
    # Extract each <PubmedArticle>...</PubmedArticle> block
    blocks = re.findall(r"<PubmedArticle>.*?</PubmedArticle>", xml_text, flags=re.S)
    for block in blocks[:max_articles]:
        try:
            article = ET.fromstring(block)
        except ET.ParseError as e:
            log(f"[XML] Failed to parse PubmedArticle block: {e}", "warning")
            continue

        medline = article.find(".//MedlineCitation")
        if medline is None:
            continue

        pmid_elem = medline.find(".//PMID")
        pmid = pmid_elem.text.strip() if pmid_elem is not None and pmid_elem.text else ""

        title_elem = medline.find(".//ArticleTitle")
        title = title_elem.text if title_elem is not None and title_elem.text else ""

        # Handle multi-part abstracts
        abstract_parts = []
        for at in medline.findall(".//AbstractText"):
            if at.text:
                abstract_parts.append(at.text.strip())
        abstract = " ".join(abstract_parts)

        year_elem = medline.find(".//PubDate/Year")
        if year_elem is None:
            year_elem = medline.find(".//PubDate/MedlineDate")
        year = ""
        if year_elem is not None and year_elem.text:
            m = re.search(r"(\d{4})", year_elem.text)
            year = m.group(1) if m else year_elem.text[:4]

        if pmid:
            docs.append({"pmid": pmid, "title": title, "abstract": abstract, "year": year})

    return docs


# ---------------------------
# BM25 (no external deps)
# ---------------------------
def tokenize(text: str) -> List[str]:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9\s\-]+", " ", text)
    toks = [t for t in text.split() if t and len(t) > 2]
    return toks

_bm25_ranker = BM25Ranker(k1=1.5, b=0.75)

def bm25_rank(query: str, docs: List[Dict[str, Any]], k1: float = 1.5, b: float = 0.75, topk: int = 80) -> List[Tuple[float, Dict[str, Any]]]:
    """Delegate to modular BM25Ranker from src/dr/evidence/ranker.py."""
    return _bm25_ranker.rank(query, docs, topk=topk)

# ---------------------------
# Ollama embedding rerank (optional)
# ---------------------------
def ollama_embed(texts: List[str], model: str) -> Optional[List[List[float]]]:
    if DISABLE_EMBED:
        return None

    if not texts:
        return []
    url = f"{OLLAMA_HOST}/api/embed"
    try:
        r = request_with_retries("POST", url, json={"model": model, "input": texts}, timeout=OLLAMA_TIMEOUT, trust_env=False)
        data = r.json()
        embs = data.get("embeddings")
        if isinstance(embs, list) and embs and isinstance(embs[0], list):
            return embs
        # fallback older endpoint
    except Exception as e:
        log(f"[EMBED] /api/embed failed ({type(e).__name__}: {e}), trying legacy /api/embeddings", "warning")
    try:
        url2 = f"{OLLAMA_HOST}/api/embeddings"
        out = []
        for t in texts:
            r = request_with_retries("POST", url2, json={"model": model, "prompt": t}, timeout=OLLAMA_TIMEOUT, trust_env=False)
            data = r.json()
            e = data.get("embedding")
            if not isinstance(e, list):
                return None
            out.append(e)
        return out
    except Exception as e:
        log(f"[EMBED] Legacy endpoint also failed ({type(e).__name__}: {e}), embedding disabled", "warning")
        return None

def ollama_embed_batched(texts: List[str], model: str) -> Optional[List[List[float]]]:
    """Batch embeddings to avoid timeouts with very large payloads."""
    if not texts:
        return []
    out: List[List[float]] = []
    bs = max(1, int(EMBED_BATCH_SIZE))
    for i in range(0, len(texts), bs):
        chunk = texts[i:i+bs]
        emb = ollama_embed(chunk, model)
        if not emb:
            return None
        out.extend(emb)
    return out

def cosine(a: List[float], b: List[float]) -> float:

    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x*y for x,y in zip(a,b))
    na = math.sqrt(sum(x*x for x in a)) + 1e-12
    nb = math.sqrt(sum(x*x for x in b)) + 1e-12
    return dot / (na*nb)

def rerank_with_embeddings(query: str, ranked: List[Tuple[float, Dict[str, Any]]], topk: int = 25) -> List[Dict[str, Any]]:
    """Optional embedding rerank on top of BM25.

    MAX_RERANK_DOCS controls how many BM25 docs are sent to the embedder.
    EMBED_BATCH_SIZE controls embed batching to reduce HTTP timeouts.
    """
    if DISABLE_EMBED:
        return [d for _, d in ranked[:topk]]

    docs = [d for _, d in ranked]
    if not docs:
        return []

    docs = docs[:max(1, int(MAX_RERANK_DOCS))]
    qemb = ollama_embed([query], OLLAMA_EMBED_MODEL)
    if not qemb:
        return [d for _, d in ranked[:topk]]
    qemb = qemb[0]

    doc_texts = [(d.get("title","") + "\n" + d.get("abstract","")).strip()[:3000] for d in docs]
    demb = ollama_embed_batched(doc_texts, OLLAMA_EMBED_MODEL)
    if not demb:
        return [d for _, d in ranked[:topk]]

    scored = []
    for d, e in zip(docs, demb):
        scored.append((cosine(qemb, e), d))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [d for _, d in scored[:topk]]

# ---------------------------
# Evidence extraction: rule + LLM (JSON)
# ---------------------------
DIRECTION_WORDS = {
    "benefit": ["reduced","decreased","attenuated","improved","lowered","regressed","stabilized","inhibited","prevented","associated with improvement","significantly reduced"],
    "harm": ["increased","worsened","aggravated","promoted","accelerated","associated with higher","significantly increased"],
    "neutral": ["no difference","not significantly","did not improve","failed to","not associated","nonsignificant","no significant"]
}

MODEL_HINTS = {
    "human": ["patients","randomized","placebo","trial","phase","double-blind","cohort","participants"],
    "animal": ["mice","mouse","rat","porcine","pig","apoe","ldlr","rabbit"],
    "cell": ["cells","macrophage","endothelial","in vitro","cell line","thp-1","huh7","hek"]
}


# JSON schema used to hard-constrain model output (Ollama /api/chat supports "format")
EVIDENCE_JSON_SCHEMA = {
  "type": "array",
  "maxItems": 2,
  "items": {
    "type": "object",
    "required": ["pmid","supports","direction","model","endpoint","claim","confidence"],
    "properties": {
      "pmid": {"type":"string"},
      "supports": {"type":"boolean"},
      "direction": {"type":"string", "enum":["benefit","harm","neutral","unknown"]},
      "model": {"type":"string", "enum":["human","animal","cell","unknown"]},
      "endpoint": {"type":"string"},
      "claim": {"type":"string"},
      "confidence": {"type":"number", "minimum":0, "maximum":1}
    },
    "additionalProperties": True
  }
}
def guess_model(text: str) -> str:
    t = (text or "").lower()
    scores = {k:0 for k in MODEL_HINTS}
    for k, hints in MODEL_HINTS.items():
        for h in hints:
            if h in t:
                scores[k] += 1
    best = max(scores.items(), key=lambda x:x[1])[0]
    return best if scores[best] > 0 else "unknown"

def guess_direction(text: str) -> str:
    t = (text or "").lower()
    # neutral first (so "not significantly reduced" -> neutral)
    for w in DIRECTION_WORDS["neutral"]:
        if w in t:
            return "neutral"
    for w in DIRECTION_WORDS["harm"]:
        if w in t:
            return "harm"
    for w in DIRECTION_WORDS["benefit"]:
        if w in t:
            return "benefit"
    return "unknown"

def split_sentences(text: str) -> List[str]:
    t = (text or "").strip()
    if not t:
        return []
    # basic sentence split
    parts = re.split(r"(?<=[\.\!\?])\s+", t)
    out = []
    for p in parts:
        p = re.sub(r"\s+", " ", p).strip()
        if len(p) >= 40:
            out.append(p)
    return out

def pick_evidence_fragments(title: str, abstract: str, endpoint_type: str, max_frags: int = 3) -> List[str]:
    sents = split_sentences(abstract)
    if not sents:
        return []
    # score each sentence by endpoint keyword hits + direction words
    kws = TOPIC_KEYWORDS.get(endpoint_type, TOPIC_KEYWORDS["OTHER"])
    def sent_score(s: str) -> float:
        t = s.lower()
        k_hit = sum(1 for k in kws if k in t)
        dir_bonus = 1.0 if guess_direction(s) in {"benefit","harm","neutral"} else 0.0
        return k_hit + dir_bonus

    scored = [(sent_score(s), i, s) for i, s in enumerate(sents)]
    scored.sort(key=lambda x: x[0], reverse=True)

    frags = []
    used = set()
    for sc, idx, s in scored:
        if sc <= 0:
            continue
        if idx in used:
            continue
        # take a small window (idx-1..idx+1) to keep context
        win = [sents[j] for j in range(max(0, idx-1), min(len(sents), idx+2))]
        for j in range(max(0, idx-1), min(len(sents), idx+2)):
            used.add(j)
        frag = (title.strip() + "\n" + " ".join(win)).strip()
        frags.append(frag[:1600])
        if len(frags) >= max_frags:
            break

    if not frags:
        # fallback: first 2 sentences
        frag = (title.strip() + "\n" + " ".join(sents[:2])).strip()
        frags = [frag[:1600]]
    return frags

def repair_json(text: str) -> Optional[str]:
    """Delegate to modular repair_json from src/dr/evidence/extractor.py.

    The modular version handles: markdown code blocks, bracket extraction,
    regex fallback, trailing comma cleanup, and single-quote repair.
    """
    return modular_repair_json(text)

def ollama_chat_json(system: str, user: str, temperature: float = 0.2) -> Optional[Any]:
    if DISABLE_LLM:
        return None

    url = f"{OLLAMA_HOST}/api/chat"
    payload = {
        "model": OLLAMA_LLM_MODEL,
        "messages": [
            {"role":"system","content":system},
            {"role":"user","content":user},
        ],
        "options": {"temperature": temperature},
        "stream": False
    }
    # Hard-constrain output to JSON to prevent "Extra data" errors.
    # If your Ollama version doesn't support schema, set USE_CHAT_SCHEMA=0.
    if USE_CHAT_SCHEMA:
        payload["format"] = EVIDENCE_JSON_SCHEMA
    else:
        payload["format"] = OLLAMA_CHAT_FORMAT

    try:
        r = request_with_retries("POST", url, json=payload, timeout=OLLAMA_TIMEOUT, trust_env=False)
        data = r.json()
        content = (((data.get("message") or {}).get("content")) or "").strip()
        if not content:
            return None
        rep = repair_json(content)
        if rep:
            try:
                return json.loads(rep)
            except json.JSONDecodeError as je:
                log(f"[LLM] JSON parse failed after repair: {je} | raw={content[:200]}", "warning")
        try:
            return json.loads(content)
        except json.JSONDecodeError as je:
            log(f"[LLM] JSON parse failed on raw content: {je} | raw={content[:200]}", "warning")
            return None
    except Exception as e:
        log(f"[LLM] Chat call failed: {type(e).__name__}: {e}", "error")
        return None

def extract_evidence_with_llm(drug: str, target_disease: str, endpoint_type: str, pmid: str, fragment: str) -> List[Dict[str, Any]]:
    system = (
        "You extract citable evidence items from biomedical abstracts. "
        "Return STRICT JSON ONLY (no markdown). Output must be a JSON array of 0-2 objects. "
        "Each object must have keys: pmid, supports (true/false), direction (benefit|harm|neutral|unknown), "
        "model (human|animal|cell|unknown), endpoint (short phrase), claim (<=25 words), confidence (0-1)."
    )
    user = (
        f"DRUG={drug}\nTARGET={target_disease}\nENDPOINT_TYPE={endpoint_type}\nPMID={pmid}\n\n"
        f"TEXT:\n{fragment}\n\n"
        "Decide if the text supports repurposing for the TARGET (or its clinical spectrum if ENDPOINT_TYPE indicates it). "
        "Prefer extracting Results/Conclusions. If no actionable evidence, return []."
    )

    # retries with lower temperature
    for temp in (0.2, 0.1, 0.0):
        out = ollama_chat_json(system, user, temperature=temp)
        # Ollama 'format' sometimes returns wrapped objects like {"array": [...]} even when asked for an array.
        if isinstance(out, dict):
            if isinstance(out.get('array'), list):
                out = out.get('array')
            elif isinstance(out.get('items'), list):
                out = out.get('items')
            elif isinstance(out.get('evidence'), list):
                out = out.get('evidence')
        if isinstance(out, list):
            items = []
            for o in out[:2]:
                if not isinstance(o, dict):
                    continue
                o['pmid'] = str(o.get('pmid') or pmid).strip()
                if not o['pmid']:
                    o['pmid'] = pmid
                # --- modular hallucination detection ---
                warnings = detect_hallucination(o, pmid, fragment, drug)
                if warnings:
                    o['hallucination_warnings'] = warnings
                    log(f"[HALLUCINATION] drug={drug} PMID={pmid}: {warnings}", "warning")
                items.append(o)
            return items
    return []

def extract_evidence_rule_based(pmid: str, title: str, abstract: str, endpoint_type: str) -> List[Dict[str, Any]]:
    frags = pick_evidence_fragments(title, abstract, endpoint_type, max_frags=2)
    out = []
    for frag in frags:
        model = guess_model(frag)
        direction = guess_direction(frag)
        # supports: benefit -> True, harm/neutral -> False
        supports = True if direction == "benefit" else False
        endpoint = endpoint_type
        claim = frag.split("\n",1)[-1]
        claim = re.sub(r"\s+", " ", claim).strip()
        claim = claim[:160]  # short claim
        out.append({
            "pmid": pmid,
            "supports": supports,
            "direction": direction if direction != "unknown" else "unknown",
            "model": model,
            "endpoint": endpoint,
            "claim": claim,
            "confidence": 0.35 if direction != "unknown" else 0.2
        })
    # deduplicate by claim
    uniq = []
    seen = set()
    for e in out:
        k = (e.get("pmid",""), e.get("claim",""))
        if k in seen: 
            continue
        seen.add(k)
        uniq.append(e)
    return uniq[:2]

# ---------------------------
# CT.gov negative evidence loading
# ---------------------------
def load_negative_trials(neg_path: Optional[str], canonical_name: str) -> Tuple[str, List[Dict[str, Any]], str]:
    """Return (endpoint_type, trials, text_block_for_md).

    Robust matching: many CT.gov exports store intervention names differently (brackets, salts, casing).
    We therefore match by a set of normalized tokens rather than the full canonical string.
    """
    if not neg_path or not os.path.exists(neg_path):
        return "OTHER", [], ""
    neg = pd.read_csv(neg_path)

    # choose best name column
    name_col = None
    for c in ("drug_raw", "drug_name", "intervention", "drug_normalized"):
        if c in neg.columns:
            name_col = c
            break
    if name_col is None:
        return "OTHER", [], ""

    def _norm(s: str) -> str:
        s = (s or "").lower()
        s = re.sub(r"\(.*?\)|\[.*?\]|\{.*?\}", " ", s)  # drop bracketed content
        s = s.replace("_", " ").replace("-", " ").replace("/", " ")
        s = re.sub(r"[^a-z0-9\s]+", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    cn = _norm(str(canonical_name))
    # token-based keys (avoid very short/common tokens)
    STOP = {"human","acid","salt","hydrate","monohydrate","solution","injection","tablet","capsule","extended","release"}
    toks = [t for t in cn.split() if len(t) >= 5 and t not in STOP]
    keys = [cn] + toks[:6]

    col = neg[name_col].astype(str).map(_norm)
    mask = False
    for k in keys:
        if not k:
            continue
        mask = mask | col.str.contains(re.escape(k), na=False)
    cand = neg[mask].copy()

    trials = []
    endpoint = "OTHER"
    if len(cand):
        first = cand.iloc[0]
        endpoint = classify_endpoint(str(first.get("primary_outcome_title","")), str(first.get("conditions","")))
        for _, t in cand.head(10).iterrows():
            trials.append({
                "nctId": str(t.get("nctId","")),
                "conditions": str(t.get("conditions","")),
                "phase": str(t.get("phase","")),
                "primary_outcome_title": str(t.get("primary_outcome_title","")),
                "primary_outcome_pvalues": str(t.get("primary_outcome_pvalues","")),
                "url": str(t.get("url","") or t.get("link","") or "")
            })

    lines = []
    for tr in trials:
        lines.append(f"- **{tr['nctId']}** | {tr['conditions']} | {tr['phase']} | primary: {tr['primary_outcome_title']} | {tr['primary_outcome_pvalues']}")
    return endpoint, trials, "\n".join(lines)

def negative_evidence_from_trials(trials: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for tr in trials[:6]:
        # treat as neutral/harm unless p suggests benefit
        pv = (tr.get("primary_outcome_pvalues") or "").lower()
        direction = "neutral"
        supports = False
        # if p<0.05 mentioned, still could be benefit or harm; we keep neutral and let human interpret
        out.append({
            "pmid": "",
            "supports": supports,
            "direction": direction,
            "model": "human",
            "endpoint": "clinical trial primary outcome",
            "claim": f"{tr.get('nctId','')} primary outcome: {tr.get('primary_outcome_title','')} (p-values: {tr.get('primary_outcome_pvalues','')})",
            "confidence": 0.55
        })
    return out

# ---------------------------
# Main per-candidate
# ---------------------------
def build_query(drug: str, target_disease: str, endpoint_type: str) -> str:
    endpoint_clause = ENDPOINT_QUERY.get(endpoint_type, ENDPOINT_QUERY["OTHER"])
    # keep it broad but on-topic: drug AND (endpoint terms)
    # include target disease in OTHER only; for PAD/CV_EVENTS endpoint clause already includes spectrum
    if endpoint_type == "OTHER":
        return f'("{drug}") AND ({endpoint_clause}) AND ("{target_disease}")'
    return f'("{drug}") AND ({endpoint_clause})'

def process_one(drug_id: str, canonical_name: str, target_disease: str, endpoint_type_hint: str,
                neg_path: Optional[str], out_dir: Path, cache_dir: Path, all_drug_names: List[str]) -> Tuple[Path, Path, Dict[str, Any]]:
    # cache layout
    base = cache_dir / safe_filename(drug_id) / safe_filename(canonical_name)
    base.mkdir(parents=True, exist_ok=True)
    pmids_path = base / "pmids.json"
    xml_path = base / "pubmed.xml"
    docs_path = base / "docs.json"
    reranked_path = base / "reranked_pmids.json"

    endpoint_type, trials, trials_md = load_negative_trials(neg_path, canonical_name)
    if endpoint_type_hint and endpoint_type_hint != "nan":
        endpoint_type = str(endpoint_type_hint)

    query = build_query(canonical_name, target_disease, endpoint_type)

    # Markers for cross-drug leakage filtering (built once per drug)
    other_markers: List[str] = []
    if CROSS_DRUG_FILTER:
        def _norm_name(n: str) -> str:
            s = (n or '').lower().strip()
            s = s.replace('_',' ').replace('-',' ').replace('/',' ')
            s = re.sub(r"\s+", " ", s)
            return s
        this = _norm_name(canonical_name)
        markers = set()
        for n in (all_drug_names or []):
            nn = _norm_name(str(n))
            if not nn or nn == this:
                continue
            # full name marker
            if len(nn) >= 4:
                markers.add(nn)
            # token markers (avoid very short/common tokens)
            for tok in re.split(r"[^a-z0-9]+", nn):
                if len(tok) >= 6:
                    markers.add(tok)
        other_markers = sorted(markers, key=len, reverse=True)


    # 1) retrieve pmids
    if FORCE_REBUILD or is_empty(pmids_path) or (REFRESH_EMPTY_CACHE and is_empty(pmids_path)):
        pmids = pubmed_esearch(query, retmax=200)
        write_json(pmids_path, {"query": query, "pmids": pmids})
    else:
        pmids = (read_json(pmids_path) or {}).get("pmids", [])

    # 2) fetch xml -> parse docs
    if FORCE_REBUILD or is_empty(docs_path) or (REFRESH_EMPTY_CACHE and is_empty(docs_path)):
        # fetch in batches to avoid URL length issues
        all_docs = []
        for i in range(0, min(len(pmids), 200), 50):
            batch = pmids[i:i+50]
            xml = pubmed_efetch_xml(batch)
            all_docs.extend(parse_pubmed_xml(xml, max_articles=80))
            # keep last xml for debugging
            if i == 0:
                write_text(xml_path, xml)
        write_json(docs_path, {"query": query, "endpoint_type": endpoint_type, "docs": all_docs})
    else:
        all_docs = (read_json(docs_path) or {}).get("docs", [])

    # 3) stage-1 rank with BM25
    bm25 = bm25_rank(query, all_docs, topk=80)

    # 4) stage-2 rerank with embeddings (optional)
    reranked_docs = rerank_with_embeddings(query, bm25, topk=30)
    top_docs = reranked_docs[:10]  # for md display

    write_json(reranked_path, {"query": query, "top_pmids": [d["pmid"] for d in reranked_docs]})

    # 5) evidence extraction per top docs (LLM + fallback rule)
    supporting: List[Dict[str, Any]] = []
    harm_or_neutral: List[Dict[str, Any]] = []
    top_sentences = []  # for compatibility with step7 rag avg score

    # collect early QC drops during extraction (before final QC section)
    pre_qc_reasons: List[str] = []
    pre_removed = 0
    pre_removed_cross_drug = 0
    llm_items_total = 0
    for d in reranked_docs[:18]:
        pmid = d.get("pmid","")
        title = d.get("title","")
        abstract = d.get("abstract","") or ""

        # pick fragments
        frags = pick_evidence_fragments(title, abstract, endpoint_type, max_frags=2)

        extracted: List[Dict[str, Any]] = []
        # try LLM on the most on-topic fragments first
        for frag in frags:
            items = extract_evidence_with_llm(canonical_name, target_disease, endpoint_type, pmid, frag)
            if items:
                for it in items:
                    it["source"] = "llm"
                llm_items_total += len(items)
                extracted.extend(items)

        if not extracted:
            extracted = extract_evidence_rule_based(pmid, title, abstract, endpoint_type)
            for it in extracted:
                it["source"] = "rule"

        # attach topic score + rag sentence score, then classify to supporting vs harm/neutral
        for ev in extracted:
            raw_pmid = ev.get('pmid','')
            clean_pmid = normalize_pmid(raw_pmid) if PMID_STRICT else str(raw_pmid or '').strip()
            if clean_pmid != str(raw_pmid or '').strip():
                ev['pmid_raw'] = str(raw_pmid or '').strip()
            ev['pmid'] = clean_pmid or str(pmid).strip()

            if CROSS_DRUG_FILTER and other_markers:
                claim_txt = str(ev.get('claim','') or '')
                if contains_other_drug(claim_txt, other_markers):
                    pre_qc_reasons.append('cross_drug_leakage')
                    pre_removed += 1
                    pre_removed_cross_drug += 1
                    continue

            ev_text = f"{ev.get('claim','')} {ev.get('endpoint','')}"
            tmr = topic_match_ratio(ev_text, endpoint_type)
            ev["topic_match_ratio"] = round(tmr, 4)

            score = tmr + (0.15 if ev.get("direction") in {"benefit","harm","neutral"} else 0.0)
            top_sentences.append({"pmid": pmid, "text": ev.get("claim","")[:240], "score": round(float(score), 4)})

            direction = ev.get("direction","unknown")
            supports = bool(ev.get("supports", False))
            if supports and direction == "benefit":
                supporting.append(ev)
            else:
                harm_or_neutral.append(ev)

    # add CT.gov as negative evidence (counts)
    trial_neg = negative_evidence_from_trials(trials)
    harm_or_neutral.extend(trial_neg)

    # dedupe by (pmid, claim)
    def dedupe(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen = set()
        out = []
        for it in items:
            k = (str(it.get("pmid","")).strip(), str(it.get("claim","")).strip())
            if k in seen:
                continue
            seen.add(k)
            out.append(it)
        return out

    supporting = dedupe(supporting)
    harm_or_neutral = dedupe(harm_or_neutral)

    # final PMID normalization (digits-only) + drop any evidence that still mentions other drugs
    def _clean_list(items: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int, int]:
        out: List[Dict[str, Any]] = []
        removed_local = 0
        removed_cross_local = 0
        for ev in items:
            raw = ev.get('pmid','')
            ev['pmid'] = normalize_pmid(raw) if PMID_STRICT else str(raw or '').strip()
            if CROSS_DRUG_FILTER and other_markers:
                if contains_other_drug(str(ev.get('claim','') or ''), other_markers):
                    removed_local += 1
                    removed_cross_local += 1
                    continue
            out.append(ev)
        return out, removed_local, removed_cross_local
    supporting, _r1, _c1 = _clean_list(supporting)
    harm_or_neutral, _r2, _c2 = _clean_list(harm_or_neutral)
    if (_r1 + _r2) > 0:
        pre_qc_reasons.append('cross_drug_leakage')
        pre_removed += int(_r1 + _r2)
        pre_removed_cross_drug += int(_c1 + _c2)

    # 6) QC / topic mismatch decision (endpoint-driven)
    # use evidence-block text (not raw abstract) for mismatch decision
    ev_text_all = " ".join([str(e.get("claim","")) for e in supporting[:8]]) + " " + " ".join([d.get("abstract","")[:500] for d in top_docs[:2]])
    tmr_all = topic_match_ratio(ev_text_all, endpoint_type)
    mismatch = tmr_all < _Config.gating.TOPIC_MISMATCH_THRESHOLD and endpoint_type != "OTHER"
    qc_reasons = list(pre_qc_reasons)
    removed = int(pre_removed)
    removed_cross_drug = int(pre_removed_cross_drug)

    # light QC: remove ultra-offtopic supporting evidence (topic_match_ratio == 0.0) but keep as neutral list
    kept_supporting = []
    for ev in supporting:
        if float(ev.get("topic_match_ratio", 0.0)) == 0.0 and endpoint_type != "OTHER":
            removed += 1
            qc_reasons.append("removed_offtopic_supporting")
            harm_or_neutral.append({**ev, "supports": False, "direction": "neutral", "confidence": min(0.5, float(ev.get("confidence",0.3)))})
        else:
            kept_supporting.append(ev)
    supporting = kept_supporting

    # confidence mode based on UNIQUE supporting PMIDs (more robust than sentence count)
    support_pmids = [str(e.get('pmid','')).strip() for e in supporting if str(e.get('pmid','')).strip()]
    unique_support_pmids = sorted(set(support_pmids))
    conf = "LOW"
    if len(unique_support_pmids) >= _Config.gating.HIGH_CONFIDENCE_MIN_PMIDS:
        conf = "HIGH"
    elif len(unique_support_pmids) >= _Config.gating.MED_CONFIDENCE_MIN_PMIDS:
        conf = "MED"
    # if topic mismatch, cap at MED and add reason
    if mismatch and conf == "HIGH":
        conf = "MED"
    if mismatch:
        qc_reasons.append("topic_mismatch_flag")

    # 7) Build dossier JSON
    dossier = {
        "drug_id": drug_id,
        "canonical_name": canonical_name,
        "target_disease": target_disease,
        "endpoint_type": endpoint_type,
        "query": query,
        "qc": {
            "topic_match_ratio": round(float(tmr_all), 4),
            "topic_mismatch": bool(mismatch),
            "removed_evidence_count": int(removed),
            "removed_cross_drug_count": int(removed_cross_drug),
            "supporting_evidence_after_qc": int(len(unique_support_pmids)),
            "supporting_sentence_count_after_qc": int(len(supporting)),
            "qc_reasons": sorted(set(qc_reasons)) if qc_reasons else []
        },
        "clinicaltrials_negative": trials,
        "pubmed_rag": {
            "top_abstracts": top_docs,
            "top_sentences": sorted(top_sentences, key=lambda x: x.get("score",0), reverse=True)[:20]
        },
        "llm_structured": {
            "confidence": conf,
            "mode": ("llm" if llm_items_total > 0 else "rule"),
            "repurpose_rationale": "",
            "proposed_mechanisms": sorted(set([k for k in tokenize(" ".join([d.get("title","") for d in top_docs[:4]])) if len(k) < 18]))[:12],
            "key_risks": [],
            "supporting_evidence": supporting,
            "harm_or_neutral_evidence": harm_or_neutral[:20],
            "counts": {
                "supporting_evidence_count": int(len(unique_support_pmids)) if _Config.gating.SUPPORT_COUNT_MODE == "unique_pmids" else int(len(supporting)),
                "supporting_sentence_count": int(len(supporting)),
                "unique_supporting_pmids_count": int(len(unique_support_pmids)),
                "unique_supporting_pmids": unique_support_pmids,
                "harm_or_neutral_count": int(len(harm_or_neutral)),
                "llm_items_total": int(llm_items_total)
            }
        }
    }

    # 8) Write dossier files
    dossiers_dir = out_dir / "dossiers"
    dossiers_dir.mkdir(parents=True, exist_ok=True)
    json_path = dossiers_dir / f"{drug_id}__{safe_filename(canonical_name)}.json"
    md_path = dossiers_dir / f"{drug_id}__{safe_filename(canonical_name)}.md"

    # md rendering (human)
    md_lines = []
    md_lines.append(f"# {canonical_name}\n")
    md_lines.append(f"**Target disease:** {target_disease}\n")
    md_lines.append("## QC summary")
    qc = dossier["qc"]
    for k in ["topic_match_ratio","topic_mismatch","removed_evidence_count","supporting_evidence_after_qc","qc_reasons"]:
        md_lines.append(f"- {k}: {qc.get(k)}")
    md_lines.append("")
    md_lines.append("## ClinicalTrials negative evidence")
    md_lines.append(trials_md if trials_md else "- (no negative trial row found)")
    md_lines.append("")
    md_lines.append("## PubMed top abstracts (after rerank)")
    for d in top_docs:
        md_lines.append(f"- PMID:{d.get('pmid','')} | {d.get('title','')} ({d.get('year','')})")
        abs1 = (d.get("abstract","") or "").strip().replace("\n"," ")
        md_lines.append(f"  - Abstract: {abs1[:520]}{'...' if len(abs1)>520 else ''}")
    md_lines.append("")
    md_lines.append(f"## Structured evidence (confidence={conf})")
    md_lines.append("")
    se_u = dossier['llm_structured']['counts'].get('unique_supporting_pmids_count', dossier['llm_structured']['counts']['supporting_evidence_count'])
    se_s = dossier['llm_structured']['counts'].get('supporting_sentence_count', None)
    md_lines.append(f"### Supporting evidence (unique_pmids={se_u}{', sentences='+str(se_s) if se_s is not None else ''})")
    if supporting:
        for ev in supporting[:10]:
            md_lines.append(f"- PMID:{ev.get('pmid','')} | **{ev.get('direction','')}** | model={ev.get('model','')} | endpoint={ev.get('endpoint','')}")
            md_lines.append(f"  - claim: {ev.get('claim','')}")
    else:
        md_lines.append("- (none)")
    md_lines.append("")
    md_lines.append(f"### Harm/neutral evidence (count={dossier['llm_structured']['counts']['harm_or_neutral_count']})")
    for ev in harm_or_neutral[:10]:
        src = f"PMID:{ev.get('pmid','')}" if ev.get("pmid") else "CT.gov"
        md_lines.append(f"- {src} | **{ev.get('direction','')}** | model={ev.get('model','')}")
        md_lines.append(f"  - claim: {ev.get('claim','')}")
    md_lines.append("")

    write_json(json_path, dossier)
    write_text(md_path, "\n".join(md_lines))

    return json_path, md_path, dossier

# ---------------------------
# CLI
# ---------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rank_in", default="data/step6_rank.csv", help="Input rank CSV (must have drug_id, canonical_name).")
    ap.add_argument("--neg", default="data/poolA_negative_drug_level.csv", help="Optional CT.gov negative CSV (e.g., data/poolA_negative_drug_level.csv).")
    ap.add_argument("--out", default="output/step6", help="Output directory.")
    ap.add_argument("--target_disease", default="atherosclerosis")
    ap.add_argument("--topn", type=int, default=50)
    args = ap.parse_args()

    rank = pd.read_csv(args.rank_in)
    needed = {"drug_id","canonical_name"}
    miss = [c for c in needed if c not in rank.columns]
    if miss:
        raise ValueError(f"{args.rank_in} missing columns: {miss}")

    out_dir = Path(args.out).resolve()
    cache_dir = out_dir / "cache" / "pubmed"
    out_dir.mkdir(parents=True, exist_ok=True)

    # keep topn if rank_score exists
    if "rank_score" in rank.columns:
        rank = rank.sort_values("rank_score", ascending=False).head(args.topn).copy()
    else:
        rank = rank.head(args.topn).copy()

    all_drug_names = [str(x).strip() for x in rank['canonical_name'].tolist() if str(x).strip()]

    dossier_json_paths = []
    dossier_md_paths = []
    llm_conf = []
    pubmed_total = []
    rag_top_sent = []
    endpoint_types = []
    se_cnts = []
    harm_cnts = []
    tmr_list = []
    se_sentence_cnts = []
    se_unique_pmids_cnts = []

    for _, rr in tqdm(rank.iterrows(), total=len(rank), desc="step6_v2"):
        drug_id = str(rr.get("drug_id","")).strip()
        canon = str(rr.get("canonical_name","")).strip()
        if not drug_id or not canon:
            dossier_json_paths.append("")
            dossier_md_paths.append("")
            llm_conf.append("LOW")
            pubmed_total.append(int(rr.get("pubmed_total_articles", 0) or 0))
            rag_top_sent.append(int(rr.get("rag_top_sentences", 0) or 0))
            endpoint_types.append(str(rr.get("endpoint_type","OTHER")))
            se_cnts.append(0); harm_cnts.append(0); tmr_list.append(0.0)
            se_sentence_cnts.append(0); se_unique_pmids_cnts.append(0)
            continue

        endpoint_hint = str(rr.get("endpoint_type","OTHER"))
        json_path, md_path, dossier = process_one(
            drug_id, canon, args.target_disease, endpoint_hint,
            args.neg if args.neg else None, out_dir, cache_dir, all_drug_names
        )

        dossier_json_paths.append(str(json_path))
        dossier_md_paths.append(str(md_path))
        conf = (dossier.get("llm_structured") or {}).get("confidence","LOW")
        llm_conf.append(conf)
        pubmed_total.append(len(((dossier.get("pubmed_rag") or {}).get("top_abstracts")) or []))
        rag_top_sent.append(len(((dossier.get("pubmed_rag") or {}).get("top_sentences")) or []))
        endpoint_types.append(dossier.get("endpoint_type","OTHER"))
        counts = ((dossier.get("llm_structured") or {}).get("counts") or {})
        se_cnts.append(int(counts.get("supporting_evidence_count", 0) or 0))
        se_sentence_cnts.append(int(counts.get("supporting_sentence_count", 0) or 0))
        se_unique_pmids_cnts.append(int(counts.get("unique_supporting_pmids_count", 0) or 0))
        harm_cnts.append(int(counts.get("harm_or_neutral_count", 0) or 0))
        tmr_list.append(float(((dossier.get("qc") or {}).get("topic_match_ratio", 0.0))))

    # update rank
    out_rank = rank.copy()
    out_rank["dossier_json"] = dossier_json_paths
    out_rank["dossier_md"] = dossier_md_paths
    out_rank["llm_confidence"] = llm_conf
    out_rank["pubmed_total_articles"] = pubmed_total
    out_rank["rag_top_sentences"] = rag_top_sent
    out_rank["endpoint_type"] = endpoint_types
    out_rank["supporting_evidence_count"] = se_cnts
    out_rank["supporting_sentence_count"] = se_sentence_cnts
    out_rank["unique_supporting_pmids_count"] = se_unique_pmids_cnts
    out_rank["harm_or_neutral_count"] = harm_cnts
    out_rank["topic_match_ratio"] = tmr_list

    out_csv = out_dir / "step6_rank_v2.csv"
    out_rank.to_csv(out_csv, index=False, encoding="utf-8-sig")
    log(f"[OK] wrote: {out_csv}")

if __name__ == "__main__":
    main()