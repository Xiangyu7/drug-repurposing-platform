"""Gold-standard management for evidence extraction evaluation

Provides loading, validation, and bootstrapping of gold-standard annotation sets.
Gold-standard records are the ground truth for measuring extraction accuracy.
"""

import csv
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional, Dict, Any

from ..logger import get_logger

logger = get_logger(__name__)

VALID_DIRECTIONS = {"benefit", "harm", "neutral", "unclear", "unknown"}
VALID_MODELS = {"human", "animal", "cell", "computational", "unclear", "unknown"}
VALID_ENDPOINTS = {
    "PLAQUE_IMAGING", "CV_EVENTS", "PAD_FUNCTION", "BIOMARKER", "OTHER",
    # lowercase variants from step6 inline extraction
    "clinical trial primary outcome", "reduces monocyte adhesion to endothelial cells",
}
VALID_CONFIDENCES = {"HIGH", "MED", "LOW"}


@dataclass
class GoldStandardRecord:
    """Single gold-standard annotation for evidence extraction evaluation.

    Attributes:
        pmid: PubMed ID
        drug_name: Canonical drug name
        direction: Expected direction (benefit/harm/neutral/unclear)
        model: Expected model type (human/animal/cell/computational/unclear)
        endpoint: Expected endpoint category
        confidence: Annotation confidence (HIGH/MED/LOW)
        source: How this annotation was created (bootstrap/manual/expert)
        annotator: Who created this annotation
        notes: Optional notes
    """
    pmid: str
    drug_name: str
    direction: str
    model: str
    endpoint: str
    confidence: str = "MED"
    source: str = "bootstrap"
    annotator: str = "system"
    notes: str = ""

    def validate(self) -> List[str]:
        """Validate record fields, return list of issues (empty = valid)."""
        issues = []
        if not self.pmid or not self.pmid.strip():
            issues.append("pmid is empty")
        if not self.drug_name or not self.drug_name.strip():
            issues.append("drug_name is empty")
        if self.direction not in VALID_DIRECTIONS:
            issues.append(f"invalid direction: {self.direction}")
        if self.model not in VALID_MODELS:
            issues.append(f"invalid model: {self.model}")
        if self.confidence and self.confidence not in VALID_CONFIDENCES:
            issues.append(f"invalid confidence: {self.confidence}")
        return issues


