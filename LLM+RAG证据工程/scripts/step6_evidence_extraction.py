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

import os, re, json, math, time, hashlib, argparse, sys, logging, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Load .env BEFORE any os.getenv calls
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).resolve().parent.parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path, override=False)
except ImportError:
    pass

import pandas as pd
try:
    from tqdm import tqdm
except Exception:  # pragma: no cover - fallback for broken/missing tqdm metadata
    def tqdm(iterable, *args, **kwargs):
        return iterable

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
from src.dr.evidence.ranker import (
    BM25Ranker,
    HybridRanker,
    RankingPipeline,
    reciprocal_rank_fusion,
)
from src.dr.evidence.extractor import repair_json as modular_repair_json
from src.dr.evidence.extractor import validate_extraction, coerce_extraction, detect_hallucination
from src.dr.common.http import request_with_retries as shared_request_with_retries
from src.dr.common.provenance import build_manifest, write_manifest
from src.dr.contracts import (
    STEP6_DOSSIER_SCHEMA,
    STEP6_DOSSIER_VERSION,
    stamp_step6_dossier_contract,
    validate_step6_dossier,
)
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
DISABLE_EMBED = os.getenv("DISABLE_EMBED", "0") == "1"
DISABLE_LLM = os.getenv("DISABLE_LLM", "0") == "1"

CROSS_DRUG_FILTER = os.getenv("CROSS_DRUG_FILTER", "1") == "1"
PMID_STRICT = os.getenv("PMID_STRICT", "1") == "1"
SUPPORT_COUNT_MODE = os.getenv("SUPPORT_COUNT_MODE", "unique_pmids")  # unique_pmids | sentences

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "8"))
RETRY_SLEEP = float(os.getenv("RETRY_SLEEP", "3"))

# Thread-safe PubMed rate limiter (NCBI allows 3 req/s without API key, 10 req/s with)
_pubmed_rate_lock = threading.Lock()
_pubmed_last_request_time = 0.0

def _pubmed_rate_wait():
    """Ensure minimum NCBI_DELAY between any two PubMed requests (thread-safe)."""
    global _pubmed_last_request_time
    with _pubmed_rate_lock:
        now = time.monotonic()
        elapsed = now - _pubmed_last_request_time
        if elapsed < NCBI_DELAY:
            time.sleep(NCBI_DELAY - elapsed)
        _pubmed_last_request_time = time.monotonic()

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

def request_with_retries(method: str, url: str, **kwargs):
    """Compatibility wrapper around shared HTTP retry helper."""
    return shared_request_with_retries(
        method=method,
        url=url,
        max_retries=MAX_RETRIES,
        retry_sleep=RETRY_SLEEP,
        **kwargs,
    )

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

RELATED_DISEASE_TERMS = {
    "atherosclerosis": [
        "coronary artery disease",
        "peripheral artery disease",
        "ischemic stroke",
        "carotid stenosis",
    ],
    "coronary artery disease": [
        "atherosclerosis",
        "myocardial infarction",
        "ischemic heart disease",
    ],
    "heart failure": [
        "cardiomyopathy",
        "ischemic heart disease",
        "cardiac remodeling",
    ],
}

MECHANISM_HINTS_BY_ENDPOINT = {
    "PLAQUE_IMAGING": [
        "endothelial dysfunction",
        "foam cell",
        "oxidative stress",
        "NLRP3",
        "NF-kB",
    ],
    "PAD_FUNCTION": [
        "microvascular perfusion",
        "angiogenesis",
        "exercise tolerance",
        "skeletal muscle ischemia",
    ],
    "CV_EVENTS": [
        "thrombosis",
        "platelet activation",
        "vascular inflammation",
        "plaque instability",
    ],
    "OTHER": [
        "inflammation",
        "oxidative stress",
        "immune modulation",
    ],
}

def topic_match_ratio(text: str, endpoint_type: str) -> float:
    kws = TOPIC_KEYWORDS.get(endpoint_type, TOPIC_KEYWORDS["OTHER"])
    t = (text or "").lower()
    if not kws: return 0.0
    hit = sum(1 for k in kws if k.lower() in t)
    return hit / float(len(kws))

def pubmed_esearch(term: str, retmax: int = 200) -> List[str]:
    _pubmed_rate_wait()
    params = {"db":"pubmed","term":term,"retmode":"json","retmax":str(retmax),"sort":"relevance"}
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY
    url = f"{NCBI_EUTILS}/esearch.fcgi"
    r = request_with_retries("GET", url, params=params, timeout=PUBMED_TIMEOUT)
    data = r.json()
    return (data.get("esearchresult", {}) or {}).get("idlist", []) or []

