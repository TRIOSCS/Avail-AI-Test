import pytest

from app.services.enrichment_worker.trusted_domains import is_trusted_domain


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://www.digikey.com/en/products/detail/x", id="distributor-digikey"),
        pytest.param("https://www.mouser.com/ProductDetail/x", id="distributor-mouser"),
        pytest.param("https://www.ti.com/product/LM317", id="manufacturer-suffix-www"),
        pytest.param("https://st.com/foo", id="manufacturer-apex"),
    ],
)
def test_trusted_urls_accepted(url):
    assert is_trusted_domain(url)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://evil-st.com/foo", id="suffix-spoof"),
        pytest.param("https://www.ebay.com/itm/123", id="untrusted-marketplace"),
        pytest.param("ftp://www.ti.com/x", id="non-http-scheme"),
        pytest.param("not a url", id="unparseable"),
    ],
)
def test_untrusted_urls_rejected(url):
    assert not is_trusted_domain(url)
