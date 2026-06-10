"""Security allowlist for OEM-sourced enrichment.

``is_oem_domain``: official system-vendor parts/support hosts (Lenovo, HPE, HP, Dell,
Acer, ASUS, IBM). A page on one of these may produce an ``oem_sourced`` description.

``is_crossref_domain``: the OEM-official set UNION the existing distributor + manufacturer
allowlist — a distributor/manufacturer page that lists an OEM FRU next to the commodity
MPN is acceptable evidence for the *linkage* (the resolved MPN is independently
re-verified against distributors regardless). Validated in code; the LLM's domain claims
are never trusted.

Called by: app.services.enrichment_worker.oem_extractor.
Depends on: app.services.enrichment_worker.trusted_domains.
"""

from __future__ import annotations

from urllib.parse import urlparse

from .trusted_domains import is_trusted_domain

# Exact-host official OEM/system-vendor parts & support domains.
OEM_OFFICIAL_HOSTS: frozenset[str] = frozenset(
    {
        "support.lenovo.com",
        "pcsupport.lenovo.com",
        "partsurfer.hpe.com",
        "partsurfer.hp.com",  # canonical HP PartSurfer host (also covered by the hp.com root)
        "partsurfer.com",
        "support.hpe.com",
        "support.hp.com",
        "parts.hp.com",
        "www.dell.com",
        "dell.com",
        "www.acer.com",
        "us.acer.com",
        "www.asus.com",
    }
)

# Vendor root domains matched by dot-suffix (foo.lenovo.com matches lenovo.com).
OEM_VENDOR_ROOTS: frozenset[str] = frozenset(
    {"lenovo.com", "hpe.com", "hp.com", "dell.com", "acer.com", "asus.com", "ibm.com"}
)


def is_oem_domain(url: str) -> bool:
    """Return True if *url* is an official OEM/system-vendor parts/support domain."""
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    host = p.hostname.lower()
    if host in OEM_OFFICIAL_HOSTS:
        return True
    return any(host == r or host.endswith("." + r) for r in OEM_VENDOR_ROOTS)


def is_crossref_domain(url: str) -> bool:
    """Return True if *url* is authoritative for asserting a FRU<->MPN linkage.

    OEM-official set plus the existing distributor / manufacturer allowlist.
    """
    return is_oem_domain(url) or is_trusted_domain(url)
