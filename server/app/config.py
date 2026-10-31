from decimal import Decimal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration. Secrets come from the environment, never from the repo."""

    model_config = SettingsConfigDict(extra="ignore")

    app_env: str = Field(default="development", validation_alias="APP_ENV")
    host: str = Field(default="0.0.0.0", validation_alias="HOST")
    port: int = Field(default=8000, validation_alias="PORT")
    database_url: str = Field(
        default="sqlite:///./var/heli.db",
        validation_alias="DATABASE_URL",
    )
    jwt_secret: str = Field(default="", validation_alias="JWT_SECRET")
    trajectory_hash_pepper: str = Field(
        default="",
        validation_alias="TRAJECTORY_HASH_PEPPER",
    )
    smtp_host: str = Field(default="", validation_alias="SMTP_HOST")
    daily_limit_rub: Decimal = Field(default=Decimal("1000"), validation_alias="DAILY_LIMIT_RUB")
    default_monthly_limit_rub: Decimal = Field(
        default=Decimal("1000"),
        validation_alias="DEFAULT_MONTHLY_LIMIT_RUB",
    )
    browser_max: int = Field(default=8, validation_alias="BROWSER_MAX")
    browser_image: str = Field(default="heli-browser:local", validation_alias="BROWSER_IMAGE")

    def __repr__(self) -> str:
        return f"Settings(app_env={self.app_env!r}, port={self.port})"


def get_settings() -> Settings:
    return Settings()
