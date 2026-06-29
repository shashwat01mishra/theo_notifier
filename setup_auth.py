"""
Run this ONCE locally to:
1. Authenticate with Gmail (opens browser)
2. Print base64-encoded token + credentials for GitHub Secrets
"""

import pickle
import base64
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify"
]

def main():
    creds = None

    if Path("token.pickle").exists():
        with open("token.pickle", "rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)

        with open("token.pickle", "wb") as f:
            pickle.dump(creds, f)

    print("\n✅ Auth successful. Copy these into GitHub Secrets:\n")

    token_b64 = base64.b64encode(Path("token.pickle").read_bytes()).decode()
    creds_b64 = base64.b64encode(Path("credentials.json").read_bytes()).decode()

    print(f"Secret name : GMAIL_TOKEN_PICKLE")
    print(f"Secret value: {token_b64[:80]}...  (full string, copy all of it)\n")

    print(f"Secret name : GMAIL_CREDENTIALS")
    print(f"Secret value: {creds_b64[:80]}...  (full string, copy all of it)\n")

    print("Also add these secrets:")
    print("  TELEGRAM_BOT_TOKEN  — from @BotFather")
    print("  TELEGRAM_CHAT_ID    — your personal chat ID (get from @userinfobot)")
    print("  ANTHROPIC_API_KEY   — from console.anthropic.com")

if __name__ == "__main__":
    main()