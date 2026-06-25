"""test_avatar_upload_route.py — profile-avatar upload/serve/delete route.

Covers app/routers/avatars.py:
  - POST /api/user/avatar stores a valid image, sets the AUTHENTICATED user's
    avatar_path (own-profile only — no user path param exists), persists the file;
  - magic-byte (real content) and size validation return the project's JSON
    `"error"` shape — a non-image payload mislabelled ``image/png`` is rejected and
    nothing is written to disk;
  - DELETE /api/user/avatar clears avatar_path and removes the file;
  - GET /api/user/avatar/{filename} serves the stored image and blocks traversal;
  - the routes are login-gated (unauthenticated → 401/403).

Called by: pytest
Depends on: tests/conftest.py (client, db_session, test_user, unauthenticated_client),
            app/routers/avatars.py
"""

import pytest

# Smallest valid PNG (1x1 transparent), as raw bytes.
_PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d4944415478da6360000002000001e221bc330000000049454e44ae426082"
)


@pytest.fixture()
def avatars_tmp(tmp_path, monkeypatch):
    """Point AVATARS_DIR at a writable temp dir and reset the ready flag."""
    from app.routers import avatars

    target = tmp_path / "avatars"
    target.mkdir()
    monkeypatch.setattr(avatars, "AVATARS_DIR", str(target))
    monkeypatch.setattr(avatars, "_avatar_dir_ready", False)
    return target


class TestUpload:
    def test_valid_png_sets_avatar_path_and_writes_file(self, client, db_session, test_user, avatars_tmp):
        resp = client.post(
            "/api/user/avatar",
            files={"file": ("me.png", _PNG_1X1, "image/png")},
        )
        assert resp.status_code == 200

        db_session.refresh(test_user)
        assert test_user.avatar_path is not None
        assert test_user.avatar_path.startswith(f"user_{test_user.id}_")
        assert test_user.avatar_path.endswith(".png")
        # File actually persisted on disk under the (patched) AVATARS_DIR.
        assert (avatars_tmp / test_user.avatar_path).is_file()

    def test_upload_targets_only_the_authenticated_user(self, client, db_session, test_user, avatars_tmp):
        """Own-profile only: there is no user path param, so the route can only ever
        mutate the logged-in user. A second user in the DB must be untouched."""
        from app.models import User

        other = User(email="other@trioscs.com", name="Other", role="buyer", azure_id="az-other")
        db_session.add(other)
        db_session.commit()
        db_session.refresh(other)

        resp = client.post("/api/user/avatar", files={"file": ("me.png", _PNG_1X1, "image/png")})
        assert resp.status_code == 200

        db_session.refresh(test_user)
        db_session.refresh(other)
        assert test_user.avatar_path is not None
        assert other.avatar_path is None

    def test_rejects_non_image_bytes(self, client, db_session, test_user, avatars_tmp):
        """Bytes that aren't a recognised image are rejected regardless of the (here,
        honest) Content-Type, in the project's JSON `"error"` shape."""
        resp = client.post(
            "/api/user/avatar",
            files={"file": ("notes.txt", b"hello", "text/plain")},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert "error" in body and "detail" not in body
        db_session.refresh(test_user)
        assert test_user.avatar_path is None

    def test_rejects_polyglot_mislabelled_as_png(self, client, db_session, test_user, avatars_tmp):
        """SECURITY (magic-byte root fix): an upload whose BYTES are not a valid image —
        e.g. an HTML/JS polyglot — but sent with ``Content-Type: image/png`` must be
        rejected by the magic-byte check, and nothing may be written to disk.

        Validating
        the header (``file.content_type``) instead of the bytes would let this through,
        store it as ``.png``, and serve it back inline same-origin.
        """
        payload = b"<html><script>alert(1)</script>"
        resp = client.post(
            "/api/user/avatar",
            files={"file": ("evil.png", payload, "image/png")},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert "error" in body and "detail" not in body
        assert "PNG" in body["error"]  # the image-type rejection, not size/empty

        db_session.refresh(test_user)
        assert test_user.avatar_path is None
        # Root-fix guarantee: the malicious payload never touched the storage volume.
        assert list(avatars_tmp.iterdir()) == []

    def test_rejects_oversize_image(self, client, db_session, test_user, avatars_tmp):
        from app.routers import avatars

        big = b"\x89PNG" + b"\x00" * (avatars.MAX_AVATAR_BYTES + 1)
        resp = client.post(
            "/api/user/avatar",
            files={"file": ("huge.png", big, "image/png")},
        )
        assert resp.status_code == 400
        assert "2 MB" in resp.json()["error"]

    def test_requires_login(self, unauthenticated_client, avatars_tmp):
        resp = unauthenticated_client.post(
            "/api/user/avatar",
            files={"file": ("me.png", _PNG_1X1, "image/png")},
        )
        assert resp.status_code in (401, 403)


class TestServeAndDelete:
    def test_serve_returns_stored_image(self, client, db_session, test_user, avatars_tmp):
        client.post("/api/user/avatar", files={"file": ("me.png", _PNG_1X1, "image/png")})
        db_session.refresh(test_user)

        resp = client.get(f"/api/user/avatar/{test_user.avatar_path}")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert resp.content == _PNG_1X1

    def test_serve_blocks_path_traversal(self, client, avatars_tmp):
        resp = client.get("/api/user/avatar/..%2f..%2fetc%2fpasswd")
        assert resp.status_code in (403, 404)

    def test_serve_missing_file_404(self, client, avatars_tmp):
        resp = client.get("/api/user/avatar/user_999_deadbeef.png")
        assert resp.status_code == 404

    def test_delete_clears_avatar_and_removes_file(self, client, db_session, test_user, avatars_tmp):
        client.post("/api/user/avatar", files={"file": ("me.png", _PNG_1X1, "image/png")})
        db_session.refresh(test_user)
        stored = avatars_tmp / test_user.avatar_path
        assert stored.is_file()

        resp = client.delete("/api/user/avatar")
        assert resp.status_code == 200

        db_session.refresh(test_user)
        assert test_user.avatar_path is None
        assert not stored.exists()

    def test_replacing_avatar_removes_old_file(self, client, db_session, test_user, avatars_tmp):
        client.post("/api/user/avatar", files={"file": ("a.png", _PNG_1X1, "image/png")})
        db_session.refresh(test_user)
        first = avatars_tmp / test_user.avatar_path
        assert first.is_file()

        client.post("/api/user/avatar", files={"file": ("b.png", _PNG_1X1, "image/png")})
        db_session.refresh(test_user)
        second = avatars_tmp / test_user.avatar_path

        assert second.is_file()
        assert second != first
        assert not first.exists()  # old file cleaned up

    def test_files_left_after_upload(self, avatars_tmp):
        """Sanity: nothing is written until an upload happens (clean fixture)."""
        assert list(avatars_tmp.iterdir()) == []
