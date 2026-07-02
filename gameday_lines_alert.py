# ============================================================
# GameDayTweets NHL Line Alert Bot
# GitHub Actions Production Version
# ============================================================
#
# What it does:
#   1. Scrapes https://www.gamedaytweets.com/lines
#   2. Detects new GameDayLines tweet/status IDs
#   3. Uses Twitter/X oEmbed to retrieve full tweet text
#   4. Sends new alerts to Slack
#   5. Saves seen IDs and alert log for future runs
#
# Required GitHub Secret:
#   SLACK_WEBHOOK_URL
#
# Files used:
#   seen_gameday_line_status_ids.json
#   gameday_line_alert_log.csv
# ============================================================

import os
import re
import csv
import json
import time
import argparse
import requests
import pandas as pd

from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse


# ============================================================
# CONFIG
# ============================================================

URL = "https://www.gamedaytweets.com/lines"
NHL_SCHEDULE_API = "https://api-web.nhle.com/v1/schedule/{date}"

SEEN_PATH = "seen_gameday_line_status_ids.json"
ALERT_LOG_PATH = "gameday_line_alert_log.csv"

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "").strip()

# Safety cap so one run cannot accidentally blast Slack with too many messages.
MAX_NEW_ALERTS_PER_RUN = int(os.getenv("MAX_NEW_ALERTS_PER_RUN", "15"))

# Seconds between oEmbed requests / Slack sends.
REQUEST_SLEEP_SECONDS = float(os.getenv("REQUEST_SLEEP_SECONDS", "0.75"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}


# ============================================================
# BASIC HELPERS
# ============================================================

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clean_text(text: str) -> str:
    if not isinstance(text, str):
        return ""

    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)

    return text.strip()


def normalize_tweet_url(url: str) -> str:
    if not isinstance(url, str):
        return ""

    url = url.strip()
    url = url.replace("https://x.com/", "https://twitter.com/")
    url = url.replace("http://x.com/", "https://twitter.com/")
    url = url.replace("http://twitter.com/", "https://twitter.com/")

    parsed = urlparse(url)

    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")


def extract_status_id(tweet_url: str):
    if not isinstance(tweet_url, str):
        return None

    match = re.search(r"/status/(\d+)", tweet_url)

    if match:
        return match.group(1)

    return None


def looks_truncated(text: str) -> bool:
    if not isinstance(text, str):
        return False

    stripped = text.strip()

    return stripped.endswith("…") or "…" in stripped[-15:]


def safe_str(value):
    if value is None:
        return ""

    return str(value)


# ============================================================
# SEEN-ID STORAGE
# ============================================================

def load_seen_status_ids(path: str = SEEN_PATH) -> set:
    if not os.path.exists(path):
        return set()

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            return set(str(x) for x in data if x)

        return set()

    except json.JSONDecodeError:
        print(f"WARNING: Could not parse {path}. Treating as empty.")
        return set()


def save_seen_status_ids(seen_ids: set, path: str = SEEN_PATH):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(list(seen_ids)), f, indent=2)


# ============================================================
# SCRAPE GAMEDAYTWEETS
# ============================================================

def scrape_gameday_line_posts() -> pd.DataFrame:
    response = requests.get(URL, headers=HEADERS, timeout=25)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "lxml")

    records = []
    blocks = soup.select("blockquote.tweet.full-sized-tweet")

    print(f"Line tweet blocks found: {len(blocks)}")

    for i, block in enumerate(blocks, start=1):
        p_tag = block.find("p")
        gdt_preview_text = clean_text(p_tag.get_text("\n", strip=True)) if p_tag else ""

        handle_tag = block.select_one("a.handle")
        source_handle = clean_text(handle_tag.get_text(" ", strip=True)) if handle_tag else None
        source_profile_url = normalize_tweet_url(handle_tag.get("href", "")) if handle_tag else None

        status_link_tag = None

        for a_tag in block.find_all("a", href=True):
            href = a_tag.get("href", "")

            if "/status/" in href:
                status_link_tag = a_tag
                break

        if status_link_tag:
            tweet_url = normalize_tweet_url(status_link_tag.get("href", ""))
            tweet_date_text = clean_text(status_link_tag.get_text(" ", strip=True))
        else:
            tweet_url = None
            tweet_date_text = None

        status_id = extract_status_id(tweet_url)
        raw_block_text = clean_text(block.get_text("\n", strip=True))

        if not status_id:
            continue

        records.append({
            "row_num": i,
            "scraped_at": now_str(),
            "status_id": str(status_id),
            "source_handle": source_handle,
            "source_profile_url": source_profile_url,
            "tweet_date_text": tweet_date_text,
            "tweet_url": tweet_url,
            "gdt_is_truncated": looks_truncated(gdt_preview_text),
            "gdt_preview_text": gdt_preview_text,
            "raw_block_text": raw_block_text,
        })

    df = pd.DataFrame(records)

    if not df.empty:
        df = df.drop_duplicates(subset=["status_id"]).reset_index(drop=True)

    return df


