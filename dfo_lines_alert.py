# ============================================================
# Daily Faceoff NHL Line Alert Bot
# ============================================================
#
# What it does:
#   1. Reads every team's line-combinations page on dailyfaceoff.com
#      (the same data @DFOFantasy tweets its lineups from)
#   2. Detects lineup updates: a new source (usually a beat reporter's
#      tweet) or a changed lineup from the same source
#   3. Sends the lines to Slack, with who moved in and out
#   4. Saves per-team state and an alert log for future runs
#
# X timelines can't be polled (no API plan, embed feed returns 429),
# so the DFO site is used instead of the @DFOFantasy account itself.
# Post-game "Last Game" resets are recorded but never alerted.
#
# Required GitHub Secret:
#   SLACK_WEBHOOK_URL
#
# Files used:
#   dfo_lines_state.json
#   dfo_line_alert_log.csv
# ============================================================

import os
import re
import csv
import json
import time
import argparse
import requests

from datetime import datetime, timezone

from gameday_lines_alert import (
    HEADERS,
    MAX_NEW_ALERTS_PER_RUN,
    cffi_requests,
    extract_status_id,
    format_et,
    now_str,
    parse_bool,
    send_slack_message,
    status_id_to_utc,
)


# ============================================================
# CONFIG
# ============================================================

TEAM_URL = "https://www.dailyfaceoff.com/teams/{slug}/line-combinations"

STATE_PATH = "dfo_lines_state.json"
ALERT_LOG_PATH = "dfo_line_alert_log.csv"

# Seconds between team page requests.
PAGE_SLEEP_SECONDS = float(os.getenv("DFO_PAGE_SLEEP_SECONDS", "0.3"))

TEAM_SLUGS = [
    "anaheim-ducks", "boston-bruins", "buffalo-sabres", "calgary-flames",
    "carolina-hurricanes", "chicago-blackhawks", "colorado-avalanche",
    "columbus-blue-jackets", "dallas-stars", "detroit-red-wings",
    "edmonton-oilers", "florida-panthers", "los-angeles-kings",
    "minnesota-wild", "montreal-canadiens", "nashville-predators",
    "new-jersey-devils", "new-york-islanders", "new-york-rangers",
    "ottawa-senators", "philadelphia-flyers", "pittsburgh-penguins",
    "san-jose-sharks", "seattle-kraken", "st-louis-blues",
    "tampa-bay-lightning", "toronto-maple-leafs", "utah-mammoth",
    "vancouver-canucks", "vegas-golden-knights", "washington-capitals",
    "winnipeg-jets",
]

NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

LOG_COLUMNS = [
    "alert_sent_at", "team", "kind", "dfo_updated_at", "source_name",
    "source", "lineup",
]


# ============================================================
# SCRAPE DAILY FACEOFF
# ============================================================

def fetch_page(url: str) -> str:
    errors = []

    if cffi_requests is not None:
        try:
            response = cffi_requests.get(url, impersonate="chrome", timeout=25)
            if response.status_code == 200:
                return response.text
            errors.append(f"curl_cffi: HTTP {response.status_code}")
        except Exception as e:
            errors.append(f"curl_cffi: {e}")

    response = requests.get(url, headers=HEADERS, timeout=25)
    if response.status_code == 200:
        return response.text
    errors.append(f"requests: HTTP {response.status_code}")

    raise RuntimeError(f"Could not fetch {url} ({'; '.join(errors)})")


def last_name(full_name: str) -> str:
    parts = full_name.split()
    return " ".join(parts[1:]) if len(parts) > 1 else full_name


