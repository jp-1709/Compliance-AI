"""
quantbit_compliance_ai/qms_capa/state_engine.py

CAPA Case state machine (Q2 spec §3) + the extension-escalation anti-pattern
guard (§5.5: "≥3 extensions triggers escalation... auto-CAPA-on-CAPA").

check_and_advance() writes only via frappe.db.set_value, so it is safe to
call reactively from CAPA Case.on_update() without any recursion risk — the
same pattern used for the equivalent engines in Q1/Q5/Q6.
"""

import frappe
from frappe import _
from frappe.utils import add_days, getdate, now_datetime, nowdate, today

_ALLOWED_TRANSITIONS = {
	"Draft": {"Pending Review", "Voided"},
	"Pending Review": {"In RCA", "Draft", "Voided"},
	"In RCA": {"Action Planning", "Voided"},
	"Action Planning": {"Plan Approved", "Voided"},
	"Plan Approved": {"In Progress", "Voided"},
	"In Progress": {"Pending Effectiveness", "Voided"},
	"Pending Effectiveness": {"Effectiveness Check", "Voided"},
	"Effectiveness Check": {"Closed", "Action Planning", "Voided"},
	"Closed": {"Reopened", "Voided"},
	"Reopened": {"In Progress", "Voided"},
	"Voided": set(),
}

MAX_REOPENS = 1
EXTENSION_ESCALATION_THRESHOLD = 3


def validate_transition(before_status: str, after_status: str):
	if before_status == after_status:
		return
	allowed = _ALLOWED_TRANSITIONS.get(before_status, set())
	if after_status not in allowed:
		frappe.throw(
			_("Cannot move CAPA Case from {0} to {1} directly. Allowed next states: {2}.").format(
				before_status, after_status, ", ".join(sorted(allowed)) or "(terminal)"
			)
		)


def check_and_advance(capa_name: str) -> bool:
	"""§3.3 auto-transitions. Returns True if a transition was applied."""
	capa = frappe.db.get_value("CAPA Case", capa_name, ["status", "effectiveness_check"], as_dict=True)
	if not capa:
		return False

	if capa.status == "In Progress":
		actions = frappe.get_all("CAPA Action", filters={"parent": capa_name, "parenttype": "CAPA Case"}, fields=["status"])
		active = [a for a in actions if a.status != "Cancelled"]
		if active and all(a.status == "Completed" for a in active):
			frappe.db.set_value("CAPA Case", capa_name, "status", "Pending Effectiveness")
			return True

	elif capa.status == "Pending Effectiveness" and capa.effectiveness_check:
		ec = frappe.db.get_value("Effectiveness Check", capa.effectiveness_check, ["earliest_check_date", "status"], as_dict=True)
		if ec and ec.earliest_check_date and getdate(nowdate()) >= getdate(ec.earliest_check_date) and ec.status != "Voided":
			frappe.db.set_value("CAPA Case", capa_name, "status", "Effectiveness Check")
			return True

	return False


def advance_states(business_entity: str | None = None) -> dict:
	"""Manual/administrative sweep (not cron-wired at this build depth)."""
	filters = {"status": ("in", ["In Progress", "Pending Effectiveness"])}
	if business_entity:
		filters["business_entity"] = business_entity
	names = frappe.get_all("CAPA Case", filters=filters, pluck="name")
	advanced = sum(1 for name in names if check_and_advance(name))
	return {"checked": len(names), "advanced": advanced}


def reopen_capa(capa_name: str, reason: str):
	capa = frappe.get_doc("CAPA Case", capa_name)
	if capa.status != "Closed":
		frappe.throw(_("Only a Closed CAPA can be reopened."))
	if (capa.reopen_count or 0) >= MAX_REOPENS:
		frappe.throw(
			_("This CAPA has already been reopened once. A second recurrence must be raised as a new CAPA, not reopened again.")
		)
	if not reason:
		frappe.throw(_("A reopen reason is required."))
	capa.status = "Reopened"
	capa.reopen_count = (capa.reopen_count or 0) + 1
	capa.flags.ignore_permissions = True
	capa.save()
	capa.add_comment("Info", _("Reopened: {0}").format(reason))
	return {"name": capa.name, "status": capa.status, "reopen_count": capa.reopen_count}


def extend_target_date(capa_name: str, new_date, reason: str) -> dict:
	if not reason:
		frappe.throw(_("An extension reason is required."))
	capa = frappe.get_doc("CAPA Case", capa_name)
	if getdate(new_date) <= getdate(capa.target_close_date):
		frappe.throw(_("The new target date must be later than the current target close date."))

	history = frappe.parse_json(capa.extension_history or "[]")
	history.append({"from": str(capa.target_close_date), "to": str(new_date), "reason": reason, "by": frappe.session.user, "at": str(now_datetime())})
	capa.extension_history = frappe.as_json(history)
	capa.target_close_date = new_date
	capa.due_date_extensions = (capa.due_date_extensions or 0) + 1
	capa.flags.ignore_permissions = True
	capa.save()

	if capa.due_date_extensions >= EXTENSION_ESCALATION_THRESHOLD:
		_raise_capa_on_capa(capa)

	return {"name": capa.name, "target_close_date": str(capa.target_close_date), "extensions_count": capa.due_date_extensions}


def _raise_capa_on_capa(capa):
	"""§5.5 anti-pattern guard: 3+ extensions is itself a process failure.
	Idempotent — only ever raises one self-correction CAPA per parent."""
	if frappe.db.exists("CAPA Case", {"source": "Other", "source_reference": capa.name}):
		return

	followup = frappe.get_doc(
		{
			"doctype": "CAPA Case",
			"title": f"Process failure: repeated date extensions on {capa.name}"[:140],
			"organisation": capa.organisation,
			"business_entity": capa.business_entity,
			"capa_type": "Corrective",
			"severity": "Medium",
			"priority": "P3",
			"source": "Other",
			"source_reference": capa.name,
			"problem_statement": (
				f"CAPA {capa.name} has been extended {capa.due_date_extensions} times, indicating a systemic "
				f"planning or resourcing failure in the CAPA process itself, not just this one case."
			),
			"capa_owner": capa.qa_approver,
			"qa_approver": capa.capa_owner,
			"target_close_date": add_days(today(), 90),
			"status": "Draft",
		}
	)
	followup.insert(ignore_permissions=True, ignore_mandatory=True)
	capa.add_comment("Info", _("3rd extension recorded; self-correction CAPA {0} was auto-drafted for QMS Manager review.").format(followup.name))
