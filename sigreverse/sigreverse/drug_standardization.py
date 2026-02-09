"""Drug name standardization module.

Implements the drug identity resolution pipeline:
    1. LINCS pert_name / pert_id → PubChem CID (via PubChem PUG REST)
    2. PubChem CID → InChIKey (canonical chemical identifier)
    3. InChIKey → UniChem cross-references (ChEMBL, DrugBank, etc.)
    4. Synonym resolution (BRD-ID, generic name, brand name)
    5. Drug deduplication (merge aliases pointing to same InChIKey)

Why this matters:
    - LINCS uses internal pert_id (e.g., BRD-K12345678) and generic pert_name
    - Same drug appears with different names across experiments
    - Without standardization, the same compound is counted as multiple drugs
    - InChIKey is the universal chemical identifier across databases

API endpoints used:
    - PubChem PUG REST: https://pubchem.ncbi.nlm.nih.gov/rest/pug/
    - UniChem: https://www.ebi.ac.uk/unichem/rest/
    - ChEMBL: https://www.ebi.ac.uk/chembl/api/data/

References:
    - Chambers et al. 2013: UniChem unified chemical identifier
    - Kim et al. 2023: PubChem 2023 update
"""
from __future__ import annotations

import logging
import time
import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import requests
import pandas as pd

logger = logging.getLogger("sigreverse.drug_standardization")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class DrugIdentity:
    """Standardized drug identity with cross-database references.
    
    Attributes:
        original_name: Original pert_name from LINCS.
        canonical_name: Standardized canonical name.
        inchikey: InChIKey (27-character canonical identifier).
        pubchem_cid: PubChem Compound ID.
        chembl_id: ChEMBL compound ID (e.g., CHEMBL25).
        drugbank_id: DrugBank ID (e.g., DB00945).
        synonyms: Set of known aliases.
        source: How the identity was resolved.
    """
    original_name: str
    canonical_name: str = ""
    inchikey: str = ""
    pubchem_cid: Optional[int] = None
    chembl_id: str = ""
    drugbank_id: str = ""
    synonyms: Set[str] = field(default_factory=set)
    source: str = "unresolved"


# ---------------------------------------------------------------------------
# PubChem resolver
# ---------------------------------------------------------------------------

