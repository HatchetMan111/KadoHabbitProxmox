"""Unit-Tests für EMA-Score + Streak (aus docs/habit-score.md / streak.md abgeleitet)."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from app import ema_score, streaks  # noqa: E402


def test_empty_history_is_zero():
    s, _ = ema_score(date(2026, 1, 1), date(2026, 1, 10), {}, "daily", [], 2)
    assert s == 0.0


def test_perfect_30_days_gt_075():
    comps = {(date(2026, 1, 1) + __import__("datetime").timedelta(days=i)).isoformat(): 1.0 for i in range(30)}
    s, _ = ema_score(date(2026, 1, 1), date(2026, 1, 30), comps, "daily", [], 2)
    assert s > 0.75, s


def test_single_miss_barely_dents():
    import datetime
    start = date(2026, 1, 1)
    full = {(start + datetime.timedelta(days=i)).isoformat(): 1.0 for i in range(30)}
    s_full, _ = ema_score(start, date(2026, 1, 30), full, "daily", [], 2)
    miss = dict(full)
    miss[date(2026, 1, 30).isoformat()] = 0.0
    s_miss, _ = ema_score(start, date(2026, 1, 30), miss, "daily", [], 2)
    assert 0 < (s_full - s_miss) < 0.06, (s_full, s_miss)


def test_counter_partial_credit():
    s, hist = ema_score(date(2026, 1, 1), date(2026, 1, 1), {"2026-01-01": 0.75}, "daily", [], 2)
    assert hist[0]["value"] == 0.75
    assert abs(s - 0.05 * 0.75) < 1e-9


def test_specific_days_skips_others():
    # Nur montags due: Dienstag darf Score nicht ändern.
    s_mon, _ = ema_score(date(2026, 1, 5), date(2026, 1, 5), {"2026-01-05": 1.0}, "specificDays", [0], 2)
    s_tue, hist = ema_score(date(2026, 1, 5), date(2026, 1, 6), {"2026-01-05": 1.0}, "specificDays", [0], 2)
    assert s_mon == s_tue
    assert hist[1]["value"] is None


def test_streak_daily():
    import datetime
    start = date(2026, 1, 1)
    end = date(2026, 1, 5)
    comps = {(start + datetime.timedelta(days=i)).isoformat(): 1.0 for i in range(5)}
    cur, best = streaks(start, end, comps, "daily", [], 2)
    assert (cur, best) == (5, 5)


def test_streak_breaks_on_miss():
    cur, best = streaks(date(2026, 1, 1), date(2026, 1, 3),
                        {"2026-01-01": 1.0, "2026-01-03": 1.0}, "daily", [], 2)
    assert cur == 1 and best == 1
