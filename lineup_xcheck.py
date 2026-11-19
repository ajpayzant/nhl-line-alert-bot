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
            "extra": [n.canon(x) for x in lineup.get("extra") or []],
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


def _space_row(line: str):
    """2-3 capitalised words and nothing else: a line written without dashes."""
    words = line.split()
    return 2 <= len(words) <= 3 and all(re.match(r"^[A-Z][a-zà-ÿ'’.A-Z]+$", w) for w in words) and ":" not in line


def _team_of(rows: list, rosters: dict, exact: bool):
    """The team whose roster has (nearly) all these names, or None."""
    names = [x for r in rows for x in r]
    best, hits = None, 0
    for team, roster in rosters.items():
        n = Names(roster)
        h = sum(1 for x in names if (key(x) in n.last if exact else n.find(x)))
        if h > hits:
            best, hits = team, h
    need = 0.85 if exact else 0.7
    return best if best and hits >= 4 and hits >= need * len(names) else None


def parse_tweet(text: str, rosters: dict):
    """(team, lineup) for a tweet that lists lines, else None."""
    text = (text or "").replace("\\n", "\n")
    text = re.split(r"\n\s*—\s", text)[0]               # drop the "— Reporter (@handle) date" footer
    lines = [l.strip() for l in text.split("\n")]
    is_row = [bool(_row(l)) or bool(_space_row(l)) for l in lines]
    first_row = next((i for i, r in enumerate(is_row) if r), None)
    if first_row is None or _NOT_LINES.search("\n".join(lines[:first_row])):
        return None                                     # PP units or an in-game change: post as-is
    rows, spaced, singles = [], [], []
    for line in lines:
        if _STOP.match(line):
            break                                       # power-play / penalty-kill units follow
        r = _row(line)
        if r:
            rows.append(r)
        elif _space_row(line):
            spaced.append([w.rstrip(".") for w in line.split()])
        if not r and re.match(r"^[A-Z][\w'’.-]+(?: [A-Z][\w'’.-]+)?$", line):
            singles.append(line)
    if len(rows) >= 2:
        best = _team_of(rows, rosters, exact=False)
    elif len(spaced) >= 4:
        # "Benson Kulich Thompson": no dashes, so every word must be a roster last name.
        rows, best = spaced, _team_of(spaced, rosters, exact=True)
    else:
        return None
    if not best:
        return None
    n = Names(rosters[best])
    rows = [_join_hyphenated(r, n) for r in rows]
    forwards, defense, two_f = [], [], []
    for r in rows:
        pos = [(n.find(x) or {}).get("pos", "") for x in r]
        if len(r) == 3:
            (defense if sum(p == "D" for p in pos) >= 2 else forwards).append(r)
        elif len(r) == 2 and not all(p in ("C", "L", "R") for p in pos):
            defense.append(r)
        elif len(r) == 2:
            two_f.append((len(forwards), len(defense), r))
    # Two confirmed forwards are usually the extras ("McLeod-Danforth"), but in an
    # 11F/7D lineup they are the fourth line: right after three full lines, before
    # the pairs, and not already on another line.
    in_lines = {key(x) for r in forwards for x in r}
    for n_f, n_d, r in two_f:
        if n_f == 3 and n_d == 0 and len(forwards) == 3 and not in_lines & {key(x) for x in r}:
            forwards.append(r)
            break
    if len(forwards) < 2 and len(defense) < 2:
        return None
    goalie = next((s for s in singles if (p := n.find(s)) and p["pos"] == "G"), "")
    extra = []
    if len(forwards) == 4 and len(forwards[3]) == 2 and len(defense) == 3:
        # 11F/7D: the lone defenceman listed on his own line is the 7th D.
        paired = {key(x) for r in defense for x in r}
        extra = [s for s in singles if (p := n.find(s)) and p["pos"] == "D" and key(s) not in paired][:1]
    return best, {"forwards": forwards[:4], "defense": defense[:3], "extra": extra, "goalie": goalie}


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
            if k < keys_b[j] or keys_b[j] < k:
                continue  # one source left a player off the line ("Viel-Sabourin"): not a difference
            odd = {n for n in a[i] + b[j] if key(n) not in k & keys_b[j]}
            if any(n.startswith("?") for n in odd):
                continue  # involves a name not on the roster: don't call it a difference
            diffs.append((f"{tag}{i + 1}", _fmt(a[i]), _fmt(b[j])))
    g1, g2 = new.get("goalie") or "", old.get("goalie") or ""
    if g1 and g2 and key(g1) != key(g2) and not (g1.startswith("?") or g2.startswith("?")):
        diffs.append(("G", g1, g2))
    return diffs


def _complete(l: dict) -> bool:
    return len(l.get("forwards") or []) >= 4 and len(l.get("defense") or []) >= 3


def shape(l: dict) -> str:
    """"12F/6D", "11F/7D"."""
    f = sum(len(r) for r in l.get("forwards") or [])
    d = sum(len(r) for r in l.get("defense") or []) + len(l.get("extra") or [])
    return f"{f}F/{d}D"


