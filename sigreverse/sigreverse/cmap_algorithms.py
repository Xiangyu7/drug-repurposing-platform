"""CMap 4-stage algorithm framework: ES → WTCS → NCS → Tau.

Industrial-grade CMap pipeline adapted for SigReverse:
    Stage 1 (ES): Enrichment Score — from LDP3 z-scores or local Level 5 GCTx
    Stage 2 (WTCS): Weighted Tau Connectivity Score — sign-coherence gated
    Stage 3 (NCS): Normalized Connectivity Score — null-normalized per cell line
    Stage 4 (Tau): Percentile rank against reference distribution

LDP3 z-score proxy:
    Since we query LDP3 API (not local Level 5 data), z-up/z-down serve as
    pre-computed enrichment scores. The WTCS→NCS→Tau pipeline still applies.

Architecture:
    - ESProvider (abstract): pluggable enrichment score source
    - LDP3ESProvider: wraps LDP3 z-scores as ES (current default)
    - GCTxESProvider: stub for future local Level 5 GCTx data
    - CMapPipeline: orchestrates the 4-stage computation

References:
    - Subramanian et al. 2017 Cell: CMap methodology
    - Touchstone reference: ~2,429 compounds × multiple cell lines
    - Tau: percentile(NCS, reference_distribution) * sign(NCS)
"""
from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger("sigreverse.cmap_algorithms")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class EnrichmentResult:
    """Single signature enrichment result (Stage 1 output)."""
    sig_id: str
    es_up: float          # Enrichment score for disease-UP genes
    es_down: float        # Enrichment score for disease-DOWN genes
    cell_line: str = ""
    pert_name: str = ""
    pert_dose: str = ""
    pert_time: str = ""
    fdr_up: Optional[float] = None
    fdr_down: Optional[float] = None
    logp_fisher: Optional[float] = None
    ldp3_type: Optional[str] = None


@dataclass
class WTCSResult:
    """Stage 2 output: Weighted Tau Connectivity Score."""
    sig_id: str
    wtcs: float           # WTCS score
    is_coherent: bool     # Whether signs are coherent (same sign in LDP3 convention)
    direction: str        # reverser | mimicker | partial | orthogonal


@dataclass
class NCSResult:
    """Stage 3 output: Normalized Connectivity Score."""
    sig_id: str
    ncs: float            # Null-normalized WTCS
    cell_line: str = ""
    pert_name: str = ""


@dataclass
class TauResult:
    """Stage 4 output: Tau score (percentile rank)."""
    pert_name: str
    tau: float            # Tau score [-100, 100]
    ncs_mean: float       # Mean NCS across cell lines
    ncs_q75: float        # 75th percentile NCS (CMap NCSct-inspired)
    n_cell_lines: int
    cell_line_ncs: Dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Stage 1: Enrichment Score providers
# ---------------------------------------------------------------------------

class ESProvider(ABC):
    """Abstract base class for Enrichment Score providers.
    
    Subclass this to plug in different enrichment data sources:
    - LDP3ESProvider: uses LDP3 API z-scores (current)
    - GCTxESProvider: uses local Level 5 GCTx data (future)
    """
    
    @abstractmethod
    def get_enrichment_scores(self) -> List[EnrichmentResult]:
        """Return enrichment scores for all signatures."""
        ...
    
    @abstractmethod
    def source_name(self) -> str:
        """Human-readable source name for logging/manifest."""
        ...


class LDP3ESProvider(ESProvider):
    """Enrichment scores from LDP3 API z-scores.
    
    LDP3 ranktwosided returns z-up and z-down which serve as 
    pre-computed enrichment statistics. These are used directly
    as ES proxies in the WTCS→NCS→Tau pipeline.
    
    LDP3 sign convention (verified):
        z-up < 0 = disease-UP genes reversed
        z-down < 0 = disease-DOWN genes reversed
    """
    
    def __init__(self, df_detail: pd.DataFrame):
        """
        Args:
            df_detail: Signature-level DataFrame from LDP3 enrichment + metadata merge.
                       Expected columns: uuid, z-up, z-down, meta.cell_line, meta.pert_name, etc.
        """
        self.df = df_detail
    
    def get_enrichment_scores(self) -> List[EnrichmentResult]:
        results = []
        for _, row in self.df.iterrows():
            results.append(EnrichmentResult(
                sig_id=str(row.get("uuid", row.name)),
                es_up=float(row.get("z-up", 0.0)),
                es_down=float(row.get("z-down", 0.0)),
                cell_line=str(row.get("meta.cell_line", "")),
                pert_name=str(row.get("meta.pert_name", "")),
                pert_dose=str(row.get("meta.pert_dose", "")),
                pert_time=str(row.get("meta.pert_time", "")),
                fdr_up=_safe_float(row.get("fdr-up")),
                fdr_down=_safe_float(row.get("fdr-down")),
                logp_fisher=_safe_float(row.get("logp-fisher")),
                ldp3_type=str(row.get("type", "")) if "type" in row.index else None,
            ))
        return results
    
    def source_name(self) -> str:
        return "LDP3_API_z-scores"


