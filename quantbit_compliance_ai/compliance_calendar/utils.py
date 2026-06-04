"""
complyai/compliance/compliance_calendar/utils.py

Shared helpers for the Compliance Calendar module.

Covers:
  - compute_next_period()  : calculates next period dates from frequency + due_timing_rule
  - log_task_activity()    : append-only audit entry creation
  - notify_user()          : stub for notification dispatch (email / WhatsApp / in-app)
  - get_actor_role()       : highest-privilege role of a user
  - compute_reminder_dates(): compute actual calendar dates for reminders (with holiday shift)
  - parse_due_timing_rule(): interpret plain-English timing rules into date offsets
"""

import frappe
from frappe.utils import (
    add_days,
    add_months,
    get_last_day,
    getdate,
    today,
    now,
)
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta


# ──────────────────────────────────────────────────────────────────────────────
# PERIOD COMPUTATION
# ──────────────────────────────────────────────────────────────────────────────

FREQUENCY_DELTA: dict = {
    "Daily":       relativedelta(days=1),
    "Weekly":      relativedelta(weeks=1),
    "Fortnightly": relativedelta(weeks=2),
    "Monthly":     relativedelta(months=1),
    "Bi-Monthly":  relativedelta(months=2),
    "Quarterly":   relativedelta(months=3),
    "Half-Yearly": relativedelta(months=6),
    "Annual":      relativedelta(years=1),
    "Bi-Annual":   relativedelta(years=2),
}


def compute_next_period(
    frequency: str,
    current_period_end: "date | str",
    due_timing_rule: str,
) -> tuple:
    """
    Given the current period end and the obligation frequency, return:
        (next_period_start, next_period_end, next_due_date)

    All three are returned as datetime.date objects.

    Period semantics:
    -----------------
    The next period starts one day after the current period ends.
    The next period ends when the delta is applied to the start (minus 1 day).
    The due date is computed by applying the due_timing_rule to the next period.

    Note: Monthly GST returns — Sep return covers Aug period. The period_start /
    period_end is the compliance period; the due_date is when the filing is due.
    These are three distinct values; do not conflate them.
    """
    current_end = getdate(current_period_end)
    next_start = current_end + timedelta(days=1)

    delta = FREQUENCY_DELTA.get(frequency)
    if not delta:
        frappe.throw(f"Unknown frequency: {frequency}")

    next_end = (next_start + delta) - timedelta(days=1)
    next_due = parse_due_timing_rule(due_timing_rule, next_start, next_end)

    return next_start, next_end, next_due


def parse_due_timing_rule(rule: str, period_start: date, period_end: date) -> date:
    """
    Convert a plain-English due timing rule string into a concrete date.

    Supported patterns (case-insensitive):
      - "Nth of next month"            → day N of the month after period_end
      - "Nth of following month"       → same as above
      - "Nth of the month"             → day N of the same month as period_end
      - "Within N days of period end"  → period_end + N days
      - "Within N days of FY end"      → next 31 Mar + N days
      - "Last day of month"            → last day of period_end's month
      - "15th of next month"           → 15th of next month (numeric shorthand)

    Falls back to period_end + 30 days if the rule cannot be parsed.
    """
    import re

    rule_lower = rule.lower().strip()

    # Pattern: "Nth of next month" or "Nth of following month"
    m = re.search(r"(\d+)(st|nd|rd|th)?\s+of\s+(next|following)\s+month", rule_lower)
    if m:
        day = int(m.group(1))
        base = period_end + relativedelta(months=1)
        return _safe_date(base.year, base.month, day)

    # Pattern: "Nth of the month" (same month as period end)
    m = re.search(r"(\d+)(st|nd|rd|th)?\s+of\s+the\s+month", rule_lower)
    if m:
        day = int(m.group(1))
        return _safe_date(period_end.year, period_end.month, day)

    # Pattern: "Within N days of period end"
    m = re.search(r"within\s+(\d+)\s+days?\s+of\s+period\s+end", rule_lower)
    if m:
        return period_end + timedelta(days=int(m.group(1)))

    # Pattern: "Within N days of FY end"
    m = re.search(r"within\s+(\d+)\s+days?\s+of\s+fy\s+end", rule_lower)
    if m:
        fy_end = _next_fy_end(period_end)
        return fy_end + timedelta(days=int(m.group(1)))

    # Pattern: "Last day of month"
    if "last day of month" in rule_lower:
        return get_last_day(period_end)

    # Fallback
    return period_end + timedelta(days=30)


