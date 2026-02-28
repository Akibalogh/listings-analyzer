"""One-time OAuth flow for Gmail API access.

Run this locally to authorize the app:
    uv run python scripts/gmail_auth.py

Prerequisites:
    1. Go to console.cloud.google.com
    2. Create project "Listings Analyzer"
    3. Enable Gmail API
    4. Configure OAuth consent screen (External, test mode, add your email)
    5. Create OAuth 2.0 Client ID (Desktop app)
    6. Download credentials.json to this project root

This script will:
    - Open your browser for Google sign-in
    - Request Gmail read + modify access
    - Print your refresh token
    - Save it to copy into .env or Heroku config vars
"""

import json
import sys
from pathlib import Path

from google_auth_oauthlib.flow import Flow, InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

CREDENTIALS_FILE = Path(__file__).parent.parent / "credentials.json"

REDIRECT_URI = "http://localhost:8080/"


def main():
    if not CREDENTIALS_FILE.exists():
        print(f"Error: {CREDENTIALS_FILE} not found.")
        print()
        print("To get this file:")
        print("  1. Go to console.cloud.google.com")
        print("  2. Create project 'Listings Analyzer'")
        print("  3. Enable Gmail API")
        print("  4. OAuth consent screen → External, test mode, add your email")
        print("  5. Credentials → Create OAuth 2.0 Client ID (Desktop app or Web app)")
        print("  6. Download and save as credentials.json in the project root")
        sys.exit(1)

    # Detect credential type (installed vs web)
    with open(CREDENTIALS_FILE) as f:
        creds_data = json.load(f)

    if "installed" in creds_data:
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
        creds = flow.run_local_server(port=0)
    elif "web" in creds_data:
        # Web credentials need a redirect URI added to Google Cloud Console
        print("Detected 'Web application' credentials.")
        print(f"Make sure {REDIRECT_URI} is listed as an")
        print("Authorized redirect URI in your Google Cloud Console.")
        print()
        flow = Flow.from_client_secrets_file(
            str(CREDENTIALS_FILE),
            scopes=SCOPES,
            redirect_uri=REDIRECT_URI,
        )
        auth_url, _ = flow.authorization_url(
            access_type="offline", prompt="consent"
        )
        print(f"Open this URL in your browser:\n\n  {auth_url}\n")
        code = input("Paste the authorization code here: ").strip()
        flow.fetch_token(code=code)
        creds = flow.credentials
    else:
        print("Error: credentials.json must contain 'installed' or 'web' key")
        sys.exit(1)

    print()
    print("=" * 60)
    print("  Gmail OAuth setup complete!")
    print("=" * 60)
    print()
    print("Add these to your .env file (or Heroku config vars):")
    print()

    # Read credentials.json to get client config for env var
    with open(CREDENTIALS_FILE) as f:
        creds_data = json.load(f)
    print(f"GMAIL_CREDENTIALS_JSON={json.dumps(creds_data)}")
    print()
    print(f"GMAIL_REFRESH_TOKEN={creds.refresh_token}")
    print()
    print("=" * 60)
    print("  Keep these values safe. Do NOT commit them to git.")
    print("=" * 60)


if __name__ == "__main__":
    main()