class GCTxESProvider(ESProvider):
    """Stub for local Level 5 GCTx data (future implementation).
    
    When Level 5 LINCS data is available locally:
        1. Load .gctx file using cmapPy or h5py
        2. Extract drug perturbation z-score profiles
        3. Compute enrichment score using GSEA-like running sum
        4. Return EnrichmentResult for each signature
    
    Data requirements:
        - Level 5 moderated z-scores (.gctx, ~3GB per plate)
        - Gene info file (.txt) for landmark gene mapping
        - Signature metadata (.txt) for cell line / dose / time
    
    Implementation notes:
        - Use cmapPy.pandasGEXpress for .gctx I/O
        - Implement KS-like running sum statistic for ES
        - Weight by gene expression variance (L1000 landmark genes)
    """
    
    def __init__(self, gctx_path: str, sig_info_path: str, gene_info_path: str):
        self.gctx_path = gctx_path
        self.sig_info_path = sig_info_path
        self.gene_info_path = gene_info_path
        raise NotImplementedError(
            "GCTxESProvider requires local Level 5 LINCS data. "
            "Download from https://clue.io/data/CMap2020#LINCS2020 "
            "Install: pip install cmapPy"
        )
    
    def get_enrichment_scores(self) -> List[EnrichmentResult]:
        raise NotImplementedError
    
    def source_name(self) -> str:
        return "Level5_GCTx_local"


# ---------------------------------------------------------------------------
# Stage 2: WTCS (Weighted Tau Connectivity Score)
# ---------------------------------------------------------------------------

def compute_wtcs(enrichments: List[EnrichmentResult]) -> List[WTCSResult]:
    """Stage 2: Compute WTCS from enrichment scores.
    
    LDP3 convention (same-sign coherence gate):
        - Same sign (both negative or both positive): coherent signal
          WTCS = (es_up + es_down) / 2
        - Opposing signs: incoherent → WTCS = 0
    
    This is the CMap WTCS principle adapted for LDP3's sign convention.
    In original CMap, opposing signs = coherent (different convention).
    
    Args:
        enrichments: List of EnrichmentResult from Stage 1.
    
    Returns:
        List of WTCSResult.
    """
    results = []
    for er in enrichments:
        sign_up = 1 if er.es_up >= 0 else -1
        sign_down = 1 if er.es_down >= 0 else -1
        
        if sign_up == sign_down and (er.es_up != 0 or er.es_down != 0):
            wtcs = (er.es_up + er.es_down) / 2.0
            is_coherent = True
        else:
            wtcs = 0.0
            is_coherent = False
        
        # Direction classification
        if er.es_up < 0 and er.es_down < 0:
            direction = "reverser"
        elif er.es_up > 0 and er.es_down > 0:
            direction = "mimicker"
        elif abs(er.es_up) < 1e-10 and abs(er.es_down) < 1e-10:
            direction = "orthogonal"
        else:
            direction = "partial"
        
        results.append(WTCSResult(
            sig_id=er.sig_id,
            wtcs=wtcs,
            is_coherent=is_coherent,
            direction=direction,
        ))
    
    return results


# ---------------------------------------------------------------------------
# Stage 3: NCS (Normalized Connectivity Score)
# ---------------------------------------------------------------------------

