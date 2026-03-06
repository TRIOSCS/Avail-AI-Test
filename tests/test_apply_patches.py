"""Tests for scripts/apply_patches.py — patch application with pre-flight validation.

Called by: pytest
Depends on: scripts/apply_patches.py
"""

import sys
from pathlib import Path
from unittest.mock import patch as mock_patch

import pytest

# Import from scripts directory
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from apply_patches import apply_patch, validate_all_patches


class TestValidateAllPatches:
    def test_all_valid(self, tmp_path):
        """All patches with matching search strings pass pre-flight."""
        src = tmp_path / "test.py"
        src.write_text("old code\nmore code\n")

        patches = [
            {"file": "test.py", "search": "old code", "replace": "new code", "explanation": "fix"},
        ]
        with mock_patch("apply_patches.PROJ_DIR", tmp_path):
            ok = validate_all_patches(patches)
        assert ok is True

    def test_one_invalid_fails_all(self, tmp_path):
        """If any patch search string doesn't match, pre-flight rejects all."""
        src = tmp_path / "test.py"
        src.write_text("old code\n")

        patches = [
            {"file": "test.py", "search": "old code", "replace": "new code", "explanation": "ok"},
            {"file": "test.py", "search": "MISSING", "replace": "new", "explanation": "bad"},
        ]
        with mock_patch("apply_patches.PROJ_DIR", tmp_path):
            ok = validate_all_patches(patches)
        assert ok is False

    def test_missing_file_fails(self, tmp_path):
        """Patch targeting non-existent file fails pre-flight."""
        patches = [
            {"file": "nope.py", "search": "x", "replace": "y", "explanation": "fix"},
        ]
        with mock_patch("apply_patches.PROJ_DIR", tmp_path):
            ok = validate_all_patches(patches)
        assert ok is False

    def test_empty_patches_pass(self, tmp_path):
        """Empty patch list passes pre-flight."""
        with mock_patch("apply_patches.PROJ_DIR", tmp_path):
            ok = validate_all_patches([])
        assert ok is True


class TestApplyPatch:
    def test_successful_apply(self, tmp_path):
        """Patch with matching search string applies correctly."""
        src = tmp_path / "test.py"
        src.write_text("def foo():\n    return 1\n")

        patch = {"file": "test.py", "search": "return 1", "replace": "return 2", "explanation": "fix"}
        with mock_patch("apply_patches.PROJ_DIR", tmp_path):
            result = apply_patch(patch, 0)
        assert result is True
        assert "return 2" in src.read_text()

    def test_search_not_found(self, tmp_path):
        """Patch with non-matching search string fails."""
        src = tmp_path / "test.py"
        src.write_text("def foo():\n    return 1\n")

        patch = {"file": "test.py", "search": "MISSING", "replace": "new", "explanation": "fix"}
        with mock_patch("apply_patches.PROJ_DIR", tmp_path):
            result = apply_patch(patch, 0)
        assert result is False
        assert "return 1" in src.read_text()  # unchanged
