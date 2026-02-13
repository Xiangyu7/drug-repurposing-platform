"""LDP3 API client — industrial-grade with error classification and retry logic.

Improvements over v0.3.1:
    - Custom exception hierarchy: LDP3Error → LDP3RateLimitError, LDP3NotFoundError, LDP3ServerError
    - HTTP status code aware retry: 429 → wait Retry-After, 5xx → exponential backoff, 4xx → no retry
    - Response schema validation: checks for expected keys before returning
    - Input validation: prevents empty gene lists from hitting the API
    - Request/response logging with timing
    - Graceful degradation: empty results return instead of crashing on 404
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Any, List, Optional

import requests

logger = logging.getLogger("sigreverse.ldp3_client")


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class LDP3Error(Exception):
    """Base exception for LDP3 API errors."""
    def __init__(self, message: str, status_code: Optional[int] = None, url: str = ""):
        self.status_code = status_code
        self.url = url
        super().__init__(message)


class LDP3RateLimitError(LDP3Error):
    """429 Too Many Requests — caller should wait and retry."""
    pass


class LDP3NotFoundError(LDP3Error):
    """404 Not Found — resource does not exist, do not retry."""
    pass


class LDP3ServerError(LDP3Error):
    """5xx Server Error — transient, should retry with backoff."""
    pass


class LDP3ValidationError(LDP3Error):
    """Response does not match expected schema."""
    pass


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class LDP3Client:
    """Industrial-grade LDP3 API client with error classification and retry.

    Features:
        - Automatic retry with exponential backoff for 5xx and connection errors
        - Rate-limit aware: respects Retry-After header on 429
        - No retry for 4xx client errors (except 429)
        - Response schema validation
        - Input validation (prevents empty requests)
        - Request timing and structured logging
    """

    def __init__(
        self,
        metadata_api: str,
        data_api: str,
        timeout_sec: int = 120,
        retries: int = 3,
        backoff_sec: float = 2.0,
        max_backoff_sec: float = 60.0,
        user_agent: str = "sigreverse/0.4.0",
    ) -> None:
        self.metadata_api = metadata_api.rstrip("/") + "/"
        self.data_api = data_api.rstrip("/") + "/"
        self.timeout_sec = timeout_sec
        self.retries = retries
        self.backoff_sec = backoff_sec
        self.max_backoff_sec = max_backoff_sec
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})

        # Request statistics
        self._stats = {
            "total_requests": 0,
            "retries": 0,
            "rate_limited": 0,
            "errors_4xx": 0,
            "errors_5xx": 0,
            "cache_hits": 0,
        }

    @property
    def stats(self) -> Dict[str, int]:
        """Return copy of request statistics."""
        return dict(self._stats)

    def _post(self, url: str, json_payload: Dict[str, Any]) -> Any:
        """POST with classified error handling and retry logic.

        Retry policy:
            - 429 (rate limit): wait Retry-After header (or 2x backoff), retry
            - 5xx (server error): exponential backoff, retry
            - Connection/Timeout: exponential backoff, retry
            - 4xx (client error, not 429): raise immediately, no retry
            - JSON decode error: raise immediately

        Returns:
            Parsed JSON response.

        Raises:
            LDP3RateLimitError: Persistent 429 after all retries.
            LDP3NotFoundError: 404 response.
            LDP3ServerError: Persistent 5xx after all retries.
            LDP3Error: Other HTTP errors.
            RuntimeError: Exhausted retries due to connection/timeout.
        """
        self._stats["total_requests"] += 1
        last_err: Optional[Exception] = None

        for attempt in range(self.retries):
            t_start = time.time()
            try:
                logger.debug(f"POST {url} (attempt {attempt + 1}/{self.retries})")
                r = self.session.post(url, json=json_payload, timeout=self.timeout_sec)
                elapsed = time.time() - t_start

                # --- Success ---
                if r.ok:
                    try:
                        data = r.json()
                    except ValueError as e:
                        raise LDP3ValidationError(
                            f"Invalid JSON response from {url}: {e}",
                            status_code=r.status_code, url=url,
                        )
                    logger.debug(f"POST {url} → {r.status_code} ({elapsed:.2f}s)")
                    return data

                # --- Classify HTTP error ---
                status = r.status_code

                if status == 429:
                    # Rate limited — respect Retry-After header
                    self._stats["rate_limited"] += 1
                    retry_after = _parse_retry_after(r)
                    wait_time = retry_after if retry_after else self.backoff_sec * (2 ** attempt)
                    wait_time = min(wait_time, self.max_backoff_sec)
                    logger.warning(
                        f"Rate limited (429) on {url}. "
                        f"Waiting {wait_time:.1f}s (Retry-After: {retry_after})"
                    )
                    last_err = LDP3RateLimitError(
                        f"429 Too Many Requests: {url}", status_code=429, url=url
                    )
                    time.sleep(wait_time)
                    self._stats["retries"] += 1
                    continue

                elif status == 404:
                    # Not found — do NOT retry
                    self._stats["errors_4xx"] += 1
                    raise LDP3NotFoundError(
                        f"404 Not Found: {url} (response: {r.text[:200]})",
                        status_code=404, url=url,
                    )

                elif 400 <= status < 500:
                    # Other client errors — do NOT retry (bad request, auth, etc.)
                    self._stats["errors_4xx"] += 1
                    raise LDP3Error(
                        f"HTTP {status} Client Error: {url} → {r.text[:300]}",
                        status_code=status, url=url,
                    )

                elif status >= 500:
                    # Server error — retry with backoff
                    self._stats["errors_5xx"] += 1
                    wait_time = min(self.backoff_sec * (2 ** attempt), self.max_backoff_sec)
                    logger.warning(
                        f"Server error ({status}) on {url}: {r.text[:200]}. "
                        f"Retrying in {wait_time:.1f}s..."
                    )
                    last_err = LDP3ServerError(
                        f"HTTP {status}: {url}", status_code=status, url=url
                    )
                    time.sleep(wait_time)
                    self._stats["retries"] += 1
                    continue

                else:
                    # Unexpected status code
                    raise LDP3Error(
                        f"Unexpected HTTP {status}: {url} → {r.text[:200]}",
                        status_code=status, url=url,
                    )

            except (LDP3RateLimitError, LDP3ServerError) as e:
                # Retryable errors — already handled above with sleep+continue
                last_err = e

            except LDP3Error:
                raise  # Non-retryable errors (404, other 4xx) → raise immediately

            except requests.exceptions.ConnectionError as e:
                wait_time = min(self.backoff_sec * (2 ** attempt), self.max_backoff_sec)
                logger.warning(f"Connection error on {url}: {e}. Retrying in {wait_time:.1f}s...")
                last_err = e
                time.sleep(wait_time)
                self._stats["retries"] += 1

            except requests.exceptions.Timeout as e:
                wait_time = min(self.backoff_sec * (2 ** attempt), self.max_backoff_sec)
                logger.warning(
                    f"Timeout after {self.timeout_sec}s on {url}. "
                    f"Retrying in {wait_time:.1f}s..."
                )
                last_err = e
                time.sleep(wait_time)
                self._stats["retries"] += 1

        # Exhausted all retries
        if isinstance(last_err, LDP3Error):
            raise last_err
        raise RuntimeError(
            f"POST failed after {self.retries} retries: {url} | {last_err}"
        )

    # -------------------------------------------------------------------
    # Response validation helpers
    # -------------------------------------------------------------------

    @staticmethod
    def _validate_list_response(data: Any, url: str) -> List[Dict[str, Any]]:
        """Validate that response is a list of dicts."""
        if not isinstance(data, list):
            raise LDP3ValidationError(
                f"Expected list response from {url}, got {type(data).__name__}",
                url=url,
            )
        return data

    @staticmethod
    def _validate_enrichment_response(data: Any, url: str) -> Dict[str, Any]:
        """Validate enrichment response has 'results' key."""
        if not isinstance(data, dict):
            raise LDP3ValidationError(
                f"Expected dict response from {url}, got {type(data).__name__}",
                url=url,
            )
        if "results" not in data:
            raise LDP3ValidationError(
                f"Enrichment response missing 'results' key. Keys found: {list(data.keys())}",
                url=url,
            )
        if not isinstance(data["results"], list):
            raise LDP3ValidationError(
                f"'results' should be a list, got {type(data['results']).__name__}",
                url=url,
            )
        return data

    # -------------------------------------------------------------------
    # Public API methods
    # -------------------------------------------------------------------

    def entities_find_by_symbols(self, symbols: List[str]) -> List[Dict[str, Any]]:
        """Map gene symbols to LINCS entity UUIDs.

        Args:
            symbols: Non-empty list of gene symbol strings.

        Returns:
            List of entity dicts with 'id' and 'meta.symbol' fields.

        Raises:
            ValueError: If symbols list is empty.
            LDP3Error: On API errors.
        """
        if not symbols:
            raise ValueError("symbols list must not be empty")
        if not all(isinstance(s, str) and s.strip() for s in symbols):
            logger.warning("symbols list contains non-string or empty entries, filtering...")
            symbols = [s.strip() for s in symbols if isinstance(s, str) and s.strip()]
            if not symbols:
                raise ValueError("After filtering, symbols list is empty")

        logger.info(f"Querying entities for {len(symbols)} gene symbols...")
        payload = {
            "filter": {
                "where": {"meta.symbol": {"inq": symbols}},
                "fields": ["id", "meta.symbol"],
            }
        }
        url = self.metadata_api + "entities/find"
        result = self._post(url, payload)
        result = self._validate_list_response(result, url)

        logger.info(f"Found {len(result)} entities out of {len(symbols)} queried")
        return result

    def signatures_find_metadata(self, sig_uuids: List[str]) -> List[Dict[str, Any]]:
        """Fetch metadata for a list of signature UUIDs.

        Args:
            sig_uuids: Non-empty list of signature UUID strings.

        Returns:
            List of metadata dicts.

        Raises:
            ValueError: If sig_uuids is empty.
            LDP3Error: On API errors.
        """
        if not sig_uuids:
            raise ValueError("sig_uuids list must not be empty")

        logger.info(f"Fetching metadata for {len(sig_uuids)} signatures...")
        payload = {
            "filter": {
                "where": {"id": {"inq": sig_uuids}},
                "fields": [
                    "id",
                    "meta.pert_name",
                    "meta.cell_line",
                    "meta.pert_dose",
                    "meta.pert_dose_unit",
                    "meta.pert_time",
                    "meta.pert_time_unit",
                    "meta.pert_type",
                ],
            }
        }
        url = self.metadata_api + "signatures/find"
        result = self._post(url, payload)
        result = self._validate_list_response(result, url)

        logger.info(f"Retrieved metadata for {len(result)} signatures")
        return result

    def enrich_ranktwosided(
        self,
        up_entities: List[str],
        down_entities: List[str],
        limit: int = 500,
        database: str = "l1000_cp",
    ) -> Dict[str, Any]:
        """Run ranktwosided enrichment analysis.

        Args:
            up_entities: Non-empty list of entity UUIDs for upregulated genes.
            down_entities: Non-empty list of entity UUIDs for downregulated genes.
            limit: Maximum number of results to return.
            database: LINCS database to query.

        Returns:
            Dict with 'results' key containing list of enrichment results.

        Raises:
            ValueError: If entity lists are empty or limit < 1.
            LDP3Error: On API errors.
        """
        if not up_entities:
            raise ValueError("up_entities must not be empty")
        if not down_entities:
            raise ValueError("down_entities must not be empty")
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")

        logger.info(
            f"Running ranktwosided enrichment "
            f"(up={len(up_entities)}, down={len(down_entities)}, "
            f"limit={limit}, db={database})..."
        )
        payload = {
            "up_entities": up_entities,
            "down_entities": down_entities,
            "limit": int(limit),
            "database": database,
        }
        url = self.data_api + "enrich/ranktwosided"
        result = self._post(url, payload)
        result = self._validate_enrichment_response(result, url)

        n_results = len(result.get("results", []))
        logger.info(f"Enrichment returned {n_results} signatures")
        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_retry_after(response: requests.Response) -> Optional[float]:
    """Parse Retry-After header value (seconds).

    Returns:
        Seconds to wait, or None if header is missing/unparseable.
    """
    header = response.headers.get("Retry-After")
    if header is None:
        return None
    try:
        return float(header)
    except (ValueError, TypeError):
        return None
