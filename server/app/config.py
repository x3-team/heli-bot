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
    # Testing without sign-up: POST /v1/auth/open hands out a token for one shared account.
    open_access: bool = Field(default=False, validation_alias="HELI_OPEN_ACCESS")
    open_access_email: str = Field(default="owner@heli.local", validation_alias="HELI_OPEN_ACCESS_EMAIL")
    smtp_host: str = Field(default="", validation_alias="SMTP_HOST")
    daily_limit_rub: Decimal = Field(default=Decimal("1000"), validation_alias="DAILY_LIMIT_RUB")
    default_monthly_limit_rub: Decimal = Field(
        default=Decimal("1000"),
        validation_alias="DEFAULT_MONTHLY_LIMIT_RUB",
    )
    # PC hands (use_computer on the user's own Windows). Off in v0.1: the agent works
    # in the cloud computer. Turn on when desktop 1С needs it.
    pc_hands: bool = Field(default=False, validation_alias="HELI_PC_HANDS")
    browser_max: int = Field(default=8, validation_alias="BROWSER_MAX")
    browser_image: str = Field(default="heli-browser:local", validation_alias="BROWSER_IMAGE")
    # Heli's own browser on this server (app/cloud_browser.py). Needs `pip install -e .[cloud]`.
    cloud_browser_enabled: bool = Field(default=False, validation_alias="CLOUD_BROWSER_ENABLED")
    cloud_browser_max: int = Field(default=2, validation_alias="CLOUD_BROWSER_MAX")
    cloud_browser_executable: str = Field(default="", validation_alias="CLOUD_BROWSER_EXECUTABLE")
    # Libraries unpacked without root by remote-install.sh, handed to Chromium as LD_LIBRARY_PATH.
    cloud_browser_libs: str = Field(default="", validation_alias="CLOUD_BROWSER_LIBS")
    cloud_browser_profiles: str = Field(default="var/browser-profiles", validation_alias="CLOUD_BROWSER_PROFILES")

    def __repr__(self) -> str:
        return f"Settings(app_env={self.app_env!r}, port={self.port})"


def get_settings() -> Settings:
    return Settings()