# ============================================================
# OEMBED FULL-TEXT FETCHER
# ============================================================

def fetch_oembed_text(tweet_url: str) -> dict:
    endpoint = "https://publish.twitter.com/oembed"

    params = {
        "url": tweet_url,
        "omit_script": "true",
        "dnt": "true",
    }

    try:
        response = requests.get(endpoint, params=params, timeout=20)

        result = {
            "oembed_status_code": response.status_code,
            "oembed_error": None,
            "oembed_text": None,
            "oembed_author_name": None,
            "oembed_author_url": None,
        }

        if response.status_code != 200:
            result["oembed_error"] = response.text[:500]
            return result

        data = response.json()

        html = data.get("html", "")
        result["oembed_author_name"] = data.get("author_name")
        result["oembed_author_url"] = data.get("author_url")

        embed_soup = BeautifulSoup(html, "html.parser")

        for tag in embed_soup(["script", "style"]):
            tag.decompose()

        text = embed_soup.get_text("\n", strip=True)
        result["oembed_text"] = clean_text(text)

        return result

    except Exception as e:
        return {
            "oembed_status_code": None,
            "oembed_error": str(e),
            "oembed_text": None,
            "oembed_author_name": None,
            "oembed_author_url": None,
        }


def choose_best_text(gdt_preview_text: str, oembed_text: str):
    gdt_preview_text = gdt_preview_text or ""
    oembed_text = oembed_text or ""

    if oembed_text and len(oembed_text) > len(gdt_preview_text):
        return oembed_text, "oembed"

    if oembed_text and not looks_truncated(oembed_text):
        return oembed_text, "oembed"

    return gdt_preview_text, "gamedaytweets_preview"


def enrich_new_posts_with_oembed(new_df: pd.DataFrame) -> pd.DataFrame:
    if new_df.empty:
        return new_df

    enriched_rows = []

    for _, row in new_df.iterrows():
        print(f"Fetching full text for {row['status_id']} from {row.get('source_handle')}...")

        oembed_result = fetch_oembed_text(row["tweet_url"])
        oembed_text = oembed_result.get("oembed_text")

        best_text, best_text_source = choose_best_text(
            gdt_preview_text=row.get("gdt_preview_text"),
            oembed_text=oembed_text,
        )

        line_count = len((best_text or "").splitlines())

        enriched_row = row.to_dict()
        enriched_row.update({
            "oembed_status_code": oembed_result.get("oembed_status_code"),
            "oembed_error": oembed_result.get("oembed_error"),
            "oembed_author_name": oembed_result.get("oembed_author_name"),
            "oembed_author_url": oembed_result.get("oembed_author_url"),
            "oembed_text": oembed_text,
            "best_text": best_text,
            "best_text_source": best_text_source,
            "best_text_is_truncated": looks_truncated(best_text),
            "is_probably_image_based": (
                "pic.twitter.com" in (best_text or "")
                and line_count <= 5
            ),
        })

        enriched_rows.append(enriched_row)
        time.sleep(REQUEST_SLEEP_SECONDS)

    return pd.DataFrame(enriched_rows)


# ============================================================
# SLACK
# ============================================================

def validate_slack_webhook():
    if not SLACK_WEBHOOK_URL:
        raise ValueError("Missing SLACK_WEBHOOK_URL environment variable / GitHub Secret.")

    if not SLACK_WEBHOOK_URL.startswith("https://hooks.slack.com/services/"):
        raise ValueError("SLACK_WEBHOOK_URL does not look like a valid Slack Incoming Webhook URL.")


def send_slack_message(message: str):
    validate_slack_webhook()

    payload = {
        "text": message,
    }

    response = requests.post(
        SLACK_WEBHOOK_URL,
        json=payload,
        timeout=20,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Slack send failed. Status={response.status_code}, Body={response.text}"
        )

    return True


