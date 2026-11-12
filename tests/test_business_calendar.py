"""'Today' for planning is the bakery's day (America/New_York), not the
server's UTC day: at 8 pm Eastern the server already says tomorrow, which
moved POs out of the bake queue and closed Toast days early."""
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, ".")

import core.util as util


class _FakeDT(datetime):
    @classmethod
    def now(cls, tz=None):
        utc = datetime(2026, 11, 13, 1, 30, tzinfo=timezone.utc)   # 8:30 pm ET on the 12th
        return utc.astimezone(tz) if tz else utc.replace(tzinfo=None)


def test_business_today_is_new_york_not_utc(monkeypatch):
    import datetime as _d
    monkeypatch.setattr(_d, "datetime", _FakeDT)
    monkeypatch.delenv("BUSINESS_TZ", raising=False)
    assert util.business_today() == date(2026, 11, 12)
    assert util.business_now().hour == 20


def test_business_tz_env_override_and_bad_name_falls_back(monkeypatch):
    monkeypatch.setenv("BUSINESS_TZ", "America/Los_Angeles")
    assert str(util.business_tz()) == "America/Los_Angeles"
    monkeypatch.setenv("BUSINESS_TZ", "Not/AZone")
    assert str(util.business_tz()) == "America/New_York"


def test_business_days_between_skips_weekends():
    # Fri 11/06 -> Thu 11/12: Mon, Tue, Wed, Thu = 4
    assert util.business_days_between(date(2026, 11, 6), date(2026, 11, 12)) == 4
    assert util.business_days_between(date(2026, 11, 12), date(2026, 11, 12)) == 0
    assert util.business_days_between(date(2026, 11, 13), date(2026, 11, 16)) == 1


def test_planners_default_to_business_now(monkeypatch):
    import integrations.order_plan as op
    import integrations.production_planner as pp
    fixed = datetime(2026, 11, 12, 20, 30)
    monkeypatch.setattr(op, "business_now", lambda: fixed)
    monkeypatch.setattr(util, "business_now", lambda: fixed)
    plan = op.plan_orders({}, {})
    assert plan["as_of"].startswith("2026-11-12T20:30")
    guide = pp.build_production_guide({}, [], [], {})
    assert guide["generated_at"].startswith("2026-11-12T20:30")