def _unknown(l: dict) -> int:
    return sum(1 for r in (l.get("forwards") or []) + (l.get("defense") or []) for n in r if n.startswith("?"))


def personnel(new: dict, old: dict, new_src: str = "", old_src: str = "") -> dict:
    """Who dresses, when both lineups are complete: {"format": (a, b) or None,
    "only_new": [...], "only_old": [...]}. This is what line-by-line compare can't
    show: an 11F/7D lineup vs a 12F/6D one, or a scratch on one side only.
    A name not on the roster counts when it comes from NHL.com or DFO (full names
    from a structured page, e.g. a same-day call-up). From a tweet it may be a
    nickname the reader couldn't place, so if a tweet has one, players aren't
    compared at all (the line-by-line compare still runs)."""
    out = {"format": None, "only_new": [], "only_old": []}
    if not (_complete(new) and _complete(old)):
        return out
    if shape(new) != shape(old):
        out["format"] = (shape(new), shape(old))
    if (new_src == "gameday" and _unknown(new)) or (old_src == "gameday" and _unknown(old)):
        return out

    def skaters(l):
        names = [n for r in (l.get("forwards") or []) + (l.get("defense") or []) for n in r] + list(l.get("extra") or [])
        return {key(n): n.lstrip("?") for n in names}
    a, b = skaters(new), skaters(old)
    out["only_new"] = sorted(a[k] for k in a.keys() - b.keys())
    out["only_old"] = sorted(b[k] for k in b.keys() - a.keys())
    return out


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


# ============================================================
# WHICH SOURCE TO TRUST
# ============================================================
#
# Tier 1  a beat reporter's tweet: a GameDay tweet, or a DFO update whose source
#         is a reporter's tweet (dated by the tweet, not by DFO's page, which
#         lags it ~25 min)
# Tier 2  NHL.com projection (dated by the article's last edit), or a DFO
#         update with no tweet behind it
# Within a tier the newer information wins. A tweet from an earlier day (DFO
# sometimes still shows yesterday's practice tweet) drops to tier 2.

def _when(meta: dict) -> str:
    return meta.get("info_at") or meta.get("at") or ""


def _earlier_day(meta: dict, today: str) -> bool:
    return bool(today and _when(meta) and datetime.fromisoformat(_when(meta)).astimezone(ET).date().isoformat() < today)


def tier(source: str, meta: dict, today: str = "") -> int:
    tweet = source == "gameday" or (source == "dfo" and meta.get("ref"))
    return 1 if tweet and not _earlier_day(meta, today) else 2


def describe(source: str, meta: dict, today: str = "") -> str:
    """'Daily Faceoff (via Zach Cavanagh, tweet 3:20 PM)', 'NHL.com projection (edited 3:26 PM)';
    the date is added when the information is from an earlier day."""
    when = _clock(_when(meta))
    if _earlier_day(meta, today):
        t = datetime.fromisoformat(_when(meta)).astimezone(ET)
        when = f"{t:%b} {t.day} {when}"
    via = meta.get("via") or ""
    if source == "gameday":
        return f"GameDay tweet ({via + ', ' if via else ''}{when})"
    if source == "dfo":
        return f"Daily Faceoff ({'via ' + via + ', ' if via else ''}{'tweet ' if meta.get('ref') else 'updated '}{when})"
    return f"NHL.com projection (edited {when})"


def _kind(source: str, meta: dict, today: str = "") -> str:
    if tier(source, meta, today) == 1:
        return "a beat-reporter tweet"
    if _earlier_day(meta, today) and source != "nhlcom":
        return "an earlier day's tweet"
    return "the NHL.com projection" if source == "nhlcom" else "an unsourced DFO update"


def verdict(this: tuple, agree: list, disagree: list, today: str = "") -> str:
    """One line naming the lineup most likely to be right: the higher tier, then
    the newer information. Each item is (source, meta); `agree` are the sources
    that match this post, `disagree` the ones that don't."""
    def rank(item):
        return (-tier(*item, today), _when(item[1]))
    ours = [this, *agree]
    best = max(ours + disagree, key=rank)
    our_side = best in ours
    rival = max(disagree if our_side else ours, key=rank)
    if tier(*best, today) < tier(*rival, today):
        why = f"{_kind(*best, today)} outranks {_kind(*rival, today)}"
    elif _when(best[1]) == _when(rival[1]):
        why = "same tier and time; check the link"
    else:
        why = f"newer info ({_clock(_when(best[1]))} vs {_clock(_when(rival[1]))})"
    if our_side:
        backed = "" if best == this else f", backed by {describe(*best, today)}"
        return f"➜ *Most likely right: this lineup*{backed} — {why}"
    return f"➜ *Most likely right: {describe(*best, today)}* — {why}. _This post may be out of date._"


# ============================================================
# BUILD THE CROSS-CHECK
# ============================================================

