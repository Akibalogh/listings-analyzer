"""Tests for forwarded email detection and unwrapping."""

from app.parsers.forwarded import is_forwarded, unwrap_html, unwrap_text


class TestForwardedDetection:
    def test_detects_fwd_subject(self):
        assert is_forwarded("Fwd: listing alert", None, None) is True

    def test_detects_fw_subject(self):
        assert is_forwarded("Fw: new properties", None, None) is True

    def test_detects_gmail_quote_html(self):
        html = '<div>stuff</div><div class="gmail_quote">inner content</div>'
        assert is_forwarded("", html, None) is True

    def test_detects_forwarded_text_marker(self):
        text = "Some text\n---------- Forwarded message ---------\nFrom: Ken"
        assert is_forwarded("", None, text) is True

    def test_not_forwarded(self):
        assert is_forwarded("any interest in any of these?", None, "hello") is False


class TestUnwrapHtml:
    def test_extracts_gmail_quote(self):
        html = '<div>outer</div><div class="gmail_quote"><p>inner listing data</p></div>'
        result = unwrap_html(html)
        assert "inner listing data" in result
        assert "outer" not in result

    def test_returns_original_if_no_quote(self):
        html = "<div>no forwarded content</div>"
        result = unwrap_html(html)
        assert result == html


class TestUnwrapText:
    def test_extracts_forwarded_content(self):
        text = """Hey check this out

---------- Forwarded message ---------
From: Ken Wile <ken@example.com>
Date: Thu, Feb 27, 2026
Subject: listing alert
To: someone@example.com

$1,295,000
11 Jennifer Lane
4 bd, 3 ba"""
        result = unwrap_text(text)
        assert "$1,295,000" in result
        assert "Hey check this out" not in result
        assert "From: Ken Wile" not in result

    def test_returns_original_if_no_marker(self):
        text = "Just a regular email"
        result = unwrap_text(text)
        assert result == text
