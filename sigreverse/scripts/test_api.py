#!/usr/bin/env python3
"""API connectivity test for SigReverse.

Tests both ldp3.cloud and maayanlab.cloud/sigcom-lincs endpoints.
"""

from __future__ import annotations

import sys
import time
import requests

# Test URLs
ENDPOINTS = {
    "ldp3": {
        "metadata_api": "https://ldp3.cloud/metadata-api/",
        "data_api": "https://ldp3.cloud/data-api/api/v1/",
    },
    "maayanlab": {
        "metadata_api": "https://maayanlab.cloud/sigcom-lincs/metadata-api/",
        "data_api": "https://maayanlab.cloud/sigcom-lincs/data-api/api/v1/",
    },
}

# Test genes
TEST_GENES = ["IL1B", "TNF", "CCL2"]

TIMEOUT = 30


def test_metadata_api(base_url: str, name: str) -> dict | None:
    """Test entities/find endpoint."""
    url = base_url.rstrip("/") + "/entities/find"
    payload = {
        "filter": {
            "where": {"meta.symbol": {"inq": TEST_GENES}},
            "fields": ["id", "meta.symbol"],
        }
    }

    print(f"\n[{name}] Testing metadata API: {url}")
    try:
        start = time.time()
        r = requests.post(url, json=payload, timeout=TIMEOUT)
        elapsed = time.time() - start

        if r.status_code == 200:
            data = r.json()
            print(f"  ✓ Status: {r.status_code} | Time: {elapsed:.2f}s | Entities found: {len(data)}")
            if data:
                print(f"  ✓ Sample entity: {data[0]}")
            return {"status": "ok", "count": len(data), "data": data}
        else:
            print(f"  ✗ Status: {r.status_code} | Response: {r.text[:200]}")
            return None
    except requests.exceptions.ConnectionError as e:
        print(f"  ✗ Connection error: {e}")
        return None
    except requests.exceptions.Timeout:
        print(f"  ✗ Timeout after {TIMEOUT}s")
        return None
    except Exception as e:
        print(f"  ✗ Unexpected error: {e}")
        return None


def test_data_api(base_url: str, name: str, entity_uuids: list[str]) -> dict | None:
    """Test enrich/ranktwosided endpoint."""
    if not entity_uuids or len(entity_uuids) < 2:
        print(f"\n[{name}] Skipping data API test - need entity UUIDs from metadata API")
        return None

    url = base_url.rstrip("/") + "/enrich/ranktwosided"
    # Use first entity as up, rest as down
    payload = {
        "up_entities": entity_uuids[:1],
        "down_entities": entity_uuids[1:],
        "limit": 10,
        "database": "l1000_xpr",
    }

    print(f"\n[{name}] Testing data API: {url}")
    try:
        start = time.time()
        r = requests.post(url, json=payload, timeout=TIMEOUT)
        elapsed = time.time() - start

        if r.status_code == 200:
            data = r.json()
            results = data.get("results", [])
            print(f"  ✓ Status: {r.status_code} | Time: {elapsed:.2f}s | Signatures: {len(results)}")
            if results:
                sample = results[0]
                print(f"  ✓ Sample result keys: {list(sample.keys())}")
            return {"status": "ok", "count": len(results)}
        else:
            print(f"  ✗ Status: {r.status_code} | Response: {r.text[:200]}")
            return None
    except requests.exceptions.ConnectionError as e:
        print(f"  ✗ Connection error: {e}")
        return None
    except requests.exceptions.Timeout:
        print(f"  ✗ Timeout after {TIMEOUT}s")
        return None
    except Exception as e:
        print(f"  ✗ Unexpected error: {e}")
        return None


def main():
    print("=" * 60)
    print("SigReverse API Connectivity Test")
    print("=" * 60)

    results = {}

    for name, urls in ENDPOINTS.items():
        print(f"\n{'='*60}")
        print(f"Testing: {name}")
        print("=" * 60)

        # Test metadata API
        meta_result = test_metadata_api(urls["metadata_api"], name)

        # Test data API if metadata worked
        entity_uuids = []
        if meta_result and meta_result.get("data"):
            entity_uuids = [e["id"] for e in meta_result["data"] if "id" in e]

        data_result = test_data_api(urls["data_api"], name, entity_uuids)

        results[name] = {
            "metadata_api": meta_result is not None,
            "data_api": data_result is not None,
        }

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    working_endpoint = None
    for name, status in results.items():
        meta_ok = "✓" if status["metadata_api"] else "✗"
        data_ok = "✓" if status["data_api"] else "✗"
        print(f"  {name}: metadata={meta_ok}  data={data_ok}")

        if status["metadata_api"] and status["data_api"]:
            working_endpoint = name

    if working_endpoint:
        print(f"\n✓ Recommended endpoint: {working_endpoint}")
        print(f"  metadata_api: {ENDPOINTS[working_endpoint]['metadata_api']}")
        print(f"  data_api: {ENDPOINTS[working_endpoint]['data_api']}")
        return 0
    else:
        print("\n✗ No working endpoint found!")
        return 1


if __name__ == "__main__":
    sys.exit(main())
