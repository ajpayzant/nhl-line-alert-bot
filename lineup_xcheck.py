# ============================================================
# Cross-check lineups between the three line sources
# ============================================================
#
# GameDayTweets (beat-reporter tweets), Daily Faceoff line combinations and
# NHL.com projected lineups all post to the same channel. Before a post goes
# out, its lineup is compared with the latest lineup posted earlier the same
# day for that team by each OTHER source, and one line is added to the post:
#
#   ✅ Cross-check: matches NHL.com projection (12:35 PM)
#   ⚠️ Cross-check vs Daily Faceoff (1:02 PM): L2 Minten-Lindholm-Khusnutdinov
#      (DFO: Mittelstadt-Lindholm-Khusnutdinov) · G Swayman (DFO: DiPietro)
#
# Only what both lineups have is compared (a practice tweet with forward lines
# only is compared on the forward lines). Line order within a line is ignored.
# Names are compared by NHL.com roster last name, so "Pastrnl", "Pastrnak" and
# "David Pastrnak" all match.
#
# Tweets that aren't line lists (power-play units, injury notes, images) get
# no cross-check line and are posted exactly as before. Every function here is
# called through safe(): a cross-check problem can never stop a post.
#
# Files used:
#   posted_lineups.json   latest posted lineup per team per source, by day
#   roster_cache.json     NHL.com rosters, refreshed once a day
# ============================================================

import json
import os
import re
import time
import difflib
import unicodedata
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")
POSTED_PATH = "posted_lineups.json"
ROSTER_PATH = "roster_cache.json"
ROSTER_API = "https://api-web.nhle.com/v1/roster/{team}/current"
TEAMS = ["ANA", "BOS", "BUF", "CGY", "CAR", "CHI", "COL", "CBJ", "DAL", "DET", "EDM", "FLA", "LAK", "MIN",
         "MTL", "NSH", "NJD", "NYI", "NYR", "OTT", "PHI", "PIT", "SJS", "SEA", "STL", "TBL", "TOR", "UTA",
         "VAN", "VGK", "WSH", "WPG"]
LABELS = {"gameday": "GameDay tweet", "dfo": "Daily Faceoff", "nhlcom": "NHL.com projection"}
SHORT = {"gameday": "tweet", "dfo": "DFO", "nhlcom": "NHL.com"}
MAX_DIFFS = 4


