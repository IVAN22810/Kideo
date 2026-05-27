from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = "development"
    port: int = 4242
    database_path: str = "./data/minor.db"
    log_level: str = "debug"


settings = Settings()