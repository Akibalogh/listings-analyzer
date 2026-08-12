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


class TestNtfy:
    """ntfy replaces Pushover: one topic, everyone subscribed gets the alert."""

    LISTING = {"address": "1 A St", "town": "Katonah", "state": "NY", "price": 1400000,
               "sqft": 3000, "bedrooms": 4, "commute_minutes": 75,
               "listing_url": "https://www.redfin.com/NY/Katonah/1-A-St/home/1"}

    def test_noop_without_topic(self):
        from app.notifier import notify_ntfy
        with patch("app.notifier.settings") as s:
            s.ntfy_url = ""
            with patch("app.notifier.httpx.post") as p:
                assert notify_ntfy(self.LISTING, 82, "Strong Match") is False
                p.assert_not_called()

    def test_publishes_to_topic_url(self):
        from app.notifier import notify_ntfy
        with patch("app.notifier.settings") as s:
            s.ntfy_url = "https://ntfy.sh/secret-topic"
            s.ntfy_token = ""
            with patch("app.notifier.httpx.post") as p:
                p.return_value = MagicMock(raise_for_status=MagicMock())
                assert notify_ntfy(self.LISTING, 82, "Strong Match") is True
                assert p.call_args[0][0] == "https://ntfy.sh/secret-topic"

    def test_content_is_readable_from_the_lock_screen(self):
        """Address, price and score must be in the notification itself."""
        from app.notifier import notify_ntfy
        with patch("app.notifier.settings") as s:
            s.ntfy_url = "https://ntfy.sh/secret-topic"
            s.ntfy_token = ""
            with patch("app.notifier.httpx.post") as p:
                p.return_value = MagicMock(raise_for_status=MagicMock())
                notify_ntfy(self.LISTING, 82, "Strong Match")
                headers = p.call_args[1]["headers"]
                body = p.call_args[1]["content"].decode("utf-8")
                assert "82" in headers["Title"] and "Strong Match" in headers["Title"]
                assert "Katonah" in headers["Title"]
                assert "1 A St, Katonah" in body
                assert "1,400,000" in body
                assert headers["Priority"] == "high"
                assert headers["Click"].endswith("/home/1")

    def test_title_stays_ascii(self):
        """HTTP headers can't carry emoji — they belong in Tags."""
        from app.notifier import notify_ntfy
        with patch("app.notifier.settings") as s:
            s.ntfy_url = "https://ntfy.sh/secret-topic"
            s.ntfy_token = ""
            with patch("app.notifier.httpx.post") as p:
                p.return_value = MagicMock(raise_for_status=MagicMock())
                notify_ntfy({**self.LISTING, "town": "Croton-on-Hudson — NY"}, 82, "Strong Match")
                headers = p.call_args[1]["headers"]
                headers["Title"].encode("ascii")  # must not raise
                assert headers["Tags"] == "house_with_garden"

    def test_worth_touring_uses_plain_house_tag(self):
        from app.notifier import notify_ntfy
        with patch("app.notifier.settings") as s:
            s.ntfy_url = "https://ntfy.sh/secret-topic"
            s.ntfy_token = ""
            with patch("app.notifier.httpx.post") as p:
                p.return_value = MagicMock(raise_for_status=MagicMock())
                notify_ntfy(self.LISTING, 72, "Worth Touring")
                assert p.call_args[1]["headers"]["Tags"] == "house"

    def test_token_sent_as_bearer_when_set(self):
        from app.notifier import notify_ntfy
        with patch("app.notifier.settings") as s:
            s.ntfy_url = "https://ntfy.example.com/secret-topic"
            s.ntfy_token = "tk_abc"
            with patch("app.notifier.httpx.post") as p:
                p.return_value = MagicMock(raise_for_status=MagicMock())
                notify_ntfy(self.LISTING, 82, "Strong Match")
                assert p.call_args[1]["headers"]["Authorization"] == "Bearer tk_abc"

    def test_no_auth_header_without_token(self):
        from app.notifier import notify_ntfy
        with patch("app.notifier.settings") as s:
            s.ntfy_url = "https://ntfy.sh/secret-topic"
            s.ntfy_token = ""
            with patch("app.notifier.httpx.post") as p:
                p.return_value = MagicMock(raise_for_status=MagicMock())
                notify_ntfy(self.LISTING, 82, "Strong Match")
                assert "Authorization" not in p.call_args[1]["headers"]

    def test_fails_silently_on_http_error(self):
        from app.notifier import notify_ntfy
        with patch("app.notifier.settings") as s:
            s.ntfy_url = "https://ntfy.sh/secret-topic"
            s.ntfy_token = ""
            with patch("app.notifier.httpx.post", side_effect=Exception("connection refused")):
                assert notify_ntfy(self.LISTING, 82, "Strong Match") is False


