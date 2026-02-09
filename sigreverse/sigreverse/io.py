"""I/O module — industrial grade with input validation and error handling.

Improvements (v0.4.0):
    - File-not-found specific error messages
    - JSON parse error handling with actionable hints
    - Minimum gene count validation
    - Comprehensive signature schema validation
    - Safe CSV writing with encoding
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict, Any, List

logger = logging.getLogger("sigreverse.io")

# Minimum genes per direction for meaningful LINCS enrichment
MIN_GENES_PER_DIRECTION = 10
RECOMMENDED_GENES_PER_DIRECTION = 50


def read_disease_signature(path: str) -> Dict[str, Any]:
    """Read and validate a disease signature JSON file.

    Expected format:
        {
            "name": "disease_name",
            "up": ["GENE1", "GENE2", ...],
            "down": ["GENE3", "GENE4", ...],
            "meta": { ... }  // optional
        }

    Args:
        path: Path to the disease signature JSON file.

    Returns:
        Validated signature dict.

    Raises:
        FileNotFoundError: If the file does not exist.
        json.JSONDecodeError: If the file is not valid JSON.
        ValueError: If the signature schema is invalid.
    """
    path_obj = Path(path)

    # File existence check with helpful message
    if not path_obj.exists():
        raise FileNotFoundError(
            f"Disease signature file not found: {path}\n"
            f"  Expected location: {path_obj.resolve()}\n"
            f"  Hint: If using dsmeta pipeline output, look for "
            f"'sigreverse_input.json' in the signature/ directory."
        )

    if not path_obj.is_file():
        raise ValueError(f"Path exists but is not a file: {path}")

    # Parse JSON with helpful error
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise json.JSONDecodeError(
            f"Invalid JSON in {path}: {e.msg}\n"
            f"  Line {e.lineno}, column {e.colno}\n"
            f"  Hint: Check for trailing commas or unquoted strings.",
            e.doc, e.pos,
        )

    # Schema validation
    if not isinstance(data, dict):
        raise ValueError(
            f"Disease signature must be a JSON object (dict), got {type(data).__name__}"
        )

    if "up" not in data or "down" not in data:
        available_keys = list(data.keys())
        raise ValueError(
            f"Disease signature must contain keys: 'up' and 'down'.\n"
            f"  Found keys: {available_keys}\n"
            f"  Hint: If your file uses 'up_genes'/'down_genes', rename to 'up'/'down'."
        )

    if not isinstance(data["up"], list) or not isinstance(data["down"], list):
        raise ValueError(
            f"'up' and 'down' must be lists of gene symbols.\n"
            f"  up type: {type(data['up']).__name__}, down type: {type(data['down']).__name__}"
        )

    # Size warnings
    n_up = len(data["up"])
    n_down = len(data["down"])

    if n_up == 0 or n_down == 0:
        raise ValueError(
            f"Both 'up' and 'down' gene lists must be non-empty. "
            f"Got up={n_up}, down={n_down}."
        )

    if n_up < MIN_GENES_PER_DIRECTION or n_down < MIN_GENES_PER_DIRECTION:
        logger.warning(
            f"Very small signature: up={n_up}, down={n_down} genes. "
            f"Minimum recommended: {MIN_GENES_PER_DIRECTION} per direction. "
            f"Results will have high variance."
        )
    elif n_up < RECOMMENDED_GENES_PER_DIRECTION or n_down < RECOMMENDED_GENES_PER_DIRECTION:
        logger.info(
            f"Signature below optimal size: up={n_up}, down={n_down}. "
            f"Optimal: {RECOMMENDED_GENES_PER_DIRECTION}+ per direction."
        )

    return data


def sanitize_genes(
    genes: List[str],
    dedupe: bool = True,
    trim_topn: int | None = None,
) -> List[str]:
    """Clean and validate gene symbol list.

    Operations:
        1. Remove non-string entries
        2. Strip whitespace
        3. Remove empty strings
        4. Optionally deduplicate (preserving order)
        5. Optionally trim to top N

    Args:
        genes: Raw gene symbol list.
        dedupe: If True, remove duplicates preserving first occurrence.
        trim_topn: If set, keep only the first N genes.

    Returns:
        Cleaned gene symbol list.
    """
    out = []
    seen = set()
    n_dropped_nonstr = 0
    n_dropped_empty = 0
    n_dropped_dupe = 0

    for g in genes:
        if not isinstance(g, str):
            n_dropped_nonstr += 1
            continue
        gg = g.strip()
        if not gg:
            n_dropped_empty += 1
            continue
        if dedupe:
            if gg in seen:
                n_dropped_dupe += 1
                continue
            seen.add(gg)
        out.append(gg)

    if n_dropped_nonstr > 0:
        logger.warning(f"Dropped {n_dropped_nonstr} non-string entries from gene list")
    if n_dropped_empty > 0:
        logger.debug(f"Dropped {n_dropped_empty} empty/whitespace gene entries")
    if n_dropped_dupe > 0:
        logger.debug(f"Dropped {n_dropped_dupe} duplicate gene entries")

    if trim_topn is not None:
        n_before = len(out)
        out = out[: int(trim_topn)]
        if len(out) < n_before:
            logger.info(f"Trimmed gene list from {n_before} to {len(out)} (top_n={trim_topn})")

    return out


def ensure_dir(path: str) -> None:
    """Create directory and all parents if they don't exist."""
    os.makedirs(path, exist_ok=True)


def write_json(path: str, obj: Dict[str, Any]) -> None:
    """Write object as formatted JSON with UTF-8 encoding.

    Creates parent directories if needed.
    Handles NaN/Inf by converting to null (valid JSON).
    """
    ensure_dir(os.path.dirname(path) or ".")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, cls=_SafeJSONEncoder)
    logger.debug(f"Wrote JSON: {path}")


def write_csv(path: str, df) -> None:
    """Write DataFrame to CSV with UTF-8 encoding.

    Creates parent directories if needed.
    """
    ensure_dir(os.path.dirname(path) or ".")
    df.to_csv(path, index=False, encoding="utf-8")
    logger.debug(f"Wrote CSV: {path} ({len(df)} rows)")


class _SafeJSONEncoder(json.JSONEncoder):
    """JSON encoder that safely handles NaN, Inf, and numpy types.

    Standard json.dump outputs NaN/Inf as bare literals (invalid JSON).
    This encoder converts them to null.
    """

    def default(self, obj):
        import numpy as np
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            if np.isnan(obj) or np.isinf(obj):
                return None
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        return super().default(obj)

    def encode(self, o):
        """Override encode to handle Python float NaN/Inf."""
        return super().encode(_sanitize_for_json(o))

    def iterencode(self, o, _one_shot=False):
        """Override iterencode to handle Python float NaN/Inf."""
        return super().iterencode(_sanitize_for_json(o), _one_shot)


def _sanitize_for_json(obj):
    """Recursively replace NaN/Inf float values with None."""
    import math
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(v) for v in obj]
    return obj


def _json_default(obj):
    """JSON serialization fallback for numpy/pandas types."""
    import numpy as np
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        if np.isnan(obj) or np.isinf(obj):
            return None
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
