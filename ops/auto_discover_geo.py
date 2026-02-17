#!/usr/bin/env python3
"""
auto_discover_geo.py — GEO Dataset Auto-Discovery for dsmeta pipeline
=====================================================================

Given a disease name, search NCBI GEO for suitable expression datasets,
auto-detect case/control groups, score quality, and generate a candidate
dsmeta YAML config ready for human review.

Usage:
  # Single disease
  python auto_discover_geo.py --disease "heart failure" --out-dir geo_curation

  # Batch mode (from disease_list.txt)
  python auto_discover_geo.py --batch disease_list_day1_origin.txt --out-dir geo_curation

  # Generate dsmeta YAML directly
  python auto_discover_geo.py --disease "atherosclerosis" --out-dir geo_curation --write-yaml

Output:
  geo_curation/<disease_key>/
    ├── candidates.tsv        — All candidate GSE with scores
    ├── selected.tsv          — Top GSE selected for pipeline
    ├── discovery_log.txt     — Detailed search & filter log
    └── candidate_config.yaml — Ready-to-review dsmeta config (if --write-yaml)

Requirements:
  pip install requests pyyaml
  (No LLM needed — pure rule-based)

Author: auto-generated for Drug Repurposing Platform
"""

import argparse
import json
import logging
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import yaml

# ── Constants ──────────────────────────────────────────────────────────────

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
GEO_BROWSE_URL = "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi"

# NCBI rate limit: max 3 requests/second without API key
NCBI_DELAY = 0.4  # seconds between requests

# Platforms with known good annotations (probe_to_gene works well)
GOOD_PLATFORMS = {
    "GPL570":  "Affymetrix HG-U133 Plus 2.0",
    "GPL96":   "Affymetrix HG-U133A",
    "GPL571":  "Affymetrix HG-U133A 2.0",
    "GPL6244": "Affymetrix HuGene 1.0 ST",
    "GPL10558":"Illumina HumanHT-12 V4",
    "GPL6480": "Agilent Whole Human Genome 4x44K",
    "GPL6947": "Illumina HumanHT-12 V3",
    "GPL6884": "Illumina HumanWG-6 V3",
    "GPL13667":"Affymetrix HG-U219",
    "GPL17586":"Affymetrix HTA 2.0",
    "GPL16686":"Affymetrix HuGene 2.0 ST",
    "GPL11532":"Affymetrix HuGene 1.1 ST",
}

# Case/control detection rules (priority order)
CASE_CONTROL_PATTERNS = [
    # Most explicit
    (r"(?:disease|condition)\s*[:=]\s*(\S+)", "explicit_field"),
    # Common binary patterns
    (r"\b(disease|patient|case|affected|tumor|lesion|plaque|stenosis|"
     r"aneurysm|infarct|fail|ischemi|athero|fibrosis|inflam|diabetic)\b", "case_keyword"),
    (r"\b(normal|healthy|control|sham|non[_\-]?disease|intact|"
     r"unaffected|benign|donor|non[_\-]?fail|non[_\-]?diabetic)\b", "control_keyword"),
]

# Exclude these from GEO results (non-expression or irrelevant)
EXCLUDE_TITLE_PATTERNS = [
    r"\bcell\s+line\b",
    r"\bin\s+vitro\b",
    r"\b(mouse|murine|rat|porcine|bovine|canine|rabbit)\b",
    r"\b(mirna|microrna|methylation|chip[_\-]?seq|atac[_\-]?seq|single[_\-]?cell|scrna)\b",
    r"\b(lncrna|circrna|proteom)\b",
]

# ── Data Classes ───────────────────────────────────────────────────────────

@dataclass
class SampleInfo:
    gsm_id: str
    title: str
    source: str
    characteristics: str
    group: str = ""  # "case", "control", "unknown"
    confidence: str = ""  # "high", "medium", "low"
    matched_by: str = ""  # rule that matched

@dataclass
class GSECandidate:
    gse_id: str
    title: str
    summary: str
    platform: str
    platform_name: str
    organism: str
    sample_count: int
    submission_date: str
    samples: List[SampleInfo] = field(default_factory=list)
    case_count: int = 0
    control_count: int = 0
    case_regex: str = ""
    control_regex: str = ""
    case_field: str = "title"  # which metadata field matched
    quality_score: float = 0.0
    confidence: str = "low"
    exclude_reason: str = ""
    include: bool = False

# ── NCBI E-utilities ───────────────────────────────────────────────────────

