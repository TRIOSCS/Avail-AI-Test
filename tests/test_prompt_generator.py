"""Tests for prompt generator service.

Covers: prompt generation, category rules, constraints, file sections,
promise tags, edge cases.

Called by: pytest
Depends on: app.services.prompt_generator
"""

import pytest

from app.services.prompt_generator import (
    generate_fix_prompt,
    generate_prompt_for_ticket,
    BASE_CONSTRAINTS,
    CATEGORY_RULES,
)


class TestGenerateFixPrompt:
    def test_basic_prompt_structure(self):
        prompt = generate_fix_prompt(
            ticket_id=1,
            title="Button broken",
            description="Submit button does nothing",
            category="ui",
            diagnosis={"root_cause": "Missing onclick", "fix_approach": "Add handler",
                       "test_strategy": "Click test"},
        )
        assert "# Fix: Button broken" in prompt
        assert "Ticket #1" in prompt
        assert "Submit button does nothing" in prompt
        assert "Missing onclick" in prompt
        assert "Add handler" in prompt
        assert "<promise>FIXED</promise>" in prompt
        assert "<promise>ESCALATE</promise>" in prompt

    def test_includes_base_constraints(self):
        prompt = generate_fix_prompt(
            ticket_id=1, title="T", description="D", category="ui",
            diagnosis={"root_cause": "R", "fix_approach": "F", "test_strategy": "T"},
        )
        assert "NEVER modify these files" in prompt
        assert "NEVER run destructive operations" in prompt
        assert "ALWAYS write or update tests" in prompt

    def test_ui_category_rules(self):
        prompt = generate_fix_prompt(
            ticket_id=1, title="T", description="D", category="ui",
            diagnosis={"root_cause": "R", "fix_approach": "F", "test_strategy": "T"},
        )
        assert "UI Bug Rules" in prompt
        assert "never innerHTML" in prompt

    def test_api_category_rules(self):
        prompt = generate_fix_prompt(
            ticket_id=1, title="T", description="D", category="api",
            diagnosis={"root_cause": "R", "fix_approach": "F", "test_strategy": "T"},
        )
        assert "API Bug Rules" in prompt
        assert "db.get(Model, id)" in prompt

    def test_data_category_rules(self):
        prompt = generate_fix_prompt(
            ticket_id=1, title="T", description="D", category="data",
            diagnosis={"root_cause": "R", "fix_approach": "F", "test_strategy": "T"},
        )
        assert "Data Bug Rules" in prompt
        assert "Alembic migration" in prompt

    def test_performance_category_rules(self):
        prompt = generate_fix_prompt(
            ticket_id=1, title="T", description="D", category="performance",
            diagnosis={"root_cause": "R", "fix_approach": "F", "test_strategy": "T"},
        )
        assert "Performance Bug Rules" in prompt
        assert "cached_endpoint" in prompt

    def test_other_category_fallback(self):
        prompt = generate_fix_prompt(
            ticket_id=1, title="T", description="D", category="other",
            diagnosis={"root_cause": "R", "fix_approach": "F", "test_strategy": "T"},
        )
        assert "General Bug Rules" in prompt

    def test_unknown_category_uses_other(self):
        prompt = generate_fix_prompt(
            ticket_id=1, title="T", description="D", category="unknown_xyz",
            diagnosis={"root_cause": "R", "fix_approach": "F", "test_strategy": "T"},
        )
        assert "General Bug Rules" in prompt

    def test_relevant_files_section(self):
        files = [
            {"path": "app/routers/vendors.py", "role": "router", "confidence": 0.9, "stable": False},
            {"path": "app/main.py", "role": "mentioned", "confidence": 0.7, "stable": True},
        ]
        prompt = generate_fix_prompt(
            ticket_id=1, title="T", description="D", category="api",
            diagnosis={"root_cause": "R", "fix_approach": "F", "test_strategy": "T"},
            relevant_files=files,
        )
        assert "app/routers/vendors.py (router, confidence: 0.9)" in prompt
        assert "app/main.py" in prompt
        assert "[STABLE — DO NOT MODIFY]" in prompt

    def test_affected_files_from_diagnosis(self):
        diagnosis = {
            "root_cause": "R", "fix_approach": "F", "test_strategy": "T",
            "affected_files": ["app/services/vendor_service.py", "app/routers/vendors.py"],
        }
        prompt = generate_fix_prompt(
            ticket_id=1, title="T", description="D", category="api",
            diagnosis=diagnosis,
        )
        assert "Affected Files (from diagnosis)" in prompt
        assert "app/services/vendor_service.py" in prompt

    def test_empty_diagnosis_fields(self):
        prompt = generate_fix_prompt(
            ticket_id=1, title="T", description="D", category="ui",
            diagnosis={},
        )
        assert "Unknown" in prompt  # default root_cause
        assert "Not specified" in prompt  # default fix_approach

    def test_no_relevant_files(self):
        prompt = generate_fix_prompt(
            ticket_id=1, title="T", description="D", category="ui",
            diagnosis={"root_cause": "R", "fix_approach": "F", "test_strategy": "T"},
            relevant_files=[],
        )
        assert "Relevant Files" not in prompt

    def test_stable_files_in_constraints(self):
        prompt = generate_fix_prompt(
            ticket_id=1, title="T", description="D", category="ui",
            diagnosis={"root_cause": "R", "fix_approach": "F", "test_strategy": "T"},
        )
        assert "app/main.py" in prompt  # from STABLE_FILES
        assert "app/database.py" in prompt


class TestGeneratePromptForTicket:
    def test_from_ticket_object(self):
        """Test with a mock ticket object."""
        class FakeTicket:
            id = 42
            title = "Search broken"
            description = "Search returns no results"
            category = "api"
            diagnosis = {"root_cause": "Query error", "fix_approach": "Fix SQL",
                         "test_strategy": "Test search"}
            file_mapping = ["app/search_service.py"]

        prompt = generate_prompt_for_ticket(FakeTicket())
        assert "Ticket #42" in prompt
        assert "Search broken" in prompt
        assert "app/search_service.py" in prompt

    def test_from_ticket_no_diagnosis(self):
        class FakeTicket:
            id = 1
            title = "T"
            description = "D"
            category = "ui"
            diagnosis = None
            file_mapping = None

        prompt = generate_prompt_for_ticket(FakeTicket())
        assert "Ticket #1" in prompt
        assert "Unknown" in prompt

    def test_stable_file_flagged_in_mapping(self):
        class FakeTicket:
            id = 1
            title = "T"
            description = "D"
            category = "api"
            diagnosis = {}
            file_mapping = ["app/main.py", "app/routers/vendors.py"]

        prompt = generate_prompt_for_ticket(FakeTicket())
        assert "[STABLE — DO NOT MODIFY]" in prompt


class TestCategoryRulesComplete:
    def test_all_expected_categories_exist(self):
        for cat in ["ui", "api", "data", "performance", "other"]:
            assert cat in CATEGORY_RULES

    def test_base_constraints_not_empty(self):
        assert len(BASE_CONSTRAINTS) > 100
        assert "NEVER" in BASE_CONSTRAINTS
