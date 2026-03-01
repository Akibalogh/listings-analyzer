"""Gmail API client for fetching listing alert emails."""

import base64
import logging
from email.mime.text import MIMEText

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from app.config import settings

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

PROCESSED_LABEL = "ListingsAnalyzer/Processed"


def _build_service():
    """Build Gmail API service from refresh token."""
    creds_data = settings.gmail_credentials
    client_config = creds_data.get("installed", creds_data.get("web", {}))

    creds = Credentials(
        token=None,
        refresh_token=settings.gmail_refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_config["client_id"],
        client_secret=client_config["client_secret"],
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)


def _get_or_create_label(service) -> str:
    """Get or create the processed label, return label ID."""
    results = service.users().labels().list(userId="me").execute()
    for label in results.get("labels", []):
        if label["name"] == PROCESSED_LABEL:
            return label["id"]

    label_body = {
        "name": PROCESSED_LABEL,
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show",
    }
    created = service.users().labels().create(userId="me", body=label_body).execute()
    logger.info(f"Created Gmail label: {PROCESSED_LABEL}")
    return created["id"]


def fetch_new_emails() -> list[dict]:
    """Fetch unprocessed listing alert emails.

    Returns list of dicts with keys: id, subject, sender, html, text, date
    """
    service = _build_service()
    label_id = _get_or_create_label(service)

    # Build search query for alert senders, excluding already-processed
    sender_query = " OR ".join(f"from:{s}" for s in settings.sender_list)
    query = f"({sender_query}) -label:{PROCESSED_LABEL}"

    logger.info(f"Gmail search: {query}")

    results = service.users().messages().list(userId="me", q=query).execute()
    messages = results.get("messages", [])

    if not messages:
        logger.info("No new listing emails found")
        return []

    logger.info(f"Found {len(messages)} new email(s)")

    emails = []
    for msg_ref in messages:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=msg_ref["id"], format="full")
            .execute()
        )
        email_data = _extract_email_data(msg)
        email_data["gmail_id"] = msg_ref["id"]
        email_data["label_id"] = label_id
        emails.append(email_data)

    return emails


def _extract_email_data(msg: dict) -> dict:
    """Extract subject, sender, html, text, and date from a Gmail message."""
    headers = {h["name"].lower(): h["value"] for h in msg["payload"]["headers"]}

    data = {
        "subject": headers.get("subject", ""),
        "sender": headers.get("from", ""),
        "date": headers.get("date", ""),
        "message_id": headers.get("message-id", ""),
        "html": "",
        "text": "",
    }

    _extract_parts(msg["payload"], data)
    return data


def _extract_parts(payload: dict, data: dict):
    """Recursively extract HTML and text parts from email payload."""
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/html" and "body" in payload:
        body_data = payload["body"].get("data", "")
        if body_data:
            data["html"] = base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")

    elif mime_type == "text/plain" and "body" in payload:
        body_data = payload["body"].get("data", "")
        if body_data:
            data["text"] = base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")

    for part in payload.get("parts", []):
        _extract_parts(part, data)


def fetch_email_by_id(gmail_id: str) -> dict | None:
    """Re-fetch a single email by Gmail ID for reprocessing."""
    try:
        service = _build_service()
        msg = service.users().messages().get(userId="me", id=gmail_id, format="full").execute()
        email_data = _extract_email_data(msg)
        email_data["gmail_id"] = gmail_id
        return email_data
    except Exception as e:
        logger.error(f"Failed to re-fetch email {gmail_id}: {e}")
        return None


def mark_processed(gmail_id: str, label_id: str):
    """Mark an email as processed by adding the label and marking as read."""
    service = _build_service()
    service.users().messages().modify(
        userId="me",
        id=gmail_id,
        body={
            "addLabelIds": [label_id],
            "removeLabelIds": ["UNREAD"],
        },
    ).execute()
    logger.info(f"Marked email {gmail_id} as processed")