def safe(fn, *args, default=None, **kwargs):
    """Run a cross-check step; on any error, log it and carry on without it."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 - never let the cross-check block a post
        print(f"Cross-check skipped ({fn.__name__}: {e})")
        return default


def key(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return re.sub(r"[^a-z]", "", "".join(c for c in s if not unicodedata.combining(c)).lower())


# ============================================================
# ROSTERS
# ============================================================

def _fetch_roster(team: str) -> list:
    r = requests.get(ROSTER_API.format(team=team), timeout=20)
    r.raise_for_status()
    d = r.json()
    return [{"first": p["firstName"]["default"], "last": p["lastName"]["default"], "pos": p.get("positionCode", "")}
            for g in ("forwards", "defensemen", "goalies") for p in d.get(g) or []]


def load_rosters(today: str, path: str = ROSTER_PATH, fetch=_fetch_roster) -> dict:
    """{team: [player]} for all 32 teams, cached for the day."""
    cache = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cache = json.load(f)
    if cache.get("date") != today:
        cache = {"date": today, "teams": {}}
    missing = [t for t in TEAMS if t not in cache["teams"]]
    for t in missing:
        try:
            cache["teams"][t] = fetch(t)
        except Exception as e:  # noqa: BLE001
            print(f"Roster {t} unavailable: {e}")
        time.sleep(0.1)
    if missing:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    return cache["teams"]


# Initials reporters use in line tweets -> roster last name.
NICKNAMES = {"oel": "Ekman-Larsson", "mbn": "Brandsegg-Nygard", "rnh": "Nugent-Hopkins", "jvr": "van Riemsdyk",
             "pld": "Dubois", "jgp": "Pageau", "ekk": "Karlsson", "ep40": "Pettersson", "nuge": "Nugent-Hopkins"}


class Names:
    """Maps any spelling of a player on one team to his roster last name."""

    def __init__(self, roster: list):
        self.full = {key(p["first"] + p["last"]): p for p in roster}
        self.last: dict = {}
        for p in roster:
            self.last.setdefault(key(p["last"]), []).append(p)

    def find(self, name: str):
        k = key(re.sub(r"\s*\(.*?\)", "", name))
        k = key(NICKNAMES.get(k, k))
        if k in self.full:
            return self.full[k]
        if k in self.last:
            return self.last[k][0]   # same last name twice (two Lindholms): same key either way
        prefix = [p for fk, p in self.full.items() if len(k) >= 5 and fk.startswith(k)]
        if len(prefix) == 1:         # "Brady T" -> Brady Tkachuk
            return prefix[0]
        close =difflib.get_close_matches(k, list(self.full), n=1, cutoff=0.85) or \
            difflib.get_close_matches(k, list(self.last), n=1, cutoff=0.85)
        if close:
            return self.full.get(close[0]) or self.last[close[0]][0]
        return None

    def canon(self, name: str) -> str:
        p = self.find(name)
        if p:
            return p["last"]
        parts = re.sub(r"\s*\(.*?\)", "", name).split()
        return "?" + (parts[-1] if parts else name)   # "?": not on the roster (see compare)


def canonical(lineup: dict, roster: list) -> dict:
    """Lineup with every name replaced by the roster last name."""
    n = Names(roster)
    return {"forwards": [[n.canon(x) for x in r] for r in lineup.get("forwards") or []],
            "defense": [[n.canon(x) for x in r] for r in lineup.get("defense") or []],
            "goalie": n.canon(lineup["goalie"]) if lineup.get("goalie") else ""}


# ============================================================
# READ A GAMEDAY TWEET
# ============================================================

_SEP = re.compile(r"\s*[-–—]\s*")
# "Celebrini", "van Riemsdyk", "Brady T", "D’Astous"
_NAME = re.compile(r"^(?:(?:van|von|de|der|di|da|du|le|la)\s+)?[A-Z][\w'’.]*(?:\s+[A-Z][\w'’.]*)*$", re.UNICODE)
# Power-play units listed in dash form look like lines; in-game shuffles aren't the lineup.
_NOT_LINES = re.compile(r"power[- ]?play|\bpp\s*(?:units?|groups?|1|2)\b|penalty[- ]kill|\bpk\b|6-on-5|"
                        r"\bperiod\b|blender|to start the (?:second|third|2nd|3rd)|shuffled|intermission", re.I)
_STOP = re.compile(r"^(?:pp\s*\d|pp\d?\)|power[- ]?play|pk\b|penalty kill|\d\))", re.I)


def _row(line: str):
    """['Stenberg', 'Celebrini', 'Smith'] if the line is only 2-3 dash-separated names."""
    if re.search(r"\((?:extra|spare|scratch)", line, re.I):
        return None
    line = re.sub(r"\s*\([^)]*\)\s*$", "", line).strip()
    if not re.search(r"[-–—]", line) or len(line) > 70:
        return None
    parts = [p for p in _SEP.split(line) if p]
    if not 2 <= len(parts) <= 6:
        return None
    for p in parts:
        if len(p.split()) > 3 or not _NAME.match(p):
            return None
    return parts


def _join_hyphenated(parts: list, n: Names) -> list:
    """Re-join names the dash split broke ("Nugent", "Hopkins" -> "Nugent-Hopkins")."""
    out, i = [], 0
    while i < len(parts):
        if i + 1 < len(parts) and n.find(f"{parts[i]}-{parts[i + 1]}") and not (n.find(parts[i]) and n.find(parts[i + 1])):
            out.append(f"{parts[i]}-{parts[i + 1]}")
            i += 2
        else:
            out.append(parts[i])
            i += 1
    return out


def parse_tweet(text: str, rosters: dict):
    """(team, lineup) for a tweet that lists lines, else None."""
    text = (text or "").replace("\\n", "\n")
    text = re.split(r"\n\s*—\s", text)[0]               # drop the "— Reporter (@handle) date" footer
    lines = text.split("\n")
    first_row = next((i for i, l in enumerate(lines) if _row(l.strip())), None)
    if first_row is None or _NOT_LINES.search("\n".join(lines[:first_row])):
        return None                                     # PP units or an in-game change: post as-is
    rows, singles = [], []
    for line in (l.strip() for l in text.split("\n")):
        if _STOP.match(line):
            break                                       # power-play / penalty-kill units follow
        r = _row(line)
        if r:
            rows.append(r)
        elif re.match(r"^[A-Z][\w'’.-]+(?: [A-Z][\w'’.-]+)?$", line):
            singles.append(line)
    if len(rows) < 2:
        return None
    best, hits = None, 0
    names = [x for r in rows for x in r]
    for team, roster in rosters.items():
        n = Names(roster)
        h = sum(1 for x in names if n.find(x))
        if h > hits:
            best, hits = team, h
    if not best or hits < 4 or hits < 0.7 * len(names):
        return None
    n = Names(rosters[best])
    rows = [_join_hyphenated(r, n) for r in rows]
    forwards = [r for r in rows if len(r) == 3]
    defense = [r for r in rows if len(r) == 2]
    if len(forwards) < 2 and len(defense) < 2:
        return None
    goalie = next((s for s in singles if (p := n.find(s)) and p["pos"] == "G"), "")
    return best, {"forwards": forwards[:4], "defense": defense[:3], "goalie": goalie}


# ============================================================
# COMPARE
# ============================================================

def _fmt(row: list) -> str:
    return "-".join(n.lstrip("?") for n in row)


def compare(new: dict, old: dict) -> list:
    """Differences between two canonical lineups, on what both have. A line whose
    difference involves a name not on the roster is skipped rather than reported,
    so an unreadable nickname never shows up as a disagreement."""
    diffs = []
    for tag, kind in (("L", "forwards"), ("D", "defense")):
        a, b = new.get(kind) or [], old.get(kind) or []
        if not a or not b:
            continue
        # Lines are compared as groups, not by number: warmup tweets often list
        # them in a different order than NHL.com's line numbers.
        keys_b = [frozenset(map(key, r)) for r in b]
        unmatched_b = [i for i in range(len(b))]
        changed = []
        for i, r in enumerate(a):
            k = frozenset(map(key, r))
            if k in keys_b and keys_b.index(k) in unmatched_b:
                unmatched_b.remove(keys_b.index(k))
            else:
                changed.append(i)
        for i in changed:
            if not unmatched_b:
                break   # the other source lists fewer lines: nothing to pair this one with
            k = frozenset(map(key, a[i]))
            j = max(unmatched_b, key=lambda j: len(k & keys_b[j]))
            unmatched_b.remove(j)
            odd = {n for n in a[i] + b[j] if key(n) not in k & keys_b[j]}
            if any(n.startswith("?") for n in odd):
                continue  # involves a name not on the roster: don't call it a difference
            diffs.append((f"{tag}{i + 1}", _fmt(a[i]), _fmt(b[j])))
    g1, g2 = new.get("goalie") or "", old.get("goalie") or ""
    if g1 and g2 and key(g1) != key(g2) and not (g1.startswith("?") or g2.startswith("?")):
        diffs.append(("G", g1, g2))
    return diffs


def _comparable(a: dict, b: dict) -> bool:
    return bool(min(len(a.get("forwards") or []), len(b.get("forwards") or []))
                or min(len(a.get("defense") or []), len(b.get("defense") or []))
                or (a.get("goalie") and b.get("goalie")))


def _clock(iso: str) -> str:
    t = datetime.fromisoformat(iso).astimezone(ET)
    return f"{t.hour % 12 or 12}:{t:%M} {'AM' if t.hour < 12 else 'PM'}"


def load_posted(path: str = POSTED_PATH) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def cross_check_line(team: str, source: str, lineup: dict, now: datetime, rosters: dict,
                     path: str = POSTED_PATH) -> str:
    """The line to add to a post, or "" when no other source posted this team today."""
    roster = rosters.get(team) or []
    new = canonical(lineup, roster)
    day = now.astimezone(ET).date().isoformat()
    others = (load_posted(path).get(day) or {}).get(team) or {}
    matches, lines = [], []
    for src in ("gameday", "dfo", "nhlcom"):
        if src == source or src not in others:
            continue
        old = others[src]
        if not _comparable(new, old):
            continue
        label = f"{LABELS[src]} ({_clock(old['at'])})"
        diffs = compare(new, old)
        if not diffs:
            matches.append(label)
            continue
        shown = [f"{slot} {a} ({SHORT[src]}: {b})" for slot, a, b in diffs[:MAX_DIFFS]]
        more = f" · +{len(diffs) - MAX_DIFFS} more" if len(diffs) > MAX_DIFFS else ""
        lines.append(f"⚠️ *Cross-check vs {label}:* " + " · ".join(shown) + more)
    if matches:
        lines.insert(0, "✅ *Cross-check:* matches " + " and ".join(matches))
    return "\n".join(lines)


def add_to_message(message: str, line: str) -> str:
    """Put the cross-check line just above the message's *Link:* line."""
    if not line:
        return message
    i = message.rfind("\n*Link:*")
    return message + "\n\n" + line if i < 0 else message[:i] + "\n" + line + message[i:]


