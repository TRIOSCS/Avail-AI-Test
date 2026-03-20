"""
Purpose: Application configuration from environment variables.
Description: All settings loaded via pydantic-settings BaseSettings with typed
    fields and validation. Fail-fast at startup if a required value is missing
    or has the wrong type.
Business Rules: DATABASE_URL must be postgresql:// (or sqlite:// when TESTING=1).
Called by: Nearly every module in the app.
Depends on: pydantic-settings.
"""

import os

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_VERSION = "3.1.0"

# Microsoft Graph API scopes — single source of truth for auth + token refresh.
# These must match exactly to prevent scope loss after token refresh.
GRAPH_SCOPES = (
    "openid profile email offline_access "
    "Mail.Send Mail.ReadWrite Contacts.Read MailboxSettings.Read User.Read "
    "Files.ReadWrite Chat.ReadWrite Calendars.Read "
    "ChannelMessage.Send Team.ReadBasic.All Channel.ReadBasic.All"
)


def _csv_to_list(val: str, *, lower: bool = True) -> list[str]:
    """Split a comma-separated string into a cleaned list."""
    items = [v.strip() for v in val.split(",") if v.strip()]
    return [v.lower() for v in items] if lower else items


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Core ---
    app_url: str = "http://localhost:8000"
    secret_key: str = "change-me-in-production"
    database_url: str = "postgresql://availai:availai@db:5432/availai"

    # --- Sentry ---
    sentry_dsn: str = ""
    sentry_traces_sample_rate: float = 0.1
    sentry_profiles_sample_rate: float = 0.1

    # --- Rate limiting ---
    rate_limit_default: str = "120/minute"
    rate_limit_search: str = "20/minute"
    rate_limit_enabled: bool = True
    rate_limit_ai_search: str = "10/minute"

    # --- Redis ---
    redis_url: str = "redis://redis:6379/0"
    cache_backend: str = "redis"

    # --- Microsoft Azure OAuth ---
    azure_client_id: str = ""
    azure_client_secret: str = ""
    azure_tenant_id: str = ""

    # --- Nexar (Octopart) ---
    nexar_client_id: str = ""
    nexar_client_secret: str = ""
    octopart_api_key: str = ""

    # --- BrokerBin ---
    brokerbin_api_key: str = ""
    brokerbin_api_secret: str = ""

    # --- eBay ---
    ebay_client_id: str = ""
    ebay_client_secret: str = ""

    # --- DigiKey ---
    digikey_client_id: str = ""
    digikey_client_secret: str = ""

    # --- Mouser ---
    mouser_api_key: str = ""

    # --- OEMSecrets ---
    oemsecrets_api_key: str = ""

    # --- Sourcengine ---
    sourcengine_api_key: str = ""

    # --- element14 / Newark ---
    element14_api_key: str = ""

    # --- AI ---
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"

    # --- Agent service-to-service auth ---
    agent_api_key: str = ""

    # --- Explorium / Vibe Prospecting ---
    explorium_api_key: str = ""
    explorium_api_base_url: str = "https://api.explorium.ai"

    # --- Customer enrichment ---
    customer_enrichment_enabled: bool = True
    customer_enrichment_cooldown_days: int = 90
    customer_enrichment_contacts_per_account: int = 5

    # --- Material card AI enrichment ---
    material_enrichment_enabled: bool = True
    material_enrichment_batch_size: int = 300

    # --- Tagging ---
    min_tag_confidence: float = 0.90

    # --- AI Features ---
    ai_features_enabled: str = "mike_only"  # "all", "mike_only", "off"

    # --- Email Intelligence ---
    email_mining_enabled: bool = False
    email_mining_lookback_days: int = 180

    # --- M365 Integration v2 ---
    inbox_scan_interval_min: int = 30
    inbox_backfill_days: int = 180
    contacts_sync_enabled: bool = True

    # --- Admin (CSV string, parsed to list in model_post_init) ---
    admin_emails: str = ""

    # --- RFQ ---
    follow_up_days: int = 3

    # --- Activity tracking & customer ownership ---
    activity_tracking_enabled: bool = True
    ownership_sweep_enabled: bool = False
    customer_inactivity_days: int = 30
    strategic_inactivity_days: int = 90
    customer_warning_days: int = 23
    offer_attribution_days: int = 14
    vendor_protection_warn_days: int = 60
    vendor_protection_drop_days: int = 90
    routing_window_hours: int = 48
    collision_lookback_days: int = 7

    # --- Proactive offers ---
    proactive_matching_enabled: bool = True
    proactive_throttle_days: int = 21
    proactive_scan_interval_hours: int = 4
    excess_bid_scan_enabled: bool = True
    excess_bid_parse_lookback_days: int = 14
    proactive_min_margin_pct: float = 10.0
    proactive_match_expiry_days: int = 30

    # --- Buy plan (CSV strings, parsed to lists in model_post_init) ---
    stock_sale_vendor_names: str = "trio,trio supply chain,stock,internal"
    stock_sale_notify_emails: str = "logistics@trioscs.com,accounting@trioscs.com"
    buyplan_auto_complete_hour: int = 18
    buyplan_auto_complete_tz: str = "America/New_York"
    po_verify_interval_min: int = 30

    # --- Buy Plan V3 ---
    buyplan_auto_approve_threshold: float = 5000
    buyplan_stale_offer_days: int = 5
    buyplan_min_margin_pct: float = 10
    buyplan_nudge_buyer_hours: int = 4
    buyplan_escalate_manager_hours: int = 8
    buyplan_nudge_ops_hours: int = 2
    buyplan_favoritism_threshold_pct: float = 60
    buyplan_better_offer_pct: float = 5

    # --- Search ---
    search_concurrency_limit: int = 10

    # --- Contact intelligence ---
    contact_scoring_enabled: bool = True
    contact_nudge_dormant_days: int = 30
    contact_nudge_cooling_days: int = 14

    # --- Own company domains (CSV string, parsed to frozenset in model_post_init) ---
    own_domains: str = "trioscs.com"

    # --- 8x8 Work Analytics ---
    eight_by_eight_api_key: str = ""
    eight_by_eight_username: str = ""
    eight_by_eight_password: str = ""
    eight_by_eight_pbx_id: str = ""
    eight_by_eight_timezone: str = "America/Los_Angeles"
    eight_by_eight_enabled: bool = False
    eight_by_eight_poll_interval_minutes: int = 30

    # --- MVP Mode ---
    # When True, disables: Dashboard/Analytics, Enrichment, Teams, Task Manager
    # Set MVP_MODE=false in .env to re-enable all features
    mvp_mode: bool = True

    # --- Frontend ---
    use_htmx: bool = True

    # --- On-demand enrichment orchestrator ---
    on_demand_enrichment_enabled: bool = True

    # --- Prospecting ---
    prospecting_enabled: bool = True
    prospecting_min_fit_for_contacts: int = 60
    prospecting_expire_days: int = 90
    prospecting_resurface_days: int = 180

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        testing = os.getenv("TESTING", "").strip()
        if testing == "1" and v.startswith("sqlite"):
            return v
        if not v.startswith("postgresql"):
            raise ValueError("DATABASE_URL must start with 'postgresql://'")
        return v

    @field_validator("sentry_traces_sample_rate", "sentry_profiles_sample_rate")
    @classmethod
    def validate_sample_rate(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("Sample rate must be between 0.0 and 1.0")
        return v

    @field_validator("min_tag_confidence")
    @classmethod
    def validate_confidence(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("Confidence/threshold must be between 0.0 and 1.0")
        return v

    @model_validator(mode="after")
    def parse_csv_fields(self) -> "Settings":
        """Convert CSV string fields to their runtime list/frozenset types."""
        # These are stored as str to avoid pydantic-settings JSON parsing issues
        # with dotenv sources, but at runtime code expects list/frozenset.
        object.__setattr__(self, "admin_emails", _csv_to_list(self.admin_emails))
        object.__setattr__(self, "stock_sale_vendor_names", _csv_to_list(self.stock_sale_vendor_names))
        object.__setattr__(self, "stock_sale_notify_emails", _csv_to_list(self.stock_sale_notify_emails))
        object.__setattr__(
            self, "own_domains", frozenset(d.strip().lower() for d in self.own_domains.split(",") if d.strip())
        )
        return self


# Handle SESSION_SECRET → secret_key fallback before instantiation
if os.getenv("SESSION_SECRET") and not os.getenv("SECRET_KEY"):
    os.environ["SECRET_KEY"] = os.getenv("SESSION_SECRET", "")

settings = Settings()
