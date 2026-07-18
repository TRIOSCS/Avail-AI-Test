# tests/test_release_draft_workflow.py — guards .github/workflows/release-draft.yml.
# Called by: pytest suite (CI).
# Depends on: PyYAML, app/config.py APP_VERSION format, the workflow YAML itself.

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "release-draft.yml"
CONFIG_PATH = REPO_ROOT / "app" / "config.py"

# Python equivalent of the workflow's `grep -oP '^APP_VERSION = "\K...'`.
VERSION_RE = re.compile(r'^APP_VERSION = "([0-9]+(?:\.[0-9]+)*)"', re.MULTILINE)


def _load_workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text())


def _triggers(workflow: dict) -> dict:
    # PyYAML parses the bare `on:` key as boolean True (YAML 1.1).
    return workflow.get("on", workflow.get(True))


def test_workflow_file_exists_and_parses():
    workflow = _load_workflow()
    assert workflow["name"] == "Draft Release on Version Bump"


def test_workflow_triggers_on_config_change_and_manual_dispatch():
    triggers = _triggers(_load_workflow())
    assert triggers["push"]["branches"] == ["main"]
    assert "app/config.py" in triggers["push"]["paths"]
    assert "workflow_dispatch" in triggers


def test_workflow_has_contents_write_permission():
    workflow = _load_workflow()
    assert workflow["permissions"] == {"contents": "write"}


def test_workflow_creates_draft_not_published_release():
    script = _load_workflow()["jobs"]["draft-release"]["steps"][-1]["run"]
    assert "draft=true" in script
    assert "generate_release_notes=true" in script


def test_version_grep_pattern_matches_current_config():
    """If APP_VERSION's format in app/config.py changes, the workflow's grep silently
    extracts nothing — this test fails first."""
    match = VERSION_RE.search(CONFIG_PATH.read_text())
    assert match, "APP_VERSION line in app/config.py no longer matches the workflow's grep pattern"

    from app.config import APP_VERSION

    assert match.group(1) == APP_VERSION