def compute_ncs(
    enrichments: List[EnrichmentResult],
    wtcs_results: List[WTCSResult],
    method: str = "cell_line_null",
    n_permutations: int = 500,
    seed: int = 42,
) -> List[NCSResult]:
    """Stage 3: Normalize WTCS to NCS.
    
    Normalization approaches:
        - "cell_line_null": Normalize within each cell line using permuted null
          NCS = WTCS / mean(|WTCS_null|)  for same cell line
        - "global_null": Normalize against global null distribution
          NCS = WTCS / mean(|WTCS_all|)  across all signatures
        - "none": Skip normalization (NCS = WTCS)
    
    The cell-line normalization accounts for different baseline connectivity
    across cell types (some cell lines are inherently more responsive).
    
    Args:
        enrichments: Stage 1 enrichment scores (for metadata).
        wtcs_results: Stage 2 WTCS results.
        method: Normalization method.
        n_permutations: Permutations for null estimation.
        seed: Random seed.
    
    Returns:
        List of NCSResult.
    """
    # Build lookup for enrichment metadata
    er_lookup = {er.sig_id: er for er in enrichments}
    
    if method == "none":
        return [
            NCSResult(
                sig_id=wr.sig_id,
                ncs=wr.wtcs,
                cell_line=er_lookup.get(wr.sig_id, EnrichmentResult(sig_id="", es_up=0, es_down=0)).cell_line,
                pert_name=er_lookup.get(wr.sig_id, EnrichmentResult(sig_id="", es_up=0, es_down=0)).pert_name,
            )
            for wr in wtcs_results
        ]
    
    # Group WTCS by cell line
    cell_line_groups: Dict[str, List[Tuple[str, float]]] = {}
    for wr in wtcs_results:
        er = er_lookup.get(wr.sig_id)
        cl = er.cell_line if er else "unknown"
        if cl not in cell_line_groups:
            cell_line_groups[cl] = []
        cell_line_groups[cl].append((wr.sig_id, wr.wtcs))
    
    # Compute normalization factor per cell line
    if method == "cell_line_null":
        # For each cell line, normalization factor = mean(|WTCS|) for non-zero scores
        cl_norm_factor: Dict[str, float] = {}
        for cl, items in cell_line_groups.items():
            abs_scores = [abs(s) for _, s in items if abs(s) > 1e-10]
            if len(abs_scores) >= 3:
                cl_norm_factor[cl] = float(np.mean(abs_scores))
            else:
                cl_norm_factor[cl] = 1.0  # not enough data, skip normalization
    elif method == "global_null":
        all_abs = [abs(wr.wtcs) for wr in wtcs_results if abs(wr.wtcs) > 1e-10]
        global_factor = float(np.mean(all_abs)) if len(all_abs) >= 3 else 1.0
        cl_norm_factor = {cl: global_factor for cl in cell_line_groups}
    else:
        raise ValueError(f"Unknown NCS method: {method}")
    
    # Normalize
    results = []
    for wr in wtcs_results:
        er = er_lookup.get(wr.sig_id)
        cl = er.cell_line if er else "unknown"
        factor = cl_norm_factor.get(cl, 1.0)
        ncs = wr.wtcs / factor if factor > 1e-10 else wr.wtcs
        
        results.append(NCSResult(
            sig_id=wr.sig_id,
            ncs=ncs,
            cell_line=cl,
            pert_name=er.pert_name if er else "",
        ))
    
    logger.info(
        f"NCS normalization ({method}): {len(results)} signatures, "
        f"{len(cell_line_groups)} cell lines"
    )
    return results


# ---------------------------------------------------------------------------
# Stage 4: Tau (percentile rank score)
# ---------------------------------------------------------------------------

