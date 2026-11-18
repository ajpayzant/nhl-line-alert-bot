# ============================================================
# NHL.com Projected Lineups Alert Bot
# ============================================================
#
# What it does:
#   1. Reads NHL.com's season-long "projected lineups" article
#      (nhl.com/news/nhl-lineup-projections-<season>-season), rewritten
#      every day around 3:30 PM ET with that night's games and edited after
#   2. The first time a team's lineup for a game appears, posts it to Slack
#   3. When NHL.com edits the article later, re-posts ONLY the teams whose
#      lines, pairs or starting goalie changed, with what changed listed first
#   4. Saves per-game state and an alert log for future runs
#
# The article comes from NHL.com's content API as JSON, one Markdown part per
# game. Player names are plain text with the odd typo ("David Pastrnl"), so
# each name is corrected against the team's NHL.com roster before lineups are
# compared; a typo fix is never mistaken for a lineup change.
#
# One request per run when nothing changed (the article's last-edited time is
# compared first). Nothing is posted for a game after its puck drop.
#
# Required GitHub Secret:
#   SLACK_WEBHOOK_URL
#
# Files used:
#   nhlcom_lines_state.json
#   nhlcom_line_alert_log.csv
# ============================================================

import os
import re
import csv
import json
import time
import argparse
import difflib
import unicodedata

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

import lineup_xcheck
from gameday_lines_alert import (
    HEADERS,
    format_et,
    now_str,
    parse_bool,
    send_slack_message,
)


# ============================================================
# CONFIG
# ============================================================

API = "https://forge-dapi.d3.nhle.com/v2/content/en-us/stories/{slug}"
ROSTER_API = "https://api-web.nhle.com/v1/roster/{team}/current"
ARTICLE_URL = "https://www.nhl.com/news/{slug}"

STATE_PATH = "nhlcom_lines_state.json"
ALERT_LOG_PATH = "nhlcom_line_alert_log.csv"

# The first post of the day is one message per team (about 20-30), so this
# cap is higher than the other bots'. Slack webhooks take ~1 message/second.
MAX_ALERTS_PER_RUN = int(os.getenv("NHLCOM_MAX_ALERTS_PER_RUN", "40"))
SEND_SLEEP_SECONDS = float(os.getenv("NHLCOM_SEND_SLEEP_SECONDS", "1.1"))
KEEP_DAYS = 3

ET = ZoneInfo("America/New_York")

# Nickname as NHL.com writes it in game headers / "<Team> projected lineup".
TEAMS = {
    "Ducks": ("ANA", "Anaheim Ducks"), "Bruins": ("BOS", "Boston Bruins"),
    "Sabres": ("BUF", "Buffalo Sabres"), "Flames": ("CGY", "Calgary Flames"),
    "Hurricanes": ("CAR", "Carolina Hurricanes"), "Blackhawks": ("CHI", "Chicago Blackhawks"),
    "Avalanche": ("COL", "Colorado Avalanche"), "Blue Jackets": ("CBJ", "Columbus Blue Jackets"),
    "Stars": ("DAL", "Dallas Stars"), "Red Wings": ("DET", "Detroit Red Wings"),
    "Oilers": ("EDM", "Edmonton Oilers"), "Panthers": ("FLA", "Florida Panthers"),
    "Kings": ("LAK", "Los Angeles Kings"), "Wild": ("MIN", "Minnesota Wild"),
    "Canadiens": ("MTL", "Montreal Canadiens"), "Predators": ("NSH", "Nashville Predators"),
    "Devils": ("NJD", "New Jersey Devils"), "Islanders": ("NYI", "New York Islanders"),
    "Rangers": ("NYR", "New York Rangers"), "Senators": ("OTT", "Ottawa Senators"),
    "Flyers": ("PHI", "Philadelphia Flyers"), "Penguins": ("PIT", "Pittsburgh Penguins"),
    "Sharks": ("SJS", "San Jose Sharks"), "Kraken": ("SEA", "Seattle Kraken"),
    "Blues": ("STL", "St. Louis Blues"), "Lightning": ("TBL", "Tampa Bay Lightning"),
    "Maple Leafs": ("TOR", "Toronto Maple Leafs"), "Mammoth": ("UTA", "Utah Mammoth"),
    "Canucks": ("VAN", "Vancouver Canucks"), "Golden Knights": ("VGK", "Vegas Golden Knights"),
    "Capitals": ("WSH", "Washington Capitals"), "Jets": ("WPG", "Winnipeg Jets"),
}
_BY_NICK = {k.lower(): v for k, v in TEAMS.items()}