class NCBIClient:
    """Thin wrapper around NCBI E-utilities with rate limiting."""

    def __init__(self, api_key: Optional[str] = None):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "DrugRepurposing-AutoGEO/1.0"})
        self.api_key = api_key
        self.delay = 0.12 if api_key else NCBI_DELAY
        self._last_request = 0.0

    def _throttle(self):
        elapsed = time.time() - self._last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_request = time.time()

    def _get(self, url: str, params: dict, retries: int = 3) -> requests.Response:
        if self.api_key:
            params["api_key"] = self.api_key
        for attempt in range(retries):
            self._throttle()
            try:
                resp = self.session.get(url, params=params, timeout=30)
                if resp.status_code == 429:
                    wait = min(2 ** attempt * 2, 30)
                    logging.warning(f"Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp
            except requests.RequestException as e:
                if attempt == retries - 1:
                    raise
                logging.warning(f"Request failed ({e}), retrying ({attempt+1}/{retries})...")
                time.sleep(2 ** attempt)
        raise RuntimeError("Max retries exceeded")

    def esearch_geo(self, disease: str, max_results: int = 200) -> List[str]:
        """Search GEO for expression datasets related to a disease."""
        query = (
            f'"{disease}"[Title] AND '
            f'"Homo sapiens"[Organism] AND '
            f'"Expression profiling by array"[DataSet Type]'
        )
        params = {
            "db": "gds",
            "term": query,
            "retmax": max_results,
            "retmode": "json",
            "sort": "relevance",
        }
        resp = self._get(ESEARCH_URL, params)
        data = resp.json()
        result = data.get("esearchresult", {})
        count = int(result.get("count", 0))
        id_list = result.get("idlist", [])
        logging.info(f"GEO search '{disease}': {count} total, retrieved {len(id_list)} IDs")

        # Also search RNA-seq (high throughput sequencing)
        query_rnaseq = (
            f'"{disease}"[Title] AND '
            f'"Homo sapiens"[Organism] AND '
            f'"Expression profiling by high throughput sequencing"[DataSet Type]'
        )
        params["term"] = query_rnaseq
        resp2 = self._get(ESEARCH_URL, params)
        data2 = resp2.json()
        result2 = data2.get("esearchresult", {})
        id_list2 = result2.get("idlist", [])
        logging.info(f"GEO search RNA-seq '{disease}': {result2.get('count', 0)} total, retrieved {len(id_list2)} IDs")

        # Combine and deduplicate
        all_ids = list(dict.fromkeys(id_list + id_list2))
        return all_ids

    def esummary_gds(self, gds_ids: List[str]) -> List[dict]:
        """Get summary info for GDS/GSE records. Process in batches of 20."""
        all_summaries = []
        for i in range(0, len(gds_ids), 20):
            batch = gds_ids[i:i+20]
            params = {
                "db": "gds",
                "id": ",".join(batch),
                "retmode": "json",
            }
            resp = self._get(ESUMMARY_URL, params)
            data = resp.json()
            result = data.get("result", {})
            for uid in batch:
                if uid in result:
                    all_summaries.append(result[uid])
        return all_summaries

    def fetch_series_matrix_header(self, gse_id: str) -> Optional[str]:
        """Download only the header portion of a series matrix file to get sample metadata."""
        url = f"https://ftp.ncbi.nlm.nih.gov/geo/series/{gse_id[:-3]}nnn/{gse_id}/matrix/{gse_id}_series_matrix.txt.gz"
        try:
            self._throttle()
            resp = self.session.get(url, timeout=30, stream=True)
            if resp.status_code != 200:
                return None
            import gzip
            import io
            # Read first 100KB (enough for metadata header)
            raw = resp.raw.read(100_000)
            resp.close()
            try:
                text = gzip.decompress(raw).decode("utf-8", errors="replace")
            except Exception:
                text = raw.decode("utf-8", errors="replace")
            return text
        except Exception as e:
            logging.debug(f"Could not fetch series matrix for {gse_id}: {e}")
            return None

    def fetch_soft_header(self, gse_id: str) -> Optional[str]:
        """Fetch GSE SOFT format header (lighter alternative)."""
        params = {
            "acc": gse_id,
            "targ": "gsm",
            "form": "text",
            "view": "brief",
        }
        try:
            self._throttle()
            resp = self.session.get(GEO_BROWSE_URL, params=params, timeout=30)
            if resp.status_code == 200:
                return resp.text[:200_000]  # first 200KB
        except Exception as e:
            logging.debug(f"Could not fetch SOFT for {gse_id}: {e}")
        return None


# ── Sample Classification ──────────────────────────────────────────────────

class SampleClassifier:
    """Rule-based case/control classifier for GEO sample metadata."""

    def __init__(self, disease: str):
        self.disease = disease.lower()
        self.disease_tokens = set(re.split(r"\s+", self.disease))

    def classify_from_summary(self, summary: dict, disease: str) -> Tuple[List[SampleInfo], str, str, str, str]:
        """
        Classify samples using esummary metadata.
        Returns: (samples, case_regex, control_regex, confidence, field_name)
        """
        samples_info = summary.get("samples", [])
        if isinstance(samples_info, str):
            # Parse "GSMxxx: title\nGSMyyyy: title" format
            samples_info = self._parse_sample_string(samples_info)

        samples = []
        for s in samples_info:
            if isinstance(s, dict):
                accession = s.get("accession", "")
                title = s.get("title", "")
            else:
                accession = str(s)
                title = ""
            samples.append(SampleInfo(
                gsm_id=accession,
                title=title,
                source="",
                characteristics=""
            ))

        if not samples:
            return samples, "", "", "low", "title"

        return self._classify_samples(samples)

    def classify_from_soft(self, soft_text: str) -> Tuple[List[SampleInfo], str, str, str, str]:
        """Parse SOFT text and classify samples."""
        samples = self._parse_soft(soft_text)
        if not samples:
            return [], "", "", "low", "title"
        return self._classify_samples(samples)

    def classify_from_series_matrix(self, text: str) -> Tuple[List[SampleInfo], str, str, str, str]:
        """Parse series matrix header and classify samples."""
        samples = self._parse_series_matrix(text)
        if not samples:
            return [], "", "", "low", "title"
        return self._classify_samples(samples)

    def _classify_samples(self, samples: List[SampleInfo]) -> Tuple[List[SampleInfo], str, str, str, str]:
        """Core classification logic. Try multiple strategies."""
        best_result = None
        best_score = -1

        # Strategy 1: Try title field
        result = self._try_field(samples, "title")
        if result and result[0] > best_score:
            best_score, best_result = result[0], result[1:]

        # Strategy 2: Try source field
        result = self._try_field(samples, "source")
        if result and result[0] > best_score:
            best_score, best_result = result[0], result[1:]

        # Strategy 3: Try characteristics field
        result = self._try_field(samples, "characteristics")
        if result and result[0] > best_score:
            best_score, best_result = result[0], result[1:]

        if best_result is None:
            # No classification found
            for s in samples:
                s.group = "unknown"
                s.confidence = "low"
            return samples, "", "", "low", "title"

        classified, case_regex, control_regex, confidence, field_name = best_result
        return classified, case_regex, control_regex, confidence, field_name

    def _try_field(self, samples: List[SampleInfo], field_name: str) -> Optional[Tuple[float, List[SampleInfo], str, str, str, str]]:
        """Try to classify samples using a specific metadata field."""
        texts = []
        for s in samples:
            if field_name == "title":
                texts.append(s.title.lower())
            elif field_name == "source":
                texts.append(s.source.lower())
            elif field_name == "characteristics":
                texts.append(s.characteristics.lower())
            else:
                texts.append("")

        if not any(texts):
            return None

        # Find the most discriminating keywords
        case_keywords = set()
        control_keywords = set()

        for text in texts:
            for pattern, ptype in CASE_CONTROL_PATTERNS:
                if ptype == "case_keyword":
                    for m in re.finditer(pattern, text, re.IGNORECASE):
                        case_keywords.add(m.group(0).lower())
                elif ptype == "control_keyword":
                    for m in re.finditer(pattern, text, re.IGNORECASE):
                        control_keywords.add(m.group(0).lower())

        if not case_keywords and not control_keywords:
            return None

        # Build regex patterns
        case_pattern = "|".join(sorted(case_keywords)) if case_keywords else ""
        control_pattern = "|".join(sorted(control_keywords)) if control_keywords else ""

        if not case_pattern and not control_pattern:
            return None

        # Classify each sample
        classified = []
        n_case = 0
        n_control = 0
        n_unknown = 0

        for s, text in zip(samples, texts):
            s_copy = SampleInfo(
                gsm_id=s.gsm_id, title=s.title,
                source=s.source, characteristics=s.characteristics
            )
            is_case = bool(case_pattern and re.search(case_pattern, text, re.IGNORECASE))
            is_control = bool(control_pattern and re.search(control_pattern, text, re.IGNORECASE))

            if is_case and not is_control:
                s_copy.group = "case"
                s_copy.confidence = "high"
                s_copy.matched_by = f"case:{case_pattern}"
                n_case += 1
            elif is_control and not is_case:
                s_copy.group = "control"
                s_copy.confidence = "high"
                s_copy.matched_by = f"control:{control_pattern}"
                n_control += 1
            elif is_case and is_control:
                # Both match — ambiguous, mark as unknown
                s_copy.group = "unknown"
                s_copy.confidence = "low"
                s_copy.matched_by = "ambiguous"
                n_unknown += 1
            else:
                s_copy.group = "unknown"
                s_copy.confidence = "low"
                n_unknown += 1

            classified.append(s_copy)

        if n_case == 0 or n_control == 0:
            return None

        # Score: higher is better
        total = len(classified)
        classified_ratio = (n_case + n_control) / total if total > 0 else 0
        balance = min(n_case, n_control) / max(n_case, n_control) if max(n_case, n_control) > 0 else 0
        score = classified_ratio * 0.6 + balance * 0.4

        # Determine confidence
        if classified_ratio >= 0.8 and n_unknown <= 2:
            confidence = "high"
        elif classified_ratio >= 0.5:
            confidence = "medium"
        else:
            confidence = "low"

        return (score, classified, case_pattern, control_pattern, confidence, field_name)

    def _parse_sample_string(self, s: str) -> List[dict]:
        """Parse 'GSMxxx Title\nGSMyyy Title' format."""
        samples = []
        for line in s.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                samples.append({"accession": parts[0].strip(), "title": parts[1].strip()})
            else:
                m = re.match(r"(GSM\d+)\s+(.+)", line)
                if m:
                    samples.append({"accession": m.group(1), "title": m.group(2)})
        return samples

    def _parse_soft(self, text: str) -> List[SampleInfo]:
        """Parse SOFT format text into sample info."""
        samples = []
        current = None
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("^SAMPLE"):
                if current:
                    samples.append(current)
                m = re.match(r"\^SAMPLE\s*=\s*(GSM\d+)", line)
                gsm_id = m.group(1) if m else ""
                current = SampleInfo(gsm_id=gsm_id, title="", source="", characteristics="")
            elif current:
                if line.startswith("!Sample_title"):
                    current.title = line.split("=", 1)[-1].strip()
                elif line.startswith("!Sample_source_name"):
                    current.source = line.split("=", 1)[-1].strip()
                elif line.startswith("!Sample_characteristics"):
                    val = line.split("=", 1)[-1].strip()
                    if current.characteristics:
                        current.characteristics += "; " + val
                    else:
                        current.characteristics = val
        if current:
            samples.append(current)
        return samples

    def _parse_series_matrix(self, text: str) -> List[SampleInfo]:
        """Parse series matrix header into sample info."""
        titles = {}
        sources = {}
        chars = {}

        for line in text.split("\n"):
            if line.startswith("!Sample_geo_accession"):
                parts = line.split("\t")
                gsm_ids = [p.strip().strip('"') for p in parts[1:] if p.strip()]
            elif line.startswith("!Sample_title"):
                parts = line.split("\t")
                for i, val in enumerate(parts[1:], 0):
                    titles[i] = val.strip().strip('"')
            elif line.startswith("!Sample_source_name"):
                parts = line.split("\t")
                for i, val in enumerate(parts[1:], 0):
                    sources[i] = val.strip().strip('"')
            elif line.startswith("!Sample_characteristics_ch1"):
                parts = line.split("\t")
                for i, val in enumerate(parts[1:], 0):
                    val = val.strip().strip('"')
                    if i in chars:
                        chars[i] += "; " + val
                    else:
                        chars[i] = val

        if "gsm_ids" not in dir():
            return []

        samples = []
        for i, gsm in enumerate(gsm_ids):
            samples.append(SampleInfo(
                gsm_id=gsm,
                title=titles.get(i, ""),
                source=sources.get(i, ""),
                characteristics=chars.get(i, ""),
            ))
        return samples


# ── Quality Scoring ────────────────────────────────────────────────────────

def score_gse(gse: GSECandidate) -> float:
    """Compute a quality score (0-100) for a GSE candidate."""
    score = 0.0

    # 1. Sample count (max 30 points)
    total = gse.case_count + gse.control_count
    if total >= 40:
        score += 30
    elif total >= 20:
        score += 25
    elif total >= 12:
        score += 20
    elif total >= 6:
        score += 10
    else:
        score += 0

    # 2. Balance (max 20 points) — how balanced are case/control
    if gse.case_count > 0 and gse.control_count > 0:
        balance = min(gse.case_count, gse.control_count) / max(gse.case_count, gse.control_count)
        score += balance * 20
    else:
        score += 0

    # 3. Platform quality (max 20 points)
    if gse.platform in GOOD_PLATFORMS:
        score += 20
    elif gse.platform.startswith("GPL"):
        score += 10
    else:
        score += 5

    # 4. Classification confidence (max 20 points)
    if gse.confidence == "high":
        score += 20
    elif gse.confidence == "medium":
        score += 12
    else:
        score += 5

    # 5. Recency bonus (max 10 points)
    try:
        year = int(gse.submission_date[:4])
        if year >= 2020:
            score += 10
        elif year >= 2015:
            score += 7
        elif year >= 2010:
            score += 4
        else:
            score += 1
    except (ValueError, IndexError):
        score += 0

    return round(score, 1)


def should_exclude(title: str, summary: str) -> str:
    """Check if a GSE should be excluded. Returns reason or empty string."""
    combined = (title + " " + summary).lower()
    for pattern in EXCLUDE_TITLE_PATTERNS:
        m = re.search(pattern, combined, re.IGNORECASE)
        if m:
            return f"excluded: {m.group(0)}"
    return ""


# ── Main Discovery Pipeline ───────────────────────────────────────────────

def discover_geo_datasets(
    disease: str,
    disease_key: str,
    ncbi: NCBIClient,
    max_search: int = 200,
    top_k: int = 5,
    min_samples: int = 6,
) -> Tuple[List[GSECandidate], List[GSECandidate]]:
    """
    Main discovery pipeline:
    1. Search GEO for disease
    2. Get summaries
    3. Filter by organism/type/size
    4. Auto-classify case/control
    5. Score and rank
    6. Return (all_candidates, selected_top_k)
    """
    classifier = SampleClassifier(disease)

    # Step 1: Search
    logging.info(f"[1/5] Searching GEO for '{disease}'...")
    gds_ids = ncbi.esearch_geo(disease, max_results=max_search)
    if not gds_ids:
        logging.warning(f"No GEO datasets found for '{disease}'")
        return [], []

    # Step 2: Get summaries
    logging.info(f"[2/5] Fetching summaries for {len(gds_ids)} records...")
    summaries = ncbi.esummary_gds(gds_ids)
    logging.info(f"  Got {len(summaries)} summaries")

    # Step 3: Process each GSE
    logging.info(f"[3/5] Processing candidates...")
    candidates: List[GSECandidate] = []
    seen_gse = set()

    for s in summaries:
        # Extract GSE accession
        gse_id = s.get("accession", "")
        if not gse_id.startswith("GSE"):
            # GDS records have GSE in the gpl field or need different extraction
            gse_id = s.get("gse", "")
            if not gse_id:
                # Try to extract from accession like "GDS1234"
                ftplink = s.get("ftplink", "")
                m = re.search(r"(GSE\d+)", ftplink)
                if m:
                    gse_id = m.group(1)
                else:
                    continue

        if gse_id in seen_gse:
            continue
        seen_gse.add(gse_id)

        title = s.get("title", "")
        summary_text = s.get("summary", "")
        organism = s.get("taxon", "")
        platform = s.get("gpl", "")
        sample_count = int(s.get("n_samples", 0) or 0)
        submission_date = s.get("pdat", s.get("suppdata", ""))

        # Get platform name
        platform_name = GOOD_PLATFORMS.get(platform, platform)

        # Filter: organism
        if organism and "homo sapiens" not in organism.lower():
            continue

        # Filter: minimum samples
        if sample_count < min_samples:
            continue

        # Filter: excluded titles
        exclude_reason = should_exclude(title, summary_text)

        gse = GSECandidate(
            gse_id=gse_id,
            title=title,
            summary=summary_text,
            platform=platform,
            platform_name=platform_name,
            organism=organism,
            sample_count=sample_count,
            submission_date=submission_date or "unknown",
            exclude_reason=exclude_reason,
        )

        if exclude_reason:
            candidates.append(gse)
            continue

        # Step 4: Classify samples
        samples, case_regex, control_regex, confidence, field_name = \
            classifier.classify_from_summary(s, disease)

        # If esummary didn't have enough info, try series matrix
        if confidence == "low" and not case_regex:
            logging.debug(f"  {gse_id}: trying series matrix header...")
            matrix_text = ncbi.fetch_series_matrix_header(gse_id)
            if matrix_text:
                samples2, case_regex2, control_regex2, conf2, field2 = \
                    classifier.classify_from_series_matrix(matrix_text)
                if conf2 in ("high", "medium") and case_regex2:
                    samples, case_regex, control_regex, confidence, field_name = \
                        samples2, case_regex2, control_regex2, conf2, field2

        gse.samples = samples
        gse.case_count = sum(1 for s in samples if s.group == "case")
        gse.control_count = sum(1 for s in samples if s.group == "control")
        gse.case_regex = case_regex
        gse.control_regex = control_regex
        gse.case_field = field_name
        gse.confidence = confidence

        # Score
        gse.quality_score = score_gse(gse)
        candidates.append(gse)

    # Step 5: Rank and select
    logging.info(f"[4/5] Scoring {len(candidates)} candidates...")
    valid = [c for c in candidates if not c.exclude_reason and c.case_count > 0 and c.control_count > 0]
    valid.sort(key=lambda x: x.quality_score, reverse=True)

    # Select top-K
    selected = valid[:top_k]
    for s in selected:
        s.include = True

    logging.info(f"[5/5] Selected {len(selected)} / {len(valid)} valid / {len(candidates)} total candidates")
    return candidates, selected


# ── Output Writers ─────────────────────────────────────────────────────────

def write_candidates_tsv(candidates: List[GSECandidate], path: Path):
    """Write all candidates to TSV."""
    with open(path, "w", encoding="utf-8") as f:
        f.write("gse_id\ttitle\tplatform\torganism\tsample_total\tcase_n\tcontrol_n\t"
                "confidence\tquality_score\tcase_regex\tcontrol_regex\t"
                "include\texclude_reason\n")
        for c in candidates:
            f.write(f"{c.gse_id}\t{c.title[:80]}\t{c.platform}\t{c.organism}\t"
                    f"{c.sample_count}\t{c.case_count}\t{c.control_count}\t"
                    f"{c.confidence}\t{c.quality_score}\t{c.case_regex}\t{c.control_regex}\t"
                    f"{'1' if c.include else '0'}\t{c.exclude_reason}\n")
    logging.info(f"Wrote {len(candidates)} candidates to {path}")


def write_selected_tsv(selected: List[GSECandidate], disease_key: str, path: Path):
    """Write selected GSEs to TSV (matches existing geo_curation format)."""
    with open(path, "w", encoding="utf-8") as f:
        f.write("disease_key\tgse_id\tplatform\tcase_n\tcontrol_n\t"
                "case_rule\tcontrol_rule\tstatus\tnote\n")
        for s in selected:
            note = f"{s.title[:60]}, score={s.quality_score}, conf={s.confidence}"
            f.write(f"{disease_key}\t{s.gse_id}\t{s.platform}\t"
                    f"{s.case_count}\t{s.control_count}\t"
                    f"{s.case_regex}\t{s.control_regex}\t"
                    f"auto_selected\t{note}\n")
    logging.info(f"Wrote {len(selected)} selected to {path}")


def write_dsmeta_yaml(selected: List[GSECandidate], disease_key: str, path: Path):
    """Generate a candidate dsmeta config YAML."""
    if not selected:
        logging.warning("No selected GSEs, skipping YAML generation")
        return

    # Build config
    config = {
        "project": {
            "name": f"{disease_key}_meta_signature",
            "outdir": f"outputs/{disease_key}",
            "workdir": f"work/{disease_key}",
            "seed": 13,
        },
        "geo": {
            "gse_list": [s.gse_id for s in selected],
            "prefer_series_matrix": True,
        },
        "labeling": {
            "mode": "regex",
            "regex_rules": {},
        },
        "de": {
            "method": "limma",
            "covariates": [],
            "qc": {
                "remove_outliers": True,
                "pca_outlier_z": 3.5,
            },
        },
        "probe_to_gene": {
            "enable": True,
            "skip_if_gene_symbols": True,
        },
        "meta": {
            "model": "random",
            "min_sign_concordance": 0.7,
            "flag_i2_above": 0.6,
            "top_n": 300,
        },
        "rank_aggregation": {
            "enable": True,
            "method": "rra",
            "ensemble": {
                "enable": True,
                "w_meta": 0.7,
                "w_rra": 0.3,
            },
        },
        "genesets": {
            "enable_reactome": True,
            "enable_wikipathways": True,
            "enable_kegg": False,
        },
        "gsea": {
            "method": "fgsea",
            "min_size": 15,
            "max_size": 500,
            "nperm": 10000,
        },
        "pathway_meta": {
            "method": "stouffer",
            "min_concordance": 0.7,
        },
        "report": {
            "enable": True,
        },
    }

    # Fill regex rules
    for s in selected:
        if s.case_regex and s.control_regex:
            config["labeling"]["regex_rules"][s.gse_id] = {
                "case": {"any": [s.case_regex]},
                "control": {"any": [s.control_regex]},
            }
        else:
            config["labeling"]["regex_rules"][s.gse_id] = {
                "case": {"any": ["TODO_FILL_CASE_PATTERN"]},
                "control": {"any": ["TODO_FILL_CONTROL_PATTERN"]},
            }

    # Write with comments
    header = (
        f"# AUTO-GENERATED by auto_discover_geo.py\n"
        f"# Disease: {disease_key}\n"
        f"# Date: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"# Selected {len(selected)} GSE datasets\n"
        f"#\n"
        f"# ⚠️  REVIEW BEFORE RUNNING:\n"
    )
    for s in selected:
        conf_marker = "✅" if s.confidence == "high" else ("⚠️" if s.confidence == "medium" else "❓")
        header += (
            f"#   {s.gse_id}: {conf_marker} confidence={s.confidence}, "
            f"case={s.case_count}, control={s.control_count}, "
            f"score={s.quality_score}\n"
        )
    header += f"#\n# After review, copy to: dsmeta_signature_pipeline/configs/{disease_key}.yaml\n\n"

    with open(path, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    logging.info(f"Wrote dsmeta config to {path}")


def write_discovery_log(candidates: List[GSECandidate], selected: List[GSECandidate],
                        disease: str, disease_key: str, path: Path):
    """Write human-readable discovery log."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"GEO Auto-Discovery Report\n")
        f.write(f"========================\n")
        f.write(f"Disease: {disease} (key: {disease_key})\n")
        f.write(f"Date: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Total candidates: {len(candidates)}\n")
        f.write(f"Valid (case+control detected): {sum(1 for c in candidates if c.case_count > 0 and c.control_count > 0 and not c.exclude_reason)}\n")
        f.write(f"Excluded: {sum(1 for c in candidates if c.exclude_reason)}\n")
        f.write(f"Selected: {len(selected)}\n\n")

        f.write(f"SELECTED DATASETS\n")
        f.write(f"-" * 60 + "\n")
        for i, s in enumerate(selected, 1):
            f.write(f"\n{i}. {s.gse_id} (score: {s.quality_score}, confidence: {s.confidence})\n")
            f.write(f"   Title: {s.title}\n")
            f.write(f"   Platform: {s.platform} ({s.platform_name})\n")
            f.write(f"   Samples: {s.sample_count} total, {s.case_count} case, {s.control_count} control\n")
            f.write(f"   Case regex: {s.case_regex}\n")
            f.write(f"   Control regex: {s.control_regex}\n")
            f.write(f"   Field matched: {s.case_field}\n")
            f.write(f"   Date: {s.submission_date}\n")

        f.write(f"\n\nEXCLUDED DATASETS\n")
        f.write(f"-" * 60 + "\n")
        excluded = [c for c in candidates if c.exclude_reason]
        for c in excluded[:20]:  # show first 20
            f.write(f"  {c.gse_id}: {c.exclude_reason} | {c.title[:60]}\n")

        f.write(f"\n\nALL VALID CANDIDATES (ranked by score)\n")
        f.write(f"-" * 60 + "\n")
        valid = sorted(
            [c for c in candidates if not c.exclude_reason and c.case_count > 0],
            key=lambda x: x.quality_score, reverse=True
        )
        for c in valid:
            marker = ">>>" if c.include else "   "
            f.write(f"{marker} {c.gse_id} score={c.quality_score:5.1f} "
                    f"conf={c.confidence:6s} "
                    f"case={c.case_count:3d} ctrl={c.control_count:3d} "
                    f"plt={c.platform:8s} | {c.title[:50]}\n")

    logging.info(f"Wrote discovery log to {path}")


# ── CLI ────────────────────────────────────────────────────────────────────

def disease_key_from_name(name: str) -> str:
    """Convert disease name to filesystem-safe key."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def run_single(disease: str, disease_key: str, out_dir: Path,
               ncbi: NCBIClient, top_k: int, min_samples: int,
               write_yaml: bool):
    """Run discovery for a single disease."""
    logging.info(f"\n{'='*60}")
    logging.info(f"Disease: {disease} (key: {disease_key})")
    logging.info(f"{'='*60}")

    disease_dir = out_dir / disease_key
    disease_dir.mkdir(parents=True, exist_ok=True)

    candidates, selected = discover_geo_datasets(
        disease=disease,
        disease_key=disease_key,
        ncbi=ncbi,
        max_search=200,
        top_k=top_k,
        min_samples=min_samples,
    )

    write_candidates_tsv(candidates, disease_dir / "candidates.tsv")
    write_selected_tsv(selected, disease_key, disease_dir / "selected.tsv")
    write_discovery_log(candidates, selected, disease, disease_key, disease_dir / "discovery_log.txt")

    if write_yaml and selected:
        write_dsmeta_yaml(selected, disease_key, disease_dir / "candidate_config.yaml")

    # Summary with route recommendation
    n_sel = len(selected)
    n_high = sum(1 for s in selected if s.confidence == "high")
    n_med = sum(1 for s in selected if s.confidence == "medium")
    n_low = sum(1 for s in selected if s.confidence == "low")
    logging.info(f"Result: {n_sel} selected (high={n_high}, med={n_med}, low={n_low})")

    # Route recommendation based on GSE count
    route = _recommend_route(n_sel, n_high)
    logging.info(f"Route recommendation: {route['label']}")
    logging.info(f"  → {route['action']}")

    # Write route recommendation to discovery_log
    route_path = disease_dir / "route_recommendation.txt"
    with open(route_path, "w", encoding="utf-8") as f:
        f.write(f"Disease: {disease} ({disease_key})\n")
        f.write(f"GSE found: {n_sel} (high={n_high}, med={n_med}, low={n_low})\n")
        f.write(f"Route: {route['label']}\n")
        f.write(f"Action: {route['action']}\n")
        f.write(f"Direction A (signature): {'YES' if route['dir_a'] else 'NO — skip, use Direction B only'}\n")
        f.write(f"Direction B (origin-only): YES (always available)\n")
        if route.get('warning'):
            f.write(f"Warning: {route['warning']}\n")

    return selected


def _recommend_route(n_selected: int, n_high_conf: int) -> dict:
    """Recommend pipeline route based on available GSE count and quality."""
    if n_selected == 0:
        return {
            "label": "DIRECTION_B_ONLY",
            "dir_a": False,
            "action": "Skip Direction A (no GEO data). Use Direction B (origin-only: CT.gov → KG → LLM) only.",
            "warning": "No suitable GEO expression datasets found. Disease signature cannot be computed.",
        }
    elif n_selected == 1:
        return {
            "label": "DIRECTION_A_LOW_CONFIDENCE",
            "dir_a": True,
            "action": "Direction A can run but will produce LOW confidence results (single-study, no cross-validation). "
                      "Consider supplementing with manual GEO search or using Direction B as primary.",
            "warning": "Only 1 GSE: meta-analysis degrades to single-study stats. "
                       "sign_concordance and I² heterogeneity are not available. "
                       "Signature reliability is limited.",
        }
    elif n_selected <= 3:
        if n_high_conf >= 2:
            return {
                "label": "DIRECTION_A_GOOD",
                "dir_a": True,
                "action": "Direction A is viable with good confidence. Meta-analysis and RRA will provide cross-study validation.",
            }
        else:
            return {
                "label": "DIRECTION_A_MODERATE",
                "dir_a": True,
                "action": "Direction A is viable but review case/control regex carefully. "
                          "Some datasets have medium/low classification confidence.",
                "warning": f"Only {n_high_conf} of {n_selected} GSE have high-confidence case/control detection.",
            }
    else:  # n_selected >= 4
        return {
            "label": "DIRECTION_A_IDEAL",
            "dir_a": True,
            "action": "Direction A has ideal coverage. Meta-analysis will be robust with strong cross-study validation.",
        }


def parse_disease_list(path: Path) -> List[Tuple[str, str]]:
    """Parse disease list file. Returns [(disease_key, disease_query), ...]."""
    diseases = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            key = parts[0].strip()
            query = parts[1].strip() if len(parts) > 1 else key.replace("_", " ")
            diseases.append((key, query))
    return diseases


def main():
    parser = argparse.ArgumentParser(
        description="Auto-discover GEO datasets for dsmeta pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--disease", type=str, help="Disease name (e.g. 'heart failure')")
    group.add_argument("--batch", type=str, help="Path to disease_list.txt for batch processing")

    parser.add_argument("--disease-key", type=str, help="Override disease key (default: auto from disease name)")
    parser.add_argument("--out-dir", type=str, default="geo_curation",
                        help="Output directory (default: geo_curation)")
    parser.add_argument("--top-k", type=int, default=5,
                        help="Select top-K datasets per disease (default: 5)")
    parser.add_argument("--min-samples", type=int, default=6,
                        help="Minimum total samples per GSE (default: 6)")
    parser.add_argument("--write-yaml", action="store_true",
                        help="Generate candidate dsmeta YAML config")
    parser.add_argument("--api-key", type=str, default=None,
                        help="NCBI API key (optional, increases rate limit)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable verbose logging")

    args = parser.parse_args()

    # Logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    ncbi = NCBIClient(api_key=args.api_key)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.disease:
        disease_key = args.disease_key or disease_key_from_name(args.disease)
        selected = run_single(
            disease=args.disease,
            disease_key=disease_key,
            out_dir=out_dir,
            ncbi=ncbi,
            top_k=args.top_k,
            min_samples=args.min_samples,
            write_yaml=args.write_yaml,
        )
        if not selected:
            logging.warning("No datasets selected. Try a broader disease term.")
            sys.exit(1)
    else:
        # Batch mode
        batch_path = Path(args.batch)
        if not batch_path.exists():
            logging.error(f"Batch file not found: {batch_path}")
            sys.exit(1)

        diseases = parse_disease_list(batch_path)
        logging.info(f"Batch mode: {len(diseases)} diseases from {batch_path}")

        summary = []
        for key, query in diseases:
            try:
                selected = run_single(
                    disease=query,
                    disease_key=key,
                    out_dir=out_dir,
                    ncbi=ncbi,
                    top_k=args.top_k,
                    min_samples=args.min_samples,
                    write_yaml=args.write_yaml,
                )
                n_sel = len(selected) if selected else 0
                n_hi = sum(1 for s in selected if s.confidence == "high") if selected else 0
                route = _recommend_route(n_sel, n_hi)
                summary.append((key, query, n_sel, "ok", route["label"]))
            except Exception as e:
                logging.error(f"Failed for {key}: {e}")
                summary.append((key, query, 0, str(e), "ERROR"))

        # Write batch summary
        summary_path = out_dir / "batch_summary.tsv"
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write("disease_key\tdisease_query\tselected_count\tstatus\troute_recommendation\n")
            for key, query, n, status, route in summary:
                f.write(f"{key}\t{query}\t{n}\t{status}\t{route}\n")
        logging.info(f"\nBatch summary: {summary_path}")

        # Categorized report
        dir_a_ideal = [(k,q,n) for k,q,n,s,r in summary if r == "DIRECTION_A_IDEAL"]
        dir_a_good = [(k,q,n) for k,q,n,s,r in summary if r == "DIRECTION_A_GOOD"]
        dir_a_mod = [(k,q,n) for k,q,n,s,r in summary if r == "DIRECTION_A_MODERATE"]
        dir_a_low = [(k,q,n) for k,q,n,s,r in summary if r == "DIRECTION_A_LOW_CONFIDENCE"]
        dir_b_only = [(k,q,n) for k,q,n,s,r in summary if r == "DIRECTION_B_ONLY"]
        errors = [(k,q,n) for k,q,n,s,r in summary if r == "ERROR"]

        logging.info(f"\n{'='*60}")
        logging.info(f"BATCH ROUTE SUMMARY")
        logging.info(f"{'='*60}")
        logging.info(f"  Direction A ready (≥4 GSE):     {len(dir_a_ideal)} diseases")
        logging.info(f"  Direction A good (2-3 GSE, hi):  {len(dir_a_good)} diseases")
        logging.info(f"  Direction A moderate (2-3 GSE):  {len(dir_a_mod)} diseases")
        logging.info(f"  Direction A low (1 GSE only):    {len(dir_a_low)} diseases")
        logging.info(f"  Direction B only (0 GSE):        {len(dir_b_only)} diseases")
        if errors:
            logging.info(f"  Errors:                          {len(errors)} diseases")
        logging.info(f"{'='*60}")

        if dir_b_only:
            logging.info(f"\nDiseases skipping Direction A (no GEO data — Direction B only):")
            for k, q, _ in dir_b_only:
                logging.info(f"  - {k} ({q})")

        if dir_a_low:
            logging.info(f"\nDiseases with only 1 GSE (low confidence, consider manual GEO search):")
            for k, q, n in dir_a_low:
                logging.info(f"  - {k} ({q}): {n} GSE")


if __name__ == "__main__":
    main()
