"""Tests for free enrichment service (SAM.gov + Google News).

Tests headline classification and enrichment data structure.
External API calls are mocked.

Called by: pytest
Depends on: conftest (db_session)
"""

import pytest

from app.services.prospect_free_enrichment import _classify_headline


# ═══════════════════════════════════════════════════════════════════════
#  Headline Classification
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "headline,expected",
    [
        ("Acme Corp raises $50M in Series C funding round", "funding"),
        ("Company announces IPO plans for 2026", "funding"),
        ("TechCo acquires rival firm for $2B", "acquisition"),
        ("Merger between Alpha and Beta finalized", "acquisition"),
        ("Company expands with new manufacturing facility in Texas", "expansion"),
        ("New office headquarters announced in Boston", "expansion"),
        ("Company launches revolutionary new product line", "product"),
        ("Company unveils next-gen semiconductor chip", "product"),
        ("Company hiring 500 engineers for new division", "hiring"),
        ("Major tech firm announces layoffs of 1,000 workers", "layoffs"),
        ("Company restructures operations amid downturn", "layoffs"),
        ("Company wins $100M DoD defense contract", "contract"),
        ("Pentagon awards new government contract to company", "contract"),
        ("Company receives FDA certification for medical device", "regulatory"),
        ("Just a regular news article about the company", "general"),
        ("Quarterly earnings report shows growth", "general"),
    ],
)
def test_classify_headline(headline, expected):
    """Headlines are classified into the correct signal type."""
    assert _classify_headline(headline) == expected


def test_classify_headline_case_insensitive():
    """Classification is case-insensitive."""
    assert _classify_headline("COMPANY RAISES FUNDING") == "funding"
    assert _classify_headline("New Acquisition Announced") == "acquisition"