class TestPushChannelSelection:
    """Exactly one phone channel fires, so a half-migrated config can't
    double-buzz every phone."""

    LISTING = TestNtfy.LISTING

    def test_ntfy_takes_over_when_topic_is_set(self):
        from app.notifier import send_high_score_alert
        with patch("app.notifier.settings") as s:
            s.ntfy_url = "https://ntfy.sh/secret-topic"
            s.slack_webhook_url = ""
            with patch("app.notifier.notify_ntfy") as ntfy, \
                 patch("app.notifier.notify_pushover") as push:
                send_high_score_alert(self.LISTING, 82, "Strong Match")
                ntfy.assert_called_once()
                push.assert_not_called()

    def test_pushover_still_used_until_the_topic_is_set(self):
        from app.notifier import send_high_score_alert
        with patch("app.notifier.settings") as s:
            s.ntfy_url = ""
            s.slack_webhook_url = ""
            with patch("app.notifier.notify_ntfy") as ntfy, \
                 patch("app.notifier.notify_pushover") as push:
                send_high_score_alert(self.LISTING, 82, "Strong Match")
                push.assert_called_once()
                ntfy.assert_not_called()


class TestNtfyConfig:
    def test_url_empty_without_topic(self):
        from app.config import Settings
        assert Settings(ntfy_topic="").ntfy_url == ""

    def test_url_built_from_topic(self):
        from app.config import Settings
        assert Settings(ntfy_topic="abc123").ntfy_url == "https://ntfy.sh/abc123"

    def test_custom_server_and_trailing_slash(self):
        from app.config import Settings
        s = Settings(ntfy_topic="abc123", ntfy_server="https://ntfy.example.com/")
        assert s.ntfy_url == "https://ntfy.example.com/abc123"


class TestUnknownStatusIsCalledOut:
    """516 Bellwood Avenue was alerted as a new Worth Touring (72) after it had
    already sold. Its OneHome alert email carried no listing_status at all — 9
    of 14 status-less listings come from that source — so nothing in the system
    could know. The criteria are right that a null status is unknown rather than
    sold, so the alert says which it is instead of reading as available.
    """

    BELLWOOD = {
        "address": "516 Bellwood Avenue", "town": "Sleepy Hollow", "state": "New York",
        "price": 2000000, "sqft": 3053, "bedrooms": 4, "commute_minutes": 79,
    }

    def test_missing_status_is_flagged(self):
        from app.notifier import _listing_summary
        _, stats, _ = _listing_summary(self.BELLWOOD)
        assert "status unknown — may be sold" in stats

    def test_known_status_is_not_flagged(self):
        from app.notifier import _listing_summary
        _, stats, _ = _listing_summary({**self.BELLWOOD, "listing_status": "Active"})
        assert "status unknown" not in stats

    def test_blank_status_counts_as_missing(self):
        from app.notifier import _listing_summary
        for blank in ("", "   ", None):
            _, stats, _ = _listing_summary({**self.BELLWOOD, "listing_status": blank})
            assert "status unknown" in stats, repr(blank)

    def test_the_real_stats_still_come_first(self):
        """The flag is appended, not a replacement — price and specs stay."""
        from app.notifier import _listing_summary
        _, stats, _ = _listing_summary(self.BELLWOOD)
        assert stats.startswith("$2,000,000")
        for part in ("3,053 sqft", "4 bd", "79 min commute"):
            assert part in stats
        assert stats.index("79 min commute") < stats.index("status unknown")

    def test_reaches_every_channel(self):
        """One summary builder feeds ntfy, Pushover and Slack alike."""
        from app.notifier import _listing_summary
        assert "status unknown" in _listing_summary(self.BELLWOOD)[1]

    def test_ntfy_body_carries_the_flag(self):
        from unittest.mock import patch
        from app.notifier import notify_ntfy
        with patch("app.notifier.settings") as s:
            s.ntfy_url = "https://ntfy.sh/topic"
            s.ntfy_token = ""
            with patch("app.notifier.httpx.post") as post:
                notify_ntfy(self.BELLWOOD, 72, "Worth Touring")
            body = post.call_args.kwargs.get("content") or post.call_args.args[1]
        assert "status unknown" in (body.decode() if isinstance(body, bytes) else body)


