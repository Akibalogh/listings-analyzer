import hashlib
import json

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Gmail OAuth
    gmail_credentials_json: str = "{}"
    gmail_refresh_token: str = ""

    # Alert senders (supports domains like "redfin.com" for all senders from that domain)
    alert_senders: str = "redfin.com,alerts@mls.example.com"

    # Date-filtered senders: "email:days,email:days" — only fetch emails newer than N days
    sender_date_filters: str = ""

    # Max email age in days (0 = no limit). Emails older than this are ignored.
    max_email_age_days: int = 21

    # Anthropic
    anthropic_api_key: str = ""

    # Database
    database_url: str = "sqlite:///listings.db"

    # Auth
    allowed_emails: str = "you@example.com,friend@example.com,alt@example.com"
    session_secret: str = ""

    # Scheduled polling (hours between auto-polls; 0 = disabled)
    poll_interval_hours: int = 1

    # Management API key (for sync-criteria endpoint)
    manage_key: str = ""

    # AI evaluation model
    ai_eval_model: str = "claude-haiku-4-5-20251001"

    # SchoolDigger API (free dev tier: 20 calls/day)
    schooldigger_app_id: str = ""
    schooldigger_app_key: str = ""

    # Google Maps / Routes API
    google_maps_api_key: str = ""

    # Commute destination (default: Brookfield Place)
    commute_destination: str = "Brookfield Place, 230 Vesey St, New York, NY 10281"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def sender_list(self) -> list[str]:
        return [s.strip() for s in self.alert_senders.split(",") if s.strip()]

    @property
    def date_filtered_sender_list(self) -> list[tuple[str, int]]:
        """Parse date-filtered senders into [(email, days), ...]."""
        if not self.sender_date_filters.strip():
            return []
        result = []
        for entry in self.sender_date_filters.split(","):
            entry = entry.strip()
            if ":" in entry:
                email, days_str = entry.rsplit(":", 1)
                try:
                    result.append((email.strip(), int(days_str.strip())))
                except ValueError:
                    continue
        return result

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