LOG_COLUMNS = ["alert_sent_at", "game_date", "team", "kind", "article_updated", "changes", "lineup"]


def season_slug(today=None) -> str:
    today = today or datetime.now(ET).date()
    start = today.year if today.month >= 7 else today.year - 1
    return f"nhl-lineup-projections-{start}-{str(start + 1)[2:]}-season"


SLUG = os.getenv("NHLCOM_LINEUP_SLUG") or season_slug()


# ============================================================
# FETCH
# ============================================================

def get_json(url: str):
    last = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Could not fetch {url}: {last}")


def fetch_article(slug: str = SLUG) -> dict:
    story = get_json(API.format(slug=slug))
    if not story:
        raise RuntimeError(f"NHL.com has no story {slug}")
    return story


def fetch_roster(team: str) -> list:
    """[(full name, position code)] for a team, injured players included."""
    data = get_json(ROSTER_API.format(team=team)) or {}
    return [(f"{p['firstName']['default']} {p['lastName']['default']}", p.get("positionCode", ""))
            for group in ("forwards", "defensemen", "goalies") for p in data.get(group) or []]


# ============================================================
# PARSE
# ============================================================

def plain(s: str) -> str:
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)        # Markdown links
    s = re.sub(r"[*_]", "", s).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


_GAME = re.compile(r"^##\s*\**\s*(.+?)\s*(?:\([^)]*\))?\s+at\s+(.+?)\s*(?:\([^)]*\))?\s*\**\s*$", re.M)
_TIME = re.compile(r"^(\d{1,2})(?::(\d\d))?\s*([ap])\.m\.,?\s*ET", re.I)   # "7 p.m. ET", "7:30 p.m., ET"
_DASH = re.compile(r"\s+(?:--|–|—|-)\s+|\s*(?:--|–|—)\s*")


def team_from(nick: str):
    return _BY_NICK.get(plain(nick).lower())


def parse_article(story: dict) -> list:
    """One dict per team per game, in article order (away team first)."""
    game_date = datetime.fromisoformat(story["contentDate"].replace("Z", "+00:00")).astimezone(ET).date()
    out = []
    for part in (p.get("content") or "" for p in story.get("parts") or [] if p.get("type") == "markdown"):
        g = _GAME.search(part)
        if not g or not team_from(g.group(1)) or not team_from(g.group(2)):
            continue
        away, home = team_from(g.group(1)), team_from(g.group(2))
        blocks = [plain(b) for b in re.split(r"\n\s*\n", part[g.end():])]
        start = None
        for b in blocks:
            m = _TIME.match(b)
            if m:
                hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
                start = datetime(game_date.year, game_date.month, game_date.day, hour, int(m.group(2) or 0), tzinfo=ET)
                break
        notes = []
        if "Status report" in blocks:
            notes = [n.strip() for b in blocks[blocks.index("Status report") + 1:]
                     for n in re.split(r"\s*(?:…|\.\.\.)\s*", b) if n.strip()]
        team = None
        for b in blocks:
            m = re.match(r"(.+?) projected lineup$", b)
            if m and team_from(m.group(1)):
                abbr, full = team_from(m.group(1))
                team = {"team": abbr, "team_name": full, "rows": [], "singles": [], "scratched": [], "injured": [],
                        "game_date": game_date.isoformat(), "start": start.isoformat() if start else "",
                        "away": away, "home": home, "notes": notes}
                out.append(team)
            elif b == "Status report":
                team = None
            elif team is None or not b:
                continue
            elif b.startswith("Scratched:") or b.startswith("Injured:"):
                key, _, rest = b.partition(":")
                names = [] if rest.strip().lower() in ("", "none") else [n.strip() for n in re.split(r",\s*", rest) if n.strip()]
                team[key.lower()] = names
            elif _DASH.search(b):
                team["rows"].append([n.strip() for n in _DASH.split(b) if n.strip()])
            elif len(b.split()) <= 4 and not _TIME.match(b):
                team["singles"].append(b)
    return out


