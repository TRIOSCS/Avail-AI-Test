"""test_qcm_email_attachment_safety.py — email HTML-escape + attachment OOM cap.

Two 2026-08-08 QC audit findings:
- _build_html_body did not escape, so a `<`/`&` in an RFQ/reply body vanished
  from the delivered email.
- store_and_attach buffered the whole request body before the size check, an
  OOM vector under concurrent oversized uploads.

Called by: pytest
Depends on: app.email_service, app.services.attachment_service
"""

import io

import pytest
from fastapi import HTTPException

from app.email_service import _build_html_body
from app.services import attachment_service

# ── Email HTML-escape ─────────────────────────────────────────────────────


def test_build_html_body_escapes_angle_brackets_and_amp():
    out = _build_html_body("price < 5 & part <GSOT36C>")
    assert "&lt;" in out and "&gt;" in out and "&amp;" in out
    assert "<GSOT36C>" not in out  # raw tag no longer survives to the client


def test_build_html_body_preserves_newline_as_break():
    out = _build_html_body("line one\nline two")
    assert "<br>\n" in out
    assert "line one" in out and "line two" in out


# ── Attachment size cap (chunked, early abort) ────────────────────────────


class _FakeUpload:
    """Minimal UploadFile stand-in: only `await read(n)` is exercised."""

    def __init__(self, data: bytes):
        self._b = io.BytesIO(data)

    async def read(self, n: int = -1) -> bytes:
        return self._b.read(n)


@pytest.mark.anyio
async def test_read_capped_rejects_oversized(monkeypatch):
    monkeypatch.setattr(attachment_service, "MAX_ATTACHMENT_BYTES", 100)
    with pytest.raises(HTTPException) as exc:
        await attachment_service._read_capped(_FakeUpload(b"x" * 201))
    assert exc.value.status_code == 400


@pytest.mark.anyio
async def test_read_capped_accepts_boundary(monkeypatch):
    monkeypatch.setattr(attachment_service, "MAX_ATTACHMENT_BYTES", 100)
    out = await attachment_service._read_capped(_FakeUpload(b"x" * 100))
    assert out == b"x" * 100