class PubChemResolver:
    """Resolve drug names via PubChem PUG REST API.
    
    PubChem PUG REST: https://pubchem.ncbi.nlm.nih.gov/rest/pug/
    Rate limit: ~5 requests/second (we add delay).
    """
    
    BASE_URL = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
    
    def __init__(self, delay_sec: float = 0.25, timeout: int = 30):
        self.delay_sec = delay_sec
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "sigreverse/0.2.0 (drug standardization)"
        })
        self._cache: Dict[str, Optional[dict]] = {}
    
    def name_to_cid(self, name: str) -> Optional[int]:
        """Resolve drug name to PubChem CID.
        
        Tries multiple search strategies:
            1. Exact name match
            2. Synonym search
        """
        cache_key = f"name2cid:{name.lower()}"
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            return cached.get("cid") if cached else None
        
        time.sleep(self.delay_sec)
        
        try:
            url = f"{self.BASE_URL}/compound/name/{requests.utils.quote(name)}/cids/JSON"
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                cids = data.get("IdentifierList", {}).get("CID", [])
                if cids:
                    cid = cids[0]
                    self._cache[cache_key] = {"cid": cid}
                    return cid
        except Exception as e:
            logger.debug(f"PubChem name lookup failed for '{name}': {e}")
        
        self._cache[cache_key] = None
        return None
    
    def cid_to_inchikey(self, cid: int) -> Optional[str]:
        """Get InChIKey for a PubChem CID."""
        cache_key = f"cid2inchikey:{cid}"
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            return cached.get("inchikey") if cached else None
        
        time.sleep(self.delay_sec)
        
        try:
            url = f"{self.BASE_URL}/compound/cid/{cid}/property/InChIKey/JSON"
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                props = data.get("PropertyTable", {}).get("Properties", [])
                if props and "InChIKey" in props[0]:
                    inchikey = props[0]["InChIKey"]
                    self._cache[cache_key] = {"inchikey": inchikey}
                    return inchikey
        except Exception as e:
            logger.debug(f"PubChem InChIKey lookup failed for CID {cid}: {e}")
        
        self._cache[cache_key] = None
        return None
    
    def cid_to_synonyms(self, cid: int, max_synonyms: int = 20) -> List[str]:
        """Get synonyms for a PubChem CID."""
        cache_key = f"cid2syn:{cid}"
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            return cached.get("synonyms", []) if cached else []
        
        time.sleep(self.delay_sec)
        
        try:
            url = f"{self.BASE_URL}/compound/cid/{cid}/synonyms/JSON"
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                info = data.get("InformationList", {}).get("Information", [])
                if info:
                    syns = info[0].get("Synonym", [])[:max_synonyms]
                    self._cache[cache_key] = {"synonyms": syns}
                    return syns
        except Exception as e:
            logger.debug(f"PubChem synonym lookup failed for CID {cid}: {e}")
        
        self._cache[cache_key] = None
        return []
    
    def resolve_drug(self, name: str) -> DrugIdentity:
        """Full resolution pipeline for a single drug name."""
        identity = DrugIdentity(original_name=name)
        
        # Step 1: Name → CID
        cid = self.name_to_cid(name)
        if cid is None:
            logger.debug(f"Could not resolve '{name}' via PubChem")
            return identity
        
        identity.pubchem_cid = cid
        identity.source = "pubchem"
        
        # Step 2: CID → InChIKey
        inchikey = self.cid_to_inchikey(cid)
        if inchikey:
            identity.inchikey = inchikey
        
        # Step 3: CID → synonyms
        synonyms = self.cid_to_synonyms(cid)
        identity.synonyms = set(synonyms)
        if synonyms:
            identity.canonical_name = synonyms[0]  # First synonym is usually canonical
        else:
            identity.canonical_name = name
        
        return identity


# ---------------------------------------------------------------------------
# UniChem cross-reference resolver
# ---------------------------------------------------------------------------

class UniChemResolver:
    """Cross-reference InChIKey to ChEMBL, DrugBank, etc. via UniChem API.
    
    UniChem source IDs:
        1 = ChEMBL
        2 = DrugBank
        22 = PubChem
    """
    
    BASE_URL = "https://www.ebi.ac.uk/unichem/rest"
    
    # Source ID mapping
    SOURCES = {
        "chembl": 1,
        "drugbank": 2,
        "pubchem": 22,
    }
    
    def __init__(self, delay_sec: float = 0.3, timeout: int = 30):
        self.delay_sec = delay_sec
        self.timeout = timeout
        self.session = requests.Session()
        self._cache: Dict[str, Optional[dict]] = {}
    
    def inchikey_to_xrefs(self, inchikey: str) -> Dict[str, str]:
        """Get cross-references for an InChIKey.
        
        Returns:
            Dict mapping source name → compound ID.
            e.g., {"chembl": "CHEMBL25", "drugbank": "DB00945"}
        """
        if not inchikey:
            return {}
        
        cache_key = f"xref:{inchikey}"
        if cache_key in self._cache:
            return self._cache[cache_key] or {}
        
        time.sleep(self.delay_sec)
        
        xrefs = {}
        try:
            # Use the first 14 characters (connectivity layer) for broader matching
            url = f"{self.BASE_URL}/inchikey/{inchikey}"
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                # UniChem returns list of {src_id, src_compound_id}
                for entry in data:
                    src_id = int(entry.get("src_id", 0))
                    compound_id = entry.get("src_compound_id", "")
                    
                    if src_id == self.SOURCES["chembl"]:
                        xrefs["chembl"] = compound_id
                    elif src_id == self.SOURCES["drugbank"]:
                        xrefs["drugbank"] = compound_id
                    elif src_id == self.SOURCES["pubchem"]:
                        xrefs["pubchem"] = compound_id
        except Exception as e:
            logger.debug(f"UniChem lookup failed for {inchikey}: {e}")
        
        self._cache[cache_key] = xrefs
        return xrefs


