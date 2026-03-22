"""Tests for restructured substitutes JSON (list[dict] format).

Tests parse_substitute_mpns after the substitutes JSON restructure.
Depends on: app/utils/normalization.py
"""

from app.utils.normalization import parse_substitute_mpns


def test_parse_subs_new_format():
    subs = [{"mpn": "LM338T", "manufacturer": "TI"}, {"mpn": "SG3525", "manufacturer": "ON Semi"}]
    result = parse_substitute_mpns(subs, "LM317T")
    assert len(result) == 2
    assert result[0]["mpn"] == "LM338T"
    assert result[0]["manufacturer"] == "TI"


def test_parse_subs_excludes_primary():
    subs = [{"mpn": "LM317T", "manufacturer": "TI"}, {"mpn": "LM338T", "manufacturer": "TI"}]
    result = parse_substitute_mpns(subs, "LM317T")
    assert len(result) == 1


def test_parse_subs_deduplicates():
    subs = [{"mpn": "LM338T", "manufacturer": "TI"}, {"mpn": "LM-338T", "manufacturer": "TI"}]
    result = parse_substitute_mpns(subs, "LM317T")
    assert len(result) == 1


def test_parse_subs_respects_limit():
    subs = [{"mpn": f"MPN{i}", "manufacturer": "Test"} for i in range(30)]
    result = parse_substitute_mpns(subs, "PRIMARY", limit=5)
    assert len(result) == 5


def test_parse_subs_empty():
    assert parse_substitute_mpns([], "LM317T") == []


def test_parse_subs_skips_empty_mpn():
    subs = [{"mpn": "", "manufacturer": "TI"}, {"mpn": "LM338T", "manufacturer": "TI"}]
    result = parse_substitute_mpns(subs, "LM317T")
    assert len(result) == 1
