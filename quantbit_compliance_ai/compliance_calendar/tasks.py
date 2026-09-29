"""
quantbit_compliance_ai/compliance_calendar/tasks.py

Scheduled-job engine functions (spec §9). Written as plain callables and
NOT registered in hooks.py `scheduler_events` at this build depth — the same
choice made for every other module in this build (Q1/Q2/Q4/Q5/Q6/Foundation):
the logic exists and is independently callable/testable now; wiring the
actual cron cadence is a one-line hooks.py addition for later.

  - flag_overdue_tasks()             : bulk is_overdue/days_overdue/status sweep
  - escalate_overdue_tasks()         : L1/L2 escalation per org's Task Reminder Schedule
  - recompute_health_scores()        : cache compute_health_score() per organisation
  - generate_monthly_recurring_tasks(): proactive backstop for Monthly/Bi-Monthly obligations
  - generate_quarterly_tasks()       : proactive backstop for Quarterly/Half-Yearly/Annual/Bi-Annual
"""

import frappe
from frappe.utils import add_months, getdate, today

from quantbit_compliance_ai.compliance_calendar.doctype.task_reminder_schedule.task_reminder_schedule import (
	TaskReminderSchedule,
)
from quantbit_compliance_ai.compliance_calendar.utils import compute_health_score, compute_next_period, log_task_activity

_RECURRING_RESET_FIELDS = {
	"status": "Open",
	"completed_on": None,
	"submission_reference": None,
	"evidence_files": [],
	"completion_pct": 0,
	"reminders_sent": 0,
	"escalation_level": 0,
	"escalated_to": None,
	"is_overdue": 0,
	"days_overdue": 0,
	"docstatus": 0,
	"completion_remarks": None,
	"na_reason": None,
	"na_approved_by": None,
	"amount_paid": 0,
	"ai_risk_score": 0,
	"extension_count": 0,
}


def flag_overdue_tasks() -> dict:
	"""Bulk UPDATE, not a per-document save loop — this runs across every
	tenant daily and must stay cheap. A task's own validate() would recompute
	the same fields on next save anyway; this sweep is for tasks nobody has
	touched since they went overdue."""
	today_ = getdate(today())
	frappe.db.sql(
		"""
		UPDATE `tabCompliance Calendar Task`
		SET is_overdue = 1,
		    days_overdue = DATEDIFF(%(today)s, due_date),
		    status = IF(status = 'Open', 'Overdue', status)
		WHERE docstatus != 2
		  AND status NOT IN ('Completed', 'Not Applicable')
		  AND due_date < %(today)s
		""",
		{"today": today_},
	)
	flagged = frappe.db.sql(
		"""
		SELECT COUNT(*) FROM `tabCompliance Calendar Task`
		WHERE docstatus != 2 AND status NOT IN ('Completed', 'Not Applicable') AND due_date < %(today)s
		""",
		{"today": today_},
	)[0][0]
	frappe.db.commit()
	return {"flagged": flagged}


def escalate_overdue_tasks() -> dict:
	"""§3.4/§9: escalate tasks past their organisation's L1 (manager) / L2
	(CXO) overdue thresholds. L1 sets `escalated_to` to the assignee's
	manager (via User Profile.reports_to); L2 just raises escalation_level —
	there's no direct CXO Link field on the task, Group CXO visibility comes
	from filtering escalation_level=2 in dashboards/reports."""
	overdue_tasks = frappe.get_all(
		"Compliance Calendar Task",
		filters={"docstatus": ("!=", 2), "is_overdue": 1, "status": ("not in", ["Completed", "Not Applicable"])},
		fields=["name", "organisation", "assigned_to", "days_overdue", "escalation_level"],
	)

	escalated = 0
	for row in overdue_tasks:
		schedule = TaskReminderSchedule.get_for_organisation(row.organisation)
		l1 = schedule.escalate_after_days_overdue if schedule else 1
		l2 = schedule.escalate_to_cxo_after_days if schedule else 7

		target_level = 0
		if row.days_overdue >= l2:
			target_level = 2
		elif row.days_overdue >= l1:
			target_level = 1

		if target_level <= (row.escalation_level or 0):
			continue

		updates = {"escalation_level": target_level}
		if target_level == 1:
			manager = frappe.db.get_value("User Profile", {"user": row.assigned_to}, "reports_to")
			if manager:
				updates["escalated_to"] = manager

		frappe.db.set_value("Compliance Calendar Task", row.name, updates)
		log_task_activity(
			task=row.name,
			activity_type="Escalated",
			from_value=str(row.escalation_level or 0),
			to_value=str(target_level),
			remarks=f"Auto-escalated at {row.days_overdue} day(s) overdue.",
			actor="Administrator",
		)
		escalated += 1

	return {"checked": len(overdue_tasks), "escalated": escalated}