# ---------------------------------------------------------------------------
# Drug standardization pipeline
# ---------------------------------------------------------------------------

class DrugStandardizer:
    """Complete drug name standardization pipeline.
    
    Pipeline:
        1. Collect unique drug names from pert_name column
        2. Resolve each via PubChem → CID → InChIKey
        3. Cross-reference InChIKey via UniChem → ChEMBL, DrugBank
        4. Deduplicate: merge different names with same InChIKey
        5. Add standardized columns to DataFrame
    
    Usage:
        standardizer = DrugStandardizer()
        df_drug = standardizer.standardize_dataframe(df_drug, drug_col="drug")
        
        # Or batch resolve:
        identities = standardizer.resolve_batch(["aspirin", "ibuprofen", "BRD-K12345"])
    """
    
    def __init__(
        self,
        cache_path: Optional[str] = None,
        use_unichem: bool = True,
        pubchem_delay: float = 0.25,
        unichem_delay: float = 0.3,
    ):
        self.pubchem = PubChemResolver(delay_sec=pubchem_delay)
        self.unichem = UniChemResolver(delay_sec=unichem_delay) if use_unichem else None
        self.cache_path = cache_path
        self._identity_cache: Dict[str, DrugIdentity] = {}
        
        # Load persistent cache if available
        if cache_path and os.path.exists(cache_path):
            self._load_cache()
    
    def resolve_single(self, name: str) -> DrugIdentity:
        """Resolve a single drug name to standardized identity."""
        name_lower = name.strip().lower()
        
        if name_lower in self._identity_cache:
            return self._identity_cache[name_lower]
        
        # PubChem resolution
        identity = self.pubchem.resolve_drug(name)
        
        # UniChem cross-reference (if InChIKey was found)
        if self.unichem and identity.inchikey:
            xrefs = self.unichem.inchikey_to_xrefs(identity.inchikey)
            identity.chembl_id = xrefs.get("chembl", "")
            identity.drugbank_id = xrefs.get("drugbank", "")
            if xrefs:
                identity.source = "pubchem+unichem"
        
        self._identity_cache[name_lower] = identity
        return identity
    
    def resolve_batch(
        self,
        names: List[str],
        progress_interval: int = 10,
    ) -> Dict[str, DrugIdentity]:
        """Resolve a batch of drug names.
        
        Args:
            names: List of drug names to resolve.
            progress_interval: Log progress every N drugs.
        
        Returns:
            Dict mapping original name → DrugIdentity.
        """
        unique_names = list(dict.fromkeys(names))  # preserve order, remove dupes
        results = {}
        
        logger.info(f"Resolving {len(unique_names)} unique drug names...")
        
        for i, name in enumerate(unique_names):
            if (i + 1) % progress_interval == 0:
                logger.info(f"  Progress: {i+1}/{len(unique_names)}")
            
            identity = self.resolve_single(name)
            results[name] = identity
        
        # Summary
        resolved = sum(1 for d in results.values() if d.inchikey)
        logger.info(
            f"Drug resolution complete: {resolved}/{len(unique_names)} "
            f"resolved to InChIKey"
        )
        
        return results
    
    def standardize_dataframe(
        self,
        df: pd.DataFrame,
        drug_col: str = "drug",
    ) -> pd.DataFrame:
        """Add standardized drug identity columns to a DataFrame.
        
        Adds columns:
            - canonical_name: Standardized name
            - inchikey: InChIKey
            - pubchem_cid: PubChem CID
            - chembl_id: ChEMBL ID
            - drugbank_id: DrugBank ID
            - drug_source: Resolution source
        
        Args:
            df: DataFrame with drug column.
            drug_col: Column name containing drug names.
        
        Returns:
            DataFrame with additional standardization columns.
        """
        if drug_col not in df.columns:
            logger.warning(f"Column '{drug_col}' not found in DataFrame")
            return df
        
        names = df[drug_col].unique().tolist()
        identities = self.resolve_batch(names)
        
        # Map to columns
        df = df.copy()
        df["canonical_name"] = df[drug_col].map(
            lambda x: identities.get(x, DrugIdentity(original_name=x)).canonical_name
        )
        df["inchikey"] = df[drug_col].map(
            lambda x: identities.get(x, DrugIdentity(original_name=x)).inchikey
        )
        df["pubchem_cid"] = df[drug_col].map(
            lambda x: identities.get(x, DrugIdentity(original_name=x)).pubchem_cid
        )
        df["chembl_id"] = df[drug_col].map(
            lambda x: identities.get(x, DrugIdentity(original_name=x)).chembl_id
        )
        df["drugbank_id"] = df[drug_col].map(
            lambda x: identities.get(x, DrugIdentity(original_name=x)).drugbank_id
        )
        df["drug_source"] = df[drug_col].map(
            lambda x: identities.get(x, DrugIdentity(original_name=x)).source
        )
        
        return df
    
    def deduplicate_by_inchikey(
        self,
        df: pd.DataFrame,
        drug_col: str = "drug",
        score_col: str = "final_reversal_score",
    ) -> pd.DataFrame:
        """Merge drugs that map to the same InChIKey.
        
        When multiple pert_names map to the same compound (via InChIKey),
        keep the entry with the best (most negative) score and record aliases.
        
        Args:
            df: DataFrame with drug and InChIKey columns.
            drug_col: Drug name column.
            score_col: Score column for selecting best entry.
        
        Returns:
            Deduplicated DataFrame.
        """
        if "inchikey" not in df.columns:
            logger.warning("No 'inchikey' column found. Run standardize_dataframe first.")
            return df
        
        # Separate resolved and unresolved
        resolved = df[df["inchikey"].str.len() > 0].copy()
        unresolved = df[df["inchikey"].str.len() == 0].copy()
        
        if len(resolved) == 0:
            return df
        
        # Group by InChIKey, keep best score
        deduped_rows = []
        for inchikey, group in resolved.groupby("inchikey"):
            best_idx = group[score_col].idxmin()  # most negative = best
            best_row = group.loc[best_idx].copy()
            
            # Record all aliases
            aliases = group[drug_col].unique().tolist()
            best_row["drug_aliases"] = "; ".join(aliases)
            best_row["n_aliases"] = len(aliases)
            
            deduped_rows.append(best_row)
        
        df_deduped = pd.DataFrame(deduped_rows)
        df_result = pd.concat([df_deduped, unresolved], ignore_index=True)
        
        n_merged = len(resolved) - len(df_deduped)
        if n_merged > 0:
            logger.info(f"InChIKey deduplication: merged {n_merged} duplicate entries")
        
        return df_result.sort_values(score_col, ascending=True)
    
    def save_cache(self):
        """Persist resolution cache to disk."""
        if not self.cache_path:
            return
        
        cache_data = {}
        for name, identity in self._identity_cache.items():
            cache_data[name] = {
                "original_name": identity.original_name,
                "canonical_name": identity.canonical_name,
                "inchikey": identity.inchikey,
                "pubchem_cid": identity.pubchem_cid,
                "chembl_id": identity.chembl_id,
                "drugbank_id": identity.drugbank_id,
                "synonyms": list(identity.synonyms),
                "source": identity.source,
            }
        
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
        
        logger.info(f"Saved drug identity cache: {len(cache_data)} entries → {self.cache_path}")
    
    def _load_cache(self):
        """Load resolution cache from disk."""
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                cache_data = json.load(f)
            
            for name, data in cache_data.items():
                self._identity_cache[name] = DrugIdentity(
                    original_name=data["original_name"],
                    canonical_name=data.get("canonical_name", ""),
                    inchikey=data.get("inchikey", ""),
                    pubchem_cid=data.get("pubchem_cid"),
                    chembl_id=data.get("chembl_id", ""),
                    drugbank_id=data.get("drugbank_id", ""),
                    synonyms=set(data.get("synonyms", [])),
                    source=data.get("source", "cache"),
                )
            
            logger.info(f"Loaded drug identity cache: {len(self._identity_cache)} entries")
        except Exception as e:
            logger.warning(f"Failed to load drug identity cache: {e}")
