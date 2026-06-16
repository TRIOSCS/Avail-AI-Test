"""Security allowlist for web_sourced enrichment: only authorized-distributor or
manufacturer-official domains may produce web_sourced data. Validated in code."""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlparse


def _host_allowed(url: str, exact_hosts: Iterable[str], suffix_roots: Iterable[str]) -> bool:
    """Return True if *url*'s host exactly matches ``exact_hosts`` or is a dot-suffix of
    a ``suffix_roots`` entry (e.g. ``www.ti.com`` matches root ``ti.com`` but ``evil-
    ti.com`` does not).

    Rejects non-http/https schemes and unparseable URLs.
    """
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    host = p.hostname.lower()
    if host in exact_hosts:
        return True
    return any(host == root or host.endswith("." + root) for root in suffix_roots)


AUTHORIZED_DISTRIBUTORS: frozenset[str] = frozenset(
    {
        "www.digikey.com",
        "www.mouser.com",
        "www.newark.com",
        "www.element14.com",
        "www.farnell.com",
        "www.arrow.com",
        "www.avnet.com",
        "www.ttiinc.com",
        "uk.rs-online.com",
        "us.rs-online.com",
        "www.rs-online.com",
        "www.futureelectronics.com",
    }
)

MANUFACTURER_DOMAINS: dict[str, str] = {
    "st.com": "STMicroelectronics",
    "ti.com": "Texas Instruments",
    "analog.com": "Analog Devices",
    "infineon.com": "Infineon",
    "samsung.com": "Samsung",
    "bourns.com": "Bourns",
    "nxp.com": "NXP",
    "microchip.com": "Microchip",
    "onsemi.com": "onsemi",
    "vishay.com": "Vishay",
    "murata.com": "Murata",
    "tdk.com": "TDK",
    "te.com": "TE Connectivity",
    "molex.com": "Molex",
    "amphenol.com": "Amphenol",
    "rohm.com": "ROHM",
    "renesas.com": "Renesas",
}


def is_trusted_domain(url: str) -> bool:
    """Return True if *url* is from an authorized distributor or manufacturer domain.

    Uses exact host match for distributors and a dot-prefix suffix match for
    manufacturer domains (e.g. ``www.ti.com`` matches ``ti.com`` but
    ``evil-ti.com`` does not).  Rejects non-http/https schemes and unparseable
    URLs.
    """
    return _host_allowed(url, AUTHORIZED_DISTRIBUTORS, MANUFACTURER_DOMAINS)
