"""
due_timing_engine.py
Compute due dates from machine-readable timing rules.

Supported rule types:
  day_of_month           — fixed day-of-month with optional month offset
  days_after_period_end  — N days after period end
  days_after_event       — N days after a trigger event
  fixed_date_in_year     — specific month/day each year
  month_day_after_fy_end — N days after fiscal-year end
  continuous             — no fixed deadline (returns None)
  custom                 — reserved for future extension

Weekend / holiday rules:
  weekend_rule: "next_business_day" | "previous_business_day" | None
  holiday_calendar: list of date objects to treat as holidays
"""

from datetime import date, timedelta
from typing import Optional

from dateutil.relativedelta import relativedelta


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def compute_due_date(
    rule: dict,
    period_end: date,
    event_date: Optional[date] = None,
    fiscal_year_end: Optional[date] = None,
    holiday_calendar: Optional[list] = None,
) -> Optional[date]:
    """
    Compute the compliance due date given a machine timing rule and context.

    Parameters
    ----------
    rule           : Parsed due_timing_machine JSON dict.
    period_end     : Last day of the compliance period (e.g. 2024-03-31).
    event_date     : Trigger-event date (required for 'days_after_event').
    fiscal_year_end: Override for FY end; defaults to April-March anchor.
    holiday_calendar: List of date objects treated as non-working days.

    Returns
    -------
    date or None  (None for 'continuous' obligations with no fixed deadline)
    """
    rule_type = rule.get("type")

    if rule_type == "day_of_month":
        d = _day_of_month(rule, period_end)

    elif rule_type == "days_after_period_end":
        days = _require_int(rule, "days")
        d = period_end + timedelta(days=days)

    elif rule_type == "days_after_event":
        if event_date is None:
            raise ValueError("event_date is required for 'days_after_event' rule")
        days = _require_int(rule, "days")
        d = event_date + timedelta(days=days)

    elif rule_type == "fixed_date_in_year":
        month = _require_int(rule, "month")
        day = _require_int(rule, "day")
        d = date(period_end.year, month, day)
        # If the computed date is before period_end, use next year
        if d < period_end:
            d = date(period_end.year + 1, month, day)

    elif rule_type == "month_day_after_fy_end":
        fy_end = fiscal_year_end or _default_fy_end(period_end)
        days = _require_int(rule, "days")
        d = fy_end + timedelta(days=days)

    elif rule_type == "continuous":
        return None  # No fixed deadline

    elif rule_type == "custom":
        # Placeholder — custom rules handled by caller
        return None

    else:
        raise ValueError(f"Unknown due timing rule type: '{rule_type}'")

    # ── Weekend / holiday shift ───────────────────────────────────────────
    weekend_rule = rule.get("weekend_rule")
    if weekend_rule == "next_business_day":
        d = _shift_forward(d, holiday_calendar)
    elif weekend_rule == "previous_business_day":
        d = _shift_backward(d, holiday_calendar)

    return d


# ─────────────────────────────────────────────────────────────────────────────
# Rule-type helpers
# ─────────────────────────────────────────────────────────────────────────────

def _day_of_month(rule: dict, period_end: date) -> date:
    """
    'day_of_month' rule.
    Computes: first day of (period_end + offset_months months), then sets day.

    Example: offset_months=1, day=20 → 20th of the month following period_end.
    """
    offset = rule.get("offset_months", 1)
    day = _require_int(rule, "day")
    # Start from the 1st of period_end's month, add offset
    base = period_end.replace(day=1) + relativedelta(months=int(offset))
    # Clamp day to actual month length
    import calendar
    max_day = calendar.monthrange(base.year, base.month)[1]
    day = min(day, max_day)
    return base.replace(day=day)


def _default_fy_end(period_end: date) -> date:
    """
    Return the fiscal year end (31 March) for the FY that *period_end* falls in.
    April-March anchor (India default).
    """
    if period_end.month >= 4:
        return date(period_end.year + 1, 3, 31)
    return date(period_end.year, 3, 31)


# ─────────────────────────────────────────────────────────────────────────────
# Business-day shift helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_business_day(d: date, holidays: Optional[list]) -> bool:
    if d.weekday() >= 5:          # Saturday=5, Sunday=6
        return False
    if holidays and d in holidays:
        return False
    return True


def _shift_forward(d: date, holidays: Optional[list]) -> date:
    """Move d forward until it falls on a business day."""
    while not _is_business_day(d, holidays):
        d += timedelta(days=1)
    return d


def _shift_backward(d: date, holidays: Optional[list]) -> date:
    """Move d backward until it falls on a business day."""
    while not _is_business_day(d, holidays):
        d -= timedelta(days=1)
    return d


# ─────────────────────────────────────────────────────────────────────────────
# Utility
# ─────────────────────────────────────────────────────────────────────────────

def _require_int(rule: dict, key: str) -> int:
    value = rule.get(key)
    if value is None:
        raise ValueError(f"Due timing rule of type '{rule.get('type')}' requires '{key}'")
    return int(value)