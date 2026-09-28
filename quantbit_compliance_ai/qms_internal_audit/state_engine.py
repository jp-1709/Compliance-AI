"""
quantbit_compliance_ai/qms_internal_audit/state_engine.py

The 4 state machines from Q4 spec §3: Audit Programme, Audit, Audit
Checklist (already gated adequately in its own controller), and Audit
Finding (the most complex — see §3.4). Auto-transitions (§3.2) are exposed
as callables, not cron-wired at this build depth.
"""

import frappe
from frappe import _

PROGRAMME_TRANSITIONS = {
	"Draft": {"Pending Approval", "Cancelled"},
	"Pending Approval": {"Approved", "Draft", "Cancelled"},
	"Approved": {"Active", "Cancelled"},
	"Active": {"Completed"},
	"Completed": set(),
	"Cancelled": set(),
}

AUDIT_TRANSITIONS = {
	"Planned": {"Confirmed", "Cancelled"},
	"Confirmed": {"In Progress", "Cancelled"},
	"In Progress": {"Findings Issued", "Cancelled"},
	"Findings Issued": {"Report Issued"},
	"Report Issued": {"Closed"},
	"Closed": set(),
	"Cancelled": set(),
}

# §3.4. Two initial-approval branches, a dispute loop, a two-stage closure,
# and a single permitted reopen.
FINDING_TRANSITIONS = {
	"Draft": {"Pending Lead Approval", "Voided"},
	"Pending Lead Approval": {"Issued", "Draft", "Voided"},
	"Issued": {"Acknowledged", "Voided"},
	"Acknowledged": {"Disputed", "Action In Progress", "Voided"},
	"Disputed": {"Acknowledged", "Withdrawn", "Voided"},
	"Withdrawn": set(),
	"Action In Progress": {"Action Complete", "Voided"},
	"Action Complete": {"Closed", "Voided"},
	"Closed": {"Reopened"},
	"Reopened": {"Action In Progress", "Voided"},
	"Voided": set(),
}


def _validate(matrix: dict, before: str, after: str, doctype_label: str):
	if before == after:
		return
	allowed = matrix.get(before, set())
	if after not in allowed:
		frappe.throw(
			_("Cannot move {0} from {1} to {2} directly. Allowed next states: {3}.").format(
				doctype_label, before, after, ", ".join(sorted(allowed)) or "(terminal)"
			)
		)


def validate_programme_transition(before, after):
	_validate(PROGRAMME_TRANSITIONS, before, after, _("Audit Programme"))


def validate_audit_transition(before, after):
	_validate(AUDIT_TRANSITIONS, before, after, _("Audit"))


def validate_finding_transition(before, after):
	_validate(FINDING_TRANSITIONS, before, after, _("Audit Finding"))


# ──────────────────────────────────────────────────────────────────────────────
# AUTO-TRANSITIONS (§3.2) — callable now, cron-wireable later
# ──────────────────────────────────────────────────────────────────────────────

def check_and_close_audit(audit_name: str) -> bool:
	"""Report Issued -> Closed once every finding is Closed/Withdrawn/Voided."""
	status = frappe.db.get_value("Audit", audit_name, "status")
	if status != "Report Issued":
		return False
	open_findings = frappe.db.count("Audit Finding", {"audit": audit_name, "status": ("not in", ["Closed", "Withdrawn", "Voided"])})
	if open_findings == 0:
		frappe.db.set_value("Audit", audit_name, "status", "Closed")
		return True
	return False


def advance_audit_states(business_entity: str | None = None) -> dict:
	confirmed_filters = {"status": "Confirmed", "actual_start_date": ("is", "set")}
	report_issued_filters = {"status": "Report Issued"}
	if business_entity:
		confirmed_filters["lead_business_entity"] = business_entity
		report_issued_filters["lead_business_entity"] = business_entity

	confirmed = frappe.get_all("Audit", filters=confirmed_filters, pluck="name")
	for name in confirmed:
		frappe.db.set_value("Audit", name, "status", "In Progress")

	report_issued = frappe.get_all("Audit", filters=report_issued_filters, pluck="name")
	closed = sum(1 for name in report_issued if check_and_close_audit(name))

	return {"confirmed_to_in_progress": len(confirmed), "checked_for_closure": len(report_issued), "closed": closed}


def reopen_finding(finding_name: str, reason: str) -> dict:
	finding = frappe.get_doc("Audit Finding", finding_name)
	if finding.status != "Closed":
		frappe.throw(_("Only a Closed finding can be reopened."))
	if (finding.reopen_count or 0) >= 1:
		frappe.throw(_("This finding has already been reopened once. A second recurrence should be raised as a new finding on a follow-up audit."))
	if not reason:
		frappe.throw(_("A reopen reason is required."))
	finding.status = "Reopened"
	finding.reopen_count = (finding.reopen_count or 0) + 1
	finding.flags.ignore_permissions = True
	finding.save()
	finding.add_comment("Info", _("Reopened: {0}").format(reason))
	return {"name": finding.name, "status": finding.status, "reopen_count": finding.reopen_count}
