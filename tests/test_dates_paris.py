"""The date window counts in the user's days (Paris), not UTC's (19 Nov 2026: an offer published at 00:49 Paris time on
the 18th, stored as 23:49 UTC on the 17th, was dropped from 'Last 24 hours')."""
from datetime import datetime

from jobfinder.dates import date_decision, paris_day


def test_an_offer_published_just_after_midnight_in_paris_is_that_day_s():
    cfg = {"date_preset": "Last 24 hours"}
    now = datetime(2026, 11, 19, 12, 30)  # 13:30 in Paris
    ok, why = date_decision(datetime(2026, 11, 17, 23, 49, 15), 1.0, cfg, now)
    assert ok is True and "2026-11-18" in why
    ok, _ = date_decision(datetime(2026, 11, 17, 22, 30), 1.0, cfg, now)  # 23:30 Paris on the 17th: outside
    assert ok is False
    assert paris_day(datetime(2026, 11, 18)) == datetime(2026, 11, 18).date()  # a date alone keeps its day
    assert paris_day(datetime(2026, 7, 1, 22, 30)) == datetime(2026, 7, 2).date()  # summer time: UTC+2
    # just after midnight in Paris, 'today' is already the new day
    ok, _ = date_decision(datetime(2026, 11, 18, 23, 40), 1.0, cfg, datetime(2026, 11, 19, 23, 30))
    assert ok is True  # now = 00:30 Paris on the 20th: the 19th (Paris) is 'yesterday'