def _safe_date(year: int, month: int, day: int) -> date:
    """Return the date clamped to the last valid day of the month."""
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last_day))


def _next_fy_end(reference: date) -> date:
    """Return the next 31 March on or after the reference date."""
    if reference.month <= 3:
        return date(reference.year, 3, 31)
    return date(reference.year + 1, 3, 31)


# ──────────────────────────────────────────────────────────────────────────────
# ACTIVITY LOGGING
# ──────────────────────────────────────────────────────────────────────────────

def log_task_activity(
    task: str,
    activity_type: str,
    from_value: str = None,
    to_value: str = None,
    remarks: str = None,
    actor: str = None,
) -> "frappe.Document":
    """
    Create an immutable Task Activity entry.

    This is the ONLY way activity entries should be created — never insert
    Task Activity docs directly from outside this function (except in tests).
    """
    actor = actor or frappe.session.user
    doc = frappe.get_doc(
        {
            "doctype": "Task Activity",
            "task": task,
            "activity_type": activity_type,
            "actor": actor,
            "from_value": str(from_value) if from_value is not None else None,
            "to_value": str(to_value) if to_value is not None else None,
            "remarks": remarks,
            "occurred_at": now(),
        }
    )
    doc.insert(ignore_permissions=True)
    return doc


# ──────────────────────────────────────────────────────────────────────────────
# REMINDER DATE COMPUTATION
# ──────────────────────────────────────────────────────────────────────────────

def compute_reminder_dates(task_doc, holiday_list: list = None) -> list:
    """
    Compute the actual calendar dates on which reminders should fire for a task.

    Steps:
    1. Fetch the organisation's Task Reminder Schedule (fall back to defaults).
    2. For each configured 'days before due' offset, compute the calendar date.
    3. If the computed date falls on a weekend (Sat/Sun) or holiday, shift
       it to the preceding working day.
    4. Return the unique sorted list of reminder dates.

    NOTE: T-0 (due date itself) shifts to Friday if due_date is a Sunday,
    and to the preceding Friday if due_date is a Saturday.
    This matches Indian compliance practice — actual deadline does NOT shift,
    only the reminder does.

    Returns: List of datetime.date objects in ascending order.
    """
    from complyai.compliance.compliance_calendar.doctype.task_reminder_schedule.task_reminder_schedule import (
        TaskReminderSchedule,
    )

    schedule = TaskReminderSchedule.get_for_organisation(task_doc.organisation)

    if schedule:
        days_before_list = schedule.get_reminder_days(task_doc.risk_level or "Medium")
    else:
        # System defaults
        defaults = {
            "Critical": [30, 14, 7, 3, 1, 0],
            "High":     [14, 7, 3, 1, 0],
            "Medium":   [7, 3, 0],
            "Low":      [3, 0],
        }
        days_before_list = defaults.get(task_doc.risk_level or "Medium", [7, 3, 0])

    due = getdate(task_doc.due_date)
    holiday_set = set(holiday_list or [])

    reminder_dates = set()
    for days_before in days_before_list:
        raw_date = due - timedelta(days=days_before)
        working_date = _shift_to_preceding_working_day(raw_date, holiday_set)
        reminder_dates.add(working_date)

    return sorted(reminder_dates)


def _shift_to_preceding_working_day(d: date, holidays: set) -> date:
    """
    Shift a date backward until it lands on a working day (Mon–Fri, not a holiday).
    """
    while d.weekday() >= 5 or d in holidays:  # 5=Sat, 6=Sun
        d -= timedelta(days=1)
    return d


# ──────────────────────────────────────────────────────────────────────────────
# USER / ROLE HELPERS
# ──────────────────────────────────────────────────────────────────────────────