class Checker:
    """One per run: loads rosters on first use, then annotates and records posts.
    Every method swallows its own errors (the post goes out without a cross-check)."""

    def __init__(self, source: str, posted_path: str = POSTED_PATH, roster_path: str = ROSTER_PATH,
                 fetch=_fetch_roster):
        self.source, self.posted_path, self.roster_path, self.fetch = source, posted_path, roster_path, fetch
        self._rosters = None

    def rosters(self, now: datetime) -> dict:
        if self._rosters is None:
            self._rosters = safe(load_rosters, now.astimezone(ET).date().isoformat(), self.roster_path,
                                 self.fetch, default={}) or {}
        return self._rosters

    def annotate(self, message: str, team: str, lineup: dict, now: datetime) -> str:
        line = safe(cross_check_line, team, self.source, lineup, now, self.rosters(now), self.posted_path,
                    default="")
        return add_to_message(message, line or "")

    def record(self, team: str, lineup: dict, now: datetime):
        safe(record, team, self.source, lineup, now, self.rosters(now), self.posted_path)

    def from_tweet(self, text: str, now: datetime):
        """(team, lineup) if the tweet lists lines, else None."""
        return safe(parse_tweet, text, self.rosters(now))


def record(team: str, source: str, lineup: dict, now: datetime, rosters: dict, path: str = POSTED_PATH):
    """Remember the lineup just posted, so later posts from other sources compare against it."""
    data = load_posted(path)
    day = now.astimezone(ET).date().isoformat()
    keep = (now.astimezone(ET).date() - timedelta(days=2)).isoformat()
    data = {d: v for d, v in data.items() if d >= keep}
    entry = canonical(lineup, rosters.get(team) or [])
    entry["at"] = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    data.setdefault(day, {}).setdefault(team, {})[source] = entry
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
