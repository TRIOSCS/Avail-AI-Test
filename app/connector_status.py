"""Connector startup visibility — log which connectors are enabled/disabled."""

from loguru import logger

from .config import settings


def log_connector_status() -> dict[str, bool]:
    """Check each connector's credentials and log enabled/disabled status.

    Returns dict mapping connector name to enabled (True/False).
    """
    connectors = {
        "Nexar (Octopart)": bool(settings.nexar_client_id and settings.nexar_client_secret),
        "BrokerBin": bool(settings.brokerbin_api_key and settings.brokerbin_api_secret),
        "eBay": bool(settings.ebay_client_id and settings.ebay_client_secret),
        "DigiKey": bool(settings.digikey_client_id and settings.digikey_client_secret),
        "Mouser": bool(settings.mouser_api_key),
        "OEMSecrets": bool(settings.oemsecrets_api_key),
        "Sourcengine": bool(settings.sourcengine_api_key),
        "Element14": bool(settings.element14_api_key),
        "TME": bool(settings.tme_api_token and settings.tme_api_secret),
        "Anthropic AI": bool(settings.anthropic_api_key),
        "Azure OAuth": bool(
            settings.azure_client_id
            and settings.azure_client_secret
            and settings.azure_tenant_id
        ),
    }

    enabled = {k for k, v in connectors.items() if v}
    disabled = {k for k, v in connectors.items() if not v}

    if enabled:
        logger.info("Connectors enabled: {}", ", ".join(sorted(enabled)))
    if disabled:
        logger.warning("Connectors disabled (missing credentials): {}", ", ".join(sorted(disabled)))

    return connectors