def is_nhl_game_day() -> bool:
    """
    Returns True if there are NHL regular season or playoff games today (ET date).
    Falls back to True if the API is unreachable so we never miss a real game day.
    """
    try:
        now_et = datetime.now(tz=timezone.utc) + timedelta(hours=-4)
        today = now_et.strftime("%Y-%m-%d")
        response = requests.get(
            NHL_SCHEDULE_API.format(date=today),
            headers=HEADERS,
            timeout=10,
        )
        if response.status_code != 200:
            print(f"NHL schedule API returned {response.status_code} — defaulting to run.")
            return True
        data = response.json()
        game_days = data.get("gameWeek", [])
        for day in game_days:
            if day.get("date") != today:
                continue
            for game in day.get("games", []):
                game_type = game.get("gameType")
                # 2 = regular season, 3 = playoffs
                if game_type in (2, 3):
                    return True
        print(f"No NHL regular season or playoff games scheduled for {today}. Skipping run.")
        return False
    except Exception as e:
        print(f"NHL schedule API check failed ({e}) — defaulting to run.")
        return True


def prune_seen_ids(seen_ids: set, max_age_days: int = 7) -> set:
    """
    Removes status IDs older than max_age_days using the Snowflake timestamp.
    """
    cutoff_ms = (datetime.now(tz=timezone.utc).timestamp() * 1000) - (max_age_days * 86400 * 1000)
    pruned = set()
    removed = 0
    for sid in seen_ids:
        try:
            tweet_ms = (int(sid) >> 22) + 1288834974657
            if tweet_ms >= cutoff_ms:
                pruned.add(sid)
            else:
                removed += 1
        except Exception:
            pruned.add(sid)
    if removed:
        print(f"Pruned {removed} seen IDs older than {max_age_days} days.")
    return pruned


def snowflake_to_et(status_id: str) -> str:
    """
    Extracts the UTC timestamp encoded in a Twitter/X Snowflake ID and
    converts it to Eastern Time (ET), handling EST/EDT automatically.
    """
    try:
        snowflake_int = int(status_id)
        # Twitter epoch: 1288834974657 ms (Nov 4, 2010)
        ms = (snowflake_int >> 22) + 1288834974657
        utc_dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        # Determine EST vs EDT: second Sunday in March → first Sunday in November
        year = utc_dt.year
        # Second Sunday in March
        march1 = datetime(year, 3, 1, tzinfo=timezone.utc)
        dst_start = march1 + timedelta(days=(6 - march1.weekday()) % 7 + 7, hours=7)
        # First Sunday in November
        nov1 = datetime(year, 11, 1, tzinfo=timezone.utc)
        dst_end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7, hours=6)
        if dst_start <= utc_dt < dst_end:
            et_dt = utc_dt + timedelta(hours=-4)
            tz_label = "EDT"
        else:
            et_dt = utc_dt + timedelta(hours=-5)
            tz_label = "EST"
        return et_dt.strftime(f"%b %-d, %Y at %-I:%M %p {tz_label}")
    except Exception:
        return "Unknown"


def clean_oembed_footer(text: str) -> str:
    """
    Removes the trailing author/date footer that oEmbed appends to tweet text.
    e.g. "— Jesse Granger (@JesseGranger_)\nJune 9, 2026"
    Already shown in the Source/Date fields of the alert, so strip it from the body.
    """
    if not isinstance(text, str):
        return ""

    lines = [line.rstrip() for line in text.strip().splitlines()]

    while lines and not lines[-1].strip():
        lines.pop()

    if lines and re.match(r"^[A-Za-z]+ \d{1,2}, \d{4}$", lines[-1].strip()):
        lines.pop()

    if lines and re.match(r"^— .+\(@[^)]+\)$", lines[-1].strip()):
        lines.pop()

    while lines and not lines[-1].strip():
        lines.pop()

    return "\n".join(lines).strip()


def format_slack_quote(text: str, max_chars: int = 2800) -> str:
    """
    Formats tweet text as a Slack blockquote. Easier to read than a code block.
    """
    if not isinstance(text, str):
        text = ""

    text = text.strip()

    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n...[truncated]"

    lines = text.splitlines()
    quoted_lines = []

    for line in lines:
        line = line.rstrip()
        quoted_lines.append(f"> {line}" if line else ">")

    return "\n".join(quoted_lines).strip()


