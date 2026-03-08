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