def pubmed_efetch_xml(pmids: List[str]) -> str:
    """Fetch PubMed XML in small batches to reduce SSL/EOF failures."""
    if not pmids:
        return ""
    out = []
    step = max(1, int(PUBMED_EFETCH_CHUNK))
    for i in range(0, len(pmids), step):
        _pubmed_rate_wait()
        batch = pmids[i:i+step]
        params = {"db": "pubmed", "id": ",".join(batch), "retmode": "xml"}
        if NCBI_API_KEY:
            params["api_key"] = NCBI_API_KEY
        url = f"{NCBI_EUTILS}/efetch.fcgi"
        r = request_with_retries("GET", url, params=params, timeout=PUBMED_TIMEOUT)
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

def rerank_with_embeddings(
    query: str,
    ranked: List[Tuple[float, Dict[str, Any]]],
    topk: int = 25,
    max_rerank_docs: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Optional embedding rerank on top of BM25.

    MAX_RERANK_DOCS controls how many BM25 docs are sent to the embedder.
    EMBED_BATCH_SIZE controls embed batching to reduce HTTP timeouts.
    """
    if DISABLE_EMBED:
        return [d for _, d in ranked[:topk]]

    docs = [d for _, d in ranked]
    if not docs:
        return []

    rerank_limit = max(1, int(max_rerank_docs if max_rerank_docs is not None else MAX_RERANK_DOCS))
    docs = docs[:rerank_limit]
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

def _clean_pmid(raw: str, expected: str) -> str:
    """Post-process PMID: strip URL wrappers, strconv(), prefixes → pure digits."""
    s = str(raw).strip()
    # http://www.ncbi.nlm.nih.gov/pubmed/12345678 → 12345678
    m = re.search(r'pubmed[./](\d{6,9})', s)
    if m:
        return m.group(1)
    # strconv(12345678) or s12345678 or g12345678
    m = re.search(r'(?:strconv\()?(\d{6,9})\)?', s)
    if m:
        return m.group(1)
    # PMID:12345678 or PMID 12345678
    m = re.search(r'PMID[:\s]*(\d{6,9})', s, re.IGNORECASE)
    if m:
        return m.group(1)
    # Pure digits already
    if re.fullmatch(r'\d{6,9}', s):
        return s
    # Fallback: use expected
    return expected

def extract_evidence_with_llm(drug: str, target_disease: str, endpoint_type: str, pmid: str, fragment: str, aliases: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    system = (
        "You extract citable evidence items from biomedical abstracts. "
        "Return STRICT JSON ONLY (no markdown). Output must be a JSON array of 0-2 objects. "
        "Each object must have keys: pmid, supports (true/false), direction (benefit|harm|neutral|unknown), "
        "model (human|animal|cell|unknown), endpoint (short phrase), claim (<=25 words), confidence (0-1). "
        "CRITICAL: the 'pmid' field MUST be a numeric string of 6-9 digits ONLY (e.g. \"24861566\"). "
        "Do NOT return URLs, prefixes, or any formatting — just the digits."
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
                raw_pmid = str(o.get('pmid') or pmid).strip()
                o['pmid_raw'] = raw_pmid  # keep original for audit
                o['pmid'] = _clean_pmid(raw_pmid, pmid)
                # --- modular hallucination detection ---
                warnings = detect_hallucination(o, pmid, fragment, drug, aliases=aliases)
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
def _normalize_disease_key(disease: str) -> str:
    s = (disease or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def _related_disease_terms(target_disease: str, endpoint_type: str) -> List[str]:
    base_key = _normalize_disease_key(target_disease)
    terms = list(RELATED_DISEASE_TERMS.get(base_key, []))

    # Endpoint-specific defaults increase recall for cross-disease repurposing.
    if endpoint_type == "PLAQUE_IMAGING":
        terms.extend(["coronary artery disease", "peripheral artery disease", "ischemic stroke"])
    elif endpoint_type == "PAD_FUNCTION":
        terms.extend(["critical limb ischemia", "intermittent claudication"])
    elif endpoint_type == "CV_EVENTS":
        terms.extend(["acute coronary syndrome", "myocardial infarction", "ischemic stroke"])

    seen = set()
    out = []
    for t in terms:
        tt = (t or "").strip().lower()
        if not tt or tt == base_key or tt in seen:
            continue
        seen.add(tt)
        out.append(t)
    return out[:5]


def build_query_routes(drug: str, target_disease: str, endpoint_type: str) -> List[Dict[str, str]]:
    endpoint_clause = ENDPOINT_QUERY.get(endpoint_type, ENDPOINT_QUERY["OTHER"])
    mech_hints = MECHANISM_HINTS_BY_ENDPOINT.get(endpoint_type, MECHANISM_HINTS_BY_ENDPOINT["OTHER"])
    mech_clause = " OR ".join([f'"{m}"' for m in mech_hints[:5]])
    related_terms = _related_disease_terms(target_disease, endpoint_type)

    routes: List[Dict[str, str]] = []
    routes.append({
        "route": "exact_disease",
        "query": f'("{drug}") AND ("{target_disease}")',
    })
    routes.append({
        "route": "endpoint_mechanism",
        "query": f'("{drug}") AND ({endpoint_clause}) AND ({mech_clause})',
    })
    routes.append({
        "route": "disease_or_endpoint",
        "query": f'("{drug}") AND (("{target_disease}") OR ({endpoint_clause}))',
    })
    if related_terms:
        rel_clause = " OR ".join([f'"{t}"' for t in related_terms])
        routes.append({
            "route": "cross_disease_transfer",
            "query": f'("{drug}") AND ({rel_clause}) AND ({mech_clause})',
        })

    # Deduplicate exact query strings while preserving order.
    seen_q = set()
    deduped: List[Dict[str, str]] = []
    for item in routes:
        q = item["query"].strip()
        if q in seen_q:
            continue
        seen_q.add(q)
        deduped.append(item)
    return deduped


def build_query(drug: str, target_disease: str, endpoint_type: str) -> str:
    """Backward-compatible single query accessor (primary route)."""
    routes = build_query_routes(drug, target_disease, endpoint_type)
    if not routes:
        return f'("{drug}") AND ("{target_disease}")'
    return routes[0]["query"]

def process_one(
    drug_id: str,
    canonical_name: str,
    target_disease: str,
    endpoint_type_hint: str,
    neg_path: Optional[str],
    out_dir: Path,
    cache_dir: Path,
    all_drug_names: List[str],
    aliases: Optional[List[str]] = None,
    pubmed_retmax: int = 120,
    pubmed_parse_max: int = 60,
    max_rerank_docs: int = 40,
    max_evidence_docs: int = 12,
) -> Tuple[Path, Path, Dict[str, Any]]:
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

    query_routes = build_query_routes(canonical_name, target_disease, endpoint_type)
    query = query_routes[0]["query"] if query_routes else build_query(canonical_name, target_disease, endpoint_type)

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


    route_pmids_map: Dict[str, List[str]] = {}
    route_docs_map: Dict[str, List[Dict[str, Any]]] = {}
    route_stats: List[Dict[str, Any]] = []

    # 1) multi-route retrieve PMIDs + docs
    pubmed_retmax = max(1, int(pubmed_retmax))
    pubmed_parse_max = max(1, int(pubmed_parse_max))
    max_rerank_docs = max(1, int(max_rerank_docs))
    max_evidence_docs = max(1, int(max_evidence_docs))

    if FORCE_REBUILD or is_empty(pmids_path) or is_empty(docs_path) or (REFRESH_EMPTY_CACHE and (is_empty(pmids_path) or is_empty(docs_path))):
        # Parallel PubMed retrieval: each route runs in its own thread.
        # Rate limiting is enforced by _pubmed_rate_wait() inside pubmed_esearch/efetch.
        _first_xml_holder: Dict[str, str] = {}  # capture first route's XML for backward compat

        def _fetch_route(idx_route):
            idx, route = idx_route
            rname = safe_filename(route.get("route", f"route{idx+1}"))
            rquery = route.get("query", "")
            r_pmids_path = base / f"pmids_{rname}.json"
            r_docs_path = base / f"docs_{rname}.json"

            pmids = pubmed_esearch(rquery, retmax=pubmed_retmax)
            write_json(r_pmids_path, {"route": rname, "query": rquery, "pmids": pmids})

            all_route_docs: List[Dict[str, Any]] = []
            for i in range(0, min(len(pmids), pubmed_retmax), 50):
                batch_pmids = pmids[i:i + 50]
                xml = pubmed_efetch_xml(batch_pmids)
                parsed = parse_pubmed_xml(xml, max_articles=pubmed_parse_max)
                all_route_docs.extend(parsed)
                if idx == 0 and i == 0:
                    _first_xml_holder["xml"] = xml
            write_json(
                r_docs_path,
                {
                    "route": rname,
                    "query": rquery,
                    "endpoint_type": endpoint_type,
                    "docs": all_route_docs,
                },
            )
            return idx, rname, pmids, all_route_docs

        with ThreadPoolExecutor(max_workers=min(len(query_routes), 4)) as pool:
            futures = {pool.submit(_fetch_route, (i, r)): i for i, r in enumerate(query_routes)}
            for fut in as_completed(futures):
                idx, rname, pmids, docs = fut.result()
                route_pmids_map[rname] = pmids
                route_docs_map[rname] = docs

        # Write first route's XML sample for backward compatibility
        if _first_xml_holder.get("xml"):
            write_text(xml_path, _first_xml_holder["xml"])
    else:
        # Backward/forward-compatible cache loading.
        cached_pmids = read_json(pmids_path) or {}
        cached_docs = read_json(docs_path) or {}
        route_pmids_map = cached_pmids.get("route_pmids", {})
        route_docs_map = cached_docs.get("route_docs", {})
        if not route_pmids_map or not route_docs_map:
            for idx, route in enumerate(query_routes):
                rname = safe_filename(route.get("route", f"route{idx+1}"))
                r_pmids_path = base / f"pmids_{rname}.json"
                r_docs_path = base / f"docs_{rname}.json"
                if r_pmids_path.exists():
                    route_pmids_map[rname] = (read_json(r_pmids_path) or {}).get("pmids", [])
                else:
                    route_pmids_map[rname] = []
                if r_docs_path.exists():
                    route_docs_map[rname] = (read_json(r_docs_path) or {}).get("docs", [])
                else:
                    route_docs_map[rname] = []

    # 2) merge docs and keep route hit provenance
    merged_by_pmid: Dict[str, Dict[str, Any]] = {}
    for rname, docs in route_docs_map.items():
        for doc in docs or []:
            pmid = str(doc.get("pmid", "")).strip()
            if not pmid:
                continue
            if pmid not in merged_by_pmid:
                merged = dict(doc)
                merged["route_hits"] = [rname]
                merged_by_pmid[pmid] = merged
            else:
                hits = merged_by_pmid[pmid].get("route_hits", [])
                if rname not in hits:
                    hits.append(rname)
                merged_by_pmid[pmid]["route_hits"] = hits

    all_docs = list(merged_by_pmid.values())
    pmids = sorted(merged_by_pmid.keys())

    write_json(
        pmids_path,
        {
            "query": query,
            "query_routes": query_routes,
            "pmids": pmids,
            "route_pmids": route_pmids_map,
        },
    )
    write_json(
        docs_path,
        {
            "query": query,
            "query_routes": query_routes,
            "endpoint_type": endpoint_type,
            "docs": all_docs,
            "route_docs": route_docs_map,
        },
    )

    # 3) stage-1 rank: BM25 for each route, then fuse with RRF.
    route_ranked_lists: List[List[Tuple[float, Dict[str, Any]]]] = []
    for idx, route in enumerate(query_routes):
        rname = safe_filename(route.get("route", f"route{idx+1}"))
        rquery = route.get("query", query)
        ranked = bm25_rank(rquery, all_docs, topk=80)
        route_ranked_lists.append(ranked)

        top_pmids = [str(d.get("pmid", "")) for _, d in ranked[:10]]
        route_stats.append(
            {
                "route": rname,
                "query": rquery,
                "pmids_retrieved": int(len(route_pmids_map.get(rname, []))),
                "docs_parsed": int(len(route_docs_map.get(rname, []))),
                "top10_pmids": top_pmids,
            }
        )

    fused_ranked = reciprocal_rank_fusion(route_ranked_lists, k=60) if route_ranked_lists else []
    if not fused_ranked:
        # Fallback for degenerate cases.
        fused_ranked = bm25_rank(query, all_docs, topk=80)

    # 4) stage-2 rerank with embeddings (optional)
    reranked_docs = rerank_with_embeddings(query, fused_ranked, topk=30, max_rerank_docs=max_rerank_docs)
    top_docs = reranked_docs[:10]  # for md display

    top30_pmids = {str(d.get("pmid", "")) for d in reranked_docs[:30]}
    for rs in route_stats:
        top_hits = [pmid for pmid in rs.get("top10_pmids", []) if pmid in top30_pmids]
        rs["hits_in_top30"] = int(len(top_hits))

    write_json(
        reranked_path,
        {
            "query": query,
            "query_routes": query_routes,
            "route_stats": route_stats,
            "top_pmids": [d.get("pmid", "") for d in reranked_docs],
        },
    )

    # 5) evidence extraction per top docs (LLM + fallback rule)
    supporting: List[Dict[str, Any]] = []
    harm_or_neutral: List[Dict[str, Any]] = []
    top_sentences = []  # for compatibility with step7 rag avg score

    # collect early QC drops during extraction (before final QC section)
    pre_qc_reasons: List[str] = []
    pre_removed = 0
    pre_removed_cross_drug = 0
    llm_items_total = 0
    for d in reranked_docs[:max_evidence_docs]:
        pmid = d.get("pmid","")
        title = d.get("title","")
        abstract = d.get("abstract","") or ""

        # pick fragments
        frags = pick_evidence_fragments(title, abstract, endpoint_type, max_frags=2)

        extracted: List[Dict[str, Any]] = []
        # try LLM on the most on-topic fragments first
        for frag in frags:
            items = extract_evidence_with_llm(canonical_name, target_disease, endpoint_type, pmid, frag, aliases=aliases)
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
                    # Flag instead of delete: comparative studies (e.g. "simvastatin
                    # was more effective than pravastatin") are valuable evidence for
                    # drug repurposing.  Downstream scorers can use the flag to
                    # down-weight if needed, but the evidence is preserved.
                    ev['cross_drug_flag'] = True
                    pre_removed_cross_drug += 1

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

    # final PMID normalization (digits-only) + flag (not drop) cross-drug mentions
    def _clean_list(items: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int, int]:
        out: List[Dict[str, Any]] = []
        flagged_local = 0
        flagged_cross_local = 0
        for ev in items:
            raw = ev.get('pmid','')
            ev['pmid'] = normalize_pmid(raw) if PMID_STRICT else str(raw or '').strip()
            if CROSS_DRUG_FILTER and other_markers:
                if contains_other_drug(str(ev.get('claim','') or ''), other_markers):
                    ev['cross_drug_flag'] = True
                    flagged_local += 1
                    flagged_cross_local += 1
            out.append(ev)
        return out, flagged_local, flagged_cross_local
    supporting, _r1, _c1 = _clean_list(supporting)
    harm_or_neutral, _r2, _c2 = _clean_list(harm_or_neutral)
    if (_r1 + _r2) > 0:
        pre_qc_reasons.append('cross_drug_flagged')
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

    route_coverage = int(sum(1 for rs in route_stats if int(rs.get("docs_parsed", 0)) > 0))
    cross_disease_hits = int(
        sum(
            int(rs.get("hits_in_top30", 0))
            for rs in route_stats
            if "cross_disease" in str(rs.get("route", ""))
        )
    )

    # 7) Build dossier JSON
    dossier = {
        "drug_id": drug_id,
        "canonical_name": canonical_name,
        "target_disease": target_disease,
        "endpoint_type": endpoint_type,
        "query": query,
        "query_routes": query_routes,
        "retrieval": {
            "strategy": "multi_route_rrf_v1",
            "route_coverage": route_coverage,
            "cross_disease_hits": cross_disease_hits,
            "routes_total": int(len(query_routes)),
            "route_stats": route_stats,
            "pmids_total": int(len(pmids)),
            "docs_total": int(len(all_docs)),
        },
        "qc": {
            "topic_match_ratio": round(float(tmr_all), 4),
            "topic_mismatch": bool(mismatch),
            "removed_evidence_count": int(removed),
            "flagged_cross_drug_count": int(removed_cross_drug),
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
    md_lines.append("## Query routes")
    for route in query_routes:
        md_lines.append(f"- {route.get('route', '')}: `{route.get('query', '')}`")
    md_lines.append("")
    md_lines.append("## Retrieval coverage")
    md_lines.append(f"- route_coverage: {route_coverage}/{len(query_routes)}")
    md_lines.append(f"- cross_disease_hits(top30): {cross_disease_hits}")
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

    dossier = stamp_step6_dossier_contract(
        dossier,
        producer="scripts/step6_evidence_extraction.py",
    )
    contract_issues = validate_step6_dossier(dossier)
    if contract_issues:
        raise ValueError(
            f"Step6 dossier contract validation failed for {drug_id} ({canonical_name}): {contract_issues}"
        )

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
    ap.add_argument("--pubmed_retmax", type=int, default=int(os.getenv("STEP6_PUBMED_RETMAX", "120")))
    ap.add_argument("--pubmed_parse_max", type=int, default=int(os.getenv("STEP6_PUBMED_PARSE_MAX", "60")))
    ap.add_argument("--max_rerank_docs", type=int, default=int(os.getenv("STEP6_MAX_RERANK_DOCS", "40")))
    ap.add_argument("--max_evidence_docs", type=int, default=int(os.getenv("STEP6_MAX_EVIDENCE_DOCS", "12")))
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
        chembl_pref = str(rr.get("chembl_pref_name","")).strip()
        drug_aliases = [chembl_pref] if chembl_pref and chembl_pref.lower() != canon.lower() else []

        # --- Skip if dossier already exists (restartability) ---
        dossiers_dir = out_dir / "dossiers"
        cached_json = dossiers_dir / f"{drug_id}__{safe_filename(canon)}.json"
        cached_md = dossiers_dir / f"{drug_id}__{safe_filename(canon)}.md"
        if cached_json.exists() and not FORCE_REBUILD:
            try:
                with open(cached_json, "r", encoding="utf-8") as _f:
                    dossier = json.load(_f)
                json_path, md_path = cached_json, cached_md
                log(f"[SKIP] {drug_id} ({canon}) already has dossier, reusing cached version")
            except Exception as _e:
                log(f"[WARN] Cached dossier for {drug_id} unreadable ({_e}), reprocessing", "warning")
                json_path, md_path, dossier = process_one(
                    drug_id, canon, args.target_disease, endpoint_hint,
                    args.neg if args.neg else None, out_dir, cache_dir, all_drug_names,
                    aliases=drug_aliases,
                    pubmed_retmax=args.pubmed_retmax,
                    pubmed_parse_max=args.pubmed_parse_max,
                    max_rerank_docs=args.max_rerank_docs,
                    max_evidence_docs=args.max_evidence_docs,
                )
        else:
            json_path, md_path, dossier = process_one(
                drug_id, canon, args.target_disease, endpoint_hint,
                args.neg if args.neg else None, out_dir, cache_dir, all_drug_names,
                aliases=drug_aliases,
                pubmed_retmax=args.pubmed_retmax,
                pubmed_parse_max=args.pubmed_parse_max,
                max_rerank_docs=args.max_rerank_docs,
                max_evidence_docs=args.max_evidence_docs,
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

    repo_root = Path(__file__).resolve().parent.parent
    input_files = [Path(args.rank_in).resolve()]
    if args.neg:
        neg_path = Path(args.neg).resolve()
        if neg_path.exists():
            input_files.append(neg_path)

    output_files = [out_csv]
    output_files.extend(
        Path(p).resolve()
        for p in dossier_json_paths
        if p
    )
    output_files.extend(
        Path(p).resolve()
        for p in dossier_md_paths
        if p
    )

    manifest = build_manifest(
        pipeline="step6_evidence_extraction",
        repo_root=repo_root,
        input_files=input_files,
        output_files=output_files,
        config={
            "rank_in": str(Path(args.rank_in).resolve()),
            "neg": str(Path(args.neg).resolve()) if args.neg else "",
            "out": str(out_dir),
            "target_disease": args.target_disease,
            "topn": int(args.topn),
            "pubmed_retmax": int(args.pubmed_retmax),
            "pubmed_parse_max": int(args.pubmed_parse_max),
            "max_rerank_docs": int(args.max_rerank_docs),
            "max_evidence_docs": int(args.max_evidence_docs),
            "max_retries": int(MAX_RETRIES),
            "retry_sleep": float(RETRY_SLEEP),
            "ollama_host": OLLAMA_HOST,
            "ollama_embed_model": OLLAMA_EMBED_MODEL,
            "ollama_llm_model": OLLAMA_LLM_MODEL,
        },
        summary={
            "drugs_total": int(len(out_rank)),
            "dossiers_written": int(sum(1 for p in dossier_json_paths if p)),
            "governed_schema": STEP6_DOSSIER_SCHEMA,
            "governed_version": STEP6_DOSSIER_VERSION,
        },
        contracts={
            STEP6_DOSSIER_SCHEMA: STEP6_DOSSIER_VERSION,
        },
    )
    manifest_path = out_dir / "step6_manifest.json"
    write_manifest(manifest_path, manifest)
    log(f"[OK] wrote: {manifest_path}")

if __name__ == "__main__":
    main()
