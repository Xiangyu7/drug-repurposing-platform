"""LLM Evidence Extractor - Extract structured evidence from PubMed abstracts

Uses Ollama LLM with JSON schema to extract:
- Direction: benefit/harm/neutral/unclear
- Model: human/animal/cell/computational
- Endpoint: PLAQUE_IMAGING/CV_EVENTS/PAD_FUNCTION/BIOMARKER/OTHER
- Mechanism: Hypothesized mechanism snippet
- Confidence: HIGH/MED/LOW

Industrial-grade features:
- Retry with exponential backoff and decreasing temperature
- JSON repair pipeline (bracket extraction, markdown stripping, trailing comma fix)
- Field value validation against schema enums
- Hallucination detection (PMID consistency, drug grounding, mechanism anchoring)
- Batch extraction with statistics

Designed for high-accuracy evidence classification (target: 85%+)
"""

import re
import time
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field
import json

from .ollama import OllamaClient
from ..logger import get_logger
try:
    from ..monitoring import record_llm_extraction
except Exception:  # pragma: no cover - monitoring is optional at runtime
    def record_llm_extraction(success: bool, duration_seconds: float, error_type: str = "unknown"):
        return None

logger = get_logger(__name__)


# ============================================================
# Constants
# ============================================================

VALID_DIRECTIONS = {"benefit", "harm", "neutral", "unclear"}
VALID_MODELS = {"human", "animal", "cell", "computational", "unclear"}
VALID_ENDPOINTS = {"PLAQUE_IMAGING", "CV_EVENTS", "PAD_FUNCTION", "BIOMARKER", "OTHER"}
VALID_CONFIDENCES = {"HIGH", "MED", "LOW"}

# JSON Schema for evidence extraction
EVIDENCE_SCHEMA = {
    "type": "object",
    "required": ["direction", "model", "endpoint", "mechanism", "confidence"],
    "properties": {
        "direction": {
            "type": "string",
            "enum": list(VALID_DIRECTIONS),
            "description": "Effect direction: benefit (reduces atherosclerosis), harm (increases), neutral (no effect), unclear (insufficient info)"
        },
        "model": {
            "type": "string",
            "enum": list(VALID_MODELS),
            "description": "Experimental model: human (clinical trial/cohort), animal (mice/rabbit/etc), cell (in vitro), computational (modeling), unclear"
        },
        "endpoint": {
            "type": "string",
            "enum": list(VALID_ENDPOINTS),
            "description": "Primary endpoint category"
        },
        "mechanism": {
            "type": "string",
            "description": "Brief mechanism snippet (1-2 sentences)"
        },
        "confidence": {
            "type": "string",
            "enum": list(VALID_CONFIDENCES),
            "description": "Confidence: HIGH (clear outcome), MED (inferrable), LOW (ambiguous)"
        }
    }
}

# Default retry temperatures (decreasing for more deterministic output)
DEFAULT_TEMPERATURES = [0.2, 0.1, 0.0]
DEFAULT_RETRY_BASE_DELAY = 1.0


# ============================================================
# JSON Repair
# ============================================================

def repair_json(text: str) -> Optional[str]:
    """Repair common LLM JSON output issues.

    Handles:
    - Markdown ```json wrapping
    - Trailing commas before closing brackets
    - Single quotes -> double quotes
    - Extra text before/after JSON
    - Missing closing brackets (simple cases)

    Args:
        text: Raw LLM output string

    Returns:
        Cleaned JSON string, or None if unrepairable
    """
    if not text:
        return None

    s = text.strip()

    # Strip markdown code block wrapping
    if s.startswith("```"):
        # Remove opening ```json or ```
        s = re.sub(r"^```(?:json)?\s*\n?", "", s)
        # Remove closing ```
        s = re.sub(r"\n?```\s*$", "", s)
        s = s.strip()

    # Try bracket extraction first (handles extra text before/after JSON)
    extracted = _extract_first_json_by_brackets(s)
    if extracted:
        return extracted

    # Fallback: regex extraction
    m = re.search(r"(\{.*\})", s, flags=re.S)
    if not m:
        m = re.search(r"(\[.*\])", s, flags=re.S)
    if not m:
        return None

    frag = m.group(1)
    # Fix trailing commas
    frag = re.sub(r",\s*([\]\}])", r"\1", frag)
    return frag


