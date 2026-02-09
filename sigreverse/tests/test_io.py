"""Unit tests for sigreverse.io module.

Tests cover:
    - Disease signature reading and validation
    - Gene symbol sanitization
    - File writing utilities
    - JSON serialization with numpy types
    - Error messages and edge cases
"""
import json
import os
import pytest
import tempfile
from pathlib import Path

from sigreverse.io import (
    read_disease_signature,
    sanitize_genes,
    ensure_dir,
    write_json,
    write_csv,
    _json_default,
)


# ===== Disease signature reading =====

class TestReadDiseaseSignature:
    def _write_sig(self, tmpdir, data):
        """Helper: write a JSON signature file and return its path."""
        path = os.path.join(tmpdir, "sig.json")
        with open(path, "w") as f:
            json.dump(data, f)
        return path

    def test_valid_signature(self, tmp_path):
        path = self._write_sig(tmp_path, {
            "name": "test_disease",
            "up": ["GENE1", "GENE2", "GENE3"],
            "down": ["GENE4", "GENE5"],
        })
        result = read_disease_signature(path)
        assert result["name"] == "test_disease"
        assert len(result["up"]) == 3
        assert len(result["down"]) == 2

    def test_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="not found"):
            read_disease_signature(str(tmp_path / "nonexistent.json"))

    def test_invalid_json(self, tmp_path):
        path = os.path.join(tmp_path, "bad.json")
        with open(path, "w") as f:
            f.write("{invalid json")
        with pytest.raises(json.JSONDecodeError):
            read_disease_signature(path)

    def test_missing_up_key(self, tmp_path):
        path = self._write_sig(tmp_path, {"down": ["A"], "name": "test"})
        with pytest.raises(ValueError, match="must contain keys.*'up' and 'down'"):
            read_disease_signature(path)

    def test_missing_down_key(self, tmp_path):
        path = self._write_sig(tmp_path, {"up": ["A"], "name": "test"})
        with pytest.raises(ValueError, match="must contain keys.*'up' and 'down'"):
            read_disease_signature(path)

    def test_up_not_list(self, tmp_path):
        path = self._write_sig(tmp_path, {"up": "GENE1", "down": ["A"]})
        with pytest.raises(ValueError, match="must be lists"):
            read_disease_signature(path)

    def test_empty_up(self, tmp_path):
        path = self._write_sig(tmp_path, {"up": [], "down": ["A"]})
        with pytest.raises(ValueError, match="non-empty"):
            read_disease_signature(path)

    def test_empty_down(self, tmp_path):
        path = self._write_sig(tmp_path, {"up": ["A"], "down": []})
        with pytest.raises(ValueError, match="non-empty"):
            read_disease_signature(path)

    def test_not_dict(self, tmp_path):
        path = self._write_sig(tmp_path, ["not", "a", "dict"])
        with pytest.raises(ValueError, match="must be a JSON object"):
            read_disease_signature(path)

    def test_with_metadata(self, tmp_path):
        path = self._write_sig(tmp_path, {
            "up": ["A", "B"],
            "down": ["C", "D"],
            "meta": {"source": "dsmeta"},
        })
        result = read_disease_signature(path)
        assert result["meta"]["source"] == "dsmeta"

    def test_directory_path_raises(self, tmp_path):
        with pytest.raises(ValueError, match="not a file"):
            read_disease_signature(str(tmp_path))


# ===== Gene sanitization =====

class TestSanitizeGenes:
    def test_basic(self):
        result = sanitize_genes(["BRCA1", "TP53", "EGFR"])
        assert result == ["BRCA1", "TP53", "EGFR"]

    def test_strips_whitespace(self):
        result = sanitize_genes(["  BRCA1 ", "TP53\n"])
        assert result == ["BRCA1", "TP53"]

    def test_removes_empty(self):
        result = sanitize_genes(["BRCA1", "", "  ", "TP53"])
        assert result == ["BRCA1", "TP53"]

    def test_removes_non_strings(self):
        result = sanitize_genes(["BRCA1", 123, None, "TP53"])
        assert result == ["BRCA1", "TP53"]

    def test_deduplication(self):
        result = sanitize_genes(["BRCA1", "TP53", "BRCA1", "TP53"])
        assert result == ["BRCA1", "TP53"]

    def test_no_dedup(self):
        result = sanitize_genes(["BRCA1", "BRCA1"], dedupe=False)
        assert result == ["BRCA1", "BRCA1"]

    def test_trim_topn(self):
        result = sanitize_genes(["A", "B", "C", "D", "E"], trim_topn=3)
        assert result == ["A", "B", "C"]

    def test_empty_input(self):
        assert sanitize_genes([]) == []

    def test_all_invalid(self):
        assert sanitize_genes([None, 123, ""]) == []


# ===== Write utilities =====

class TestWriteJson:
    def test_basic_write(self, tmp_path):
        path = str(tmp_path / "test.json")
        write_json(path, {"key": "value"})
        with open(path) as f:
            data = json.load(f)
        assert data["key"] == "value"

    def test_creates_parent_dirs(self, tmp_path):
        path = str(tmp_path / "deep" / "nested" / "test.json")
        write_json(path, {"ok": True})
        assert os.path.exists(path)

    def test_numpy_types(self, tmp_path):
        import numpy as np
        path = str(tmp_path / "np.json")
        write_json(path, {
            "int": np.int64(42),
            "float": np.float64(3.14),
            "array": np.array([1, 2, 3]),
        })
        with open(path) as f:
            data = json.load(f)
        assert data["int"] == 42
        assert abs(data["float"] - 3.14) < 0.01
        assert data["array"] == [1, 2, 3]

    def test_nan_serialized_as_null(self, tmp_path):
        import numpy as np
        path = str(tmp_path / "nan.json")
        write_json(path, {"val": np.float64("nan")})
        with open(path, "r") as f:
            data = json.load(f)
        assert data["val"] is None

    def test_python_nan_serialized_as_null(self, tmp_path):
        """Native Python float('nan') should also become null."""
        path = str(tmp_path / "pynan.json")
        write_json(path, {"val": float("nan"), "inf": float("inf")})
        with open(path, "r") as f:
            data = json.load(f)
        assert data["val"] is None
        assert data["inf"] is None


class TestWriteCsv:
    def test_basic_write(self, tmp_path):
        import pandas as pd
        path = str(tmp_path / "test.csv")
        df = pd.DataFrame({"a": [1, 2], "b": [3, 4]})
        write_csv(path, df)
        df_read = pd.read_csv(path)
        assert len(df_read) == 2
        assert list(df_read.columns) == ["a", "b"]


# ===== JSON default serializer =====

class TestJsonDefault:
    def test_numpy_int(self):
        import numpy as np
        assert _json_default(np.int64(42)) == 42

    def test_numpy_float(self):
        import numpy as np
        assert abs(_json_default(np.float64(3.14)) - 3.14) < 0.01

    def test_numpy_nan(self):
        import numpy as np
        assert _json_default(np.float64("nan")) is None

    def test_numpy_inf(self):
        import numpy as np
        assert _json_default(np.float64("inf")) is None

    def test_numpy_array(self):
        import numpy as np
        assert _json_default(np.array([1, 2, 3])) == [1, 2, 3]

    def test_unsupported_raises(self):
        with pytest.raises(TypeError):
            _json_default(set([1, 2, 3]))