def _diff_bullets(new: dict, old: dict, source: str, src: str) -> list:
    """Most important first: goalie, who dresses, 11F/7D vs 12F/6D, then line placement."""
    other = SHORT[src]
    out = []
    diffs = compare(new, old)
    for slot, a, b in diffs:
        if slot == "G":
            out.append(f"• *Goalie:* {a} here · {b} on {other}")
    pers = personnel(new, old, source, src)
    if pers["only_new"] or pers["only_old"]:
        parts = []
        if pers["only_new"]:
            parts.append(f"{', '.join(pers['only_new'])} in here, not on {other}")
        if pers["only_old"]:
            parts.append(f"{', '.join(pers['only_old'])} on {other}, not here")
        out.append("• *Players:* " + " · ".join(parts))
    if pers["format"]:
        out.append(f"• *Format:* {pers['format'][0]} here · {pers['format'][1]} on {other}")
    lines = [d for d in diffs if d[0] != "G"]
    if lines:
        shown = [f"{slot} {a} ({other}: {b})" for slot, a, b in lines[:MAX_DIFFS]]
        more = f" · +{len(lines) - MAX_DIFFS} more" if len(lines) > MAX_DIFFS else ""
        out.append("• *Lines:* " + " · ".join(shown) + more)
    return out


def cross_check(team: str, source: str, lineup: dict, now: datetime, rosters: dict,
                meta: dict | None = None, path: str = POSTED_PATH) -> tuple:
    """(banner, footer) for a post. The banner goes right under the title when
    another source disagrees; the footer is a quiet ✅ line when all agree.
    Both are "" when no other source posted this team today."""
    new = canonical(lineup, rosters.get(team) or [])
    this_meta = {"at": now.astimezone(timezone.utc).isoformat(timespec="seconds"), **(meta or {})}
    day = now.astimezone(ET).date().isoformat()
    others = (load_posted(path).get(day) or {}).get(team) or {}
    agree, disagree, sections = [], [], []
    for src in ("gameday", "dfo", "nhlcom"):
        if src == source or src not in others:
            continue
        old = others[src]
        if not _comparable(new, old):
            continue
        same_tweet = bool(this_meta.get("ref") and this_meta.get("ref") == old.get("ref"))
        label = describe(src, old, day) + (", same tweet" if same_tweet else "")
        bullets = _diff_bullets(new, old, source, src)
        if bullets:
            disagree.append((src, old))
            sections.append(f"*vs {label}:*\n" + "\n".join(bullets))
        else:
            agree.append((src, old, label))
    agree_text = " and ".join(a[2] for a in agree)
    if not disagree:
        return "", f"✅ *Cross-check:* matches {agree_text}" if agree else ""
    banner = ["🚨 *SOURCES DISAGREE*", *sections]
    if agree:
        banner.append(f"✅ *Agrees with:* {agree_text}")
    banner.append(verdict((source, this_meta), [(a[0], a[1]) for a in agree], disagree, day))
    return "\n".join(banner), ""


def cross_check_line(team: str, source: str, lineup: dict, now: datetime, rosters: dict,
                     path: str = POSTED_PATH, meta: dict | None = None) -> str:
    """Banner and footer as one string (tests / logging)."""
    return "\n".join(x for x in cross_check(team, source, lineup, now, rosters, meta, path) if x)


def add_to_message(message: str, line: str = "", banner: str = "") -> str:
    """Banner just under the title (first line); footer just above the *Link:* line."""
    if banner:
        head, sep, rest = message.partition("\n")
        message = head + "\n\n" + banner + ("\n" + rest if sep else "")
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

    def annotate(self, message: str, team: str, lineup: dict, now: datetime, meta: dict | None = None) -> str:
        banner, footer = safe(cross_check, team, self.source, lineup, now, self.rosters(now), meta,
                              self.posted_path, default=("", "")) or ("", "")
        return add_to_message(message, footer, banner)

    def record(self, team: str, lineup: dict, now: datetime, meta: dict | None = None):
        safe(record, team, self.source, lineup, now, self.rosters(now), self.posted_path, meta)

    def from_tweet(self, text: str, now: datetime):
        """(team, lineup) if the tweet lists lines, else None."""
        return safe(parse_tweet, text, self.rosters(now))


def record(team: str, source: str, lineup: dict, now: datetime, rosters: dict, path: str = POSTED_PATH,
           meta: dict | None = None):
    """Remember the lineup just posted, so later posts from other sources compare
    against it. meta: info_at (when the information came out, ISO UTC), via
    (reporter / handle), ref (the source tweet's status id, to spot the same tweet)."""
    data = load_posted(path)
    day = now.astimezone(ET).date().isoformat()
    keep = (now.astimezone(ET).date() - timedelta(days=2)).isoformat()
    data = {d: v for d, v in data.items() if d >= keep}
    entry = canonical(lineup, rosters.get(team) or [])
    entry["at"] = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    entry.update({k: v for k, v in (meta or {}).items() if v})
    data.setdefault(day, {}).setdefault(team, {})[source] = entry
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