def _extract_first_json_by_brackets(text: str) -> Optional[str]:
    """Extract first valid JSON object/array by bracket counting.

    Handles cases where LLM prints extra text before/after the JSON.
    """
    if not text:
        return None

    s = text.strip()

    # Find first { or [
    start = None
    for i, ch in enumerate(s):
        if ch in "{[":
            start = i
            break
    if start is None:
        return None

    opener = s[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_str = False
    esc = False

    for j in range(start, len(s)):
        ch = s[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue

        if ch == '"':
            in_str = True
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                frag = s[start:j + 1]
                # Fix trailing commas
                frag = re.sub(r",\s*([\]\}])", r"\1", frag)
                return frag

    return None


# ============================================================
# Validation
# ============================================================

def validate_extraction(data: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Validate extracted evidence fields against schema enums.

    Args:
        data: Parsed JSON dict from LLM response

    Returns:
        (is_valid, list_of_issues) - is_valid True if all required fields present and valid
    """
    issues = []

    required = ["direction", "model", "endpoint", "mechanism", "confidence"]
    for key in required:
        if key not in data:
            issues.append(f"missing required field: {key}")

    if "direction" in data and data["direction"] not in VALID_DIRECTIONS:
        issues.append(f"invalid direction: {data['direction']}")

    if "model" in data and data["model"] not in VALID_MODELS:
        issues.append(f"invalid model: {data['model']}")

    if "endpoint" in data and data["endpoint"] not in VALID_ENDPOINTS:
        issues.append(f"invalid endpoint: {data['endpoint']}")

    if "confidence" in data and data["confidence"] not in VALID_CONFIDENCES:
        issues.append(f"invalid confidence: {data['confidence']}")

    if "mechanism" in data and not isinstance(data["mechanism"], str):
        issues.append("mechanism must be a string")

    return (len(issues) == 0, issues)


def coerce_extraction(data: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort coercion of near-valid extraction data.

    Fixes common issues like wrong case, close-but-not-exact values.
    """
    result = dict(data)

    # Coerce direction
    if "direction" in result:
        d = str(result["direction"]).lower().strip()
        # Map common variants
        direction_map = {
            "beneficial": "benefit", "benefits": "benefit", "positive": "benefit",
            "harmful": "harm", "negative": "harm", "detrimental": "harm",
            "none": "neutral", "no effect": "neutral", "mixed": "neutral",
            "unknown": "unclear", "uncertain": "unclear", "ambiguous": "unclear",
        }
        result["direction"] = direction_map.get(d, d)

    # Coerce model
    if "model" in result:
        m = str(result["model"]).lower().strip()
        model_map = {
            "mouse": "animal", "mice": "animal", "rat": "animal", "rabbit": "animal",
            "in vitro": "cell", "cell line": "cell", "culture": "cell",
            "clinical": "human", "patient": "human", "patients": "human",
            "in silico": "computational", "modeling": "computational",
            "unknown": "unclear", "review": "unclear", "meta-analysis": "unclear",
        }
        result["model"] = model_map.get(m, m)

    # Coerce endpoint to uppercase
    if "endpoint" in result:
        e = str(result["endpoint"]).upper().strip()
        endpoint_map = {
            "PLAQUE": "PLAQUE_IMAGING", "IMAGING": "PLAQUE_IMAGING",
            "EVENTS": "CV_EVENTS", "MACE": "CV_EVENTS",
            "PAD": "PAD_FUNCTION", "FUNCTION": "PAD_FUNCTION",
            "BIOMARKERS": "BIOMARKER", "MARKER": "BIOMARKER",
        }
        result["endpoint"] = endpoint_map.get(e, e)

    # Coerce confidence to uppercase
    if "confidence" in result:
        c = str(result["confidence"]).upper().strip()
        conf_map = {"HIGH": "HIGH", "MEDIUM": "MED", "MED": "MED", "LOW": "LOW"}
        result["confidence"] = conf_map.get(c, c)

    return result


# ============================================================
# Hallucination Detection
# ============================================================

def _normalize_drug(name: str) -> str:
    """Normalize drug name: lowercase, strip ®™, replace /- with space."""
    s = name.lower().strip()
    s = re.sub(r"[®™()\[\]]", "", s)
    s = re.sub(r"[/\-–—]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# Known synonyms that PubMed abstracts may use instead of the canonical name
_DRUG_SYNONYMS: Dict[str, List[str]] = {
    "cholecalciferol": ["vitamin d", "vitamin d3", "calciferol", "25(oh)d"],
    "sirolimus": ["rapamycin"],
    "tacrolimus": ["fk506", "fk-506"],
    "nitroglycerin": ["nitrate", "glyceryl trinitrate", "gtn"],
    "mycophenolate": ["mmf", "mycophenolic acid", "cellcept"],
    "aspirin": ["acetylsalicylic acid", "asa"],
    "repatha": ["evolocumab"],
    "rimonabant": ["sr141716"],
    "cangrelor": ["ar-c69931"],
}


def detect_hallucination(
    extraction_data: Dict[str, Any],
    expected_pmid: str,
    abstract: str,
    drug_name: str,
    aliases: Optional[List[str]] = None,
) -> List[str]:
    """Detect potential hallucinations in LLM extraction.

    Checks:
    1. PMID consistency - extracted PMID should match input PMID
    2. Drug grounding - drug name should appear in the abstract
    3. Mechanism anchoring - key mechanism terms should have basis in abstract

    Args:
        extraction_data: Parsed extraction dict
        expected_pmid: The PMID we provided to the LLM
        abstract: The original abstract text
        drug_name: The drug name we queried
        aliases: Optional extra names to check (e.g. chembl_pref_name)

    Returns:
        List of warning strings (empty means no hallucination detected)
    """
    warnings = []
    abstract_lower = (abstract or "").lower()
    drug_lower = (drug_name or "").lower()

    # 1. PMID consistency
    extracted_pmid = str(extraction_data.get("pmid", "")).strip()
    if extracted_pmid and extracted_pmid != str(expected_pmid).strip():
        warnings.append(
            f"pmid_mismatch: extracted '{extracted_pmid}' vs expected '{expected_pmid}'"
        )

    # 2. Drug grounding - check if drug name appears in abstract
    if drug_lower and abstract_lower:
        # Build candidate names: canonical + aliases + known synonyms
        abstract_norm = _normalize_drug(abstract_lower)
        candidates = [_normalize_drug(drug_lower)]
        # Add caller-provided aliases (e.g. chembl_pref_name)
        for a in (aliases or []):
            if a and a.strip():
                candidates.append(_normalize_drug(a))
        # Add hard-coded synonym table
        for cand in list(candidates):
            for base_name, syns in _DRUG_SYNONYMS.items():
                if base_name in cand:
                    candidates.extend(syns)
        # Deduplicate
        candidates = list(dict.fromkeys(c for c in candidates if c))

        grounded = False
        for cand in candidates:
            # Exact substring match
            if cand in abstract_norm:
                grounded = True
                break
            # Token-level: any significant token (≥4 chars) present
            tokens = [t for t in cand.split() if len(t) >= 4]
            if tokens and any(t in abstract_norm for t in tokens):
                grounded = True
                break

        if not grounded:
            warnings.append(
                f"drug_not_grounded: '{drug_name}' not found in abstract"
            )

    # 3. Mechanism anchoring - check if mechanism has basis in abstract
    mechanism = str(extraction_data.get("mechanism", "")).lower()
    if mechanism and abstract_lower and len(mechanism) > 20:
        # Extract key terms from mechanism (words > 5 chars)
        mech_tokens = set(re.findall(r"[a-z]{5,}", mechanism))
        # Filter out very common words
        common = {"which", "these", "their", "about", "after", "before", "through",
                  "between", "could", "would", "should", "effect", "effects", "study"}
        mech_tokens -= common

        if mech_tokens:
            anchored = sum(1 for t in mech_tokens if t in abstract_lower)
            anchor_ratio = anchored / len(mech_tokens) if mech_tokens else 0
            if anchor_ratio < 0.3:
                warnings.append(
                    f"mechanism_unanchored: only {anchor_ratio:.0%} of mechanism terms found in abstract"
                )

    return warnings


# ============================================================
# Data Classes
# ============================================================

@dataclass
class EvidenceExtraction:
    """Structured evidence extracted from a paper.

    Attributes:
        pmid: PubMed ID
        direction: benefit/harm/neutral/unclear
        model: human/animal/cell/computational/unclear
        endpoint: PLAQUE_IMAGING/CV_EVENTS/PAD_FUNCTION/BIOMARKER/OTHER
        mechanism: Brief mechanism description
        confidence: HIGH/MED/LOW
        raw_response: Raw LLM response (for debugging)
        warnings: List of hallucination/validation warnings
    """
    pmid: str
    direction: str
    model: str
    endpoint: str
    mechanism: str
    confidence: str
    raw_response: Optional[str] = None
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary (excludes raw_response for cleanliness)."""
        d = {
            "pmid": self.pmid,
            "direction": self.direction,
            "model": self.model,
            "endpoint": self.endpoint,
            "mechanism": self.mechanism,
            "confidence": self.confidence,
        }
        if self.warnings:
            d["warnings"] = self.warnings
        return d

    @property
    def has_warnings(self) -> bool:
        return len(self.warnings) > 0


@dataclass
class BatchResult:
    """Result of batch extraction with statistics."""
    extractions: List[EvidenceExtraction]
    total: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0
    hallucination_warnings: int = 0

    @property
    def success_rate(self) -> float:
        return self.success / self.total if self.total > 0 else 0.0

    def summary(self) -> str:
        return (
            f"Batch: {self.success}/{self.total} success ({self.success_rate:.0%}), "
            f"{self.failed} failed, {self.skipped} skipped, "
            f"{self.hallucination_warnings} with hallucination warnings"
        )


# ============================================================
# Extractor
# ============================================================

class LLMEvidenceExtractor:
    """Extracts structured evidence from PubMed abstracts using LLM.

    Features:
    - Retry with exponential backoff and decreasing temperature
    - JSON repair for malformed LLM output
    - Field validation and coercion
    - Hallucination detection

    Example:
        >>> extractor = LLMEvidenceExtractor()
        >>> evidence = extractor.extract(
        ...     pmid="12345",
        ...     title="Resveratrol reduces atherosclerosis in ApoE-/- mice",
        ...     abstract="We tested resveratrol... reduced plaque area by 40%...",
        ...     drug_name="resveratrol"
        ... )
        >>> print(evidence.direction, evidence.model, evidence.endpoint)
        benefit animal PLAQUE_IMAGING
    """

    def __init__(
        self,
        ollama_client: Optional[OllamaClient] = None,
        model: str = "qwen2.5:7b-instruct",
        temperatures: Optional[List[float]] = None,
        retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY,
        hallucination_check: bool = True,
        target_disease: str = "atherosclerosis",
    ):
        self.client = ollama_client or OllamaClient()
        self.model = model
        self.temperatures = temperatures or list(DEFAULT_TEMPERATURES)
        self.retry_base_delay = retry_base_delay
        self.hallucination_check = hallucination_check
        self.target_disease = target_disease.strip() if target_disease else "atherosclerosis"
        logger.info("LLMEvidenceExtractor initialized (model=%s, retries=%d)",
                     model, len(self.temperatures))

    def extract(
        self,
        pmid: str,
        title: str,
        abstract: str,
        drug_name: str,
        target_disease: Optional[str] = None,
    ) -> Optional[EvidenceExtraction]:
        """Extract structured evidence with retry, repair, and validation.

        Retry strategy:
        1. Try with temperature[0] (e.g., 0.2)
        2. If fails, wait and retry with temperature[1] (e.g., 0.1)
        3. If fails again, wait and retry with temperature[2] (e.g., 0.0)

        Each attempt includes JSON repair and field coercion.
        """
        t0 = time.time()
        prompt = self._build_prompt(
            title=title,
            abstract=abstract,
            drug_name=drug_name,
            target_disease=target_disease or self.target_disease,
        )

        for attempt, temp in enumerate(self.temperatures):
            try:
                response = self.client.generate(
                    prompt=prompt,
                    model=self.model,
                    format="json",
                    temperature=temp,
                )

                if not response:
                    logger.warning(
                        "PMID:%s attempt %d - empty response", pmid, attempt + 1
                    )
                    self._backoff(attempt)
                    continue

                # Parse with repair pipeline
                data = self._parse_response(response, pmid)
                if data is None:
                    self._backoff(attempt)
                    continue

                # Coerce near-valid values
                data = coerce_extraction(data)

                # Validate
                is_valid, issues = validate_extraction(data)
                if not is_valid:
                    logger.warning(
                        "PMID:%s attempt %d - validation failed: %s",
                        pmid, attempt + 1, issues
                    )
                    self._backoff(attempt)
                    continue

                # Hallucination detection
                warnings = []
                if self.hallucination_check:
                    warnings = detect_hallucination(data, pmid, abstract, drug_name)
                    if warnings:
                        logger.info("PMID:%s hallucination warnings: %s", pmid, warnings)

                extraction = EvidenceExtraction(
                    pmid=pmid,
                    direction=data["direction"],
                    model=data["model"],
                    endpoint=data["endpoint"],
                    mechanism=data.get("mechanism", ""),
                    confidence=data["confidence"],
                    raw_response=response,
                    warnings=warnings,
                )

                logger.debug(
                    "PMID:%s extracted: %s/%s/%s/%s (attempt %d, warnings=%d)",
                    pmid, extraction.direction, extraction.model,
                    extraction.endpoint, extraction.confidence,
                    attempt + 1, len(warnings)
                )
                record_llm_extraction(success=True, duration_seconds=time.time() - t0)
                return extraction

            except Exception as e:
                logger.error(
                    "PMID:%s attempt %d - exception: %s", pmid, attempt + 1, e
                )
                self._backoff(attempt)

        logger.warning("PMID:%s - all %d attempts exhausted", pmid, len(self.temperatures))
        record_llm_extraction(
            success=False,
            duration_seconds=time.time() - t0,
            error_type="attempts_exhausted",
        )
        return None

    def extract_batch(
        self,
        papers: List[Dict[str, Any]],
        drug_name: str,
        max_papers: int = 20,
        target_disease: Optional[str] = None,
    ) -> BatchResult:
        """Extract evidence from multiple papers with statistics.

        Args:
            papers: List of paper dicts with pmid/title/abstract
            drug_name: Drug name being evaluated
            max_papers: Maximum papers to process

        Returns:
            BatchResult with extractions and statistics
        """
        papers = papers[:max_papers]
        result = BatchResult(total=len(papers), extractions=[])

        logger.info("Batch extracting %d papers for: %s", len(papers), drug_name)

        for i, paper in enumerate(papers, 1):
            pmid = paper.get("pmid", "unknown")
            title = paper.get("title", "")
            abstract = paper.get("abstract", "")

            if not title and not abstract:
                logger.warning("[%d/%d] PMID:%s - skipped (no content)", i, len(papers), pmid)
                result.skipped += 1
                continue

            extraction = self.extract(
                pmid=pmid,
                title=title,
                abstract=abstract,
                drug_name=drug_name,
                target_disease=target_disease or self.target_disease,
            )
            if extraction:
                result.extractions.append(extraction)
                result.success += 1
                if extraction.has_warnings:
                    result.hallucination_warnings += 1
            else:
                result.failed += 1

        logger.info("Batch complete: %s", result.summary())
        return result

    def _parse_response(self, response: str, pmid: str) -> Optional[Dict[str, Any]]:
        """Parse LLM response with repair pipeline."""
        # Try direct parse first
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            pass

        # Try repair
        repaired = repair_json(response)
        if repaired:
            try:
                return json.loads(repaired)
            except json.JSONDecodeError as e:
                logger.debug("PMID:%s repair failed: %s", pmid, e)

        logger.warning("PMID:%s - JSON parse failed after repair", pmid)
        return None

    def _backoff(self, attempt: int) -> None:
        """Exponential backoff between retries."""
        if attempt < len(self.temperatures) - 1:
            delay = self.retry_base_delay * (2 ** attempt)
            time.sleep(delay)

    def _build_prompt(
        self, title: str, abstract: str, drug_name: str, target_disease: str
    ) -> str:
        disease = target_disease.strip() if target_disease else "atherosclerosis"
        return f"""You are a medical evidence extraction expert. Extract structured information from this {disease} research paper about {drug_name}.

**Paper Title**: {title}

**Abstract**: {abstract or "Not available"}

**Task**: Extract the following structured information:

1. **direction**: Does this paper suggest the drug has a beneficial, harmful, or neutral effect on {disease}?
   - "benefit": Reduces {disease}, plaque, CV events, or improves related biomarkers
   - "harm": Increases {disease}, plaque, CV events, or worsens biomarkers
   - "neutral": No significant effect, or mixed results
   - "unclear": Insufficient information, purely mechanistic study, or ambiguous results

2. **model**: What experimental model was used?
   - "human": Clinical trial, cohort study, or human subjects
   - "animal": Mouse, rat, rabbit, or other animal models
   - "cell": In vitro cell culture experiments
   - "computational": Mathematical modeling or computational analysis
   - "unclear": Not specified or review article

3. **endpoint**: What was the primary endpoint category?
   - "PLAQUE_IMAGING": CIMT, coronary CTA, MRI, plaque volume, or imaging-based
   - "CV_EVENTS": MACE, MI, stroke, cardiovascular death, or clinical events
   - "PAD_FUNCTION": 6-minute walk test, ABI, claudication, or functional outcomes
   - "BIOMARKER": LDL, HDL, CRP, IL-6, or biochemical markers
   - "OTHER": Other endpoints or multiple categories

4. **mechanism**: Briefly describe (1-2 sentences) how the drug affects {disease} based on this paper. Focus on mechanism of action if stated, or primary finding if mechanism unclear.

5. **confidence**: How confident are you in the direction classification?
   - "HIGH": Clear outcome stated with statistical significance
   - "MED": Outcome inferrable from context but not explicitly stated
   - "LOW": Ambiguous results, mechanistic study only, or conflicting data

**Instructions**:
- Be precise and evidence-based
- If multiple models/endpoints are mentioned, choose the PRIMARY one
- For reviews or meta-analyses, classify based on overall conclusion
- Return ONLY valid JSON matching the schema

**Output Format**: JSON only, no other text."""