def build_slack_notification(row) -> str:
    source = safe_str(row.get("source_handle")) or "Unknown source"
    tweet_url = safe_str(row.get("tweet_url"))
    status_id = safe_str(row.get("status_id"))

    tweet_time = snowflake_to_et(status_id)
    alert_time_utc = datetime.now(tz=timezone.utc)

    # Compute delay between tweet posted and alert sent
    try:
        tweet_ms = (int(status_id) >> 22) + 1288834974657
        tweet_utc = datetime.fromtimestamp(tweet_ms / 1000.0, tz=timezone.utc)
        delay_secs = int((alert_time_utc - tweet_utc).total_seconds())
        if delay_secs < 0:
            delay_secs = 0
        delay_mins, delay_rem = divmod(delay_secs, 60)
        delay_str = f"({delay_mins}m {delay_rem}s delay)"
    except Exception:
        delay_str = ""

    # Alert sent time in ET
    year = alert_time_utc.year
    march1 = datetime(year, 3, 1, tzinfo=timezone.utc)
    dst_start = march1 + timedelta(days=(6 - march1.weekday()) % 7 + 7, hours=7)
    nov1 = datetime(year, 11, 1, tzinfo=timezone.utc)
    dst_end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7, hours=6)
    if dst_start <= alert_time_utc < dst_end:
        alert_et = alert_time_utc + timedelta(hours=-4)
        alert_tz = "EDT"
    else:
        alert_et = alert_time_utc + timedelta(hours=-5)
        alert_tz = "EST"
    alert_time_str = alert_et.strftime(f"%b %-d, %Y at %-I:%M %p {alert_tz}")

    raw_best_text = safe_str(row.get("best_text") or row.get("gdt_preview_text"))
    clean_tweet_text = clean_oembed_footer(raw_best_text)
    formatted_tweet_text = format_slack_quote(clean_tweet_text)

    notes = []

    if bool(row.get("best_text_is_truncated")):
        notes.append("Text may still be truncated. Open the link for the full tweet.")

    if bool(row.get("is_probably_image_based")):
        notes.append("This appears to be an image-based lineup post. Open the link to view the image.")

    notes_text = ""

    if notes:
        notes_text = "\n\n*Notes:*\n" + "\n".join([f"• {note}" for note in notes])

    message = f"""
🚨 *New NHL Line Tweet Detected*

*Source:* {source}
*Tweet Time:* {tweet_time}
*Alert Sent:* {alert_time_str}  {delay_str}

*Line Tweet:*
{formatted_tweet_text}{notes_text}

*Link:* <{tweet_url}|Open tweet>
""".strip()

    return message


def build_test_message() -> str:
    return f"""
🚨 *NHL Line Alert Bot Test*

This is a test message from GitHub Actions.

If this appears in your Slack channel, the webhook connection is working.

Timestamp: {now_str()}
""".strip()


# ============================================================
# ALERT LOGGING
# ============================================================

def append_alert_log(alert_df: pd.DataFrame, path: str = ALERT_LOG_PATH):
    if alert_df.empty:
        return

    fieldnames = [
        "alert_sent_at",
        "status_id",
        "source_handle",
        "tweet_date_text",
        "tweet_url",
        "best_text_source",
        "best_text_is_truncated",
        "is_probably_image_based",
        "best_text",
    ]

    file_exists = os.path.exists(path)

    rows = []

    for _, row in alert_df.iterrows():
        rows.append({
            "alert_sent_at": now_str(),
            "status_id": safe_str(row.get("status_id")),
            "source_handle": safe_str(row.get("source_handle")),
            "tweet_date_text": safe_str(row.get("tweet_date_text")),
            "tweet_url": safe_str(row.get("tweet_url")),
            "best_text_source": safe_str(row.get("best_text_source")),
            "best_text_is_truncated": safe_str(row.get("best_text_is_truncated")),
            "is_probably_image_based": safe_str(row.get("is_probably_image_based")),
            "best_text": safe_str(row.get("best_text")).replace("\r", " ").replace("\n", "\\n"),
        })

    existing_ids = set()

    if file_exists:
        try:
            old_df = pd.read_csv(path)
            if "status_id" in old_df.columns:
                existing_ids = set(old_df["status_id"].dropna().astype(str))
        except Exception:
            existing_ids = set()

    filtered_rows = [row for row in rows if row["status_id"] not in existing_ids]

    if not filtered_rows:
        return

    with open(path, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)

        if not file_exists or os.path.getsize(path) == 0:
            writer.writeheader()

        writer.writerows(filtered_rows)


