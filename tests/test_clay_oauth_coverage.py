"""tests/test_clay_oauth_coverage.py — Extra coverage for clay_oauth service.

Targets missing lines in app/services/clay_oauth.py:
  - _store() function (lines 41-57)
  - _load() function (line 61)

Called by: pytest
Depends on: conftest.py, app.services.clay_oauth
"""

import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("TESTING", "1")


class TestClayOAuthStore:
    def _mock_db_with_source(self, creds=None):
        """Create a mock DB session with an ApiSource-like row."""

        mock_source = MagicMock()
        mock_source.credentials = dict(creds or {})

        mock_db = MagicMock()
        # query().filter_by().first() returns None (no existing row)
        mock_db.query.return_value.filter_by.return_value.first.return_value = None
        return mock_db, mock_source

    def test_store_creates_new_source(self):
        """_store() creates an ApiSource row when none exists."""
        from app.services.clay_oauth import _store

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None

        with (
            patch("app.services.clay_oauth.SessionLocal", return_value=mock_db),
            patch("app.services.clay_oauth.cs.encrypt_value", side_effect=lambda v: v),
            patch("app.services.clay_oauth.cs._cred_cache") as mock_cache,
        ):
            _store({"CLAY_OAUTH_CLIENT_ID": "test-cid"})

        mock_cache.clear.assert_called()
        mock_db.commit.assert_called_once()
        mock_db.close.assert_called_once()

    def test_store_updates_existing_source(self):
        """_store() updates an existing ApiSource row."""
        from app.services.clay_oauth import _store

        existing = MagicMock()
        existing.credentials = {"OLD_KEY": "old-val"}

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = existing

        with (
            patch("app.services.clay_oauth.SessionLocal", return_value=mock_db),
            patch("app.services.clay_oauth.cs.encrypt_value", side_effect=lambda v: v),
            patch("app.services.clay_oauth.cs._cred_cache") as mock_cache,
        ):
            _store({"CLAY_OAUTH_CLIENT_ID": "new-cid"})

        mock_cache.clear.assert_called()
        assert existing.credentials.get("CLAY_OAUTH_CLIENT_ID") == "new-cid"

    def test_store_deletes_key_when_none(self):
        """_store() removes a key from credentials when value is None."""
        from app.services.clay_oauth import _store

        existing = MagicMock()
        existing.credentials = {"CLAY_OAUTH_ACCESS_TOKEN": "tok"}

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = existing

        with (
            patch("app.services.clay_oauth.SessionLocal", return_value=mock_db),
            patch("app.services.clay_oauth.cs.encrypt_value", side_effect=lambda v: v),
            patch("app.services.clay_oauth.cs._cred_cache"),
        ):
            _store({"CLAY_OAUTH_ACCESS_TOKEN": None})

        assert "CLAY_OAUTH_ACCESS_TOKEN" not in existing.credentials

    def test_load_returns_value(self):
        """_load() delegates to credential_service.get_credential_cached."""
        from app.services.clay_oauth import _SOURCE, _load

        with patch("app.services.clay_oauth.cs.get_credential_cached", return_value="test-val") as mock_get:
            result = _load("CLAY_OAUTH_CLIENT_ID")

        mock_get.assert_called_once_with(_SOURCE, "CLAY_OAUTH_CLIENT_ID")
        assert result == "test-val"

    def test_load_returns_none_when_missing(self):
        """_load() returns None when the key is not set."""
        from app.services.clay_oauth import _load

        with patch("app.services.clay_oauth.cs.get_credential_cached", return_value=None):
            result = _load("CLAY_OAUTH_MISSING_KEY")

        assert result is None

    def test_pkce_pair_returns_distinct_values(self):
        """pkce_pair() returns a (verifier, challenge) tuple."""
        from app.services.clay_oauth import pkce_pair

        verifier, challenge = pkce_pair()
        assert len(verifier) > 0
        assert len(challenge) > 0
        assert verifier != challenge

    def test_build_authorize_url(self):
        """build_authorize_url() returns a URL with expected params."""
        from app.services.clay_oauth import build_authorize_url

        url = build_authorize_url("test-cid", "mystate", "mychallenge")
        assert "client_id=test-cid" in url
        assert "state=mystate" in url
        assert "code_challenge=mychallenge" in url

    def test_is_connected_false_when_no_token(self):
        """is_connected() returns False when no refresh token is stored."""
        from app.services.clay_oauth import is_connected

        with patch("app.services.clay_oauth._load", return_value=None):
            assert is_connected() is False

    def test_needs_reconnect_true(self):
        """needs_reconnect() returns True when flag is set."""
        from app.services.clay_oauth import needs_reconnect

        with patch("app.services.clay_oauth._load", return_value="1"):
            assert needs_reconnect() is True

    def test_needs_reconnect_false(self):
        from app.services.clay_oauth import needs_reconnect

        with patch("app.services.clay_oauth._load", return_value=None):
            assert needs_reconnect() is False

    def test_disconnect_clears_all_keys(self):
        """disconnect() calls _store with None for all keys."""
        from app.services.clay_oauth import disconnect

        with patch("app.services.clay_oauth._store") as mock_store:
            disconnect()

        mock_store.assert_called_once()
        updates = mock_store.call_args[0][0]
        assert all(v is None for v in updates.values())
        assert "CLAY_OAUTH_CLIENT_ID" in updates
        assert "CLAY_OAUTH_ACCESS_TOKEN" in updates
        assert "CLAY_OAUTH_REFRESH_TOKEN" in updates