def parse_team(html: str) -> dict:
    """
    Pulls one team's lineup out of the page's embedded Next.js data.
    Lines are lists of last names, ordered by DFO's position identifiers.
    """
    match = NEXT_DATA_RE.search(html)
    if not match:
        raise ValueError("No __NEXT_DATA__ block on page")

    combo = json.loads(match.group(1))["props"]["pageProps"]["combinations"]

    groups = {}
    flags = {}
    for p in combo.get("players") or []:
        groups.setdefault(p["groupIdentifier"], []).append(p)
        name = last_name(p["name"])
        if p.get("gameTimeDecision"):
            flags[name] = "GTD"
        elif p.get("injuryStatus") and p["categoryIdentifier"] != "oi":
            flags[name] = p["injuryStatus"].upper()

    def line(group_id: str) -> list:
        players = sorted(groups.get(group_id, []), key=lambda p: p["positionIdentifier"])
        if group_id.startswith("f"):
            order = {"lw": 0, "c": 1, "rw": 2}
            players = sorted(players, key=lambda p: order.get(p["positionIdentifier"], 9))
        elif group_id.startswith("d"):
            order = {"ld": 0, "rd": 1}
            players = sorted(players, key=lambda p: order.get(p["positionIdentifier"], 9))
        return [last_name(p["name"]) for p in players]

    goalies = sorted(groups.get("g", []), key=lambda p: p["positionIdentifier"])

    return {
        "team": combo["teamAbbreviation"],
        "team_name": combo["teamName"],
        "slug": combo.get("teamSlug") or "",
        "updated_at": combo.get("updatedAt") or "",
        "source_name": (combo.get("sourceName") or "").strip(),
        "source": (combo.get("source") or "").strip(),
        "forwards": [line(f"f{i}") for i in range(1, 5) if groups.get(f"f{i}")],
        "defense": [line(f"d{i}") for i in range(1, 4) if groups.get(f"d{i}")],
        "goalies": [last_name(p["name"]) for p in goalies],
        "pp1": line("pp1"),
        "pp2": line("pp2"),
        "flags": flags,
    }


def scrape_all_teams() -> list:
    teams = []
    failures = []
    for slug in TEAM_SLUGS:
        try:
            teams.append(parse_team(fetch_page(TEAM_URL.format(slug=slug))))
        except Exception as e:
            failures.append(slug)
            print(f"Failed to read {slug}: {e}")
        time.sleep(PAGE_SLEEP_SECONDS)

    if len(failures) > len(TEAM_SLUGS) // 2:
        raise RuntimeError(f"Could not read {len(failures)} of {len(TEAM_SLUGS)} DFO team pages.")

    return teams


# ============================================================
# STATE
# ============================================================

def lineup_signature(team: dict) -> str:
    """What counts as a lineup change: even-strength lines, starter, and PP1."""
    return json.dumps([team["forwards"], team["defense"], team["goalies"][:1], team["pp1"]])


def lineup_players(team: dict) -> set:
    return {n for line in team["forwards"] + team["defense"] for n in line}


def is_reset(team: dict) -> bool:
    """Post-game resets and sourceless projections are not news."""
    return team["source_name"].lower().startswith("last game") or not team["source"]


def load_state(path: str = STATE_PATH) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"Could not read {path}: {e}. Starting with empty state.")
        return {}


def save_state(state: dict, path: str = STATE_PATH):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def state_entry(team: dict) -> dict:
    return {
        "updated_at": team["updated_at"],
        "source": team["source"],
        "signature": lineup_signature(team),
        "players": sorted(lineup_players(team)),
        "goalie": team["goalies"][0] if team["goalies"] else "",
    }


def alert_kind(team: dict, prev: dict | None) -> str | None:
    """Returns "new", "update" or None (no alert)."""
    if prev is None or is_reset(team):
        return None
    if team["source"] != prev.get("source"):
        return "new"
    if lineup_signature(team) != prev.get("signature"):
        return "update"
    return None


# ============================================================
# SLACK
# ============================================================

def source_tweet_time(source: str):
    status_id = extract_status_id(source) if source else None
    if not status_id:
        return None
    try:
        return status_id_to_utc(status_id)
    except Exception:
        return None


