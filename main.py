"""
Hackathon & Deadline Notifier
Polls Gmail → Gemini API filters → Telegram notification
"""

import os
import json
import base64
import pickle
import time
import requests
from datetime import datetime, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google import genai

# ── Config ────────────────────────────────────────────────────────────────────

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly",
          "https://www.googleapis.com/auth/gmail.modify"]

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY     = os.environ["GEMINI_API_KEY"]

PROCESSED_FILE = Path("processed_ids.json")

# ── Gmail Auth ────────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    token_path = Path("token.pickle")

    if token_path.exists():
        with open(token_path, "rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "wb") as f:
            pickle.dump(creds, f)

    return build("gmail", "v1", credentials=creds)


# ── Fetch Unread Emails ────────────────────────────────────────────────────────

def fetch_recent_emails(service, max_results=25):
    result = service.users().messages().list(
        userId="me",
        labelIds=["INBOX", "UNREAD"],
        maxResults=max_results
    ).execute()

    messages = result.get("messages", [])
    emails = []

    for msg in messages:
        msg_data = service.users().messages().get(
            userId="me", id=msg["id"], format="full"
        ).execute()

        headers = {h["name"]: h["value"] for h in msg_data["payload"]["headers"]}
        emails.append({
            "id": msg["id"],
            "subject": headers.get("Subject", "(no subject)"),
            "sender":  headers.get("From", "unknown"),
            "date":    headers.get("Date", ""),
            "body":    extract_body(msg_data["payload"])[:2000]
        })

    return emails


def extract_body(payload):
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data", "")
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
    for part in payload.get("parts", []):
        result = extract_body(part)
        if result:
            return result
    return ""


# ── Gemini Classification ──────────────────────────────────────────────────────

PROMPT_TEMPLATE = """You are a classifier that reads emails and detects hackathons,
coding competitions, AI challenges, Kaggle/data-science competitions, research calls,
grant opportunities, and application/submission deadlines.

Respond ONLY with valid JSON. No explanation, no markdown, no preamble.

Schema:
{{
  "is_relevant": true or false,
  "confidence": "high" or "medium" or "low",
  "type": "hackathon" or "competition" or "kaggle" or "deadline" or "grant" or "research_call" or "other",
  "name": "<event or opportunity name>",
  "deadline": "<deadline date if found, else null>",
  "link": "<registration or info link if found, else null>",
  "one_liner": "<one sentence summary>"
}}

Classification guidance:
- Use type "kaggle" specifically for Kaggle.com competition emails: new competition
  announcements, "X days left to submit", prize pool / leaderboard deadline reminders,
  and competition results.
- Kaggle "leaderboard rank changed" or "someone outranked you" pings with NO deadline
  urgency and NO new competition info are noise — mark is_relevant=false. Only flag
  Kaggle emails about a deadline window closing, a new competition launch, or a
  submission/registration call to action.
- Use type "competition" for non-Kaggle coding/data competitions (Codeforces, HackerRank, etc).
- Use type "hackathon" for hackathon-specific events (Devpost, MLH, Unstop hackathon listings).

If not relevant, set is_relevant to false and leave other fields null.

Email to classify:
From: {sender}
Subject: {subject}
Date: {date}

Body:
{body}"""


def classify_email(client, email):
    prompt = PROMPT_TEMPLATE.format(
        sender=email["sender"],
        subject=email["subject"],
        date=email["date"],
        body=email["body"]
    )

    models_to_try = ["gemini-2.5-flash-lite", "gemini-2.5-flash"]
    last_error = None

    for model_name in models_to_try:
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt
                )
                raw = response.text.strip().replace("```json", "").replace("```", "").strip()
                return json.loads(raw)
            except Exception as e:
                last_error = e
                err_str = str(e)
                if "503" in err_str or "UNAVAILABLE" in err_str:
                    print(f"    503 on {model_name} (attempt {attempt + 1}/3), retrying in {(attempt + 1) * 5}s")
                    time.sleep((attempt + 1) * 5)
                    continue
                # not a 503 — don't retry
                raise
        # all retries on this model failed — try next model
        print(f"    {model_name} exhausted, trying next model")

    raise last_error


# ── Telegram Notification ──────────────────────────────────────────────────────

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": False
    })
    if not resp.ok:
        print(f"  ⚠ Telegram error {resp.status_code}: {resp.text[:200]}")
    resp.raise_for_status()


def format_notification(classification, email):
    emoji_map = {
        "hackathon": "🏆", "competition": "🥊", "kaggle": "📊", "deadline": "⏰",
        "grant": "💰", "research_call": "🔬", "other": "📌"
    }
    emoji = emoji_map.get(classification.get("type", "other"), "📌")
    confidence = classification.get("confidence", "")
    conf_tag = f" (confidence: {confidence})" if confidence != "high" else ""

    lines = [
        f"{emoji} {classification.get('name', email['subject'])}{conf_tag}",
        f"{classification.get('one_liner', '')}",
    ]
    if classification.get("deadline"):
        lines.append(f"⏳ Deadline: {classification['deadline']}")
    if classification.get("link"):
        lines.append(f"🔗 {classification['link']}")
    lines.append(f"\n📧 From: {email['sender']}")
    return "\n".join(lines)


# ── State Tracking ─────────────────────────────────────────────────────────────

def load_processed():
    if PROCESSED_FILE.exists():
        return set(json.loads(PROCESSED_FILE.read_text()))
    return set()


def save_processed(ids):
    PROCESSED_FILE.write_text(json.dumps(list(ids)))


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting notifier run...")

    service = get_gmail_service()
    client  = genai.Client(api_key=GEMINI_API_KEY)

    processed = load_processed()
    emails    = fetch_recent_emails(service)

    new_count = 0
    notified  = 0

    for email in emails:
        if email["id"] in processed:
            continue

        new_count += 1
        print(f"  Classifying: {email['subject'][:60]}")

        try:
            result = classify_email(client, email)
        except Exception as e:
            print(f"  ⚠ Classification error: {e}")
            processed.add(email["id"])
            time.sleep(5)
            continue

        processed.add(email["id"])

        if result.get("is_relevant") and result.get("confidence") in ("high", "medium"):
            msg = format_notification(result, email)
            try:
                send_telegram(msg)
                notified += 1
                print(f"  ✅ Notified: {result.get('name', 'unknown')}")
            except Exception as e:
                print(f"  ⚠ Telegram send failed: {e}")
        else:
            print(f"  — Not relevant, skipping")

        time.sleep(5)  # respect 15 RPM free tier limit

    save_processed(processed)
    print(f"Done. Checked {new_count} new emails, sent {notified} notifications.")


if __name__ == "__main__":
    main()