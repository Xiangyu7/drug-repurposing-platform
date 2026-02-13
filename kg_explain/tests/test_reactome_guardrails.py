import logging

import pandas as pd
import pytest
import requests

from kg_explain.cache import HTTPCache
from kg_explain.datasources import reactome


def _prepare_data(tmp_path, n_pairs: int):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    xref = pd.DataFrame([
        {"target_chembl_id": f"CHEMBL{i}", "uniprot_accession": f"U{i}"}
        for i in range(n_pairs)
    ])
    xref.to_csv(data_dir / "target_xref.csv", index=False)
    cache = HTTPCache(tmp_path / "cache", max_workers=1, ttl_seconds=0)
    return data_dir, cache


def test_reactome_small_sample_warn_only(tmp_path, monkeypatch, caplog):
    data_dir, cache = _prepare_data(tmp_path, 20)

    def _fake_fetch(_cache, uniprot):
        idx = int(uniprot[1:])
        if idx < 3:
            raise requests.Timeout("timeout")
        return [{"stId": f"R-{uniprot}", "displayName": f"PW-{uniprot}"}]

    monkeypatch.setattr(reactome, "_reactome_pathways_for_uniprot", _fake_fetch)

    with caplog.at_level(logging.WARNING):
        out = reactome.fetch_target_pathways(data_dir, cache)

    df = pd.read_csv(out, dtype=str)
    assert len(df) == 17
    assert any("Reactome 硬失败告警" in r.message for r in caplog.records)


def test_reactome_small_sample_fail(tmp_path, monkeypatch):
    data_dir, cache = _prepare_data(tmp_path, 20)

    def _fake_fetch(_cache, uniprot):
        idx = int(uniprot[1:])
        if idx < 8:
            raise requests.Timeout("timeout")
        return [{"stId": f"R-{uniprot}", "displayName": f"PW-{uniprot}"}]

    monkeypatch.setattr(reactome, "_reactome_pathways_for_uniprot", _fake_fetch)

    with pytest.raises(RuntimeError, match="硬失败率过高"):
        reactome.fetch_target_pathways(data_dir, cache)


def test_reactome_rate_mode_fail(tmp_path, monkeypatch):
    data_dir, cache = _prepare_data(tmp_path, 60)

    def _fake_fetch(_cache, uniprot):
        idx = int(uniprot[1:])
        if idx < 10:  # 10/60 = 16.7% > 15%
            raise requests.Timeout("timeout")
        return [{"stId": f"R-{uniprot}", "displayName": f"PW-{uniprot}"}]

    monkeypatch.setattr(reactome, "_reactome_pathways_for_uniprot", _fake_fetch)

    with pytest.raises(RuntimeError, match="硬失败率过高"):
        reactome.fetch_target_pathways(data_dir, cache)


def test_reactome_consecutive_failure_circuit_breaker(tmp_path, monkeypatch):
    data_dir, cache = _prepare_data(tmp_path, 60)

    def _fake_fetch(_cache, _uniprot):
        raise requests.Timeout("timeout")

    monkeypatch.setattr(reactome, "_reactome_pathways_for_uniprot", _fake_fetch)

    with pytest.raises(RuntimeError, match="连续硬失败触发熔断"):
        reactome.fetch_target_pathways(data_dir, cache)
