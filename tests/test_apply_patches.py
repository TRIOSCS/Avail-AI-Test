"""Tests for scripts/apply_patches.py — patch application.

Called by: pytest
Depends on: scripts/apply_patches.py
"""

import sys
from pathlib import Path
from unittest.mock import patch as mock_patch

# Import from scripts directory
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from apply_patches import apply_patch


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
