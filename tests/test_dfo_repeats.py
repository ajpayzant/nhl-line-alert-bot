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
