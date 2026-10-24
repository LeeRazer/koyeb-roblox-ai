from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _database_url() -> str:
    configured = os.getenv("DATABASE_URL", "").strip()
    if configured.startswith("postgres://"):
        configured = "postgresql+psycopg://" + configured.removeprefix("postgres://")
    elif configured.startswith("postgresql://"):
        configured = "postgresql+psycopg://" + configured.removeprefix("postgresql://")
    if configured:
        return configured

    sqlite_path = Path(os.getenv("SQLITE_PATH", "zombie_ai_brain_v2.db")).resolve()
    return f"sqlite:///{sqlite_path.as_posix()}"


@dataclass(frozen=True, slots=True)
class Settings:
    app_env: str
    database_url: str
    api_key: str | None
    write_rate_limit_per_minute: int
    max_batch_size: int
    learning_rate: float
    service_version: str = "4.0.0"
    schema_version: int = 2

    @property
    def persistent_database(self) -> bool:
        return not self.database_url.startswith("sqlite:")


def load_settings() -> Settings:
    api_key = os.getenv("AI_API_KEY", "").strip() or None
    return Settings(
        app_env=os.getenv("APP_ENV", "development").strip().lower(),
        database_url=_database_url(),
        api_key=api_key,
        write_rate_limit_per_minute=_integer("WRITE_RATE_LIMIT_PER_MINUTE", 120, 10, 10_000),
        max_batch_size=_integer("MAX_BATCH_SIZE", 100, 1, 500),
        learning_rate=_float("LEARNING_RATE", 0.12, 0.01, 0.50),
    )

