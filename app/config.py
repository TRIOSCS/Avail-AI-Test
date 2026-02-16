"""Configuration — all settings from environment variables."""
import os

APP_VERSION = "1.5.2"

class Settings:
    app_url: str = os.getenv("APP_URL", "http://localhost:8000")
    secret_key: str = os.getenv("SESSION_SECRET") or os.getenv("SECRET_KEY", "change-me-in-production")
    database_url: str = os.getenv("DATABASE_URL", "postgresql://availai:availai@db:5432/availai")

    # Microsoft Azure OAuth
    azure_client_id: str = os.getenv("AZURE_CLIENT_ID", "")
    azure_client_secret: str = os.getenv("AZURE_CLIENT_SECRET", "")
    azure_tenant_id: str = os.getenv("AZURE_TENANT_ID", "")

    # Nexar (Octopart) API
    nexar_client_id: str = os.getenv("NEXAR_CLIENT_ID", "")
    nexar_client_secret: str = os.getenv("NEXAR_CLIENT_SECRET", "")

    # BrokerBin API
    brokerbin_api_key: str = os.getenv("BROKERBIN_API_KEY", "")
    brokerbin_api_secret: str = os.getenv("BROKERBIN_API_SECRET", "")

    # eBay Browse API
    ebay_client_id: str = os.getenv("EBAY_CLIENT_ID", "")
    ebay_client_secret: str = os.getenv("EBAY_CLIENT_SECRET", "")

    # DigiKey API
    digikey_client_id: str = os.getenv("DIGIKEY_CLIENT_ID", "")
    digikey_client_secret: str = os.getenv("DIGIKEY_CLIENT_SECRET", "")

    # Mouser API
    mouser_api_key: str = os.getenv("MOUSER_API_KEY", "")

    # OEMSecrets API
    oemsecrets_api_key: str = os.getenv("OEMSECRETS_API_KEY", "")

    # Sourcengine API
    sourcengine_api_key: str = os.getenv("SOURCENGINE_API_KEY", "")

    # AI parsing
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

    # Acctivate SQL Server (read-only)
    acctivate_host: str = os.getenv("ACCTIVATE_HOST", "")
    acctivate_port: int = int(os.getenv("ACCTIVATE_PORT", "1433"))
    acctivate_user: str = os.getenv("ACCTIVATE_USER", "")
    acctivate_password: str = os.getenv("ACCTIVATE_PASSWORD", "")
    acctivate_database: str = os.getenv("ACCTIVATE_DATABASE", "")

    # Enrichment APIs
    clay_api_key: str = os.getenv("CLAY_API_KEY", "")
    explorium_api_key: str = os.getenv("EXPLORIUM_API_KEY", "")
    apollo_api_key: str = os.getenv("APOLLO_API_KEY", "")

    # AI Features (Definitive Spec)
    ai_features_enabled: str = os.getenv("AI_FEATURES_ENABLED", "mike_only")  # "all", "mike_only", "off"

    # Email Intelligence
    email_mining_enabled: bool = os.getenv("EMAIL_MINING_ENABLED", "false").lower() == "true"
    email_mining_lookback_days: int = int(os.getenv("EMAIL_MINING_LOOKBACK_DAYS", "180"))

    # M365 Integration v2
    inbox_scan_interval_min: int = int(os.getenv("INBOX_SCAN_INTERVAL_MIN", "30"))
    inbox_backfill_days: int = int(os.getenv("INBOX_BACKFILL_DAYS", "180"))
    contacts_sync_enabled: bool = os.getenv("CONTACTS_SYNC_ENABLED", "true").lower() == "true"

    # Scoring weights
    weight_recency: int = int(os.getenv("WEIGHT_RECENCY", "30"))
    weight_quantity: int = int(os.getenv("WEIGHT_QUANTITY", "20"))
    weight_vendor_reliability: int = int(os.getenv("WEIGHT_VENDOR_RELIABILITY", "20"))
    weight_data_completeness: int = int(os.getenv("WEIGHT_DATA_COMPLETENESS", "10"))
    weight_source_credibility: int = int(os.getenv("WEIGHT_SOURCE_CREDIBILITY", "10"))
    weight_price: int = int(os.getenv("WEIGHT_PRICE", "10"))

    # Admin
    admin_emails: list = [e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "mkhoury@trioscs.com").split(",") if e.strip()]

    # Microsoft Teams (Buy Plan notifications)
    teams_webhook_url: str = os.getenv("TEAMS_WEBHOOK_URL", "")
    teams_team_id: str = os.getenv("TEAMS_TEAM_ID", "")
    teams_channel_id: str = os.getenv("TEAMS_CHANNEL_ID", "")

    # Activity tracking & customer ownership (v1.3.0)
    activity_tracking_enabled: bool = os.getenv("ACTIVITY_TRACKING_ENABLED", "true").lower() == "true"
    customer_inactivity_days: int = int(os.getenv("CUSTOMER_INACTIVITY_DAYS", "30"))
    strategic_inactivity_days: int = int(os.getenv("STRATEGIC_INACTIVITY_DAYS", "90"))
    customer_warning_days: int = int(os.getenv("CUSTOMER_WARNING_DAYS", "23"))
    offer_attribution_days: int = int(os.getenv("OFFER_ATTRIBUTION_DAYS", "14"))
    vendor_protection_warn_days: int = int(os.getenv("VENDOR_PROTECTION_WARN_DAYS", "60"))
    vendor_protection_drop_days: int = int(os.getenv("VENDOR_PROTECTION_DROP_DAYS", "90"))
    routing_window_hours: int = int(os.getenv("ROUTING_WINDOW_HOURS", "48"))
    collision_lookback_days: int = int(os.getenv("COLLISION_LOOKBACK_DAYS", "7"))

    # Proactive offers
    proactive_matching_enabled: bool = os.getenv("PROACTIVE_MATCHING_ENABLED", "true").lower() == "true"
    proactive_archive_age_days: int = int(os.getenv("PROACTIVE_ARCHIVE_AGE_DAYS", "30"))
    proactive_throttle_days: int = int(os.getenv("PROACTIVE_THROTTLE_DAYS", "21"))

settings = Settings()