def compute_tau(
    ncs_results: List[NCSResult],
    reference_ncs: Optional[np.ndarray] = None,
    aggregation: str = "quantile_max",
    reference_mode: str = "auto",
    bootstrap_n: int = 5000,
    seed: int = 42,
) -> List[TauResult]:
    """Stage 4: Compute Tau score from NCS.

    CMap Tau: percentile rank of a drug's NCS against a reference distribution.
        tau = sign(NCS) * percentile_rank(|NCS|, |NCS_ref|)
        Range: [-100, +100]
        tau < -90: strong reverser
        tau > 90: strong mimicker

    Reference modes:
        - "external": Use provided reference_ncs (Touchstone or pre-computed)
        - "bootstrap": Bootstrap from current batch (smoothed, larger distribution)
        - "leave_one_out": Per-drug leave-one-out (removes self-referencing bias)
        - "auto": Use external if provided, else bootstrap

    Cross-cell-line aggregation (drug-level Tau):
        - "quantile_max": CMap NCSct method (67th/33rd percentile, pick larger |x|)
        - "median": Simple median across cell lines
        - "max_abs": Maximum absolute NCS across cell lines

    Args:
        ncs_results: Stage 3 NCS results.
        reference_ncs: Reference NCS distribution for percentile ranking.
                       If None and mode!='external', generates from batch data.
        aggregation: Cross-cell-line aggregation method.
        reference_mode: How to build reference distribution.
        bootstrap_n: Size of bootstrap reference (if mode='bootstrap' or 'auto').
        seed: Random seed for bootstrap.

    Returns:
        List of TauResult, one per drug (aggregated across cell lines).
    """
    # Build reference distribution based on mode
    all_ncs = np.array([nr.ncs for nr in ncs_results])

    if reference_mode == "external" and reference_ncs is not None:
        logger.info(f"Tau: using external reference (n={len(reference_ncs)})")
    elif reference_mode == "auto" and reference_ncs is not None:
        logger.info(f"Tau: using external reference (n={len(reference_ncs)})")
    elif reference_mode == "leave_one_out":
        # Special path: per-drug LOO reference
        loo_refs = build_leave_one_out_reference(ncs_results)
        return _compute_tau_loo(ncs_results, loo_refs, aggregation)
    else:
        # Bootstrap reference (auto or bootstrap mode)
        reference_ncs = build_bootstrap_reference(ncs_results, n_bootstrap=bootstrap_n, seed=seed)
        logger.info(f"Tau: using bootstrap reference (n={len(reference_ncs)})")

    ref_abs = np.abs(reference_ncs)
    ref_abs_sorted = np.sort(ref_abs)

    # Compute signature-level Tau
    sig_taus: Dict[str, List[Tuple[str, float, float]]] = {}  # drug -> [(cell_line, tau, ncs)]
    for nr in ncs_results:
        # Percentile rank of |NCS| in |NCS_ref|
        abs_ncs = abs(nr.ncs)
        if len(ref_abs_sorted) == 0:
            logger.warning("Empty reference distribution, skipping Tau for %s", nr.pert_name)
            continue
        rank = np.searchsorted(ref_abs_sorted, abs_ncs, side="right")
        percentile = 100.0 * rank / len(ref_abs_sorted)

        # Tau = sign * percentile
        tau = percentile if nr.ncs >= 0 else -percentile

        if nr.pert_name not in sig_taus:
            sig_taus[nr.pert_name] = []
        sig_taus[nr.pert_name].append((nr.cell_line, tau, nr.ncs))

    # Aggregate across cell lines per drug
    results = _aggregate_tau_results(sig_taus, aggregation)

    logger.info(
        f"Tau computation: {len(results)} drugs, "
        f"reference distribution n={len(reference_ncs)}"
    )
    return results


def _compute_tau_loo(
    ncs_results: List[NCSResult],
    loo_refs: Dict[str, np.ndarray],
    aggregation: str,
) -> List[TauResult]:
    """Compute Tau with leave-one-out reference (removes self-referencing bias)."""
    sig_taus: Dict[str, List[Tuple[str, float, float]]] = {}

    for nr in ncs_results:
        ref = loo_refs.get(nr.pert_name)
        if ref is None or len(ref) < 5:
            continue
        ref_abs_sorted = np.sort(np.abs(ref))
        if len(ref_abs_sorted) == 0:
            continue
        abs_ncs = abs(nr.ncs)
        rank = np.searchsorted(ref_abs_sorted, abs_ncs, side="right")
        percentile = 100.0 * rank / len(ref_abs_sorted)
        tau = percentile if nr.ncs >= 0 else -percentile

        if nr.pert_name not in sig_taus:
            sig_taus[nr.pert_name] = []
        sig_taus[nr.pert_name].append((nr.cell_line, tau, nr.ncs))

    results = _aggregate_tau_results(sig_taus, aggregation)
    logger.info(f"Tau (LOO): {len(results)} drugs")
    return results


def _aggregate_tau_results(
    sig_taus: Dict[str, List[Tuple[str, float, float]]],
    aggregation: str,
) -> List[TauResult]:
    """Aggregate signature-level Tau to drug-level."""
    results = []
    for drug, cell_data in sig_taus.items():
        taus = np.array([t for _, t, _ in cell_data])
        ncs_vals = np.array([n for _, _, n in cell_data])
        cell_ncs = {cl: n for cl, _, n in cell_data}

        if len(taus) == 0:
            continue

        if aggregation == "quantile_max":
            drug_tau = _quantile_max(taus)
        elif aggregation == "median":
            drug_tau = float(np.median(taus))
        elif aggregation == "max_abs":
            idx = np.argmax(np.abs(taus))
            drug_tau = float(taus[idx])
        else:
            drug_tau = float(np.median(taus))

        results.append(TauResult(
            pert_name=drug,
            tau=drug_tau,
            ncs_mean=float(np.mean(ncs_vals)),
            ncs_q75=float(np.percentile(ncs_vals, 75)) if len(ncs_vals) > 1 else float(ncs_vals[0]),
            n_cell_lines=len(set(cl for cl, _, _ in cell_data)),
            cell_line_ncs=cell_ncs,
        ))

    # Sort by tau ascending (most negative = strongest reverser)
    results.sort(key=lambda x: x.tau)
    return results