def build_slack_notification(team: dict, kind: str, prev: dict | None) -> str:
    flags = team["flags"]

    def fmt(names: list, sep: str) -> str:
        return sep.join(f"{n} ({flags[n]})" if n in flags else n for n in names)

    body = [fmt(line, " - ") for line in team["forwards"]]
    body.append("")
    body += [fmt(line, " - ") for line in team["defense"]]
    body.append("")
    if team["goalies"]:
        body.append(f"G: {fmt(team['goalies'][:1], '')}"
                    + (f"  (backup {team['goalies'][1]})" if len(team["goalies"]) > 1 else ""))
    if team["pp1"]:
        body.append(f"PP1: {fmt(team['pp1'], ', ')}")
    if team["pp2"]:
        body.append(f"PP2: {fmt(team['pp2'], ', ')}")

    changes = []
    if prev:
        before = set(prev.get("players") or [])
        now = lineup_players(team)
        if now - before:
            changes.append("*In:* " + ", ".join(sorted(now - before)))
        if before - now:
            changes.append("*Out:* " + ", ".join(sorted(before - now)))
        starter = team["goalies"][0] if team["goalies"] else ""
        if prev.get("goalie") and starter and starter != prev["goalie"]:
            changes.append(f"*Goalie:* {prev['goalie']} → {starter}")

    alert_utc = datetime.now(tz=timezone.utc)
    timing = [f"*DFO Updated:* {format_et(datetime.fromisoformat(team['updated_at'].replace('Z', '+00:00')))}"]
    tweet_utc = source_tweet_time(team["source"])
    if tweet_utc:
        delay = max(0, int((alert_utc - tweet_utc).total_seconds()))
        timing.insert(0, f"*Source Tweet:* {format_et(tweet_utc)}")
        timing.append(f"*Alert Sent:* {format_et(alert_utc)}  ({delay // 60}m {delay % 60}s after the tweet)")
    else:
        timing.append(f"*Alert Sent:* {format_et(alert_utc)}")

    title = "New DFO Lines" if kind == "new" else "DFO Lines Updated"
    source_label = team["source_name"] or "Unknown"
    if team["source"].startswith("http"):
        source_label = f"<{team['source']}|{source_label}>"

    message = [
        f"🏒 *{title}: {team['team_name']} ({team['team']})*",
        "",
        f"*Source:* {source_label}",
        *timing,
    ]
    if changes:
        message += ["", "*Changes vs previous lines:*", *changes]
    message += ["", "```" + "\n".join(body).strip() + "```"]
    if team["slug"]:
        message.append(f"*Link:* <{TEAM_URL.format(slug=team['slug'])}|DFO line combinations>")

    return "\n".join(message)


# ============================================================
# ALERT LOGGING
# ============================================================

def append_alert_log(team: dict, kind: str, path: str = ALERT_LOG_PATH):
    new_file = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(LOG_COLUMNS)
        writer.writerow([
            now_str(), team["team"], kind, team["updated_at"], team["source_name"],
            team["source"], lineup_signature(team),
        ])


# ============================================================
# MAIN CHECK
# ============================================================

def check_and_send_alerts(send_backfill_on_first_run: bool = False) -> int:
    state = load_state()
    first_run = not state
    teams = scrape_all_teams()
    print(f"Read {len(teams)} DFO team pages.")

    if first_run and not send_backfill_on_first_run:
        for team in teams:
            state[team["team"]] = state_entry(team)
        save_state(state)
        print("First run: saved current lines without alerting.")
        return 0

    # Oldest DFO update first, so Slack reads in order.
    teams.sort(key=lambda t: t["updated_at"])

    sent = 0
    for team in teams:
        prev = state.get(team["team"])
        kind = alert_kind(team, prev) if not first_run else ("new" if not is_reset(team) else None)

        if kind is None:
            if prev is None or prev.get("updated_at") != team["updated_at"]:
                state[team["team"]] = state_entry(team)
                save_state(state)
            continue

        if sent >= MAX_NEW_ALERTS_PER_RUN:
            print(f"Hit the {MAX_NEW_ALERTS_PER_RUN}-alert cap; {team['team']} waits for the next run.")
            continue

        send_slack_message(build_slack_notification(team, kind, prev))
        append_alert_log(team, kind)
        state[team["team"]] = state_entry(team)
        save_state(state)
        sent += 1
        print(f"Sent {kind} alert for {team['team']} ({team['source_name']})")

    return sent


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["check", "scrape-only", "send-latest"],
        default="check",
        help="Run mode.",
    )
    parser.add_argument(
        "--send-backfill-on-first-run",
        default="false",
        help="If true, alerts every team's current lines when the state file is empty.",
    )
    args = parser.parse_args()

    if args.mode == "scrape-only":
        for team in sorted(scrape_all_teams(), key=lambda t: t["updated_at"], reverse=True):
            flag = "skip" if is_reset(team) else "news"
            print(f"{team['team']:4} {team['updated_at']}  [{flag}] {team['source_name']}  {team['source']}")
        return

    if args.mode == "send-latest":
        # End-to-end test: sends the most recent non-reset update without touching state.
        teams = [t for t in scrape_all_teams() if not is_reset(t)]
        if not teams:
            print("No sourced line updates found.")
            return
        team = max(teams, key=lambda t: t["updated_at"])
        send_slack_message(build_slack_notification(team, "new", load_state().get(team["team"])))
        print(f"Slack alert sent for {team['team']}")
        return

    print(f"DFO run started at {now_str()}")
    sent = check_and_send_alerts(parse_bool(args.send_backfill_on_first_run))
    print(f"DFO run complete. Alerts sent: {sent}")


if __name__ == "__main__":
    main()
