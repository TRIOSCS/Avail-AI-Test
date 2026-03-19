"""Tests for material search routing (MPN vs natural language)."""

from app.services.material_search_service import classify_query


def test_classify_mpn_simple():
    assert classify_query("LM358DR") == "mpn"


def test_classify_mpn_with_dashes():
    assert classify_query("RC0805FR-07100KL") == "mpn"


def test_classify_mpn_two_words():
    assert classify_query("STM32 F407") == "mpn"


def test_classify_natural_language():
    assert classify_query("DDR5 memory 16GB") == "natural_language"


def test_classify_natural_language_description():
    assert classify_query("UHD LCD panel for automotive") == "natural_language"


def test_classify_empty():
    assert classify_query("") == "mpn"
