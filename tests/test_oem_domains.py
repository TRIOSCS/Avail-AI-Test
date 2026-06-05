from app.services.enrichment_worker.oem_domains import is_crossref_domain, is_oem_domain


def test_official_oem_hosts_accepted():
    assert is_oem_domain("https://support.lenovo.com/parts/01HW917")
    assert is_oem_domain("https://partsurfer.hpe.com/Search.aspx?SearchText=918042-601")
    assert is_oem_domain("http://www.dell.com/support/HV52W")
    assert is_oem_domain("https://parts.hp.com/x")  # dot-suffix of hp.com root


def test_lookalike_and_bad_schemes_rejected():
    assert not is_oem_domain("https://evil-lenovo.com/x")
    assert not is_oem_domain("https://lenovo.com.evil.com/x")
    assert not is_oem_domain("ftp://support.lenovo.com/x")
    assert not is_oem_domain("not a url")
    assert not is_oem_domain("")


def test_crossref_superset_includes_distributors_and_oem():
    # OEM official
    assert is_crossref_domain("https://support.lenovo.com/x")
    # distributor (from trusted_domains)
    assert is_crossref_domain("https://www.mouser.com/ProductDetail/x")
    # manufacturer (from trusted_domains)
    assert is_crossref_domain("https://www.ti.com/product/x")
    # junk
    assert not is_crossref_domain("https://reddit.com/r/x")


def test_ibm_and_uppercase_host():
    assert is_oem_domain("https://www.ibm.com/support/x")  # ibm.com vendor root
    assert is_oem_domain("HTTPS://SUPPORT.LENOVO.COM/x")  # host lowercased before matching
    assert not is_oem_domain("https://notlenovo.com/x")  # not a vendor root or official host