class TestAlertLinkNeverLeaksTheOneHomeToken:
    """A OneHome URL's `token` is base64 JSON carrying the buyer's email,
    contact id, agent id, and the saved search's id and key. There is no login
    behind it — the token IS the authentication — so the URL is a bearer
    credential and must not go to a push topic or a Slack channel. It also
    can't be trimmed: the listing id lives inside the token.
    """

    ONEHOME = ("https://portal.onehome.com/en-US/listing?token="
               "eyJPU04iOiJLRVkiLCJlbWFpbCI6ImFraWJhbG9naEBnbWFpbC5jb20ifQ==&SMS=0")

    def test_onehome_link_is_replaced(self):
        from app.notifier import _alert_link
        out = _alert_link({"listing_url": self.ONEHOME})
        assert "onehome" not in out
        assert "token=" not in out
        assert out.startswith("http")

    def test_replacement_is_the_dashboard(self):
        from app.notifier import _alert_link
        from app.config import settings
        assert _alert_link({"listing_url": self.ONEHOME}) == settings.public_base_url.rstrip("/")

    def test_real_listing_links_are_kept(self):
        """Redfin URLs carry no credential — no reason to lose them."""
        from app.notifier import _alert_link
        redfin = "https://www.redfin.com/NY/Katonah/59-Orchard-Hill-Rd-10536/home/20050852"
        assert _alert_link({"listing_url": redfin}) == redfin

    def test_missing_url_stays_empty(self):
        from app.notifier import _alert_link
        assert _alert_link({}) == ""
        assert _alert_link({"listing_url": None}) == ""

    def test_ntfy_click_header_carries_no_token(self):
        from unittest.mock import patch
        from app.notifier import notify_ntfy
        with patch("app.notifier.settings") as s:
            s.ntfy_url = "https://ntfy.sh/topic"
            s.ntfy_token = ""
            s.public_base_url = "https://listings-analyzer.fly.dev"
            with patch("app.notifier.httpx.post") as post:
                notify_ntfy({"address": "120 Cedar Drive E", "town": "Briarcliff",
                             "listing_url": self.ONEHOME, "listing_status": "Active"},
                            82, "Strong Match")
            headers = post.call_args.kwargs["headers"]
        assert "token=" not in headers.get("Click", "")
        assert "onehome" not in headers.get("Click", "")

    def test_slack_body_carries_no_token(self):
        from unittest.mock import patch
        from app.notifier import notify_new_listing
        with patch("app.notifier.settings") as s:
            s.slack_webhook_url = "https://hooks.slack.com/services/x"
            s.public_base_url = "https://listings-analyzer.fly.dev"
            with patch("app.notifier._post_slack") as post:
                notify_new_listing({"address": "120 Cedar Drive E", "town": "Briarcliff",
                                    "listing_url": self.ONEHOME}, 82, "Strong Match", "ai")
            text = post.call_args.args[0]
        assert "token=" not in text and "onehome" not in text


