from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy import URL


class ClassifierProvider(StrEnum):
    JEV = "jev"
    LAYA = "laya"


class LLMProvider(StrEnum):
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"


# Docker/Compose mounts secrets here as files named after the field (e.g. db_password).
# Only enabled if the directory exists, so local runs and tests don't warn about it.
_SECRETS_DIR = "/run/secrets" if Path("/run/secrets").is_dir() else None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", frozen=True, secrets_dir=_SECRETS_DIR)

    classifier_provider: ClassifierProvider = ClassifierProvider.LAYA
    llm_provider: LLMProvider = LLMProvider.OLLAMA
    poll_interval_seconds: int = Field(default=120, ge=30, le=3600)
    # Each poll re-searches this far before the saved watermark (search index lag, clock skew).
    poll_overlap_minutes: int = Field(default=10, ge=1, le=1440)
    # How often the worker checks the job queue for queued backfills (when idle).
    job_poll_seconds: int = Field(default=30, ge=5, le=300)
    # REGION_PACKS=india,us  (NoDecode: a plain comma list, not the JSON pydantic expects)
    region_packs: Annotated[tuple[str, ...], NoDecode] = ("india",)
    # IANA zone for calendar days: a 00:30 IST alert belongs to that IST day, not the UTC one.
    tz: str = "Asia/Kolkata"

    # A writable DIRECTORY is mounted at /app/state/gmail: the token is rewritten on refresh.
    gmail_token_file: Path = Path("/app/state/gmail/gmail_token.json")

    db_host: str = "db"
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str = "money_radar"
    db_user: str = "money_radar"
    db_password: SecretStr  # required: no default, so a missing password fails at startup

    @field_validator("region_packs", mode="before")
    @classmethod
    def _split_region_packs(cls, value: object) -> object:
        if isinstance(value, str):
            value = tuple(code.strip().lower() for code in value.split(",") if code.strip())
        if not value:
            raise ValueError("REGION_PACKS must name at least one pack, e.g. 'india'")
        return value

    @field_validator("tz")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"TZ must be an IANA zone like 'Asia/Kolkata' (got {value!r})") from e
        return value

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def database_url(self) -> URL:
        # URL.create percent-encodes the password; str(URL) masks it as ***.
        return URL.create(
            "postgresql+psycopg",
            username=self.db_user,
            password=self.db_password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
