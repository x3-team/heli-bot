from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration. Secrets come from the environment, never from the repo."""

    model_config = SettingsConfigDict(extra="ignore")

    app_env: str = Field(default="development", validation_alias="APP_ENV")
    port: int = Field(default=8000, validation_alias="PORT")

    def __repr__(self) -> str:
        return f"Settings(app_env={self.app_env!r}, port={self.port})"


def get_settings() -> Settings:
    return Settings()
