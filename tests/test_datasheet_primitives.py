import os

os.environ["TESTING"] = "1"
from app.services.datasheet_capture import pdf_contains_mpn


def _pdf_with_text(text: str) -> bytes:
    """Build a minimal but structurally valid PDF that pypdf 6.x can parse.

    The brief's hand-crafted PDF lacked a startxref/xref table so pypdf
    6.13.3 raised PdfStreamError before extracting any text.  This generator
    produces the same logical content (a single page with a Type1 text
    operator) via a proper xref-table structure, which pypdf reliably reads.
    """
    text_b = text.encode()
    stream_data = b"BT /F1 12 Tf 10 100 Td (" + text_b + b") Tj ET"
    pdf: bytearray = bytearray()
    offsets = [0] * 6

    def w(data: bytes) -> None:
        pdf.extend(data)

    w(b"%PDF-1.4\n")

    offsets[1] = len(pdf)
    w(b"1 0 obj\n<</Type /Catalog /Pages 2 0 R>>\nendobj\n")

    offsets[2] = len(pdf)
    w(b"2 0 obj\n<</Type /Pages /Kids [3 0 R] /Count 1>>\nendobj\n")

    offsets[3] = len(pdf)
    w(
        b"3 0 obj\n<</Type /Page /Parent 2 0 R /MediaBox [0 0 200 200]"
        b" /Contents 4 0 R /Resources <</Font <</F1 5 0 R>>>>>>\nendobj\n"
    )

    offsets[4] = len(pdf)
    w(b"4 0 obj\n<</Length " + str(len(stream_data)).encode() + b">>\nstream\n")
    w(stream_data)
    w(b"\nendstream\nendobj\n")

    offsets[5] = len(pdf)
    w(b"5 0 obj\n<</Type /Font /Subtype /Type1 /BaseFont /Helvetica>>\nendobj\n")

    xref_start = len(pdf)
    w(b"xref\n0 6\n")
    w(b"0000000000 65535 f \n")
    for i in range(1, 6):
        w(f"{offsets[i]:010d} 00000 n \n".encode())
    w(b"trailer\n<</Size 6 /Root 1 0 R>>\nstartxref\n")
    w(str(xref_start).encode())
    w(b"\n%%EOF\n")

    return bytes(pdf)


def test_pdf_contains_mpn_true():
    assert pdf_contains_mpn(_pdf_with_text("Part 17P9905 Hard Drive"), "17P9905") is True


def test_pdf_contains_mpn_false_for_wrong_part():
    assert pdf_contains_mpn(_pdf_with_text("Part 1300940294 component"), "17P9905") is False


def test_pdf_contains_mpn_handles_unparseable_bytes():
    assert pdf_contains_mpn(b"not a pdf", "17P9905") is False
