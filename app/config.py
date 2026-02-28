import hashlib
import json

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Gmail OAuth
    gmail_credentials_json: str = "{}"
    gmail_refresh_token: str = ""

    # Alert senders
    alert_senders: str = "ken.wile@redfin.com,alerts@mls.example.com,noreply@redfin.com"

    # Anthropic
    anthropic_api_key: str = ""

    # Database
    database_url: str = "sqlite:///listings.db"

    # Auth
    allowed_emails: str = "you@example.com,friend@example.com,alt@example.com"
    session_secret: str = ""

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

    @property
    def allowed_email_list(self) -> list[str]:
        return [e.strip().lower() for e in self.allowed_emails.split(",") if e.strip()]

    @property
    def google_client_id(self) -> str:
        """Extract client_id from Gmail credentials (works for both web and installed types)."""
        creds = self.gmail_credentials
        for key in ("web", "installed"):
            if key in creds:
                return creds[key].get("client_id", "")
        return ""

    @property
    def effective_session_secret(self) -> str:
        """Session signing key. Falls back to a hash of credentials for stability across workers."""
        if self.session_secret:
            return self.session_secret
        # Derive from credentials JSON — stable across workers and restarts
        return hashlib.sha256(self.gmail_credentials_json.encode()).hexdigest()


settings = Settings()
