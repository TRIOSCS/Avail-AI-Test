"""All settings, loaded from the .env file."""
from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # App
    app_url: str = "http://localhost:8000"
    secret_key: str = "change-me"
    database_url: str = "postgresql://availai:availai@localhost:5432/availai"

    # Microsoft Azure
    azure_client_id: str = ""
    azure_client_secret: str = ""
    azure_tenant_id: str = ""

    # AI Parsing
    anthropic_api_key: str = ""

    # Sourcing APIs
    octopart_api_key: str = ""
    brokerbin_api_key: str = ""
    brokerbin_api_secret: str = ""

    # Scoring weights (must add to 100)
    weight_recency: float = 30
    weight_quantity: float = 20
    weight_vendor_reliability: float = 20
    weight_data_completeness: float = 10
    weight_source_credibility: float = 10
    weight_price: float = 10

    # Behavior
    outreach_cooldown_days: int = 30
    poll_interval_minutes: int = 5
    auto_sighting_confidence: float = 0.7
    max_upload_size_mb: int = 50

    @property
    def async_database_url(self) -> str:
        url = self.database_url
        for old, new in [("postgresql://", "postgresql+asyncpg://"), ("postgres://", "postgresql+asyncpg://")]:
            if url.startswith(old):
                return url.replace(old, new, 1)
        return url

    class Config:
        env_file = ".env"


@lru_cache
def get_settings() -> Settings:
    return Settings()