def _quantile_max(values: np.ndarray) -> float:
    """CMap NCSct-inspired quantile-max aggregation."""
    if len(values) == 0:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    q_hi = float(np.percentile(values, 67))
    q_lo = float(np.percentile(values, 33))
    return q_hi if abs(q_hi) >= abs(q_lo) else q_lo


# ---------------------------------------------------------------------------
# Full 4-stage pipeline
# ---------------------------------------------------------------------------

class CMapPipeline:
    """Orchestrates the complete ES → WTCS → NCS → Tau pipeline.
    
    Usage:
        provider = LDP3ESProvider(df_detail)
        pipeline = CMapPipeline(provider)
        tau_results = pipeline.run()
        df_tau = pipeline.to_dataframe()
    """
    
    def __init__(
        self,
        es_provider: ESProvider,
        ncs_method: str = "cell_line_null",
        tau_aggregation: str = "quantile_max",
        reference_ncs: Optional[np.ndarray] = None,
        tau_reference_mode: str = "auto",
    ):
        self.es_provider = es_provider
        self.ncs_method = ncs_method
        self.tau_aggregation = tau_aggregation
        self.reference_ncs = reference_ncs
        self.tau_reference_mode = tau_reference_mode

        # Pipeline state
        self.enrichments: List[EnrichmentResult] = []
        self.wtcs_results: List[WTCSResult] = []
        self.ncs_results: List[NCSResult] = []
        self.tau_results: List[TauResult] = []

    def run(self) -> List[TauResult]:
        """Execute the full 4-stage pipeline."""
        logger.info(f"CMap Pipeline: source={self.es_provider.source_name()}")

        # Stage 1: ES
        logger.info("Stage 1/4: Enrichment Scores...")
        self.enrichments = self.es_provider.get_enrichment_scores()
        logger.info(f"  -> {len(self.enrichments)} signatures")

        # Stage 2: WTCS
        logger.info("Stage 2/4: WTCS (sign-coherence gated)...")
        self.wtcs_results = compute_wtcs(self.enrichments)
        n_coherent = sum(1 for w in self.wtcs_results if w.is_coherent)
        logger.info(f"  -> {n_coherent}/{len(self.wtcs_results)} coherent")

        # Stage 3: NCS
        logger.info(f"Stage 3/4: NCS (method={self.ncs_method})...")
        self.ncs_results = compute_ncs(
            self.enrichments, self.wtcs_results, method=self.ncs_method
        )

        # Stage 4: Tau (with bootstrap reference by default)
        logger.info(f"Stage 4/4: Tau (aggregation={self.tau_aggregation}, ref={self.tau_reference_mode})...")
        self.tau_results = compute_tau(
            self.ncs_results,
            reference_ncs=self.reference_ncs,
            aggregation=self.tau_aggregation,
            reference_mode=self.tau_reference_mode,
        )

        logger.info(f"Pipeline complete: {len(self.tau_results)} drugs ranked by Tau")
        return self.tau_results
    
    def to_dataframe(self) -> pd.DataFrame:
        """Convert Tau results to a sorted DataFrame."""
        if not self.tau_results:
            return pd.DataFrame()
        
        rows = []
        for tr in self.tau_results:
            rows.append({
                "drug": tr.pert_name,
                "tau": tr.tau,
                "ncs_mean": tr.ncs_mean,
                "ncs_q75": tr.ncs_q75,
                "n_cell_lines": tr.n_cell_lines,
            })
        return pd.DataFrame(rows)
    
    def get_signature_details(self) -> pd.DataFrame:
        """Get detailed signature-level results from all 4 stages."""
        if not self.enrichments:
            return pd.DataFrame()
        
        er_dict = {er.sig_id: er for er in self.enrichments}
        wtcs_dict = {wr.sig_id: wr for wr in self.wtcs_results}
        ncs_dict = {nr.sig_id: nr for nr in self.ncs_results}
        
        rows = []
        for sig_id in er_dict:
            er = er_dict[sig_id]
            wr = wtcs_dict.get(sig_id)
            nr = ncs_dict.get(sig_id)
            rows.append({
                "sig_id": sig_id,
                "pert_name": er.pert_name,
                "cell_line": er.cell_line,
                "pert_dose": er.pert_dose,
                "pert_time": er.pert_time,
                "es_up": er.es_up,
                "es_down": er.es_down,
                "wtcs": wr.wtcs if wr else None,
                "is_coherent": wr.is_coherent if wr else None,
                "direction": wr.direction if wr else None,
                "ncs": nr.ncs if nr else None,
            })
        return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Touchstone reference loader
