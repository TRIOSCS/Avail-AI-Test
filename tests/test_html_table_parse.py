from app.file_utils import extract_mpns, parse_tabular_file

_HTML = (
    b'<head><META http-equiv="Content-Type" content="text/html; charset=ISO-8859-1"></head>'
    b"<table><tr><td>Material: Material Name</td></tr>"
    b"<tr><td>M393A2K43EB3-CWEB/C</td></tr>"
    b"<tr><td>04M3HJ</td></tr>"
    b"<tr><td></td></tr>"
    b"<tr><td>LTM8053IY#PBF</td></tr></table>"
)


def test_parse_html_disguised_as_xls():
    rows = parse_tabular_file(_HTML, "report1780605266325.xls")
    # header lowercased+stripped becomes the dict key
    assert len(rows) == 3  # blank row dropped
    assert rows[0]["material: material name"] == "M393A2K43EB3-CWEB/C"


def test_extract_mpns_single_column():
    rows = parse_tabular_file(_HTML, "report.xls")
    mpns = extract_mpns(rows)
    assert mpns == ["M393A2K43EB3-CWEB/C", "04M3HJ", "LTM8053IY#PBF"]


def test_extract_mpns_named_column():
    rows = [{"part number": "ABC123"}, {"part number": "DEF456"}]
    assert extract_mpns(rows) == ["ABC123", "DEF456"]