class TestAlertDeliveryIsReported:
    """send_high_score_alert used to return None, so a publish ntfy rejected was
    indistinguishable from one that reached a phone — /manage/notify-test
    answered {"sent": true} either way.
    """

    LISTING = {"address": "1 A St", "town": "Rye", "listing_status": "Active"}

    def test_reports_ntfy_success(self):
        from unittest.mock import patch
        from app.notifier import send_high_score_alert
        with patch("app.notifier.settings") as s:
            s.ntfy_url = "https://ntfy.sh/t"
            s.slack_webhook_url = ""
            with patch("app.notifier.notify_ntfy", return_value=True):
                assert send_high_score_alert(self.LISTING, 82, "Strong Match") == {"ntfy": True}

    def test_reports_ntfy_failure(self):
        """The case that cost a debugging session. A failed ntfy publish now
        also triggers the Pushover fallback, so both channels are reported."""
        from unittest.mock import patch
        from app.notifier import send_high_score_alert
        with patch("app.notifier.settings") as s:
            s.ntfy_url = "https://ntfy.sh/t"
            s.slack_webhook_url = ""
            with patch("app.notifier.notify_ntfy", return_value=False), \
                 patch("app.notifier.notify_pushover", return_value=False):
                out = send_high_score_alert(self.LISTING, 82, "Strong Match")
        assert out["ntfy"] is False

    def test_reports_pushover_when_ntfy_is_off(self):
        from unittest.mock import patch
        from app.notifier import send_high_score_alert
        with patch("app.notifier.settings") as s:
            s.ntfy_url = ""
            s.slack_webhook_url = ""
            with patch("app.notifier.notify_pushover", return_value=True):
                assert send_high_score_alert(self.LISTING, 70, "Worth Touring") == {"pushover": True}

    def test_http_error_logs_the_ntfy_response_body(self):
        """ntfy explains rejections in the body; it used to be swallowed."""
        import httpx
        from unittest.mock import MagicMock, patch
        from app.notifier import notify_ntfy
        resp = MagicMock(status_code=400, text='{"code":40007,"error":"invalid header"}')
        with patch("app.notifier.settings") as s:
            s.ntfy_url = "https://ntfy.sh/t"
            s.ntfy_token = ""
            s.public_base_url = "https://x"
            with patch("app.notifier.httpx.post") as post:
                post.return_value.raise_for_status.side_effect = httpx.HTTPStatusError(
                    "400", request=MagicMock(), response=resp)
                with patch("app.notifier.logger") as log:
                    assert notify_ntfy(self.LISTING, 82, "Strong Match") is False
        logged = " ".join(str(a) for a in log.warning.call_args.args)
        assert "40007" in logged or "invalid header" in logged or "%s" in logged


