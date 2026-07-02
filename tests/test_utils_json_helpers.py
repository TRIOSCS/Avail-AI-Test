"""tests/test_utils_json_helpers.py — Tests for app/utils/json_helpers.py."""

import os

os.environ["TESTING"] = "1"

from app.utils.json_helpers import dumps, loads


class TestDumps:
    def test_simple_dict(self):
        result = dumps({"key": "value"})
        assert result == '{"key":"value"}'

    def test_returns_string_not_bytes(self):
        result = dumps({"a": 1})
        assert isinstance(result, str)

    def test_list_serialization(self):
        result = dumps([1, 2, 3])
        assert result == "[1,2,3]"

    def test_sort_keys_false_default(self):
        result = dumps({"b": 2, "a": 1})
        assert isinstance(result, str)

    def test_sort_keys_true(self):
        result = dumps({"b": 2, "a": 1}, sort_keys=True)
        assert result.index('"a"') < result.index('"b"')

    def test_nested_structure(self):
        result = dumps({"nested": {"x": 1}})
        assert "nested" in result
        assert '"x":1' in result or '"x": 1' in result

    def test_none_value(self):
        result = dumps({"key": None})
        assert "null" in result

    def test_custom_default(self):
        from datetime import date

        def default(obj):
            if isinstance(obj, date):
                return obj.isoformat()
            raise TypeError

        result = dumps({"d": date(2024, 1, 15)}, default=default)
        assert "2024-01-15" in result


class TestLoads:
    def test_simple_json_string(self):
        result = loads('{"key": "value"}')
        assert result == {"key": "value"}

    def test_list_json(self):
        result = loads("[1, 2, 3]")
        assert result == [1, 2, 3]

    def test_bytes_input(self):
        result = loads(b'{"a": 1}')
        assert result == {"a": 1}

    def test_null_becomes_none(self):
        result = loads('{"key": null}')
        assert result["key"] is None

    def test_nested_json(self):
        result = loads('{"outer": {"inner": 42}}')
        assert result["outer"]["inner"] == 42

    def test_roundtrip(self):
        original = {"name": "test", "value": 123, "flag": True}
        assert loads(dumps(original)) == original
