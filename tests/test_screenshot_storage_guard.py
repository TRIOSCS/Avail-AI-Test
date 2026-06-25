"""test_screenshot_storage_guard.py — TT-0002 durable screenshot-storage fix.

Covers the durable fix for trouble-ticket screenshot storage ownership:
  (a) the startup writability guard `ensure_screenshot_storage()` — creates the
      dir and raises a clear RuntimeError when it is not writable by the app process;
  (b) the save route surfaces a clear JSON 500 (the `"error"` key, not `"detail"`)
      when the screenshot write fails with PermissionError/OSError.

Called by: pytest
Depends on: app/startup.py (ensure_screenshot_storage),
            app/routers/error_reports.py (_save_screenshot, submit route)
"""

import os
import stat

import pytest

# ── Startup writability guard ─────────────────────────────────────────


class TestEnsureScreenshotStorage:
    def test_creates_missing_dir(self, tmp_path, monkeypatch):
        """A missing dir is created (parents=True) and the guard passes."""
        from app.routers import error_reports
        from app.startup import ensure_screenshot_storage

        target = tmp_path / "uploads" / "tickets"
        monkeypatch.setattr(error_reports, "UPLOAD_DIR", str(target))

        ensure_screenshot_storage()

        assert target.is_dir()

    def test_passes_when_writable(self, tmp_path, monkeypatch):
        """An existing writable dir passes without raising."""
        from app.routers import error_reports
        from app.startup import ensure_screenshot_storage

        monkeypatch.setattr(error_reports, "UPLOAD_DIR", str(tmp_path))

        ensure_screenshot_storage()  # must not raise

    def test_raises_runtimeerror_when_not_writable(self, tmp_path, monkeypatch):
        """A non-writable dir raises a clear RuntimeError naming the path."""
        from app.routers import error_reports
        from app.startup import ensure_screenshot_storage

        locked = tmp_path / "locked"
        locked.mkdir()
        # Strip all write bits so os.access(W_OK) is False for a non-root caller.
        locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
        monkeypatch.setattr(error_reports, "UPLOAD_DIR", str(locked))

        # Running as root (e.g. some CI) bypasses DAC perms — os.access stays True;
        # in that case force W_OK False so the guard's branch is still exercised.
        if os.access(str(locked), os.W_OK):
            monkeypatch.setattr(
                "app.startup.os.access",
                lambda path, mode: False,
            )

        with pytest.raises(RuntimeError) as exc:
            ensure_screenshot_storage()

        assert str(locked) in str(exc.value)
        assert "not writable" in str(exc.value)

        # Restore perms so tmp_path cleanup can remove it.
        locked.chmod(stat.S_IRWXU)


# ── Save route surfaces a clear JSON 500 on storage failure ───────────


class TestSaveRouteStorageError:
    def test_submit_returns_clear_json_500_on_permission_error(self, client, monkeypatch):
        """When the screenshot write fails (PermissionError), the submit route returns a
        clear JSON 500 using the project's `"error"` key."""
        from app.routers import error_reports

        def _raise_permission_error(*_args, **_kwargs):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(error_reports, "_save_screenshot", _raise_permission_error)

        resp = client.post(
            "/api/trouble-tickets/submit",
            json={
                "description": "Screenshot storage failure repro",
                "page_url": "/v2/search",
                "screenshot": "data:image/png;base64,iVBORw0KGgo=",
            },
        )

        assert resp.status_code == 500
        body = resp.json()
        assert "error" in body
        assert "detail" not in body
        assert "not writable" in body["error"].lower()

    def test_submit_returns_clear_json_500_on_oserror(self, client, monkeypatch):
        """A generic OSError on write is also surfaced as the clear JSON 500."""
        from app.routers import error_reports

        def _raise_oserror(*_args, **_kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(error_reports, "_save_screenshot", _raise_oserror)

        resp = client.post(
            "/api/trouble-tickets/submit",
            json={
                "description": "Disk full repro",
                "page_url": "/v2/search",
                "screenshot": "data:image/png;base64,iVBORw0KGgo=",
            },
        )

        assert resp.status_code == 500
        assert resp.json()["error"]
