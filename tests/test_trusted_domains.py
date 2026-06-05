from app.services.enrichment_worker.trusted_domains import is_trusted_domain


def test_authorized_distributor():
    assert is_trusted_domain("https://www.digikey.com/en/products/detail/x")
    assert is_trusted_domain("https://www.mouser.com/ProductDetail/x")


def test_manufacturer_suffix():
    assert is_trusted_domain("https://www.ti.com/product/LM317")
    assert is_trusted_domain("https://st.com/foo")


def test_rejects_lookalike_and_untrusted():
    assert not is_trusted_domain("https://evil-st.com/foo")  # suffix spoof
    assert not is_trusted_domain("https://www.ebay.com/itm/123")
    assert not is_trusted_domain("ftp://www.ti.com/x")  # non-http
    assert not is_trusted_domain("not a url")
