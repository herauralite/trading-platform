"""Personal UTC-session limits. Inputs must come from the same owned account."""
from datetime import timedelta, timezone
from decimal import Decimal


def assess(start_equity, equity, limit, sampled_at, now, started_at):
    if not all(isinstance(v, Decimal) and v.is_finite() for v in (start_equity, equity, limit)):
        return None
    if limit <= 0 or sampled_at is None or started_at is None:
        return None
    age = now - sampled_at
    if age < timedelta(0) or age > timedelta(seconds=90) or sampled_at < started_at:
        return None
    if started_at.astimezone(timezone.utc).date() != now.astimezone(timezone.utc).date():
        return None
    loss = max(Decimal(0), start_equity - equity)
    threshold = 100 if loss >= limit else 80 if loss >= limit * Decimal("0.80") else 0
    return loss, max(Decimal(0), limit - loss), threshold
