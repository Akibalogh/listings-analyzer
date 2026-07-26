"""Tests for Slack webhook notifications."""

from unittest.mock import MagicMock, patch


SAMPLE_LISTING = {
    "address": "123 Main St",
    "town": "Scarsdale",
    "state": "NY",
    "price": 1500000,
    "sqft": 3200,
    "bedrooms": 4,
    "bathrooms": 3,
    "commute_minutes": 55,
    "listing_url": "https://www.redfin.com/NY/Scarsdale/123-Main-St",
}


class TestNotifyNewListing:
    """Tests for notify_new_listing()."""

    def test_does_nothing_when_webhook_url_empty(self):
        """No HTTP call when slack_webhook_url is not configured."""
        from app.notifier import notify_new_listing

        with patch("app.notifier.settings") as mock_settings:
            mock_settings.slack_webhook_url = ""
            with patch("app.notifier.httpx.post") as mock_post:
                notify_new_listing(SAMPLE_LISTING, 75, "Worth Touring", "ai")
                mock_post.assert_not_called()

    def test_does_nothing_for_low_priority_verdict(self):
        """No HTTP call for verdicts below the notification threshold."""
        from app.notifier import notify_new_listing

        with patch("app.notifier.settings") as mock_settings:
            mock_settings.slack_webhook_url = "https://hooks.slack.com/services/test"
            with patch("app.notifier.httpx.post") as mock_post:
                notify_new_listing(SAMPLE_LISTING, 45, "Low Priority", "ai")
                mock_post.assert_not_called()

    def test_does_nothing_for_weak_match_verdict(self):
        """No HTTP call for Weak Match."""
        from app.notifier import notify_new_listing

        with patch("app.notifier.settings") as mock_settings:
            mock_settings.slack_webhook_url = "https://hooks.slack.com/services/test"
            with patch("app.notifier.httpx.post") as mock_post:
                notify_new_listing(SAMPLE_LISTING, 30, "Weak Match", "ai")
                mock_post.assert_not_called()

    def test_does_nothing_for_reject_verdict(self):
        """No HTTP call for Reject."""
        from app.notifier import notify_new_listing

        with patch("app.notifier.settings") as mock_settings:
            mock_settings.slack_webhook_url = "https://hooks.slack.com/services/test"
            with patch("app.notifier.httpx.post") as mock_post:
                notify_new_listing(SAMPLE_LISTING, 0, "Reject", "ai")
                mock_post.assert_not_called()

    def test_posts_to_webhook_for_worth_touring(self):
        """Makes an HTTP POST when verdict is Worth Touring."""
        from app.notifier import notify_new_listing

        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None

        with patch("app.notifier.settings") as mock_settings:
            mock_settings.slack_webhook_url = "https://hooks.slack.com/services/test"
            with patch("app.notifier.httpx.post", return_value=mock_resp) as mock_post:
                notify_new_listing(SAMPLE_LISTING, 75, "Worth Touring", "ai")
                mock_post.assert_called_once()
                call_kwargs = mock_post.call_args
                assert call_kwargs[0][0] == "https://hooks.slack.com/services/test"

    def test_posts_to_webhook_for_strong_match(self):
        """Makes an HTTP POST when verdict is Strong Match."""
        from app.notifier import notify_new_listing

        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None

        with patch("app.notifier.settings") as mock_settings:
            mock_settings.slack_webhook_url = "https://hooks.slack.com/services/test"
            with patch("app.notifier.httpx.post", return_value=mock_resp) as mock_post:
                notify_new_listing(SAMPLE_LISTING, 85, "Strong Match", "ai")
                mock_post.assert_called_once()

    def test_message_contains_address(self):
        """Slack message payload contains the listing address."""
        from app.notifier import notify_new_listing

        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None

        with patch("app.notifier.settings") as mock_settings:
            mock_settings.slack_webhook_url = "https://hooks.slack.com/services/test"
            with patch("app.notifier.httpx.post", return_value=mock_resp) as mock_post:
                notify_new_listing(SAMPLE_LISTING, 75, "Worth Touring", "ai")
                payload = mock_post.call_args[1]["json"]
                assert "123 Main St" in payload["text"]

    def test_message_contains_verdict(self):
        """Slack message payload contains the verdict string."""
        from app.notifier import notify_new_listing

        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None

        with patch("app.notifier.settings") as mock_settings:
            mock_settings.slack_webhook_url = "https://hooks.slack.com/services/test"
            with patch("app.notifier.httpx.post", return_value=mock_resp) as mock_post:
                notify_new_listing(SAMPLE_LISTING, 75, "Worth Touring", "ai")
                payload = mock_post.call_args[1]["json"]
                assert "Worth Touring" in payload["text"]

    def test_message_contains_score(self):
        """Slack message payload contains the numeric score."""
        from app.notifier import notify_new_listing

        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None

        with patch("app.notifier.settings") as mock_settings:
            mock_settings.slack_webhook_url = "https://hooks.slack.com/services/test"
            with patch("app.notifier.httpx.post", return_value=mock_resp) as mock_post:
                notify_new_listing(SAMPLE_LISTING, 75, "Worth Touring", "ai")
                payload = mock_post.call_args[1]["json"]
                assert "75" in payload["text"]

    def test_message_contains_price(self):
        """Slack message includes the formatted listing price."""
        from app.notifier import notify_new_listing

        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None

        with patch("app.notifier.settings") as mock_settings:
            mock_settings.slack_webhook_url = "https://hooks.slack.com/services/test"
            with patch("app.notifier.httpx.post", return_value=mock_resp) as mock_post:
                notify_new_listing(SAMPLE_LISTING, 75, "Worth Touring", "ai")
                payload = mock_post.call_args[1]["json"]
                assert "1,500,000" in payload["text"]

    def test_fails_silently_on_http_error(self):
        """An HTTP error from the webhook does not raise an exception."""
        from app.notifier import notify_new_listing

        with patch("app.notifier.settings") as mock_settings:
            mock_settings.slack_webhook_url = "https://hooks.slack.com/services/test"
            with patch("app.notifier.httpx.post", side_effect=Exception("connection refused")):
                # Should not raise
                notify_new_listing(SAMPLE_LISTING, 75, "Worth Touring", "ai")

    def test_strong_match_uses_house_emoji(self):
        """Strong Match uses a different emoji than Worth Touring."""
        from app.notifier import notify_new_listing

        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None

        with patch("app.notifier.settings") as mock_settings:
            mock_settings.slack_webhook_url = "https://hooks.slack.com/services/test"
            with patch("app.notifier.httpx.post", return_value=mock_resp) as mock_post:
                notify_new_listing(SAMPLE_LISTING, 85, "Strong Match", "ai")
                payload = mock_post.call_args[1]["json"]
                assert "🏡" in payload["text"]

    def test_listing_url_included_in_message(self):
        """Listing URL is embedded in the Slack message when present."""
        from app.notifier import notify_new_listing

        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None

        with patch("app.notifier.settings") as mock_settings:
            mock_settings.slack_webhook_url = "https://hooks.slack.com/services/test"
            with patch("app.notifier.httpx.post", return_value=mock_resp) as mock_post:
                notify_new_listing(SAMPLE_LISTING, 75, "Worth Touring", "ai")
                payload = mock_post.call_args[1]["json"]
                assert "redfin.com" in payload["text"]

    def test_missing_price_shows_unknown(self):
        """Listing with no price shows 'Price unknown' in the message."""
        from app.notifier import notify_new_listing

        listing = {**SAMPLE_LISTING, "price": None}
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None

        with patch("app.notifier.settings") as mock_settings:
            mock_settings.slack_webhook_url = "https://hooks.slack.com/services/test"
            with patch("app.notifier.httpx.post", return_value=mock_resp) as mock_post:
                notify_new_listing(listing, 75, "Worth Touring", "ai")
                payload = mock_post.call_args[1]["json"]
                assert "Price unknown" in payload["text"]

    def test_notify_verdicts_set_contains_expected_values(self):
        """NOTIFY_VERDICTS must include Worth Touring and Strong Match only."""
        from app.notifier import NOTIFY_VERDICTS
        assert "Worth Touring" in NOTIFY_VERDICTS
        assert "Strong Match" in NOTIFY_VERDICTS
        assert "Low Priority" not in NOTIFY_VERDICTS
        assert "Weak Match" not in NOTIFY_VERDICTS
        assert "Reject" not in NOTIFY_VERDICTS


