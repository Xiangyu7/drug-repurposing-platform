"""Unit tests for File I/O utilities"""

import pytest
import json
from pathlib import Path
from src.dr.common.file_io import read_json, write_json, write_text, is_empty


class TestFileIO:
    """Tests for file I/O utilities"""

    def test_read_write_json(self, temp_dir):
        """Test reading and writing JSON files"""
        test_file = temp_dir / "test.json"
        test_data = {
            "name": "test",
            "count": 42,
            "items": ["a", "b", "c"]
        }
        
        # Write
        write_json(test_file, test_data)
        
        # Read
        loaded = read_json(test_file)
        
        assert loaded == test_data
        assert loaded["name"] == "test"
        assert loaded["count"] == 42

    def test_read_json_missing_file(self, temp_dir):
        """Test read_json handles missing file"""
        missing_file = temp_dir / "missing.json"
        
        # Should either return None or raise FileNotFoundError
        try:
            result = read_json(missing_file)
            # If it returns something, check it's empty/None
            assert result is None or result == {}
        except FileNotFoundError:
            pass  # Also acceptable

    def test_write_text(self, temp_dir):
        """Test writing text files"""
        test_file = temp_dir / "test.txt"
        test_content = "Line 1\nLine 2\nLine 3"

        write_text(test_file, test_content)

        assert test_file.exists()
        loaded = test_file.read_text()
        assert loaded == test_content

    def test_is_empty_nonexistent_file(self, temp_dir):
        """Test is_empty returns True for nonexistent file"""
        missing_file = temp_dir / "missing.txt"
        assert is_empty(missing_file) is True

    def test_is_empty_empty_file(self, temp_dir):
        """Test is_empty returns True for empty file"""
        empty_file = temp_dir / "empty.txt"
        empty_file.write_text("")
        assert is_empty(empty_file) is True

    def test_is_empty_nonempty_file(self, temp_dir):
        """Test is_empty returns False for nonempty file"""
        nonempty_file = temp_dir / "data.txt"
        nonempty_file.write_text("content")
        assert is_empty(nonempty_file) is False

    def test_json_roundtrip_preserves_data(self, temp_dir):
        """Test JSON roundtrip preserves all data types"""
        test_file = temp_dir / "roundtrip.json"
        test_data = {
            "string": "text",
            "int": 42,
            "float": 3.14,
            "bool": True,
            "null": None,
            "list": [1, 2, 3],
            "nested": {"key": "value"}
        }
        
        write_json(test_file, test_data)
        loaded = read_json(test_file)
        
        assert loaded == test_data
        assert type(loaded["string"]) == str
        assert type(loaded["int"]) == int
        assert type(loaded["float"]) == float
        assert type(loaded["bool"]) == bool

    def test_write_json_creates_parent_dir(self, temp_dir):
        """Test write_json creates parent directories"""
        test_file = temp_dir / "subdir1" / "subdir2" / "test.json"
        test_data = {"test": "data"}

        write_json(test_file, test_data)

        assert test_file.exists()
        assert test_file.parent.exists()

    def test_json_with_nested_structure(self, temp_dir):
        """Test JSON handles deeply nested structures"""
        test_file = temp_dir / "nested.json"
        test_data = {
            "level1": {
                "level2": {
                    "level3": {
                        "value": "deep"
                    }
                }
            }
        }
        
        write_json(test_file, test_data)
        loaded = read_json(test_file)
        
        assert loaded["level1"]["level2"]["level3"]["value"] == "deep"

    def test_write_text_creates_parent_dir(self, temp_dir):
        """Test write_text creates parent directories"""
        test_file = temp_dir / "subdir" / "test.txt"

        write_text(test_file, "content")

        assert test_file.exists()
        assert test_file.parent.exists()