def load_gold_standard(path: str) -> List[GoldStandardRecord]:
    """Load gold-standard annotations from CSV.

    Args:
        path: Path to CSV file with columns matching GoldStandardRecord fields.
              Required columns: pmid, drug_name, direction, model, endpoint

    Returns:
        List of validated GoldStandardRecord objects

    Raises:
        FileNotFoundError: If path does not exist
        ValueError: If required columns are missing
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Gold standard file not found: {path}")

    records = []
    invalid_count = 0

    with open(p, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError(f"Empty CSV file: {path}")

        required = {"pmid", "drug_name", "direction", "model", "endpoint"}
        missing = required - set(reader.fieldnames)
        if missing:
            raise ValueError(f"Missing required columns: {missing}")

        for i, row in enumerate(reader, 1):
            record = GoldStandardRecord(
                pmid=row.get("pmid", "").strip(),
                drug_name=row.get("drug_name", "").strip(),
                direction=row.get("direction", "").strip().lower(),
                model=row.get("model", "").strip().lower(),
                endpoint=row.get("endpoint", "").strip(),
                confidence=row.get("confidence", "MED").strip().upper(),
                source=row.get("source", "manual").strip(),
                annotator=row.get("annotator", "unknown").strip(),
                notes=row.get("notes", "").strip(),
            )

            issues = record.validate()
            if issues:
                logger.warning("Row %d has issues: %s", i, issues)
                invalid_count += 1
            else:
                records.append(record)

    logger.info(
        "Loaded %d gold-standard records (%d invalid skipped) from %s",
        len(records), invalid_count, path
    )
    return records


def save_gold_standard(records: List[GoldStandardRecord], path: str) -> None:
    """Save gold-standard records to CSV."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "pmid", "drug_name", "direction", "model", "endpoint",
        "confidence", "source", "annotator", "notes"
    ]

    with open(p, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            writer.writerow(asdict(record))

    logger.info("Saved %d gold-standard records to %s", len(records), path)


def bootstrap_from_dossiers(
    dossier_dir: str,
    min_confidence: float = 0.7
) -> List[GoldStandardRecord]:
    """Bootstrap gold-standard set from existing step6 dossier JSON files.

    Extracts evidence items with confidence >= min_confidence as initial
    annotation seeds. These should be manually reviewed before use.

    Args:
        dossier_dir: Path to directory containing dossier JSON files
        min_confidence: Minimum confidence threshold (0-1)

    Returns:
        List of GoldStandardRecord objects (unvalidated, need review)
    """
    dossier_path = Path(dossier_dir)
    if not dossier_path.exists():
        logger.warning("Dossier directory not found: %s", dossier_dir)
        return []

    records = []
    json_files = sorted(dossier_path.glob("*.json"))

    for jf in json_files:
        try:
            with open(jf, "r", encoding="utf-8") as f:
                dossier = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to read dossier %s: %s", jf.name, e)
            continue

        drug_name = dossier.get("canonical_name", "")
        if not drug_name:
            continue

        llm_data = dossier.get("llm_structured", {})

        # Extract from supporting_evidence
        for ev in llm_data.get("supporting_evidence", []):
            conf_val = ev.get("confidence", 0)
            if isinstance(conf_val, str):
                conf_val = {"HIGH": 0.9, "MED": 0.5, "LOW": 0.2}.get(conf_val.upper(), 0)
            if conf_val < min_confidence:
                continue

            pmid = str(ev.get("pmid", "")).strip()
            if not pmid:
                continue

            direction = str(ev.get("direction", "benefit")).lower()
            model = str(ev.get("model", "unknown")).lower()
            endpoint = str(ev.get("endpoint", "OTHER"))

            # Map numeric confidence to HIGH/MED/LOW
            if conf_val >= 0.8:
                conf_label = "HIGH"
            elif conf_val >= 0.5:
                conf_label = "MED"
            else:
                conf_label = "LOW"

            records.append(GoldStandardRecord(
                pmid=pmid,
                drug_name=drug_name,
                direction=direction,
                model=model,
                endpoint=endpoint,
                confidence=conf_label,
                source="bootstrap",
                annotator="system",
                notes=f"from {jf.name}, claim: {str(ev.get('claim', ''))[:100]}",
            ))

        # Extract from harm_or_neutral_evidence
        for ev in llm_data.get("harm_or_neutral_evidence", []):
            conf_val = ev.get("confidence", 0)
            if isinstance(conf_val, str):
                conf_val = {"HIGH": 0.9, "MED": 0.5, "LOW": 0.2}.get(conf_val.upper(), 0)
            if conf_val < min_confidence:
                continue

            pmid = str(ev.get("pmid", "")).strip()
            if not pmid:
                continue

            direction = str(ev.get("direction", "neutral")).lower()
            model = str(ev.get("model", "unknown")).lower()
            endpoint = str(ev.get("endpoint", "OTHER"))

            if conf_val >= 0.8:
                conf_label = "HIGH"
            elif conf_val >= 0.5:
                conf_label = "MED"
            else:
                conf_label = "LOW"

            records.append(GoldStandardRecord(
                pmid=pmid,
                drug_name=drug_name,
                direction=direction,
                model=model,
                endpoint=endpoint,
                confidence=conf_label,
                source="bootstrap",
                annotator="system",
                notes=f"from {jf.name}, harm/neutral",
            ))

    # Deduplicate by (pmid, drug_name)
    seen = set()
    unique = []
    for r in records:
        key = (r.pmid, r.drug_name)
        if key not in seen:
            seen.add(key)
            unique.append(r)

    logger.info(
        "Bootstrapped %d gold-standard records from %d dossiers (min_confidence=%.2f)",
        len(unique), len(json_files), min_confidence
    )
    return unique
