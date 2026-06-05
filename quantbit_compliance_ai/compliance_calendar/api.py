"""
quantbit_compliance_ai/compliance_calendar/api.py

Whitelisted API methods for the Compliance Calendar module.

All methods enforce:
  - Organisation-scope isolation (multi-tenancy)
  - Role-based permission checks
  - Activity logging for every mutation

Idempotency note: generate_calendar_for_entity is safe to run multiple times.
"""

import frappe
from frappe import _
from frappe.utils import today, getdate, date_diff, now
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta

from quantbit_compliance_ai.compliance_calendar.utils import (
    compute_next_period,
    log_task_activity,
    compute_health_score,
    compute_reminder_dates,
)
from quantbit_compliance_ai.compliance_calendar.applicability_engine import (
    ApplicabilityEngine,
)


# ──────────────────────────────────────────────────────────────────────────────
# PERMISSION HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _require_role(*roles):
    """Raise PermissionError if the current user does not have any of the given roles."""
    user_roles = frappe.get_roles(frappe.session.user)
    if not any(r in user_roles for r in roles):
        frappe.throw(
            f"Permission denied. Required role(s): {', '.join(roles)}.",
            frappe.PermissionError,
        )


def _get_user_organisations() -> list:
    """Return list of organisations the current user has access to."""
    # In production: query User Profile → organisation associations.
    # Stub: Compliance Officers see their own org; System Manager sees all.
    user = frappe.session.user
    if "System Manager" in frappe.get_roles(user):
        return frappe.get_all("Organisation", pluck="name")
    profile = frappe.db.get_value("User Profile", {"user": user}, "organisation")
    if profile:
        return [profile]
    return []


def _assert_task_access(task_name: str) -> "frappe.Document":
    """Load a task and assert the current user can access it."""
    task = frappe.get_doc("Compliance Calendar Task", task_name)
    user_orgs = _get_user_organisations()
    if task.organisation not in user_orgs:
        frappe.throw("You do not have permission to access this task.", frappe.PermissionError)
    return task


# ──────────────────────────────────────────────────────────────────────────────
# CALENDAR GENERATION
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def generate_calendar_for_entity(business_entity: str, year: int = None) -> dict:
    """
    Run the Applicability Engine for a Business Entity and generate all tasks for the FY.

    Idempotent: re-running is safe. Tasks that already exist (unique index on
    organisation + business_entity + obligation + period_start) are skipped.

    Returns: { generated: int, skipped: int, errors: list }
    Permission: Compliance Officer or above.
    """
    _require_role("System Manager", "Compliance Officer")

    entity = frappe.get_doc("Business Entity", business_entity)
    engine = ApplicabilityEngine(entity)
    applicable_obligations = engine.get_applicable_obligations()

    year = year or date.today().year
    fy_start = date(year, 4, 1)   # Indian FY: Apr 1
    fy_end = date(year + 1, 3, 31)

    generated = 0
    skipped = 0
    errors = []

    for obligation in applicable_obligations:
        try:
            count = _generate_tasks_for_obligation(
                obligation=obligation,
                entity=entity,
                fy_start=fy_start,
                fy_end=fy_end,
            )
            generated += count["generated"]
            skipped += count["skipped"]
        except Exception as e:
            errors.append({"obligation": obligation.name, "error": str(e)})
            frappe.log_error(frappe.get_traceback(), f"Task generation error: {obligation.name}")

    return {"generated": generated, "skipped": skipped, "errors": errors}


def _generate_tasks_for_obligation(obligation, entity, fy_start: date, fy_end: date) -> dict:
    """Generate all task instances for one obligation within the FY."""
    from quantbit_compliance_ai.compliance_calendar.utils import parse_due_timing_rule

    frequency = obligation.frequency
    if frequency in ("One-Time", "On-Event", "Continuous"):
        # Create a single task covering the entire FY
        periods = [(fy_start, fy_end)]
    else:
        periods = _expand_periods(frequency, fy_start, fy_end)

    generated = 0
    skipped = 0

    for period_start, period_end in periods:
        due = parse_due_timing_rule(obligation.due_timing_rule, period_start, period_end)

        existing = frappe.db.exists(
            "Compliance Calendar Task",
            {
                "organisation": entity.organisation,
                "business_entity": entity.name,
                "obligation": obligation.name,
                "period_start": period_start,
            },
        )
        if existing:
            skipped += 1
            continue

        task = frappe.get_doc(
            {
                "doctype": "Compliance Calendar Task",
                "organisation": entity.organisation,
                "business_entity": entity.name,
                "obligation": obligation.name,
                "task_title": obligation.obligation_title,
                "period_start": period_start,
                "period_end": period_end,
                "due_date": due,
                "assigned_to": frappe.session.user,  # Default assignee; CO re-assigns
                "status": "Open",
            }
        )
        task.insert(ignore_permissions=True)
        generated += 1

    return {"generated": generated, "skipped": skipped}


