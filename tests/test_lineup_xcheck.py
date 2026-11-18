import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lineup_xcheck as x  # noqa: E402

ET = ZoneInfo("America/New_York")


def p(first, last, pos):
    return {"first": first, "last": last, "pos": pos}


SJS = [p("Collin", "Stenberg", "C"), p("Macklin", "Celebrini", "C"), p("Will", "Smith", "C"),
       p("Michael", "Misa", "C"), p("Alexander", "Wennberg", "C"), p("Mario", "Ferraro", "D"),
       p("Mark", "Marchment", "L"), p("Tyler", "Toffoli", "R"), p("Ty", "Dellandrea", "C"),
       p("Collin", "Graf", "R"), p("Darnell", "Nurse", "D"), p("Nick", "Kesselring", "D"),
       p("Timothy", "Liljegren", "D"), p("Jacob", "Trouba", "D"), p("Dmitry", "Orlov", "D"),
       p("Sam", "Dickinson", "D"), p("Yaroslav", "Askarov", "G"), p("Alex", "Nedeljkovic", "G")]
OTHER = [p("Connor", "McDavid", "C"), p("Leon", "Draisaitl", "C"), p("Zach", "Hyman", "L"), p("Evan", "Bouchard", "D")]
ROSTERS = {"SJS": SJS, "EDM": OTHER}
TWEET = ("#SJSharks\\nlines, d-pairs in warmups\\nStenberg-Celebrini-Smith\\nMisa-Wennberg-Marchment\\n"
         "Toffoli-Dellandrea-Graf\\nNurse-Kesselring\\nDickinson-Trouba\\nOrlov-Liljegren\\nAskarov\\n"
         "— Curtis Pashelka (@CurtisPashelka)\\nNovember 18, 2026")
PROJ = {"forwards": [["Misa", "Wennberg", "Marchment"], ["Collin Stenberg", "Macklin Celebrini", "Will Smith"],
                     ["Tyler Toffoli", "Ty Dellandrea", "Collin Graf"]],
        "defense": [["Darnell Nurse", "Nick Kesselring"], ["Sam Dickinson", "Jacob Trouba"], ["Dmitry Orlov", "Timothy Liljegren"]],
        "goalie": "Yaroslav Askarov"}
NOW = datetime(2026, 11, 18, 19, 40, tzinfo=ET)


def test_reads_a_line_tweet():
    team, lineup = x.parse_tweet(TWEET, ROSTERS)
    assert team == "SJS"
    assert lineup["forwards"][0] == ["Stenberg", "Celebrini", "Smith"]
    assert len(lineup["defense"]) == 3 and lineup["goalie"] == "Askarov"


def test_skips_pp_units_in_game_changes_and_notes():
    assert x.parse_tweet("Sharks power play units:\\nStenberg-Celebrini-Smith\\nOrlov-Wennberg", ROSTERS) is None
    assert x.parse_tweet("Lines to start the third:\\nStenberg-Celebrini-Smith\\nMisa-Wennberg-Marchment", ROSTERS) is None
    assert x.parse_tweet("Askarov starts tonight. pic.twitter.com/abc", ROSTERS) is None


def test_same_groups_in_another_order_match(tmp_path):
    path = str(tmp_path / "posted.json")
    x.record("SJS", "nhlcom", PROJ, NOW.replace(hour=16), ROSTERS, path)
    team, lineup = x.parse_tweet(TWEET, ROSTERS)
    assert x.cross_check_line(team, "gameday", lineup, NOW, ROSTERS, path) == \
        "✅ *Cross-check:* matches NHL.com projection (4:40 PM)"


def test_real_difference_is_shown(tmp_path):
    path = str(tmp_path / "posted.json")
    x.record("SJS", "nhlcom", PROJ, NOW.replace(hour=16), ROSTERS, path)
    team, lineup = x.parse_tweet(TWEET.replace("Askarov", "Nedeljkovic").replace("Toffoli-", "Ferraro-"), ROSTERS)
    line = x.cross_check_line(team, "dfo", {**lineup}, NOW, ROSTERS, path)
    assert line.startswith("⚠️ *Cross-check vs NHL.com projection (4:40 PM):*")
    assert "L3 Ferraro-Dellandrea-Graf (NHL.com: Toffoli-Dellandrea-Graf)" in line
    assert "G Nedeljkovic (NHL.com: Askarov)" in line


def test_unknown_name_is_not_called_a_difference(tmp_path):
    path = str(tmp_path / "posted.json")
    x.record("SJS", "nhlcom", PROJ, NOW.replace(hour=16), ROSTERS, path)
    team, lineup = x.parse_tweet(TWEET.replace("Graf", "Callup"), ROSTERS)
    assert x.cross_check_line(team, "gameday", lineup, NOW, ROSTERS, path).startswith("✅")


def test_no_line_without_another_source_and_message_placement(tmp_path):
    path = str(tmp_path / "posted.json")
    assert x.cross_check_line("SJS", "gameday", PROJ, NOW, ROSTERS, path) == ""
    msg = "🚨 *New*\n\n*Line Tweet:*\n> text\n\n*Link:* <u|Open tweet>"
    assert x.add_to_message(msg, "✅ ok") == "🚨 *New*\n\n*Line Tweet:*\n> text\n\n✅ ok\n*Link:* <u|Open tweet>"
    assert x.add_to_message(msg, "") == msg


def test_checker_never_raises(tmp_path):
    def broken(team):
        raise RuntimeError("roster API down")
    c = x.Checker("gameday", posted_path=str(tmp_path / "p.json"), roster_path=str(tmp_path / "r.json"), fetch=broken)
    assert c.from_tweet(TWEET, NOW) is None                 # no rosters: tweet can't be read, still posted
    assert c.annotate("msg\n*Link:* x", "SJS", PROJ, NOW) == "msg\n*Link:* x"
    c.record("SJS", PROJ, NOW)                               # no exception


def test_space_separated_lines_and_extras_row():
    tweet = ("Stenberg Celebrini Smith\nMisa Wennberg Marchment\nToffoli Dellandrea Graf\n"
             "Stenberg Misa\nNurse Kesselring\nDickinson Trouba\nOrlov Liljegren\n— Reporter (@r)")
    team, lineup = x.parse_tweet(tweet, ROSTERS)
    assert team == "SJS" and len(lineup["forwards"]) == 3
    assert lineup["defense"] == [["Nurse", "Kesselring"], ["Dickinson", "Trouba"], ["Orlov", "Liljegren"]]


def test_line_missing_a_player_is_not_a_difference(tmp_path):
    path = str(tmp_path / "posted.json")
    x.record("SJS", "gameday", PROJ, NOW.replace(hour=16), ROSTERS, path)
    short = {**PROJ, "forwards": [["Misa", "Marchment"]] + PROJ["forwards"][1:]}
    assert x.cross_check_line("SJS", "dfo", short, NOW, ROSTERS, path).startswith("✅")
