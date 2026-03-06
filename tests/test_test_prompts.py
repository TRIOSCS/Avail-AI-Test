"""Tests for app.services.test_prompts -- Claude agent test prompt generator.

Called by: pytest
Depends on: app/services/test_prompts.py
"""

from app.services.test_prompts import AREA_PROMPTS, generate_all_prompts, generate_area_prompt


ALL_AREAS = [
    "search", "requisitions", "rfq", "crm_companies", "crm_contacts",
    "crm_quotes", "prospecting", "vendors", "tagging", "tickets",
    "admin_api_health", "admin_settings", "notifications", "auth",
    "upload", "pipeline", "activity",
]


def test_generate_all_prompts_covers_every_area():
    prompts = generate_all_prompts()
    assert len(prompts) >= 15
    names = [p["area"] for p in prompts]
    assert "search" in names
    assert "crm_companies" in names
    assert "rfq" in names


def test_generate_all_prompts_has_exactly_17_areas():
    prompts = generate_all_prompts()
    assert len(prompts) == 17


def test_generate_all_prompts_all_expected_areas_present():
    prompts = generate_all_prompts()
    names = {p["area"] for p in prompts}
    for area in ALL_AREAS:
        assert area in names, f"Missing area: {area}"


def test_generate_area_prompt_has_required_fields():
    prompt = generate_area_prompt("search")
    assert "area" in prompt
    assert "url_hash" in prompt
    assert "prompt" in prompt


def test_generate_unknown_area_returns_none():
    result = generate_area_prompt("nonexistent_area")
    assert result is None


def test_each_prompt_contains_test_instructions():
    for area in ALL_AREAS:
        prompt = generate_area_prompt(area)
        assert prompt is not None, f"Missing prompt for {area}"
        assert "WHAT TO TEST" in prompt["prompt"]
        assert "WHAT CORRECT LOOKS LIKE" in prompt["prompt"]
        assert "SUBMITTING FINDINGS" in prompt["prompt"]


def test_each_prompt_mentions_trouble_tickets_endpoint():
    for area in ALL_AREAS:
        prompt = generate_area_prompt(area)
        assert "/api/trouble-tickets" in prompt["prompt"]


def test_each_prompt_includes_source_agent():
    for area in ALL_AREAS:
        prompt = generate_area_prompt(area)
        assert '"source": "agent"' in prompt["prompt"]


def test_url_hashes_are_strings():
    for area in ALL_AREAS:
        prompt = generate_area_prompt(area)
        assert isinstance(prompt["url_hash"], str)
        assert prompt["url_hash"].startswith("#")


def test_area_prompts_dict_matches_generate_all():
    assert len(AREA_PROMPTS) == len(generate_all_prompts())


def test_generate_area_prompt_returns_correct_area_name():
    for area in ALL_AREAS:
        prompt = generate_area_prompt(area)
        assert prompt["area"] == area