class TestNotifyWeeklyDigest:
    """Tests for notify_weekly_digest()."""

    HEALTHY = {"healthy": True, "auth_expired": False, "hours_since_success": 0.5}
    LISTINGS = [
        {"address": "1 A St", "town": "Katonah", "score": 82, "verdict": "Strong Match"},
        {"address": "2 B St", "town": "Bedford", "score": 72, "verdict": "Worth Touring"},
        {"address": "3 C St", "town": "Rye", "score": 30, "verdict": "Reject"},
    ]

    def test_does_nothing_when_webhook_url_empty(self):
        from app.notifier import notify_weekly_digest
        with patch("app.notifier.settings") as mock_settings:
            mock_settings.slack_webhook_url = ""
            with patch("app.notifier.httpx.post") as mock_post:
                notify_weekly_digest(self.LISTINGS, 97.2, self.HEALTHY)
                mock_post.assert_not_called()

    def test_digest_contents(self):
        from app.notifier import notify_weekly_digest
        with patch("app.notifier.settings") as mock_settings:
            mock_settings.slack_webhook_url = "https://hooks.slack.com/test"
            with patch("app.notifier.httpx.post") as mock_post:
                mock_post.return_value = MagicMock(raise_for_status=MagicMock())
                notify_weekly_digest(self.LISTINGS, 97.2, self.HEALTHY)
                text = mock_post.call_args[1]["json"]["text"]
                assert "Weekly digest" in text
                assert "3 new listings" in text
                assert "2 Worth Touring+" in text
                assert "1 A St, Katonah" in text
                assert "97%" in text
                assert "expired" not in text.lower()

    def test_digest_warns_on_auth_failure(self):
        from app.notifier import notify_weekly_digest
        unhealthy = {"healthy": False, "auth_expired": True, "hours_since_success": 50}
        with patch("app.notifier.settings") as mock_settings:
            mock_settings.slack_webhook_url = "https://hooks.slack.com/test"
            with patch("app.notifier.httpx.post") as mock_post:
                mock_post.return_value = MagicMock(raise_for_status=MagicMock())
                notify_weekly_digest([], 90.0, unhealthy)
                text = mock_post.call_args[1]["json"]["text"]
                assert "0 new listings" in text
                assert "expired" in text.lower()
                assert "reconnect" in text.lower()

    def test_digest_warns_on_stuck_pipeline(self):
        from app.notifier import notify_weekly_digest
        unhealthy = {"healthy": False, "auth_expired": False, "hours_since_success": 40}
        with patch("app.notifier.settings") as mock_settings:
            mock_settings.slack_webhook_url = "https://hooks.slack.com/test"
            with patch("app.notifier.httpx.post") as mock_post:
                mock_post.return_value = MagicMock(raise_for_status=MagicMock())
                notify_weekly_digest([], 90.0, unhealthy)
                text = mock_post.call_args[1]["json"]["text"]
                assert "40h" in text and "stuck" in text.lower()