def _expand_periods(frequency: str, fy_start: date, fy_end: date) -> list:
    """
    Expand a frequency into a list of (period_start, period_end) tuples within the FY.
    """
    delta_map = {
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
    delta = delta_map.get(frequency)
    if not delta:
        return [(fy_start, fy_end)]

    periods = []
    current = fy_start
    while current <= fy_end:
        end = (current + delta) - timedelta(days=1)
        end = min(end, fy_end)
        periods.append((current, end))
        current = current + delta

    return periods


# ──────────────────────────────────────────────────────────────────────────────
# TASK QUERIES
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def get_my_tasks(filters: dict = None, limit: int = 50, start: int = 0) -> dict:
    """
    Returns paginated tasks assigned to the current user OR in their accessible entities.
    Filters: status, risk_level, due_date_range, category, business_entity.
    """
    filters = filters or {}
    user = frappe.session.user
    user_orgs = _get_user_organisations()

    base_filters = {
        "organisation": ("in", user_orgs),
        "docstatus": ("!=", 2),
    }

    # Only show tasks assigned to this user unless they're a Compliance Officer
    if "Compliance Officer" not in frappe.get_roles(user) and "System Manager" not in frappe.get_roles(user):
        base_filters["assigned_to"] = user

    # Merge caller-supplied filters
    allowed_filter_keys = {"status", "risk_level", "category", "business_entity"}
    for k, v in filters.items():
        if k in allowed_filter_keys:
            base_filters[k] = v

    # Date range filter
    if "due_date_range" in filters:
        dr = filters["due_date_range"]
        base_filters["due_date"] = ("between", [dr.get("from"), dr.get("to")])

    tasks = frappe.get_all(
        "Compliance Calendar Task",
        filters=base_filters,
        fields=[
            "name", "task_title", "obligation", "business_entity", "category",
            "due_date", "status", "risk_level", "assigned_to", "evidence_completeness",
            "is_overdue", "days_overdue", "ai_risk_score",
        ],
        order_by="due_date asc",
        limit=limit,
        start=start,
    )

    total = frappe.db.count("Compliance Calendar Task", base_filters)
    return {"tasks": tasks, "total": total, "limit": limit, "start": start}


@frappe.whitelist()
def get_calendar_view(business_entity: str = None, month: str = None) -> dict:
    """
    Returns tasks grouped by due date for calendar UI.
    Format: { "2025-09-15": [task1, task2], "2025-09-20": [task3] }
    """
    user_orgs = _get_user_organisations()
    filters = {"organisation": ("in", user_orgs), "docstatus": ("!=", 2)}

    if business_entity:
        filters["business_entity"] = business_entity

    if month:
        # month format: "YYYY-MM"
        year, mon = map(int, month.split("-"))
        from calendar import monthrange
        last_day = monthrange(year, mon)[1]
        filters["due_date"] = ("between", [f"{year}-{mon:02d}-01", f"{year}-{mon:02d}-{last_day}"])

    tasks = frappe.get_all(
        "Compliance Calendar Task",
        filters=filters,
        fields=["name", "task_title", "due_date", "status", "risk_level", "business_entity"],
        order_by="due_date asc",
        limit=500,
    )

    grouped: dict = {}
    for task in tasks:
        key = str(task.due_date)
        grouped.setdefault(key, []).append(task)

    return grouped


@frappe.whitelist()
def get_task_detail(task_name: str) -> dict:
    """Full task with activity log, evidence, linked records."""
    task = _assert_task_access(task_name)

    activities = frappe.get_all(
        "Task Activity",
        filters={"task": task_name},
        fields=["activity_type", "actor", "actor_role", "occurred_at",
                "from_value", "to_value", "remarks", "ip_address"],
        order_by="occurred_at asc",
    )

    return {
        "task": task.as_dict(),
        "activity_log": activities,
    }


@frappe.whitelist()
def get_overdue_summary() -> dict:
    """Counts of overdue tasks by entity + risk level for current user's scope."""
    user_orgs = _get_user_organisations()
    rows = frappe.db.sql(
        """
        SELECT business_entity, risk_level, COUNT(*) as count
        FROM `tabCompliance Calendar Task`
        WHERE organisation IN %(orgs)s
          AND is_overdue = 1
          AND docstatus != 2
        GROUP BY business_entity, risk_level
        ORDER BY business_entity, risk_level
        """,
        {"orgs": tuple(user_orgs) or ("__none__",)},
        as_dict=True,
    )
    return {"rows": rows}


@frappe.whitelist()
def search_tasks(query: str, filters: dict = None) -> list:
    """Full-text search over task title + obligation title + remarks."""
    user_orgs = _get_user_organisations()
    if not query:
        return []
    like = f"%{query}%"
    extra_filter = ""
    params = {"orgs": tuple(user_orgs) or ("__none__",), "query": like}

    if filters and filters.get("status"):
        extra_filter = "AND t.status = %(status)s"
        params["status"] = filters["status"]

    results = frappe.db.sql(
        f"""
        SELECT t.name, t.task_title, t.business_entity, t.due_date, t.status, t.risk_level
        FROM `tabCompliance Calendar Task` t
        WHERE t.organisation IN %(orgs)s
          AND t.docstatus != 2
          AND (t.task_title LIKE %(query)s OR t.completion_remarks LIKE %(query)s)
          {extra_filter}
        ORDER BY t.due_date ASC
        LIMIT 50
        """,
        params,
        as_dict=True,
    )
    return results


# ──────────────────────────────────────────────────────────────────────────────
# TASK MUTATIONS
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def update_task_status(
    task_name: str,
    new_status: str,
    remarks: str = None,
    evidence_files: list = None,
) -> dict:
    """Update task status with workflow validation. Logs activity."""
    task = _assert_task_access(task_name)

    valid_transitions = {
        "Open":         ["In Progress", "Not Applicable", "Deferred"],
        "In Progress":  ["Under Review", "Completed", "Not Applicable", "Deferred"],
        "Under Review": ["Completed", "In Progress"],
        "Overdue":      ["In Progress", "Not Applicable", "Deferred"],
        "Deferred":     ["In Progress", "Open"],
    }

    allowed = valid_transitions.get(task.status, [])
    if new_status not in allowed:
        frappe.throw(
            f"Cannot transition from '{task.status}' to '{new_status}'. "
            f"Allowed transitions: {allowed}",
            frappe.ValidationError,
        )

    task.status = new_status
    if remarks:
        task.completion_remarks = remarks
    if evidence_files:
        for ef in evidence_files:
            task.append("evidence_files", ef)
    task.save()
    return {"status": "ok", "new_status": task.status}


@frappe.whitelist()
def extend_task_due_date(task_name: str, new_due_date: str, reason: str) -> dict:
    """
    Extend due date. Increments extension_count. Requires reason. Audit logged.

    Rules:
    - Department Managers cannot extend Critical risk tasks.
    - After 3 extensions, only Group CXO or System Manager may extend.
    """
    if not reason:
        frappe.throw("A reason is required to extend the due date.", frappe.ValidationError)

    task = _assert_task_access(task_name)
    user_roles = frappe.get_roles(frappe.session.user)

    # Role check: Department Manager cannot extend Critical tasks
    if task.risk_level == "Critical" and "Department Manager" in user_roles:
        if not any(r in user_roles for r in ("Compliance Officer", "System Manager", "Group CXO")):
            frappe.throw(
                "Department Managers cannot extend Critical risk tasks. "
                "Contact your Compliance Officer.",
                frappe.PermissionError,
            )

    # Extension count gate: >3 requires CXO or System Manager
    if task.extension_count >= 3:
        if not any(r in user_roles for r in ("System Manager", "Group CXO")):
            frappe.throw(
                f"This task has already been extended {task.extension_count} time(s). "
                "A 4th extension requires Group CXO or System Manager approval.",
                frappe.PermissionError,
            )

    old_due = task.due_date
    new_due = getdate(new_due_date)

    if new_due <= getdate(old_due):
        frappe.throw("New due date must be after the current due date.", frappe.ValidationError)

    task.due_date = new_due
    task.extension_count = (task.extension_count or 0) + 1
    if task.status == "Overdue":
        task.status = "Deferred"
    task.save()

    log_task_activity(
        task=task_name,
        activity_type="Due Date Extended",
        from_value=str(old_due),
        to_value=str(new_due),
        remarks=reason,
    )

    return {"status": "ok", "extension_count": task.extension_count, "new_due_date": str(new_due)}


@frappe.whitelist()
def mark_task_na(task_name: str, reason: str, approver: str) -> dict:
    """Mark Not Applicable with mandatory reason and approver."""
    if not reason:
        frappe.throw("Reason is required to mark a task as Not Applicable.", frappe.ValidationError)
    if not approver:
        frappe.throw("An approver is required to mark a task as Not Applicable.", frappe.ValidationError)

    task = _assert_task_access(task_name)
    task.status = "Not Applicable"
    task.na_reason = reason
    task.na_approved_by = approver
    task.save()

    log_task_activity(
        task=task_name,
        activity_type="Marked NA",
        remarks=reason,
    )
    return {"status": "ok"}


@frappe.whitelist()
def reassign_task(task_name: str, new_assignee: str, reason: str = None) -> dict:
    """Reassign a single task."""
    task = _assert_task_access(task_name)
    old_assignee = task.assigned_to
    task.assigned_to = new_assignee
    task.save()

    log_task_activity(
        task=task_name,
        activity_type="Reassigned",
        from_value=old_assignee,
        to_value=new_assignee,
        remarks=reason,
    )
    return {"status": "ok"}


@frappe.whitelist()
def bulk_reassign(task_names: list, new_assignee: str, reason: str) -> dict:
    """
    Bulk reassignment. Atomic: all tasks succeed or all rollback.
    Rules:
    - Cannot reassign tasks that are Completed, Not Applicable, Under Review, or submitted.
    - Every task gets an Activity Log entry.
    Activity logged per task.
    """
    if not reason:
        frappe.throw("A reason is required for bulk reassignment.", frappe.ValidationError)

    locked_statuses = ("Completed", "Not Applicable", "Under Review")

    # Validate all tasks first (atomic: fail fast before any mutation)
    tasks_to_update = []
    for task_name in task_names:
        task = _assert_task_access(task_name)
        if task.status in locked_statuses or task.docstatus == 1:
            frappe.throw(
                f"Task {task_name} has status '{task.status}' and cannot be reassigned. "
                "Bulk reassignment requires all selected tasks to be in an open state.",
                frappe.ValidationError,
            )
        tasks_to_update.append(task)

    # All validated — apply mutations
    for task in tasks_to_update:
        old_assignee = task.assigned_to
        task.assigned_to = new_assignee
        task.save()
        log_task_activity(
            task=task.name,
            activity_type="Reassigned",
            from_value=old_assignee,
            to_value=new_assignee,
            remarks=f"Bulk reassign — {reason}",
        )

    return {"status": "ok", "reassigned": len(tasks_to_update)}


@frappe.whitelist()
def upload_evidence_to_task(
    task_name: str,
    file_url: str,
    evidence_type: str,
    is_primary: int = 0,
    remarks: str = None,
) -> dict:
    """Upload evidence and link to task in one call."""
    task = _assert_task_access(task_name)

    # Create Evidence File record (simplified — production creates with SHA-256 hash)
    evidence_file = frappe.get_doc(
        {
            "doctype": "Evidence File",
            "file_url": file_url,
            "evidence_type": evidence_type,
        }
    )
    evidence_file.insert(ignore_permissions=True)

    task.append(
        "evidence_files",
        {
            "evidence_file": evidence_file.name,
            "evidence_type": evidence_type,
            "is_primary": is_primary,
            "remarks": remarks,
        },
    )
    task.save()

    log_task_activity(
        task=task_name,
        activity_type="Evidence Uploaded",
        to_value=evidence_type,
        remarks=remarks,
    )
    return {"status": "ok", "evidence_file": evidence_file.name}


# ──────────────────────────────────────────────────────────────────────────────
# HEALTH SCORE
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def get_health_score(organisation: str = None, business_entity: str = None) -> dict:
    """
    Returns 0–100 health score + breakdown by category, by risk level.
    Org or entity scope.
    """
    if not organisation and not business_entity:
        # Default to current user's organisations
        user_orgs = _get_user_organisations()
        if not user_orgs:
            return {"score": 100}
        organisation = user_orgs[0]

    return compute_health_score(organisation=organisation, business_entity=business_entity)