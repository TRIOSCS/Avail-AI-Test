"""Tests for patch_generator service — Claude-powered search/replace patch generation.

Covers: patch generation, API failure handling, file context inclusion, schema validation.
"""

from unittest.mock import AsyncMock, patch, mock_open
import pytest

from app.services.patch_generator import generate_patches, PATCH_SCHEMA


MOCK_PATCHES_RESPONSE = {
    "patches": [
        {
            "file": "app/routers/crm.py",
            "search": "return {'error': detail}",
            "replace": 'return {"error": detail, "status_code": 400}',
            "explanation": "Add missing status_code to error response",
        }
    ],
    "summary": "Fixed missing status_code in CRM error response",
}


@pytest.mark.asyncio
async def test_generates_patch_list():
    """Mock claude_structured to return valid patches, verify structure."""
    with patch(
        "app.services.patch_generator.claude_structured",
        new_callable=AsyncMock,
        return_value=MOCK_PATCHES_RESPONSE,
    ), patch("builtins.open", mock_open(read_data="def handler():\n    return {'error': detail}\n")):
        result = await generate_patches(
            title="Missing status_code in error response",
            diagnosis={"root_cause": "Error response lacks status_code", "fix_approach": "Add status_code field"},
            category="api",
            affected_files=["app/routers/crm.py"],
        )

    assert result is not None
    assert "patches" in result
    assert "summary" in result
    assert len(result["patches"]) == 1
    patch_item = result["patches"][0]
    assert "file" in patch_item
    assert "search" in patch_item
    assert "replace" in patch_item
    assert "explanation" in patch_item


@pytest.mark.asyncio
async def test_returns_none_on_api_failure():
    """When claude_structured returns None, generate_patches returns None."""
    with patch(
        "app.services.patch_generator.claude_structured",
        new_callable=AsyncMock,
        return_value=None,
    ), patch("builtins.open", mock_open(read_data="some code")):
        result = await generate_patches(
            title="Some bug",
            diagnosis={"root_cause": "Unknown"},
            category="other",
            affected_files=["app/main.py"],
        )

    assert result is None


@pytest.mark.asyncio
async def test_reads_file_contents_for_context():
    """Verify the prompt sent to Claude includes the file path."""
    captured_prompt = {}

    async def capture_claude(prompt, schema, **kwargs):
        captured_prompt["prompt"] = prompt
        return MOCK_PATCHES_RESPONSE

    with patch(
        "app.services.patch_generator.claude_structured",
        side_effect=capture_claude,
    ), patch("builtins.open", mock_open(read_data="def main():\n    pass\n")):
        await generate_patches(
            title="Bug in main",
            diagnosis={"root_cause": "Logic error"},
            category="api",
            affected_files=["app/main.py"],
        )

    assert "app/main.py" in captured_prompt["prompt"]


def test_patch_schema_is_valid():
    """Verify PATCH_SCHEMA has the required top-level properties."""
    assert "type" in PATCH_SCHEMA
    assert PATCH_SCHEMA["type"] == "object"
    props = PATCH_SCHEMA["properties"]
    assert "patches" in props
    assert "summary" in props
    # Check patches array item schema
    items_props = props["patches"]["items"]["properties"]
    assert "file" in items_props
    assert "search" in items_props
    assert "replace" in items_props
    assert "explanation" in items_props