def name_key(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return re.sub(r"[^a-z ]", "", "".join(c for c in s if not unicodedata.combining(c)).lower()).strip()


def matcher(roster: list):
    """name -> (roster name, position) or (name, "") when nothing is close."""
    by_key = {name_key(n): (n, pos) for n, pos in roster}
    keys = list(by_key)

    def match(name: str):
        k = name_key(re.sub(r"\s*\(.*?\)", "", name))
        if k in by_key:
            return by_key[k]
        close = difflib.get_close_matches(k, keys, n=2, cutoff=0.82)
        if len(close) == 1 or (close and difflib.SequenceMatcher(None, k, close[0]).ratio() >= 0.9):
            return by_key[close[0]]
        last = [key for key in keys if key.split()[-1:] == k.split()[-1:]]
        if len(last) == 1 and k[:1] == last[0][:1]:
            return by_key[last[0]]
        return (name, "")
    return match


def normalize(team: dict, roster: list) -> dict:
    """Roster-correct every name, then split the dash rows into forward lines and
    defence pairs (by roster position, else NHL.com's order: the last three rows
    are the pairs) and the single names into goalies."""
    match = matcher(roster)
    fixed, unmatched = {}, []

    def fix(name: str) -> str:
        # "James Hagens (back)"; also NHL.com's "Dylan DeMelo lower body)" with the "(" missing.
        injury = re.search(r"\s*(?:\((.*?)\)|\s([a-z][a-z .-]*)\))\s*$", name)
        bare = name[:injury.start()] if injury else name
        new, pos = match(bare)
        if not pos and roster:
            unmatched.append(bare)
        if new != bare:
            fixed[bare] = new
        return new + (f" ({injury.group(1) or injury.group(2)})" if injury else "")

    rows = [[fix(n) for n in row] for row in team["rows"]]
    pos = {n: p for n, p in roster}
    is_d = [sum(pos.get(n) == "D" for n in row) > len(row) / 2 for row in rows]
    if any(is_d) and not all(is_d):
        forwards = [r for r, d in zip(rows, is_d) if not d]
        defense = [r for r, d in zip(rows, is_d) if d]
    else:
        forwards, defense = rows[:-3], rows[-3:]
    goalies = [fix(n) for n in team["singles"]][:2]
    return {**team, "forwards": forwards, "defense": defense, "goalies": goalies,
            "scratched": [fix(n) for n in team["scratched"]], "injured": [fix(n) for n in team["injured"]],
            "fixed": fixed, "unmatched": unmatched}


# ============================================================
# CHANGES
# ============================================================

def signature(t: dict) -> str:
    """What counts as a lineup change: who is on which forward line / defence pair
    (order within a line ignored) and the projected starting goalie."""
    return json.dumps([[sorted(r) for r in t["forwards"]], [sorted(r) for r in t["defense"]], t["goalies"][:1]])


def players(t: dict) -> set:
    return {n for row in t["forwards"] + t["defense"] for n in row} | set(t["goalies"][:1])


def slots(t: dict) -> dict:
    return {**{n: f"L{i + 1}" for i, r in enumerate(t["forwards"]) for n in r},
            **{n: f"D{i + 1}" for i, r in enumerate(t["defense"]) for n in r}}


def describe_changes(t: dict, prev: dict) -> list:
    out = []
    now_g, was_g = t["goalies"][:1], prev["goalies"][:1]
    if now_g != was_g:
        out.append(f"*Starting goalie:* {was_g[0] if was_g else '—'} → {now_g[0] if now_g else '—'}")
    skaters_now = players(t) - set(now_g)
    skaters_was = players(prev) - set(was_g)
    if skaters_now - skaters_was:
        out.append("*In:* " + ", ".join(sorted(skaters_now - skaters_was)))
    if skaters_was - skaters_now:
        out.append("*Out:* " + ", ".join(sorted(skaters_was - skaters_now)))
    a, b = slots(prev), slots(t)
    moved = [f"{n} {a[n]} → {b[n]}" for n in b if n in a and a[n] != b[n]]
    if moved:
        out.append("*Moved:* " + ", ".join(moved))
    return out


def team_notes(t: dict) -> list:
    """The game's status-report sentences that mention one of this team's players."""
    names = players(t) | set(t["goalies"]) | {re.sub(r"\s*\(.*?\)", "", n) for n in t["scratched"] + t["injured"]}
    last = {n.split()[-1] for n in names if n}
    return [s for s in t["notes"] if any(re.search(rf"\b{re.escape(x)}\b", s) for x in last)]


# ============================================================
# SLACK
# ============================================================

def game_label(t: dict) -> str:
    nick = {v[0]: k for k, v in TEAMS.items()}
    label = f"{nick[t['away'][0]]} at {nick[t['home'][0]]}"
    if t["start"]:
        s = datetime.fromisoformat(t["start"])
        label += f", {s.hour % 12 or 12}:{s:%M} {'AM' if s.hour < 12 else 'PM'} ET"
    return label


def build_slack_notification(t: dict, kind: str, prev: dict | None, article_updated: str,
                             sent_at: datetime | None = None) -> str:
    changed_rows = set()
    if prev:
        old = {json.dumps(sorted(r)) for r in prev["forwards"] + prev["defense"]}
        changed_rows = {i for i, r in enumerate(t["forwards"] + t["defense"]) if json.dumps(sorted(r)) not in old}

    rows = t["forwards"] + t["defense"]
    body = []
    for i, r in enumerate(rows):
        if i == len(t["forwards"]):
            body.append("")
        body.append(" - ".join(r) + ("   ◀" if i in changed_rows else ""))
    body.append("")
    if t["goalies"]:
        g = f"G: {t['goalies'][0]}" + (f"  (backup {t['goalies'][1]})" if len(t["goalies"]) > 1 else "")
        body.append(g + ("   ◀" if prev and t["goalies"][:1] != prev["goalies"][:1] else ""))

    updated = datetime.fromisoformat(article_updated.replace("Z", "+00:00"))
    title = "NHL.com Projected Lineup" if kind == "new" else "NHL.com Lineup Changed"
    msg = [f"{'🏒' if kind == 'new' else '🔁'} *{title}: {t['team_name']} ({t['team']})*", "",
           f"*Game:* {game_label(t)}",
           f"*NHL.com Updated:* {format_et(updated)}",
           f"*Alert Sent:* {format_et(sent_at or datetime.now(tz=timezone.utc))}"]
    if prev:
        msg += ["", f"*Changes since the {format_et(datetime.fromisoformat(prev['article_updated'].replace('Z', '+00:00'))).split(' at ')[1]} projection:*",
                *describe_changes(t, prev)]
    msg += ["", "```" + "\n".join(body).strip() + "```"]
    msg.append(f"*Scratched:* {', '.join(t['scratched']) or 'None'}")
    msg.append(f"*Injured:* {', '.join(t['injured']) or 'None'}")
    notes = team_notes(t)
    if notes and kind == "new":
        msg.append("*Notes:* " + " … ".join(notes))
    msg.append(f"*Link:* <{ARTICLE_URL.format(slug=SLUG)}|NHL.com projected lineups>  "
               "_(first goalie listed is NHL.com's projected starter)_")
    return "\n".join(msg)


# ============================================================
# STATE / LOG
# ============================================================

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
        json.dump(state, f, indent=1, sort_keys=True, ensure_ascii=False)


def entry(t: dict, article_updated: str) -> dict:
    return {"forwards": t["forwards"], "defense": t["defense"], "goalies": t["goalies"],
            "scratched": t["scratched"], "article_updated": article_updated}


def append_alert_log(t: dict, kind: str, article_updated: str, changes: list, path: str = ALERT_LOG_PATH):
    new_file = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(LOG_COLUMNS)
        w.writerow([now_str(), t["game_date"], t["team"], kind, article_updated, " | ".join(changes), signature(t)])


def rosters_for(state: dict, teams: list, today: str, fetch=fetch_roster) -> dict:
    """Rosters cached in state for the day; fetched once per team per day."""
    cache = state.get("rosters") or {}
    if cache.get("date") != today:
        cache = {"date": today, "teams": {}}
    for t in teams:
        if t["team"] not in cache["teams"]:
            try:
                cache["teams"][t["team"]] = fetch(t["team"])
            except Exception as e:
                print(f"Roster for {t['team']} unavailable ({e}); using NHL.com's spelling.")
                cache["teams"][t["team"]] = []
    state["rosters"] = cache
    return cache["teams"]


# ============================================================
# MAIN CHECK
# ============================================================

def check_and_send_alerts(story: dict, state: dict, now: datetime, send=send_slack_message,
                          send_backfill_on_first_run: bool = False, fetch=fetch_roster,
                          save=save_state, log=append_alert_log, sleep=time.sleep, xcheck=None) -> list:
    """Returns the messages sent. ``story`` is the article JSON; ``state`` is
    updated in place (and saved via ``save`` after every send)."""
    article_updated = story.get("lastUpdatedDate") or story["contentDate"]
    if state.get("article_updated") == article_updated:
        print(f"Article unchanged since {article_updated}.")
        return []
    if state.get("article_updated") and article_updated < state["article_updated"]:
        # An older cached copy (as Daily Faceoff's CDN serves sometimes): never go backwards.
        print(f"Ignoring an older copy of the article ({article_updated} < {state['article_updated']}).")
        return []

    first_run = "lineups" not in state
    lineups = state.setdefault("lineups", {})
    teams = parse_article(story)
    today = now.astimezone(ET).date().isoformat()
    rosters = rosters_for(state, teams, today, fetch)
    teams = [normalize(t, rosters.get(t["team"], [])) for t in teams]
    for t in teams:
        if t["fixed"]:
            print(f"  {t['team']}: corrected {t['fixed']}")
    print(f"Article updated {article_updated}: {len(teams)} team lineups for {teams[0]['game_date'] if teams else '-'}.")

    xcheck = xcheck or lineup_xcheck.Checker("nhlcom")
    sent = []
    for t in sorted(teams, key=lambda t: (t["start"], t["home"][0], t["team"] != t["away"][0])):
        key = f"{t['game_date']}:{t['team']}"
        prev = lineups.get(key)
        if first_run and not send_backfill_on_first_run:
            lineups[key] = entry(t, article_updated)
            continue
        if t["start"] and now >= datetime.fromisoformat(t["start"]):
            continue  # game under way: projections no longer matter
        if not (t["forwards"] or t["defense"]) or (prev and signature(prev) == signature(t)):
            if prev:
                prev["scratched"] = t["scratched"]
            continue
        if len(sent) >= MAX_ALERTS_PER_RUN:
            print(f"Hit the {MAX_ALERTS_PER_RUN}-alert cap; {t['team']} waits for the next run.")
            continue
        kind = "update" if prev else "new"
        lineup = {"forwards": t["forwards"], "defense": t["defense"], "goalie": t["goalies"][0] if t["goalies"] else ""}
        message = xcheck.annotate(build_slack_notification(t, kind, prev, article_updated, now), t["team"], lineup, now)
        if sent:
            sleep(SEND_SLEEP_SECONDS)
        send(message)
        xcheck.record(t["team"], lineup, now)
        log(t, kind, article_updated, describe_changes(t, prev) if prev else [])
        lineups[key] = entry(t, article_updated)
        save(state)
        sent.append(message)
        print(f"Sent {kind} alert for {t['team']}")

    cutoff = (now.astimezone(ET).date() - timedelta(days=KEEP_DAYS)).isoformat()
    state["lineups"] = {k: v for k, v in lineups.items() if k[:10] >= cutoff}
    if not (len(sent) >= MAX_ALERTS_PER_RUN):
        state["article_updated"] = article_updated   # a capped run re-reads the article next time
    if first_run and not send_backfill_on_first_run:
        print("First run: saved current lineups without alerting.")
    save(state)
    return sent


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["check", "scrape-only", "send-latest"], default="check")
    parser.add_argument("--send-backfill-on-first-run", default="false",
                        help="If true, posts every current lineup when the state file is empty.")
    args = parser.parse_args()

    story = fetch_article()
    if args.mode == "scrape-only":
        state = load_state()
        rosters = rosters_for(state, parse_article(story), datetime.now(ET).date().isoformat())
        for t in parse_article(story):
            t = normalize(t, rosters.get(t["team"], []))
            print(f"{t['game_date']} {t['team']:4} F{len(t['forwards'])} D{len(t['defense'])} "
                  f"G {t['goalies'][:1]}  fixed {t['fixed']}")
        return

    if args.mode == "send-latest":
        # End-to-end test: posts the article's first team without touching state.
        teams = parse_article(story)
        if teams:
            t = normalize(teams[0], fetch_roster(teams[0]["team"]))
            send_slack_message(build_slack_notification(t, "new", None, story["lastUpdatedDate"]))
            print(f"Slack alert sent for {t['team']}")
        return

    print(f"NHL.com lineups run started at {now_str()}")
    sent = check_and_send_alerts(story, load_state(), datetime.now(timezone.utc),
                                 send_backfill_on_first_run=parse_bool(args.send_backfill_on_first_run))
    print(f"NHL.com lineups run complete. Alerts sent: {len(sent)}")


if __name__ == "__main__":
    main()
