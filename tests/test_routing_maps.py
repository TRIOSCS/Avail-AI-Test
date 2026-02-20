"""
test_routing_maps.py — Tests for configurable routing maps loader.

Covers: file loading, reload, missing file fallback, invalid JSON,
key normalization, and getter functions.
"""

from __future__ import annotations

import json
from pathlib import Path


class TestLoadFromFile:
    """Test the internal _load_from_file function."""

    def test_valid_file(self, tmp_path: Path):
        from app.routing_maps import _load_from_file

        config = {
            "brand_commodity_map": {"Intel": "Semiconductors"},
            "country_region_map": {"US": "Americas"},
        }
        f = tmp_path / "maps.json"
        f.write_text(json.dumps(config))

        brands, countries = _load_from_file(f)
        # Keys and values are lowercased
        assert brands == {"intel": "semiconductors"}
        assert countries == {"us": "americas"}

    def test_missing_file_returns_empty(self, tmp_path: Path):
        from app.routing_maps import _load_from_file

        brands, countries = _load_from_file(tmp_path / "nonexistent.json")
        assert brands == {}
        assert countries == {}

    def test_invalid_json_returns_empty(self, tmp_path: Path):
        from app.routing_maps import _load_from_file

        f = tmp_path / "bad.json"
        f.write_text("{invalid json!!")

        brands, countries = _load_from_file(f)
        assert brands == {}
        assert countries == {}

    def test_missing_keys_returns_empty(self, tmp_path: Path):
        from app.routing_maps import _load_from_file

        f = tmp_path / "partial.json"
        f.write_text(json.dumps({"unrelated": "data"}))

        brands, countries = _load_from_file(f)
        assert brands == {}
        assert countries == {}

    def test_whitespace_stripped(self, tmp_path: Path):
        from app.routing_maps import _load_from_file

        config = {
            "brand_commodity_map": {"  TI  ": "  Semiconductors  "},
            "country_region_map": {},
        }
        f = tmp_path / "maps.json"
        f.write_text(json.dumps(config))

        brands, _ = _load_from_file(f)
        assert brands == {"ti": "semiconductors"}


class TestLoadAndReload:
    """Test the load_routing_maps / getter cycle."""

    def test_load_custom_path(self, tmp_path: Path):
        from app.routing_maps import (
            get_brand_commodity_map,
            get_country_region_map,
            load_routing_maps,
        )

        config = {
            "brand_commodity_map": {"nvidia": "semiconductors", "cisco": "networking"},
            "country_region_map": {"de": "emea"},
        }
        f = tmp_path / "maps.json"
        f.write_text(json.dumps(config))

        load_routing_maps(path=f)
        assert len(get_brand_commodity_map()) == 2
        assert get_brand_commodity_map()["nvidia"] == "semiconductors"
        assert get_country_region_map()["de"] == "emea"

    def test_reload_replaces_previous(self, tmp_path: Path):
        from app.routing_maps import get_brand_commodity_map, load_routing_maps

        f = tmp_path / "maps.json"

        # Load version 1
        f.write_text(json.dumps({"brand_commodity_map": {"aaa": "xxx"}, "country_region_map": {}}))
        load_routing_maps(path=f)
        assert "aaa" in get_brand_commodity_map()

        # Load version 2 — aaa should be gone
        f.write_text(json.dumps({"brand_commodity_map": {"bbb": "yyy"}, "country_region_map": {}}))
        load_routing_maps(path=f)
        assert "aaa" not in get_brand_commodity_map()
        assert "bbb" in get_brand_commodity_map()


class TestDefaultConfigFile:
    """Test that the shipped config/routing_maps.json is valid."""

    def test_default_config_loads(self):
        from app.routing_maps import _CONFIG_PATH, _load_from_file

        brands, countries = _load_from_file(_CONFIG_PATH)
        assert len(brands) >= 30, f"Expected >=30 brands, got {len(brands)}"
        assert len(countries) >= 30, f"Expected >=30 countries, got {len(countries)}"
        assert brands["intel"] == "semiconductors"
        assert countries["us"] == "americas"
