import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dfo_lines_alert as b  # noqa: E402


def team(updated, source, f="A"):
    return {"team": "CHI", "updated_at": updated, "source": source, "source_name": "Reporter" if source else "Last Game",
            "forwards": [[f, "B", "C"]], "defense": [["D", "E"]], "goalies": ["G"], "pp1": []}


def test_stale_cached_page_is_ignored():
    fresh_reset = team("2026-11-18T12:10:04Z", "")
    stale = team("2026-11-17T02:36:53Z", "https://x.com/r/status/1")
    prev = b.state_entry(fresh_reset)
    assert b.is_stale(stale, prev)
    assert b.alert_kind(stale, prev) is None


def test_same_source_and_lineup_never_sent_twice():
    t = team("2026-11-18T17:33:51Z", "https://x.com/r/status/2")
    prev = b.state_entry(team("2026-11-18T12:10:04Z", ""))
    assert b.alert_kind(t, prev) == "new"
    alerted = {("CHI", t["source"], b.lineup_signature(t))}
    assert b.alert_kind(t, prev, alerted) is None
    changed = team("2026-11-18T18:00:00Z", t["source"], f="Z")
    assert b.alert_kind(changed, b.state_entry(t), alerted) == "update"   # a real edit still goes out


def test_scrapes_at_most_every_five_minutes(tmp_path):
    from datetime import datetime, timedelta, timezone
    path = tmp_path / "last.txt"
    t0 = datetime(2026, 11, 18, 18, 0, 5, tzinfo=timezone.utc)
    assert b.due_for_scrape(t0, path)                    # no file yet
    b.mark_scraped(t0, path)
    runs = [t0 + timedelta(minutes=m, seconds=s) for m, s in [(1, -2), (2, 3), (3, 0), (4, -4), (5, 1), (6, 0)]]
    due = []
    for t in runs:
        if b.due_for_scrape(t, path):
            b.mark_scraped(t, path)
            due.append(t.minute)
    assert due == [5]          # 3:56 after is too soon; the 5:01 run scrapes; 6:00 waits again
