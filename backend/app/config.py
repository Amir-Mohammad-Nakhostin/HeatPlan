"""HeatPlan configuration. Secrets come from environment / .env only."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")


@dataclass
class Settings:
    fortyguard_api_key: str = os.getenv("FORTYGUARD_API_KEY", "")
    fortyguard_base_url: str = os.getenv("FORTYGUARD_BASE_URL", "https://api.fortyguard.com")
    fortyguard_mock: str = os.getenv("FORTYGUARD_MOCK", "auto").lower()  # auto|true|false
    poll_interval_s: float = float(os.getenv("FORTYGUARD_POLL_INTERVAL_S", "5"))
    poll_timeout_s: float = float(os.getenv("FORTYGUARD_POLL_TIMEOUT_S", "600"))
    cache_ttl_s: int = int(os.getenv("CACHE_TTL_S", "900"))
    llm_provider: str = os.getenv("LLM_PROVIDER", "").lower()
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    default_city_name: str = os.getenv("DEFAULT_CITY_NAME", "Phoenix, AZ")
    default_city_lat: float = float(os.getenv("DEFAULT_CITY_LAT", "33.4484"))
    default_city_lon: float = float(os.getenv("DEFAULT_CITY_LON", "-112.0740"))

    @property
    def use_mock(self) -> bool:
        if self.fortyguard_mock == "true":
            return True
        if self.fortyguard_mock == "false":
            return False
        return not self.fortyguard_api_key  # auto: mock when no key configured

    @property
    def data_source(self) -> str:
        return "mock" if self.use_mock else "fortyguard_live"


settings = Settings()