class TestNtfyProbe:
    """A controlled experiment for when a publish fails silently: bare body, no
    Title/Tags/Priority/Click, raw status and response body returned. Succeeding
    where the real alert fails isolates the fault to a header.
    """

    def test_skipped_when_unconfigured(self):
        from unittest.mock import patch
        from app.notifier import ntfy_probe
        with patch("app.notifier.settings") as s:
            s.ntfy_url = ""
            assert ntfy_probe()["probe"] == "skipped"

    def test_reports_status_and_body(self):
        from unittest.mock import MagicMock, patch
        from app.notifier import ntfy_probe
        with patch("app.notifier.settings") as s:
            s.ntfy_url = "https://ntfy.sh/topic"
            s.ntfy_server = "https://ntfy.sh"
            s.ntfy_token = ""
            s.ntfy_topic = "topic"
            resp = MagicMock(status_code=429, text='{"code":42901,"error":"limit reached"}')
            resp.is_success = False
            with patch("app.notifier.httpx.post", return_value=resp):
                out = ntfy_probe()
        assert out["status_code"] == 429
        assert "42901" in out["response_body"]
        assert out["ok"] is False

    def test_sends_no_cosmetic_headers(self):
        """The whole point — Title/Tags/Priority/Click can't be the cause if
        none are sent."""
        from unittest.mock import MagicMock, patch
        from app.notifier import ntfy_probe
        with patch("app.notifier.settings") as s:
            s.ntfy_url = "https://ntfy.sh/topic"
            s.ntfy_server = "https://ntfy.sh"
            s.ntfy_token = ""
            with patch("app.notifier.httpx.post", return_value=MagicMock(
                    status_code=200, text="", is_success=True)) as post:
                ntfy_probe()
        sent = post.call_args.kwargs["headers"]
        for h in ("Title", "Tags", "Priority", "Click"):
            assert h not in sent

    def test_does_send_authorization_when_a_token_is_set(self):
        """Auth is not cosmetic: it decides whether the rate limit is metered
        against your account or against a Fly IP shared with strangers. An
        unauthenticated probe reported a limit the real path never hits."""
        from unittest.mock import MagicMock, patch
        from app.notifier import ntfy_probe
        with patch("app.notifier.settings") as s:
            s.ntfy_url = "https://ntfy.sh/topic"
            s.ntfy_server = "https://ntfy.sh"
            s.ntfy_token = "tk_secret"
            with patch("app.notifier.httpx.post", return_value=MagicMock(
                    status_code=200, text="", is_success=True)) as post:
                out = ntfy_probe()
        assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer tk_secret"
        assert out["authenticated"] is True

    def test_reports_when_no_token_is_set(self):
        from unittest.mock import MagicMock, patch
        from app.notifier import ntfy_probe
        with patch("app.notifier.settings") as s:
            s.ntfy_url = "https://ntfy.sh/topic"
            s.ntfy_server = "https://ntfy.sh"
            s.ntfy_token = ""
            with patch("app.notifier.httpx.post", return_value=MagicMock(
                    status_code=200, text="", is_success=True)) as post:
                out = ntfy_probe()
        assert out["authenticated"] is False
        assert "Authorization" not in post.call_args.kwargs["headers"]

    def test_token_value_is_never_echoed(self):
        import json as _json
        from unittest.mock import MagicMock, patch
        from app.notifier import ntfy_probe
        with patch("app.notifier.settings") as s:
            s.ntfy_url = "https://ntfy.sh/topic"
            s.ntfy_server = "https://ntfy.sh"
            s.ntfy_token = "tk_supersecret"
            with patch("app.notifier.httpx.post", return_value=MagicMock(
                    status_code=200, text="", is_success=True)):
                out = ntfy_probe()
        assert "tk_supersecret" not in _json.dumps(out)

    def test_never_echoes_the_topic(self):
        import json as _json
        from unittest.mock import MagicMock, patch
        from app.notifier import ntfy_probe
        with patch("app.notifier.settings") as s:
            s.ntfy_url = "https://ntfy.sh/s3cr3t-topic"
            s.ntfy_server = "https://ntfy.sh"
            s.ntfy_token = ""
            with patch("app.notifier.httpx.post", return_value=MagicMock(
                    status_code=200, text="", is_success=True)):
                out = ntfy_probe()
        assert "s3cr3t-topic" not in _json.dumps(out)

    def test_network_failure_is_reported_not_raised(self):
        import httpx
        from unittest.mock import patch
        from app.notifier import ntfy_probe
        with patch("app.notifier.settings") as s:
            s.ntfy_url = "https://ntfy.sh/topic"
            s.ntfy_server = "https://ntfy.sh"
            s.ntfy_token = ""
            with patch("app.notifier.httpx.post", side_effect=httpx.ConnectError("no route")):
                out = ntfy_probe()
        assert out["ok"] is False
        assert out["exception_type"] == "ConnectError"


