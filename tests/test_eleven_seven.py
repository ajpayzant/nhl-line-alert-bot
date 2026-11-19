"""11 forwards / 7 defencemen lineups from each source (all seen Nov 18-19, 2026)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dfo_lines_alert as dfo  # noqa: E402
import lineup_xcheck as x  # noqa: E402
import nhlcom_lines_alert as bot  # noqa: E402

F = [["Artturi Lehkonen", "Brock Nelson", "Martin Necas"], ["Jaden Schwartz", "Nazem Kadri", "T.J. Hughes"],
     ["Gabriel Landeskog", "Nicolas Roy", "Logan O'Connor"], ["Parker Kelly", "Fedor Svechkov"]]
D = [["Devon Toews", "Sam Malinski"], ["Brett Kulak", "Noah Juulsen"], ["Alex Gagne", "Brent Burns"]]
ROSTER = ([(n, "C") for r in F for n in r] + [(n, "D") for r in D for n in r]
          + [("Mike Reilly", "D"), ("Scott Wedgewood", "G"), ("Mackenzie Blackwood", "G")])


def article(f, d, singles):
    rows = "\n\n".join(" -- ".join(r) for r in f + d)
    part = ("## **DUCKS (11-10-1) at AVALANCHE (12-7-3)**\n\n### **9 p.m. ET; TVAS**\n\n"
            f"**Avalanche projected lineup**\n\n{rows}\n\n" + "\n\n".join(singles)
            + "\n\n***Scratched:** None*\n\n***Injured:** None*")
    return {"contentDate": "2026-11-19T20:00:00Z", "lastUpdatedDate": "2026-11-19T20:26:00Z",
            "parts": [{"type": "markdown", "content": part}]}


def test_nhlcom_seventh_d_is_not_the_starting_goalie():
    t = bot.normalize(bot.parse_article(article(F, D, ["Mike Reilly", "Scott Wedgewood", "Mackenzie Blackwood"]))[0], ROSTER)
    assert t["goalies"] == ["Scott Wedgewood", "Mackenzie Blackwood"]
    assert t["extra"] == ["Mike Reilly"]
    assert t["forwards"][-1] == ["Parker Kelly", "Fedor Svechkov"] and len(t["defense"]) == 3
    msg = bot.build_slack_notification(t, "new", None, "2026-11-19T20:26:00Z")
    assert "7th D: Mike Reilly" in msg and "G: Scott Wedgewood  (backup Mackenzie Blackwood)" in msg


def test_nhlcom_without_roster_still_reads_11_7():
    t = bot.normalize(bot.parse_article(article(F, D, ["Mike Reilly", "Scott Wedgewood", "Mackenzie Blackwood"]))[0], [])
    assert t["goalies"] == ["Scott Wedgewood", "Mackenzie Blackwood"] and t["extra"] == ["Mike Reilly"]
    assert len(t["forwards"]) == 4 and len(t["defense"]) == 3


def test_nhlcom_pair_with_callup_missing_from_roster_stays_a_pair():
    f = F[:3] + [["Parker Kelly", "Fedor Svechkov", "Mike Reilly"]]
    d = D[:2] + [["Stian Solberg", "Alex Gagne"]]           # Solberg: not on the roster API yet
    t = bot.normalize(bot.parse_article(article(f, d, ["Scott Wedgewood", "Mackenzie Blackwood"]))[0], ROSTER)
    assert len(t["forwards"]) == 4 and t["defense"][-1] == ["Stian Solberg", "Alex Gagne"] and t["extra"] == []


def test_nhlcom_seventh_d_change_reposts():
    a = bot.normalize(bot.parse_article(article(F, D, ["Mike Reilly", "Scott Wedgewood"]))[0], ROSTER)
    b = bot.normalize(bot.parse_article(article(F, D, ["Scott Wedgewood"]))[0], ROSTER)
    assert bot.signature(a) != bot.signature(b)
    assert "*Out:* Mike Reilly" in bot.describe_changes(b, bot.entry(a, "2026-11-19T20:26:00Z"))


def test_dfo_reads_the_fourth_defence_group():
    team = {"team": "TBL", "forwards": [["A", "B", "C"]] * 3 + [["Viel", "Sabourin"]],
            "defense": [["D1", "D2"]] * 3, "goalies": ["Vasilevskiy"], "pp1": [], "extra": ["Crozier"]}
    assert "Crozier" in dfo.lineup_players(team)
    no_extra = {**team, "extra": []}
    # a team with no 7th D keeps the exact signature format saved before this change
    assert dfo.lineup_signature(no_extra) == '[[["A", "B", "C"], ["A", "B", "C"], ["A", "B", "C"], ["Viel", "Sabourin"]], [["D1", "D2"], ["D1", "D2"], ["D1", "D2"]], ["Vasilevskiy"], []]'
    assert dfo.lineup_signature(team) != dfo.lineup_signature(no_extra)



def p(first, last, pos):
    return {"first": first, "last": last, "pos": pos}


BOLTS = [p("Jake", "Guentzel", "L"), p("Brayden", "Point", "C"), p("Nikita", "Kucherov", "R"),
         p("Ilya", "Mikheyev", "L"), p("Anthony", "Cirelli", "C"), p("Pontus", "Holmberg", "R"),
         p("Yanni", "Gourde", "C"), p("Sam", "O'Reilly", "C"), p("Gage", "Goncalves", "L"),
         p("Jeffrey", "Viel", "L"), p("Scott", "Sabourin", "R"), p("Charle-Edouard", "D'Astous", "D"),
         p("John", "Carlson", "D"), p("Ryan", "McDonagh", "D"), p("Erik", "Cernak", "D"), p("Victor", "Hedman", "D"),
         p("J.J.", "Moser", "D"), p("Emil", "Lilleberg", "D"), p("Maxwell", "Crozier", "D"),
         p("Andrei", "Vasilevskiy", "G")]
TWEET = ("#Bolts lines from warmups:\nGuentzel - Point - Kucherov\nMikheyev - Cirelli - Holmberg\n"
         "Gourde - O'Reilly - Goncalves\nViel - Sabourin\nD'Astous - Carlson\nMcDonagh - Cernak\nHedman - Moser\n"
         "Lilleberg\nVasilevskiy\n— Gabby Shirley (@Gabby_Shirley_)")


def test_tweet_11_7_reads_fourth_line_and_seventh_d():
    team, lineup = x.parse_tweet(TWEET, {"TBL": BOLTS})
    assert team == "TBL" and lineup["forwards"][3] == ["Viel", "Sabourin"]
    assert lineup["extra"] == ["Lilleberg"] and lineup["goalie"] == "Vasilevskiy"


def test_tweet_12_6_with_practice_extra_d_has_no_seventh_d():
    # Nov 18 practice: Crozier filling in at forward, Lilleberg the extra
    tweet = TWEET.replace("Viel - Sabourin", "Viel - Crozier - Sabourin").replace("\nVasilevskiy", "")
    team, lineup = x.parse_tweet(tweet, {"TBL": BOLTS})
    assert lineup["forwards"][3] == ["Viel", "Crozier", "Sabourin"] and lineup["extra"] == []