ROLE_PRECEDENCE = [
    "System Manager",
    "Compliance Officer",
    "Legal Counsel",
    "Department Manager",
    "Internal Auditor",
    "Group CXO",
    "QMS Administrator",
    "Read Only",
]


def get_actor_role(user: str = None) -> str:
    """Return the highest-privilege role of a user."""
    user = user or frappe.session.user
    roles = frappe.get_roles(user)
    for role in ROLE_PRECEDENCE:
        if role in roles:
            return role
    return roles[0] if roles else "Guest"


# ──────────────────────────────────────────────────────────────────────────────
# NOTIFICATION STUB
# ──────────────────────────────────────────────────────────────────────────────

def notify_user(user: str, template: str, context: dict, channels: list = None):
    """
    Dispatch a notification to a user.

    channels defaults to ['email', 'in_app'].
    For WhatsApp, the caller must explicitly include 'whatsapp'.
    Quiet hours are enforced for WhatsApp only.

    In the actual implementation this wraps the shared NotificationService.
    Here it is a stub that logs the intent for testing.
    """
    channels = channels or ["email", "in_app"]
    # In production: NotificationService.send(user=user, template=template, context=context, channels=channels)
    frappe.logger().debug(
        f"[notify_user] user={user} template={template} channels={channels}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# HEALTH SCORE COMPUTATION
# ──────────────────────────────────────────────────────────────────────────────

def compute_health_score(organisation: str = None, business_entity: str = None) -> dict:
    """
    Compute a 0–100 compliance health score for an organisation or specific entity.

    Scoring model
    -------------
    Start at 100. Deduct points for each open/overdue task, weighted by risk level:
      - Critical overdue : -10 pts each
      - High overdue     : -5  pts each
      - Medium overdue   : -2  pts each
      - Low overdue      : -1  pt  each
      - Non-overdue open (>0 days to due): -0.5 pts each (small drag)

    Floor at 0. Cap at 100.

    Returns dict with:
      {
        "score": int,
        "total_tasks": int,
        "completed": int,
        "overdue": int,
        "open": int,
        "by_category": { <category>: {"score": int, "total": int} },
        "by_risk": { <risk_level>: {"overdue": int, "total": int} },
      }
    """
    filters = {"docstatus": ("!=", 2)}
    if organisation:
        filters["organisation"] = organisation
    if business_entity:
        filters["business_entity"] = business_entity

    tasks = frappe.get_all(
        "Compliance Calendar Task",
        filters=filters,
        fields=["status", "risk_level", "category", "is_overdue", "days_overdue"],
    )

    if not tasks:
        return {"score": 100, "total_tasks": 0, "completed": 0, "overdue": 0, "open": 0}

    deduction = 0.0
    total = len(tasks)
    completed = 0
    overdue_count = 0
    open_count = 0

    risk_weights = {"Critical": 10, "High": 5, "Medium": 2, "Low": 1}
    by_category: dict = {}
    by_risk: dict = {}

    for t in tasks:
        rl = t.risk_level or "Medium"
        cat = t.category or "Other"

        by_category.setdefault(cat, {"completed": 0, "total": 0})
        by_risk.setdefault(rl, {"overdue": 0, "total": 0})
        by_category[cat]["total"] += 1
        by_risk[rl]["total"] += 1

        if t.status in ("Completed", "Not Applicable"):
            completed += 1
            by_category[cat]["completed"] += 1
        elif t.is_overdue:
            overdue_count += 1
            by_risk[rl]["overdue"] += 1
            deduction += risk_weights.get(rl, 2)
        else:
            open_count += 1
            deduction += 0.5  # small drag for pending tasks

    raw_score = 100.0 - deduction
    score = max(0, min(100, int(raw_score)))

    # Per-category score
    for cat, data in by_category.items():
        total_cat = data["total"]
        completed_cat = data["completed"]
        data["score"] = int((completed_cat / total_cat) * 100) if total_cat else 100

    return {
        "score": score,
        "total_tasks": total,
        "completed": completed,
        "overdue": overdue_count,
        "open": open_count,
        "by_category": by_category,
        "by_risk": by_risk,
    }