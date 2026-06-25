"""test_avatar_storage_guard.py — profile-avatar storage parity with TT-0002.

Covers the durable storage guard for the profile-avatar subdir, mirroring
test_screenshot_storage_guard.py:
  (a) ensure_avatar_storage() creates the dir (parents=True) and passes when writable;
  (b) it raises a clear RuntimeError naming the path when the dir is not writable.

Called by: pytest
Depends on: app/startup.py (ensure_avatar_storage), app/routers/avatars.py (AVATARS_DIR)
"""

import os
import stat

import pytest


class TestEnsureAvatarStorage:
    def test_creates_missing_dir(self, tmp_path, monkeypatch):
        """A missing dir is created (parents=True) and the guard passes."""
        from app.routers import avatars
        from app.startup import ensure_avatar_storage

        target = tmp_path / "uploads" / "avatars"
        monkeypatch.setattr(avatars, "AVATARS_DIR", str(target))

        ensure_avatar_storage()

        assert target.is_dir()

    def test_passes_when_writable(self, tmp_path, monkeypatch):
        """An existing writable dir passes without raising."""
        from app.routers import avatars
        from app.startup import ensure_avatar_storage

        monkeypatch.setattr(avatars, "AVATARS_DIR", str(tmp_path))

        ensure_avatar_storage()  # must not raise

    def test_raises_runtimeerror_when_not_writable(self, tmp_path, monkeypatch):
        """A non-writable dir raises a clear RuntimeError naming the path."""
        from app.routers import avatars
        from app.startup import ensure_avatar_storage

        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
        monkeypatch.setattr(avatars, "AVATARS_DIR", str(locked))

        # Running as root bypasses DAC perms — force W_OK False so the branch runs.
        if os.access(str(locked), os.W_OK):
            monkeypatch.setattr("app.startup.os.access", lambda path, mode: False)

        with pytest.raises(RuntimeError) as exc:
            ensure_avatar_storage()

        assert str(locked) in str(exc.value)
        assert "not writable" in str(exc.value)

        locked.chmod(stat.S_IRWXU)  # restore for cleanup