class TestPushover:
    LISTING = {"address": "1 A St", "town": "Katonah", "state": "NY", "price": 1400000,
               "sqft": 3000, "bedrooms": 4, "commute_minutes": 75,
               "listing_url": "https://www.redfin.com/NY/Katonah/1-A-St/home/1"}

    def test_noop_without_tokens(self):
        from app.notifier import notify_pushover
        with patch("app.notifier.settings") as s:
            s.pushover_token = ""; s.pushover_user = ""
            with patch("app.notifier.httpx.post") as p:
                assert notify_pushover(self.LISTING, 82, "Strong Match") is False
                p.assert_not_called()

    def test_sends_with_tokens(self):
        from app.notifier import notify_pushover
        with patch("app.notifier.settings") as s:
            s.pushover_token = "tok"; s.pushover_user_keys = ["usr"]
            with patch("app.notifier.httpx.post") as p:
                p.return_value = MagicMock(raise_for_status=MagicMock())
                assert notify_pushover(self.LISTING, 82, "Strong Match") is True
                data = p.call_args[1]["data"]
                assert data["token"] == "tok" and data["user"] == "usr"
                assert "82" in data["title"]
                assert "1 A St, Katonah" in data["message"]
                assert data["url"].endswith("/home/1")
                assert data["priority"] == 1  # high — surfaces on lock screen

    def test_sends_to_multiple_recipients(self):
        from app.notifier import notify_pushover
        with patch("app.notifier.settings") as s:
            s.pushover_token = "tok"
            s.pushover_user_keys = ["aki_key", "bron_key"]
            with patch("app.notifier.httpx.post") as p:
                p.return_value = MagicMock(raise_for_status=MagicMock())
                assert notify_pushover(self.LISTING, 82, "Strong Match") is True
                assert p.call_count == 2
                users = {c[1]["data"]["user"] for c in p.call_args_list}
                assert users == {"aki_key", "bron_key"}
