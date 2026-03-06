"""Tests for patch_generator — validates search string matching.

Called by: pytest
Depends on: app.services.patch_generator
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.services.patch_generator import generate_patches, validate_patches


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestValidatePatches:
    def test_valid_patches_pass(self, tmp_path):
        """Patches with exact search strings pass validation."""
        src = tmp_path / "app" / "routers" / "test.py"
        src.parent.mkdir(parents=True)
        src.write_text("def hello():\n    return 'world'\n")

        patches = [
            {
                "file": "app/routers/test.py",
                "search": "def hello():\n    return 'world'\n",
                "replace": "def hello():\n    return 'fixed'\n",
                "explanation": "Fix return value",
            }
        ]
        ok, errors = validate_patches(patches, source_dir=tmp_path)
        assert ok is True
        assert errors == []

    def test_search_string_not_found_fails(self, tmp_path):
        """Patches with non-matching search strings fail validation."""
        src = tmp_path / "app" / "routers" / "test.py"
        src.parent.mkdir(parents=True)
        src.write_text("def hello():\n    return 'world'\n")

        patches = [
            {
                "file": "app/routers/test.py",
                "search": "def goodbye():\n    return 'world'\n",
                "replace": "def goodbye():\n    return 'fixed'\n",
                "explanation": "Fix return value",
            }
        ]
        ok, errors = validate_patches(patches, source_dir=tmp_path)
        assert ok is False
        assert len(errors) == 1
        assert "not found" in errors[0].lower()

    def test_file_not_found_fails(self, tmp_path):
        """Patches targeting non-existent files fail validation."""
        patches = [
            {
                "file": "app/routers/nonexistent.py",
                "search": "anything",
                "replace": "something",
                "explanation": "Fix",
            }
        ]
        ok, errors = validate_patches(patches, source_dir=tmp_path)
        assert ok is False
        assert "not found" in errors[0].lower()

    def test_empty_patches_pass(self, tmp_path):
        """Empty patch list passes validation."""
        ok, errors = validate_patches([], source_dir=tmp_path)
        assert ok is True

    def test_multiple_patches_all_must_pass(self, tmp_path):
        """If any patch fails, entire validation fails."""
        src = tmp_path / "app" / "test.py"
        src.parent.mkdir(parents=True)
        src.write_text("line1\nline2\n")

        patches = [
            {"file": "app/test.py", "search": "line1", "replace": "fixed1", "explanation": "ok"},
            {"file": "app/test.py", "search": "MISSING", "replace": "fixed2", "explanation": "bad"},
        ]
        ok, errors = validate_patches(patches, source_dir=tmp_path)
        assert ok is False
        assert len(errors) == 1


class TestGeneratePatchesValidation:
    @patch("app.services.patch_generator.claude_structured", new_callable=AsyncMock)
    def test_invalid_patches_rejected(self, mock_claude, tmp_path):
        """generate_patches returns None when Claude produces non-matching search strings."""
        src = tmp_path / "app" / "test.py"
        src.parent.mkdir(parents=True)
        src.write_text("real content here\n")

        mock_claude.return_value = {
            "patches": [
                {
                    "file": "app/test.py",
                    "search": "WRONG CONTENT",
                    "replace": "fixed",
                    "explanation": "Bad match",
                }
            ],
            "summary": "Fix stuff",
        }

        with patch("app.services.patch_generator.SOURCE_DIR", tmp_path):
            result = _run(generate_patches(
                title="Test bug",
                diagnosis={"root_cause": "Bug", "fix_approach": "Fix it"},
                category="api",
                affected_files=["app/test.py"],
            ))
        assert result is None

    @patch("app.services.patch_generator.claude_structured", new_callable=AsyncMock)
    def test_valid_patches_returned(self, mock_claude, tmp_path):
        """generate_patches returns result when patches are valid."""
        src = tmp_path / "app" / "test.py"
        src.parent.mkdir(parents=True)
        src.write_text("buggy code\n")

        mock_claude.return_value = {
            "patches": [
                {
                    "file": "app/test.py",
                    "search": "buggy code",
                    "replace": "fixed code",
                    "explanation": "Fixed the bug",
                }
            ],
            "summary": "Fix stuff",
        }

        with patch("app.services.patch_generator.SOURCE_DIR", tmp_path):
            result = _run(generate_patches(
                title="Test bug",
                diagnosis={"root_cause": "Bug", "fix_approach": "Fix it"},
                category="api",
                affected_files=["app/test.py"],
            ))
        assert result is not None
        assert len(result["patches"]) == 1
