import hashlib
import json

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Gmail OAuth
    gmail_credentials_json: str = "{}"
    gmail_refresh_token: str = ""
    # The inbox that receives the Redfin alerts. Used as the OAuth login_hint
    # and verified in the reauth callback so the token can't be accidentally
    # granted for the wrong Google account (e.g. the dashboard-login address).
    gmail_account_email: str = "akibalogh@gmail.com"

    # Alert senders (supports domains like "redfin.com" for all senders from that domain)
    alert_senders: str = ""

    # Date-filtered senders: "email:days,email:days" — only fetch emails newer than N days
    sender_date_filters: str = ""

    # Max email age in days (0 = no limit). Emails older than this are ignored.
    max_email_age_days: int = 21

    # Anthropic
    anthropic_api_key: str = ""

    # Database
    database_url: str = "sqlite:///listings.db"

    # Auth
    allowed_emails: str = ""
    session_secret: str = ""

    # Scheduled polling (hours between auto-polls; 0 = disabled)
    poll_interval_hours: int = 1

    # Management API key (for sync-criteria endpoint)
    manage_key: str = ""

    # Slack webhook URL for listing notifications
    slack_webhook_url: str = ""

    # Pushover phone push (set both to enable): create an app at pushover.net
    pushover_token: str = ""   # application API token
    pushover_user: str = ""    # user/group key(s) — comma-separated for >1 recipient

    @property
    def pushover_user_keys(self) -> list[str]:
        """Individual Pushover recipient keys (comma-separated in the env var)."""
        return [u.strip() for u in self.pushover_user.split(",") if u.strip()]

    # ntfy phone push (open-source; replaces Pushover when NTFY_TOPIC is set).
    # SECURITY: on public ntfy.sh the topic name is the ONLY access control —
    # anyone who knows or guesses it can read every alert and publish fake
    # ones. Use a long random topic, keep it in .env, never commit it. Set
    # NTFY_TOKEN as well to use an access-controlled topic.
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    ntfy_token: str = ""

    @property
    def ntfy_url(self) -> str:
        """Full publish URL for the configured topic ("" when unconfigured)."""
        if not self.ntfy_topic.strip():
            return ""
        return f"{self.ntfy_server.rstrip('/')}/{self.ntfy_topic.strip()}"
    # Score at or above which a new listing triggers a phone push.
    #
    # 70 under the v77 arithmetic. The bar was 75 when 72 was the model's
    # habitual "clears the gates, nothing decisive" score and half the board
    # clustered there; v77's honest ledger removed that clustering, and under
    # it a 75 bar would demand a 95th-percentile school district before the
    # phone ever buzzed — the 80th-94th band, where the actual purchase most
    # plausibly lives, could never alert on any strength. 70 admits precisely
    # the tier the buyer cares about: confirmed ground-floor bedrooms in good
    # districts. Re-check the selectivity after the v77 rescore lands.
    notify_score_threshold: int = 70

    # How far a score must fall below the threshold before the notify latch is
    # re-armed. Not cosmetic: `enqueue_missing` re-queues an AI score job every
    # hour for any listing with an enrichment gap, and 85 of 163 listings have a
    # permanently un-scrapeable description or image set, so those are re-scored
    # by the model hourly, forever. Scores band at 68/71/72/73/76 — one band of
    # LLM jitter across the bar was enough to clear the latch and re-alert, every
    # hour, for a house nothing had actually changed about.
    #
    # 10 points is wider than any band gap and narrower than the failure the
    # latch exists for: 38 Westerly Ln fell from 72 to 48 on a mis-resolved
    # commute and deserved a second alert when that was fixed.
    notify_rearm_margin: int = 10

    # Public dashboard URL. Alerts fall back to it when a listing has no URL
    # of its own (see _alert_link).
    public_base_url: str = "https://listings-analyzer.fly.dev"

    # Agent name mapping: "email_or_domain:Agent Name,email_or_domain:Agent Name"
    # e.g. "redfin.com:Redfin Agent,broker@example.com:Broker Name"
    agent_map: str = ""

    # AI evaluation model
    ai_eval_model: str = "claude-haiku-4-5-20251001"

    # Hard commute limit in minutes — listings at or over this are rejected
    # deterministically before any AI call (mirrors criteria hard requirements)
    commute_hard_limit_minutes: int = 110

    # The rest of the checkable hard requirements. These mirror the criteria
    # prose and are enforced in code for the same reason the commute limit is:
    # the AI cannot be trusted to apply a numeric threshold it can see. It
    # invented a "$1,130,000 hard cap" for a $2.25M band and marked a
    # 5,962 sqft house as failing a 2,200 sqft minimum. Unknown values never
    # gate — only explicit failures do.
    price_min_dollars: int = 850_000
    price_max_dollars: int = 2_250_000
    min_sqft: int = 2_200
    min_bedrooms: int = 3

    # Elementary-school state ranking below which a district is a dealbreaker.
    # Above it the criteria only deducts points (-20 for 50th-79th), so a
    # school "hard failure" above this floor is not one.
    min_school_percentile: int = 50

    # The scoring arithmetic's base: score = base + sum(soft_points), clamped.
    # Mirrors "Base score: N" in the active criteria; hard_gate_drift() flags a
    # mismatch on /health, same as every other criteria-mirrored number.
    # 50, up from v75/v76's 30: regression of holistic scores on ledger
    # contributions put the model's natural anchor at ~61, and a base of 30
    # made the stated weights unpayable (raw ledger sums centered at 16 while
    # scores centered at 60 — the +35 median mismatch the contract kept
    # catching but could not cure).
    score_base_points: int = 50

    # Redfin saved-search filter. Redfin CAPTCHA-gates the scrape, so this no
    # longer feeds a scheduled sync — it's used only by the pending-detection
    # presence check (best-effort) and the manual /manage/sync-search endpoint.
    # Override via REDFIN_SEARCH_URL when the filter changes.
    redfin_search_url: str = (
        "https://www.redfin.com/city/30738/NY/Yorktown/filter/"
        "dyos-shape-id=98684490,property-type=house,min-price=1M,max-price=2.25M,"
        "min-beds=4,min-sqft=2.25k-sqft,min-parking=1,basement-type=finished+unfinished"
    )

    # Jina Reader API key (optional) — unauthenticated r.jina.ai is rate-limited
    # per IP, which heavy scrape days can exhaust
    jina_api_key: str = ""

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
    def agent_map_dict(self) -> dict[str, str]:
        """Parse agent_map into {email_or_domain: agent_name}."""
        if not self.agent_map.strip():
            return {}
        result = {}
        for entry in self.agent_map.split(","):
            entry = entry.strip()
            if ":" in entry:
                key, name = entry.split(":", 1)
                result[key.strip().lower()] = name.strip()
        return result

    def resolve_agent_name(self, sender_email: str) -> str | None:
        """Resolve an email sender to an agent name using agent_map.

        Matches full email first, then domain.
        """
        if not sender_email:
            return None
        mapping = self.agent_map_dict
        if not mapping:
            return None
        # Normalize: extract email from "Name <email>" format
        email = sender_email.lower().strip()
        if "<" in email:
            email = email.split("<")[-1].rstrip(">").strip()
        # Exact email match
        if email in mapping:
            return mapping[email]
        # Domain match
        domain = email.split("@")[-1] if "@" in email else ""
        if domain and domain in mapping:
            return mapping[domain]
        return None

    @property
    def effective_session_secret(self) -> str:
        """Session signing key. Falls back to a hash of credentials for stability across workers."""
        if self.session_secret:
            return self.session_secret
        # Derive from credentials JSON — stable across workers and restarts
        return hashlib.sha256(self.gmail_credentials_json.encode()).hexdigest()


settings = Settings()