def recompute_health_scores() -> dict:
	"""Recompute and cache each active organisation's health score."""
	orgs = frappe.get_all("Organisation", filters={"is_active": 1}, pluck="name")
	scores = {}
	for org in orgs:
		result = compute_health_score(organisation=org)
		frappe.cache().set_value(f"compliance_health_score:{org}", result, expires_in_sec=86400)
		scores[org] = result["score"]
	return scores


def _generate_recurring_for_frequencies(frequencies: list, horizon_months: int = 2) -> dict:
	"""Proactive backstop: for every (organisation, business_entity) that has
	ever had a task against a still-published obligation of one of these
	frequencies, ensure the next period's task exists — even if nobody
	submitted the current one (which is the only other trigger for
	create_next_recurring_instance). Never generates more than
	`horizon_months` ahead, and is idempotent via the same unique-key
	existence check used everywhere else in this module."""
	obligations = frappe.get_all(
		"Compliance Obligation", filters={"is_published": 1, "frequency": ("in", frequencies)}, pluck="name"
	)
	horizon = getdate(add_months(today(), horizon_months))
	created = 0

	for obligation_name in obligations:
		obligation_doc = frappe.get_cached_doc("Compliance Obligation", obligation_name)
		latest_rows = frappe.db.sql(
			"""
			SELECT organisation, business_entity, MAX(period_end) AS period_end
			FROM `tabCompliance Calendar Task`
			WHERE obligation = %(obligation)s AND docstatus != 2
			GROUP BY organisation, business_entity
			""",
			{"obligation": obligation_name},
			as_dict=True,
		)

		for row in latest_rows:
			next_start, next_end, next_due = compute_next_period(
				obligation_doc.frequency, row.period_end, obligation_doc.due_timing_rule
			)
			if getdate(next_start) > horizon:
				continue

			exists = frappe.db.exists(
				"Compliance Calendar Task",
				{"organisation": row.organisation, "business_entity": row.business_entity, "obligation": obligation_name, "period_start": next_start},
			)
			if exists:
				continue

			base_name = frappe.db.get_value(
				"Compliance Calendar Task",
				{"organisation": row.organisation, "business_entity": row.business_entity, "obligation": obligation_name, "period_end": row.period_end},
				"name",
			)
			if not base_name:
				continue

			next_task = frappe.copy_doc(frappe.get_doc("Compliance Calendar Task", base_name))
			next_task.period_start = next_start
			next_task.period_end = next_end
			next_task.due_date = next_due
			next_task.original_due_date = next_due
			for fieldname, value in _RECURRING_RESET_FIELDS.items():
				next_task.set(fieldname, value)
			next_task.insert(ignore_permissions=True)
			created += 1

	return {"created": created}


def generate_monthly_recurring_tasks() -> dict:
	return _generate_recurring_for_frequencies(["Monthly", "Bi-Monthly"])


def generate_quarterly_tasks() -> dict:
	return _generate_recurring_for_frequencies(["Quarterly", "Half-Yearly", "Annual", "Bi-Annual"], horizon_months=4)
