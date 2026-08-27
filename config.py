from functools import lru_cache
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("DATABASE_URL", "database_url"),
    )
    mysql_url: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("MYSQL_URL", "mysql_url"),
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def resolved_database_url(self) -> str:
        raw_url = self.database_url or self.mysql_url
        if not raw_url:
            raise RuntimeError(
                "DATABASE_URL 또는 MYSQL_URL 환경변수가 필요합니다. "
                ".env.example을 참고하세요."
            )
        return normalize_database_url(raw_url)


def normalize_database_url(database_url: str) -> str:
    """Normalize MySQL URLs to the asyncmy SQLAlchemy dialect."""
    parsed = urlsplit(database_url)
    if parsed.scheme in {"mysql", "mysql+aiomysql"}:
        parsed = parsed._replace(scheme="mysql+asyncmy")
        return urlunsplit(parsed)
    return database_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
