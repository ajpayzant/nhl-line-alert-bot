"""Re-send the latest lineup post per team for today's games that haven't started,
rebuilt with the current parsing and cross-check (used after a parsing fix, e.g.
the Nov 19 11F/7D fix). Dry run by default; --send posts to Slack. Run from the
Actions page with mode=repost-today. Does not touch the bot's state files."""
import csv
import json
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import dfo_lines_alert as dfo  # noqa: E402
import gameday_lines_alert as gd  # noqa: E402
import lineup_xcheck as x  # noqa: E402
import nhlcom_lines_alert as nhl  # noqa: E402

ET = ZoneInfo("America/New_York")
SEND = "--send" in sys.argv
now = datetime.now(timezone.utc)
today = now.astimezone(ET).date().isoformat()
NOTE = "♻️ _Re-post with corrected lineup parsing (11F/7D fix) and the new source cross-check._"


def tag(message: str) -> str:
    head, _, rest = message.partition("\n")
    return head + "\n" + NOTE + "\n" + rest


story = nhl.fetch_article()
au = story.get("lastUpdatedDate") or story["contentDate"]
state = json.load(open(nhl.STATE_PATH, encoding="utf-8"))
nh = {t["team"]: nhl.normalize(t, state["rosters"]["teams"].get(t["team"], [])) for t in nhl.parse_article(story)}
nh = {k: v for k, v in nh.items() if v["game_date"] == today}
playing = {k for k, v in nh.items() if not v["start"] or now < datetime.fromisoformat(v["start"])}
print(f"{today}: {len(nh)} teams in the NHL.com article, {len(playing)} not started yet")

rosters = x.load_rosters(today)
posted = (x.load_posted().get(today) or {})
gd_rows = [r for r in csv.DictReader(open(gd.ALERT_LOG_PATH, encoding="utf-8")) if r["alert_sent_at"][:10] == today]

out = []
for team in sorted(playing, key=lambda t: (nh[t]["start"], nh[t]["home"][0], t)):
    recs = posted.get(team) or {}
    src = max(recs, key=lambda s: recs[s]["at"]) if recs else "nhlcom"
    if src == "nhlcom":
        t = nh[team]
        lineup = {"forwards": t["forwards"], "defense": t["defense"], "extra": t["extra"],
                  "goalie": t["goalies"][0] if t["goalies"] else ""}
        meta = {"info_at": datetime.fromisoformat(au.replace("Z", "+00:00")).isoformat(timespec="seconds")}
        msg = nhl.build_slack_notification(t, "new", None, au, now)
    elif src == "dfo":
        slug = nh[team]["team_name"].lower().replace(" ", "-").replace(".", "")
        t = dfo.parse_team(dfo.fetch_page(dfo.TEAM_URL.format(slug=slug)))
        lineup = {"forwards": t["forwards"], "defense": t["defense"], "extra": t["extra"],
                  "goalie": t["goalies"][0] if t["goalies"] else ""}
        meta = dfo.xcheck_meta(t)
        msg = dfo.build_slack_notification(t, "new", None)
    else:
        row = None
        for r in reversed(gd_rows):
            parsed = x.parse_tweet(r["best_text"], rosters)
            if parsed and parsed[0] == team:
                row, lineup = r, parsed[1]
                break
        if row is None:
            print(f"{team}: latest is a tweet I can't find in the log, skipped")
            continue
        row = {**row, "best_text": row["best_text"].replace("\\n", "\n")}
        meta = {"info_at": gd.status_id_to_utc(row["status_id"]).isoformat(timespec="seconds"),
                "via": row["source_handle"], "ref": row["status_id"]}
        msg = gd.build_slack_notification(row)
    banner, footer = x.cross_check(team, src, lineup, now, rosters, meta)
    out.append((team, src, tag(x.add_to_message(msg, footer, banner)), bool(banner)))

for team, src, msg, conflict in out:
    print(f"=== {team} from {src}{'  [CONFLICT]' if conflict else ''}")
    if not SEND:
        print(msg[:1200].encode("ascii", "replace").decode())
print(f"{len(out)} posts, {sum(c for *_, c in out)} with a conflict banner")
if SEND:
    for i, (team, src, msg, _) in enumerate(out):
        gd.send_slack_message(msg)
        print(f"sent {team}")
        time.sleep(1.1)
