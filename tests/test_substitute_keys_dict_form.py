"""Regression tests for _substitute_keys handling dict-form substitutes.

Requirement.substitutes is canonically stored as a list of dicts
({"mpn": ..., "manufacturer": ...}); _substitute_keys must extract the
normalized MPN key from both that dict form and the legacy string form.

Tests: app/routers/requisitions/requirements.py::_substitute_keys
"""

from types import SimpleNamespace

from app.routers.requisitions.requirements import _substitute_keys
from app.utils.normalization import normalize_mpn_key


def _req(substitutes):
    """Minimal stand-in for a Requirement (only .substitutes is read)."""
    return SimpleNamespace(substitutes=substitutes)


def test_substitute_keys_string_form():
    req = _req(["LM317T", "LM-338T"])
    assert _substitute_keys(req) == [normalize_mpn_key("LM317T"), normalize_mpn_key("LM-338T")]


def test_substitute_keys_dict_form():
    req = _req(
        [
            {"mpn": "LM338T", "manufacturer": "TI"},
            {"mpn": "SG3525", "manufacturer": "ON Semi"},
        ]
    )
    assert _substitute_keys(req) == [normalize_mpn_key("LM338T"), normalize_mpn_key("SG3525")]


def test_substitute_keys_mixed_form():
    req = _req(["LM317T", {"mpn": "LM338T", "manufacturer": "TI"}])
    assert _substitute_keys(req) == [normalize_mpn_key("LM317T"), normalize_mpn_key("LM338T")]


def test_substitute_keys_skips_empty_dict_mpn():
    req = _req([{"mpn": "", "manufacturer": "TI"}, {"mpn": "LM338T", "manufacturer": "TI"}])
    assert _substitute_keys(req) == [normalize_mpn_key("LM338T")]


def test_substitute_keys_none():
    assert _substitute_keys(_req(None)) == []
