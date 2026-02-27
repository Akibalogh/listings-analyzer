import json

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Gmail OAuth
    gmail_credentials_json: str = "{}"
    gmail_refresh_token: str = ""

    # Alert senders
    alert_senders: str = "ken.wile@redfin.com,KEY@northeastmatrixmail.com"

    # Anthropic
    anthropic_api_key: str = ""

    # Database
    database_url: str = "sqlite:///listings.db"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def sender_list(self) -> list[str]:
        return [s.strip() for s in self.alert_senders.split(",") if s.strip()]

    @property
    def gmail_credentials(self) -> dict:
        return json.loads(self.gmail_credentials_json)

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgres")


settings = Settings()
