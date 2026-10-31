from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration. Secrets come from the environment, never from the repo."""

    model_config = SettingsConfigDict(extra="ignore")

    app_env: str = Field(default="development", validation_alias="APP_ENV")
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

    def __repr__(self) -> str:
        return f"Settings(app_env={self.app_env!r}, port={self.port})"


def get_settings() -> Settings:
    return Settings()
