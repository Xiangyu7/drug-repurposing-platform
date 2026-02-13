"""Unit tests for provenance manifest helpers."""

import json
from pathlib import Path

from src.dr.common.provenance import (
    collect_file_hashes,
    build_manifest,
    write_manifest,
)


class TestProvenance:
    def test_collect_file_hashes(self, tmp_path):
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_text("alpha", encoding="utf-8")
        b.write_text("beta", encoding="utf-8")

        hashed = collect_file_hashes([a, b, a, tmp_path / "missing.txt"])
        assert str(a) in hashed
        assert str(b) in hashed
        assert len(hashed) == 2

    def test_build_and_write_manifest(self, tmp_path):
        in_file = tmp_path / "input.csv"
        out_file = tmp_path / "output.csv"
        in_file.write_text("x\n1\n", encoding="utf-8")
        out_file.write_text("y\n2\n", encoding="utf-8")

        manifest = build_manifest(
            pipeline="unit_test_pipeline",
            repo_root=tmp_path,  # non-git path is allowed
            input_files=[in_file],
            output_files=[out_file],
            config={"mode": "test"},
            summary={"rows": 1},
            contracts={"dummy_schema": "1.0.0"},
        )
        assert manifest["pipeline"] == "unit_test_pipeline"
        assert manifest["config"]["mode"] == "test"
        assert manifest["summary"]["rows"] == 1
        assert str(in_file) in manifest["inputs"]["files"]
        assert str(out_file) in manifest["outputs"]["files"]

        manifest_path = tmp_path / "manifest.json"
        write_manifest(manifest_path, manifest)
        assert manifest_path.exists()
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert loaded["pipeline"] == "unit_test_pipeline"