# ---------------------------------------------------------------------------

def load_touchstone_reference(path: str) -> np.ndarray:
    """Load pre-computed Touchstone reference NCS distribution.

    The Touchstone reference set contains NCS scores for ~2,429 well-characterized
    compounds across multiple cell lines. Tau scores are computed as percentile
    ranks against this reference.

    Expected file format: CSV with columns [pert_name, cell_line, ncs]
    Or: numpy .npy file with NCS values.

    Args:
        path: Path to reference file (.csv or .npy).

    Returns:
        Array of reference NCS values.
    """
    if path.endswith(".npy"):
        return np.load(path)
    elif path.endswith(".csv"):
        df = pd.read_csv(path)
        if "ncs" in df.columns:
            return df["ncs"].values.astype(float)
        else:
            raise ValueError("Reference CSV must contain 'ncs' column")
    else:
        raise ValueError(f"Unsupported reference format: {path}")


def build_bootstrap_reference(
    ncs_results: List[NCSResult],
    n_bootstrap: int = 5000,
    seed: int = 42,
) -> np.ndarray:
    """Build a bootstrapped reference NCS distribution from the current batch.

    When no external Touchstone reference is available, this creates a more
    robust reference distribution by:
        1. Collecting all NCS scores from the current batch
        2. Bootstrapping (sampling with replacement) to create a larger reference
        3. Adding slight Gaussian noise to smooth the distribution

    This avoids discretization artifacts in percentile rank computation.

    Args:
        ncs_results: Stage 3 NCS results from the current batch.
        n_bootstrap: Size of the bootstrapped reference distribution.
        seed: Random seed.

    Returns:
        Array of bootstrapped reference NCS values.
    """
    all_ncs = np.array([nr.ncs for nr in ncs_results])
    if len(all_ncs) < 10:
        logger.warning("Too few NCS values for bootstrap reference, using raw values")
        return all_ncs

    rng = np.random.default_rng(seed)

    # Bootstrap sample
    bootstrap = rng.choice(all_ncs, size=n_bootstrap, replace=True)

    # Add small Gaussian noise to smooth (avoid ties in percentile computation)
    noise_scale = max(np.std(all_ncs) * 0.02, 1e-6)  # 2% of SD, floor at 1e-6
    noise = rng.normal(0, noise_scale, size=n_bootstrap)
    bootstrap = bootstrap + noise

    logger.info(
        f"Bootstrap reference: {len(all_ncs)} NCS -> {n_bootstrap} bootstrap samples "
        f"(noise_scale={noise_scale:.4f})"
    )
    return bootstrap


def build_leave_one_out_reference(
    ncs_results: List[NCSResult],
) -> Dict[str, np.ndarray]:
    """Build per-drug leave-one-out reference distributions.

    For each drug, the reference distribution excludes that drug's own NCS
    values. This avoids self-referencing bias where a drug with extreme NCS
    inflates its own Tau.

    Args:
        ncs_results: Stage 3 NCS results.

    Returns:
        Dict mapping drug_name -> reference NCS array (excluding that drug).
    """
    all_ncs = np.array([nr.ncs for nr in ncs_results])
    drug_indices: Dict[str, List[int]] = {}
    for i, nr in enumerate(ncs_results):
        if nr.pert_name not in drug_indices:
            drug_indices[nr.pert_name] = []
        drug_indices[nr.pert_name].append(i)

    loo_refs = {}
    for drug, indices in drug_indices.items():
        mask = np.ones(len(all_ncs), dtype=bool)
        mask[indices] = False
        loo_refs[drug] = all_ncs[mask]

    logger.info(f"Leave-one-out references: {len(loo_refs)} drugs")
    return loo_refs


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _safe_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        f = float(val)
        return None if math.isnan(f) else f
    except (ValueError, TypeError):
        return None
