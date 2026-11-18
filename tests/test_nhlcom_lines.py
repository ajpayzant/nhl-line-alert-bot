import copy
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nhlcom_lines_alert as bot  # noqa: E402

ET = ZoneInfo("America/New_York")
F = [["Alex Turcotte", "Alex Laferriere", "Adrian Kempe"], ["Kevin Fiala", "Quinton Byfield", "Mats Zuccarello"],
     ["Trevor Moore", "Erik Haula", "Joel Armia"], ["Andre Lee", "Scott Laughton", "Corey Perry"]]
D = [["Erik Gustafsson", "Brandt Clarke"], ["Joel Edmundson", "Drew Doughty"], ["Mikey Anderson", "Brian Dumoulin"]]
G = ["Darcy Kuemper", "Anton Forsberg"]
ROSTER = [(n, "C") for r in F for n in r] + [(n, "D") for r in D for n in r] + [(n, "G") for n in G]


def block(nick, f, d, g, scratched="None"):
    rows = "\n\n".join(" -- ".join(r) for r in f + d)
    return f"**{nick} projected lineup**\n\n{rows}\n\n" + "\n\n".join(g) + f"\n\n***Scratched:** {scratched}*\n\n***Injured:** None*"


def story(f=F, d=D, g=G, updated="2026-11-17T21:09:00Z", content="2026-11-17T20:36:00Z", time="7 p.m. ET"):
    part = (f"## **KINGS (5-11-2) at BRUINS (13-6-1)**\n\n**{time}; NESN**\n\n" + block("Kings", f, d, g)
            + "\n\n" + block("Bruins", F, D, ["Jeremy Swayman", "Joonas Korpisalo"])
            + "\n\n**Status report**\n\nKempe returns. … Someone else.")
    return {"contentDate": content, "lastUpdatedDate": updated, "parts": [{"type": "markdown", "content": part}]}


class NoCheck:
    def annotate(self, message, *a):
        return message

    def record(self, *a):
        pass


def run(s, state, hhmm="16:10"):
    h, m = map(int, hhmm.split(":"))
    sent = []
    bot.check_and_send_alerts(s, state, datetime(2026, 11, 17, h, m, tzinfo=ET), send=sent.append,
                              fetch=lambda team: ROSTER, save=lambda s: None, log=lambda *a: None,
                              sleep=lambda s: None, xcheck=NoCheck())
    return sent


def seeded():
    return {"article_updated": "2026-11-16T21:00:00Z", "lineups": {}}


def test_first_run_seeds_silently_then_posts_each_team_once():
    state = {}
    assert run(story(), state) == []
    state = seeded()
    sent = run(story(), state)
    assert [m.split("\n")[0] for m in sent] == ["🏒 *NHL.com Projected Lineup: Los Angeles Kings (LAK)*",
                                                "🏒 *NHL.com Projected Lineup: Boston Bruins (BOS)*"]
    assert "Kempe returns." in sent[0] and "Someone else" not in sent[0]
    assert run(story(), state, "16:15") == []                       # unchanged article


def test_only_changed_team_is_reposted_with_changes():
    state = seeded()
    run(story(), state)
    f2 = copy.deepcopy(F)
    f2[1][1], f2[2][1] = f2[2][1], f2[1][1]                           # Byfield and Haula swap lines
    sent = run(story(f=f2, g=G[::-1], updated="2026-11-17T22:12:00Z"), state, "17:15")
    assert len(sent) == 1 and "Lineup Changed: Los Angeles Kings" in sent[0]
    assert "*Starting goalie:* Darcy Kuemper → Anton Forsberg" in sent[0]
    assert "Quinton Byfield L2 → L3" in sent[0] and "Erik Haula L3 → L2" in sent[0]


def test_typo_order_and_scratch_edits_do_not_repost():
    state = seeded()
    run(story(), state)
    f2 = copy.deepcopy(F)
    f2[0] = ["Adrian Kempe", "Alex Laferriere", "Alex Turcote"]         # reordered + typo
    assert run(story(f=f2, updated="2026-11-17T22:12:00Z"), state, "17:15") == []


def test_no_posts_after_puck_drop_and_new_day_is_new():
    state = seeded()
    run(story(), state)
    f2 = copy.deepcopy(F)
    f2[3][2] = "Joel Armia"
    f2[2][2] = "Corey Perry"
    assert run(story(f=f2, updated="2026-11-18T00:20:00Z"), state, "19:25") == []
    nxt = story(content="2026-11-18T20:30:00Z", updated="2026-11-18T20:30:00Z")
    h = run(nxt, state, "15:35")  # clock says Nov 17 here but the game key is Nov 18
    assert len(h) == 2


def test_time_with_comma_and_missing_paren():
    s = story(time="7:30 p.m., ET")
    t = bot.parse_article(s)[0]
    assert t["start"].startswith("2026-11-17T19:30")
    t["injured"] = ["Dylan DeMelo lower body)"]
    assert bot.normalize(t, ROSTER)["injured"] == ["Dylan DeMelo (lower body)"]


def test_older_cached_article_is_ignored():
    state = seeded()
    run(story(updated="2026-11-17T22:12:00Z"), state)
    f2 = [r[::-1] for r in F[::-1]]
    assert run(story(f=f2, updated="2026-11-17T21:09:00Z"), state, "17:20") == []
