import pytest

from app.services.enrichment_worker.oem_domains import is_crossref_domain, is_oem_domain


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # Official OEM hosts / vendor roots accepted.
        ("https://support.lenovo.com/parts/01HW917", True),
        ("https://partsurfer.hpe.com/Search.aspx?SearchText=918042-601", True),
        ("http://www.dell.com/support/HV52W", True),
        ("https://parts.hp.com/x", True),  # dot-suffix of hp.com root
        ("https://www.ibm.com/support/x", True),  # ibm.com vendor root
        ("HTTPS://SUPPORT.LENOVO.COM/x", True),  # host lowercased before matching
        # Lookalikes, bad schemes, and non-vendor hosts rejected.
        ("https://evil-lenovo.com/x", False),
        ("https://lenovo.com.evil.com/x", False),
        ("ftp://support.lenovo.com/x", False),
        ("not a url", False),
        ("", False),
        ("https://notlenovo.com/x", False),  # not a vendor root or official host
    ],
)
def test_is_oem_domain(url: str, expected: bool):
    assert is_oem_domain(url) is expected


def test_partsurfer_hp_com_is_explicit_official_host():
    # The canonical HP PartSurfer host (the oem-web-resolution lookup surface) must be
    # an EXPLICIT allowlist member, not just a dot-suffix accident of the hp.com root.
    from app.services.enrichment_worker.oem_domains import OEM_OFFICIAL_HOSTS

    assert "partsurfer.hp.com" in OEM_OFFICIAL_HOSTS
    assert is_oem_domain("https://partsurfer.hp.com/Search.aspx?SearchText=875942-001")


def test_crossref_superset_includes_distributors_and_oem():
    # OEM official
    assert is_crossref_domain("https://support.lenovo.com/x")
    # distributor (from trusted_domains)
    assert is_crossref_domain("https://www.mouser.com/ProductDetail/x")
    # manufacturer (from trusted_domains)
    assert is_crossref_domain("https://www.ti.com/product/x")
    # junk
    assert not is_crossref_domain("https://reddit.com/r/x")