class TestPushoverBacksNtfyUp:
    """Public ntfy.sh rate-limits per visitor IP, and a Fly app shares its
    egress IP with other tenants — so the daily quota can be exhausted by
    strangers, returning 429 (code 42908) on every publish for the rest of the
    day. That is exactly what happened, and every alert went silently nowhere.
    A house worth touring shouldn't go unannounced because of it.
    """

    LISTING = {"address": "120 Cedar Drive E", "town": "Briarcliff Manor",
               "listing_status": "Back On Market"}

    @staticmethod
    def _send(ntfy_ok, ntfy_configured=True):
        from unittest.mock import patch
        from app.notifier import send_high_score_alert
        with patch("app.notifier.settings") as s:
            s.ntfy_url = "https://ntfy.sh/t" if ntfy_configured else ""
            s.slack_webhook_url = ""
            with patch("app.notifier.notify_ntfy", return_value=ntfy_ok) as ntfy, \
                 patch("app.notifier.notify_pushover", return_value=True) as push:
                out = send_high_score_alert(
                    TestPushoverBacksNtfyUp.LISTING, 82, "Strong Match")
        return out, ntfy, push

    def test_rate_limited_ntfy_falls_back_to_pushover(self):
        out, _, push = self._send(ntfy_ok=False)
        assert out == {"ntfy": False, "pushover": True}
        push.assert_called_once()

    def test_no_double_buzz_when_ntfy_works(self):
        """The reason the two were mutually exclusive in the first place."""
        out, _, push = self._send(ntfy_ok=True)
        assert out == {"ntfy": True}
        push.assert_not_called()

    def test_pushover_alone_when_ntfy_unconfigured(self):
        out, ntfy, push = self._send(ntfy_ok=True, ntfy_configured=False)
        assert out == {"pushover": True}
        ntfy.assert_not_called()

    def test_both_failing_is_reported_honestly(self):
        from unittest.mock import patch
        from app.notifier import send_high_score_alert
        with patch("app.notifier.settings") as s:
            s.ntfy_url = "https://ntfy.sh/t"
            s.slack_webhook_url = ""
            with patch("app.notifier.notify_ntfy", return_value=False), \
                 patch("app.notifier.notify_pushover", return_value=False):
                out = send_high_score_alert(self.LISTING, 82, "Strong Match")
        assert out == {"ntfy": False, "pushover": False}
        assert not any(out.values())


class TestProbeRedactsTheTopic:
    """ntfy echoes the topic in its success body, so returning that body
    verbatim leaked the topic back out — the one thing the probe was written
    not to do. The topic is the only access control on public ntfy.sh.
    """

    NTFY_OK = ('{"id":"4Kdy9rguf02g","time":1786494177,"event":"message",'
               '"topic":"fe2164f4d84d4a1a7e7acc9c964b23710800e0d896a863d2",'
               '"message":"probe from listings-analyzer"}')

    def _probe(self, body, status=200):
        from unittest.mock import MagicMock, patch
        from app.notifier import ntfy_probe
        with patch("app.notifier.settings") as s:
            s.ntfy_topic = "fe2164f4d84d4a1a7e7acc9c964b23710800e0d896a863d2"
            s.ntfy_url = f"https://ntfy.sh/{s.ntfy_topic}"
            s.ntfy_server = "https://ntfy.sh"
            s.ntfy_token = ""
            resp = MagicMock(status_code=status, text=body)
            resp.is_success = status < 400
            with patch("app.notifier.httpx.post", return_value=resp):
                return ntfy_probe()

    def test_topic_is_redacted_from_a_success_body(self):
        out = self._probe(self.NTFY_OK)
        assert "fe2164f4d84d4a1a7e7ac" not in out["response_body"]
        assert "<redacted>" in out["response_body"]

    def test_the_rest_of_the_body_survives(self):
        """Redaction must not cost the diagnostic its value."""
        out = self._probe(self.NTFY_OK)
        assert "4Kdy9rguf02g" in out["response_body"]
        assert out["ok"] is True

    def test_error_bodies_are_still_readable(self):
        err = '{"code":42908,"http":429,"error":"limit reached: daily message quota reached"}'
        out = self._probe(err, status=429)
        assert "42908" in out["response_body"]
        assert "daily message quota" in out["response_body"]

    def test_nothing_breaks_without_a_topic(self):
        from app.notifier import _redact_topic
        from unittest.mock import patch
        with patch("app.notifier.settings") as s:
            s.ntfy_topic = ""
            assert _redact_topic("some body") == "some body"
