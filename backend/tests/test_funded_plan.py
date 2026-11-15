from datetime import datetime, timedelta, timezone
from decimal import Decimal
from app.services.funded_plan import assess


def test_warning_limit_and_unavailable_observations():
    now = datetime(2026, 11, 15, 10, tzinfo=timezone.utc)
    start = now - timedelta(hours=2)
    def evaluate(equity, sampled=now, baseline_time=start):
        return assess(Decimal("100000"), Decimal(equity), Decimal("750"), sampled, now, baseline_time)
    assert evaluate("99400") == (600, 150, 80)
    assert evaluate("99200") == (800, 0, 100)
    assert evaluate("100100") == (0, 750, 0)
    assert evaluate("99400", now - timedelta(seconds=91)) is None
    assert evaluate("99400", now + timedelta(seconds=1)) is None
    assert evaluate("99400", baseline_time=start - timedelta(days=1)) is None