# ============================================================
# MAIN CHECK
# ============================================================

def check_and_send_alerts(send_backfill_on_first_run: bool = False) -> pd.DataFrame:
    if not is_nhl_game_day():
        return pd.DataFrame()

    current_df = scrape_gameday_line_posts()

    if current_df.empty:
        print("No posts found.")
        return pd.DataFrame()

    seen_ids = load_seen_status_ids()
    seen_ids = prune_seen_ids(seen_ids)

    print(f"Current posts on page: {len(current_df)}")
    print(f"Previously seen status IDs: {len(seen_ids)}")

    current_ids = set(current_df["status_id"].dropna().astype(str))

    if len(seen_ids) == 0 and not send_backfill_on_first_run:
        save_seen_status_ids(current_ids)

        print("First run detected.")
        print(f"Saved {len(current_ids)} current posts as seen.")
        print("No Slack alerts sent on first run.")

        return pd.DataFrame()

    current_df["already_seen"] = current_df["status_id"].astype(str).isin(seen_ids)

    new_df = current_df[current_df["already_seen"] == False].copy()

    print(f"New posts detected before cap: {len(new_df)}")

    if new_df.empty:
        return pd.DataFrame()

    if len(new_df) > MAX_NEW_ALERTS_PER_RUN:
        print(
            f"Safety cap active: limiting alerts from {len(new_df)} "
            f"to {MAX_NEW_ALERTS_PER_RUN} this run."
        )
        new_df = new_df.head(MAX_NEW_ALERTS_PER_RUN).copy()

    new_enriched_df = enrich_new_posts_with_oembed(new_df)

    sent_rows = []

    for _, row in new_enriched_df.iterrows():
        message = build_slack_notification(row)

        try:
            send_slack_message(message)
            print(f"Slack alert sent for {row['status_id']} from {row.get('source_handle')}")
            sent_rows.append(row.to_dict())
            time.sleep(REQUEST_SLEEP_SECONDS)

        except Exception as e:
            print(f"ERROR: Failed to send Slack alert for {row['status_id']}: {e}")

    sent_df = pd.DataFrame(sent_rows)

    if not sent_df.empty:
        updated_seen_ids = seen_ids.union(set(sent_df["status_id"].dropna().astype(str)))
        save_seen_status_ids(updated_seen_ids)
        append_alert_log(sent_df)

        print(f"Saved {len(updated_seen_ids)} total seen IDs.")
        print(f"Logged {len(sent_df)} sent alerts.")
    else:
        print("No alerts were successfully sent. Seen IDs were not updated.")

    return sent_df


# ============================================================
# CLI
# ============================================================

def parse_bool(value: str) -> bool:
    if isinstance(value, bool):
        return value

    value = str(value).strip().lower()

    return value in {"true", "1", "yes", "y"}


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--mode",
        choices=["check", "test-slack", "scrape-only"],
        default="check",
        help="Run mode.",
    )

    parser.add_argument(
        "--send-backfill-on-first-run",
        default="false",
        help="If true, sends current existing posts as alerts when seen file is empty.",
    )

    args = parser.parse_args()

    if args.mode == "test-slack":
        print("Sending Slack test message...")
        send_slack_message(build_test_message())
        print("Slack test message sent.")
        return

    if args.mode == "scrape-only":
        print("Running scrape-only test...")
        df = scrape_gameday_line_posts()
        print(df[[
            "row_num",
            "status_id",
            "source_handle",
            "tweet_date_text",
            "tweet_url",
            "gdt_is_truncated",
            "gdt_preview_text",
        ]].head(10).to_string(index=False))
        return

    send_backfill = parse_bool(args.send_backfill_on_first_run)

    print(f"Run started at {now_str()}")
    print(f"Mode: {args.mode}")
    print(f"send_backfill_on_first_run: {send_backfill}")

    sent_df = check_and_send_alerts(
        send_backfill_on_first_run=send_backfill
    )

    print(f"Run complete. Alerts sent: {len(sent_df)}")


if __name__ == "__main__":
    main()
