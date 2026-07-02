"""
Brilliance Feed — Step 1: fetch + raw delivery
Pulls RSS feeds and HN front page, dedups against seen cache,
sends new items to Telegram. No scoring yet — that's Step 2.

Run standalone: python brilliance.py
"""

import os
import json
import hashlib
import time
import requests
import feedparser
import yaml
from pathlib import Path
from datetime import datetime, timezone

TELEGRAM_BOT_TOKEN = os.environ["BRILLIANCE_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

SEEN_FILE    = Path("brilliance_seen.json")
SOURCES_FILE = Path("sources.yaml")

# Cap items per source per run so a backlog dump doesn't flood Telegram
DEFAULT_MAX_ITEMS = 5


# ── Fetchers ───────────────────────────────────────────────────────────────────

def fetch_rss(source):
    """Standard RSS/Atom feed → list of items."""
    d = feedparser.parse(source["url"], agent="Mozilla/5.0 (brilliance-feed)")
    items = []
    for entry in d.entries[: source.get("max_items", DEFAULT_MAX_ITEMS)]:
        items.append({
            "source":  source["name"],
            "title":   entry.get("title", "(untitled)"),
            "link":    entry.get("link", ""),
            "summary": entry.get("summary", "")[:500],
        })
    return items


def fetch_hn(source):
    """HN front page via Algolia — filter for points client-side since the
    front_page tag rejects combined numericFilters (returns 400)."""
    resp = requests.get(source["url"], timeout=15)
    resp.raise_for_status()
    hits = resp.json().get("hits", [])
    min_points = source.get("min_points", 100)
    hits = [h for h in hits if (h.get("points") or 0) >= min_points]
    items = []
    for hit in hits[: source.get("max_items", DEFAULT_MAX_ITEMS)]:
        items.append({
            "source":  source["name"],
            "title":   hit.get("title", "(untitled)"),
            "link":    hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
            "summary": f"{hit.get('points', 0)} points, {hit.get('num_comments', 0)} comments on HN",
        })
    return items


FETCHERS = {"rss": fetch_rss, "hn": fetch_hn}


# ── Dedup ──────────────────────────────────────────────────────────────────────

def item_hash(item):
    return hashlib.sha256(f"{item['source']}|{item['link']}".encode()).hexdigest()[:16]


def load_seen():
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen):
    # keep the file bounded — 2000 most recent hashes is months of history
    SEEN_FILE.write_text(json.dumps(list(seen)[-2000:]))


# ── Delivery ───────────────────────────────────────────────────────────────────

def send_telegram(message, retries=2):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for attempt in range(retries + 1):
        try:
            resp = requests.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "disable_web_page_preview": True,
            }, timeout=15)
            if not resp.ok:
                print(f"  ⚠ Telegram error {resp.status_code}: {resp.text[:200]}")
            resp.raise_for_status()
            return
        except requests.exceptions.RequestException as e:
            if attempt < retries:
                print(f"  ⚠ Send attempt {attempt + 1} failed ({e}), retrying in 2s")
                time.sleep(2)
            else:
                raise


def format_item(item):
    source_emoji = {"quanta": "🔮", "tao": "📐", "hackernews": "🗞️"}
    emoji = source_emoji.get(item["source"], "💡")
    return f"{emoji} [{item['source']}] {item['title']}\n{item['link']}"


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print(f"[{datetime.now(timezone.utc).isoformat()}] Brilliance feed run...")

    config  = yaml.safe_load(SOURCES_FILE.read_text())
    seen    = load_seen()
    print(f"  seen cache: {len(seen)} hashes loaded")
    new     = []

    for source in config["sources"]:
        fetcher = FETCHERS.get(source["type"])
        if fetcher is None:
            print(f"  ⚠ Unknown source type: {source['type']}, skipping {source['name']}")
            continue
        try:
            items = fetcher(source)
            print(f"  {source['name']}: {len(items)} items fetched")
        except Exception as e:
            print(f"  ⚠ Fetch failed for {source['name']}: {e}")
            continue

        for item in items:
            h = item_hash(item)
            if h not in seen:
                seen.add(h)
                new.append(item)

    print(f"  {len(new)} new items after dedup")

    for item in new:
        try:
            send_telegram(format_item(item))
            print(f"  ✅ Sent: {item['title'][:60]}")
        except Exception as e:
            print(f"  ⚠ Send failed: {e}")
        time.sleep(1.5)  # pace sends — rapid-fire sequential calls get connection-reset

    save_seen(seen)
    print("Done.")


if __name__ == "__main__":
    main()