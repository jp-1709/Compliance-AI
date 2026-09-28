"""
quantbit_compliance_ai/qms_management_review/quorum_engine.py

Quorum Engine (Q5 spec §6). Checked at two points:
  - pre-meeting (informational)          -> check_quorum()
  - at the Pre-Meeting Check -> Started transition (blocking) -> start_meeting()
An override path exists for the rare case the meeting must proceed anyway,
requiring a distinct Top Management approver and leaving a permanent,
non-silent record in quorum_check_log (§6.2).
"""

import frappe
from frappe import _
from frappe.utils import now_datetime

_PRE_START_STATES = ("Scheduled", "Briefing Sent", "Briefing Acknowledged", "Pre-Meeting Check")


def _get_policy(cycle_name: str):
	policy_name = frappe.db.get_value("MR Cycle", cycle_name, "policy")
	return frappe.get_doc("MR Policy", policy_name) if policy_name else None


def check_quorum(meeting_name: str) -> dict:
	meeting = frappe.get_doc("MR Meeting", meeting_name)
	policy = _get_policy(meeting.cycle)

	attendees = [row for row in meeting.attendees or [] if row.attended]
	top_mgmt_present = sum(1 for row in attendees if row.is_top_management)
	total_present = len(attendees)
	chair_present = any(row.user == meeting.chairperson and row.attended for row in meeting.attendees or [])
	qm_present = any(row.user == meeting.secretariat and row.attended for row in meeting.attendees or [])
	process_owners_present = sum(1 for row in attendees if row.role_in_meeting == "Process Owner")

	top_mgmt_required = policy.quorum_top_mgmt_min if policy else 1
	total_required = policy.quorum_total_min if policy else 2
	chair_required = bool(policy.quorum_chair_required) if policy else True
	qm_required = bool(policy.quorum_quality_mgr_required) if policy else True
	po_required = policy.quorum_process_owner_min if policy else 0

	missing = []
	if top_mgmt_present < top_mgmt_required:
		missing.append(f"Top Management ({top_mgmt_present}/{top_mgmt_required})")
	if total_present < total_required:
		missing.append(f"Total attendance ({total_present}/{total_required})")
	if chair_required and not chair_present:
		missing.append("Chairperson")
	if qm_required and not qm_present:
		missing.append("Secretariat")
	if process_owners_present < po_required:
		missing.append(f"Process Owners ({process_owners_present}/{po_required})")

	return {
		"met": not missing,
		"top_mgmt_present": top_mgmt_present,
		"top_mgmt_required": top_mgmt_required,
		"total_present": total_present,
		"total_required": total_required,
		"chair_present": chair_present,
		"chair_required": chair_required,
		"qm_present": qm_present,
		"qm_required": qm_required,
		"process_owners_present": process_owners_present,
		"process_owners_required": po_required,
		"missing": missing,
		"checked_at": str(now_datetime()),
	}


def start_meeting(meeting_name: str) -> dict:
	meeting = frappe.get_doc("MR Meeting", meeting_name)
	if meeting.status not in _PRE_START_STATES:
		frappe.throw(_("Meeting must be at Pre-Meeting Check (or earlier) before it can start."))

	result = check_quorum(meeting_name)
	if not result["met"]:
		frappe.db.set_value("MR Meeting", meeting_name, "quorum_check_log", frappe.as_json(result))
		frappe.throw(
			_("Quorum not met: missing {0}. Use override_quorum() if this meeting must proceed regardless.").format(
				", ".join(result["missing"])
			)
		)

	meeting.quorum_check_log = frappe.as_json(result)
	meeting.quorum_met = 1
	meeting.status = "Started"
	meeting.actual_start_datetime = now_datetime()
	meeting.flags.ignore_permissions = True
	meeting.save()
	return result


def override_quorum(meeting_name: str, override_reason: str, approved_by: str) -> dict:
	meeting = frappe.get_doc("MR Meeting", meeting_name)
	if meeting.status not in _PRE_START_STATES:
		frappe.throw(_("Meeting must be at Pre-Meeting Check (or earlier) before it can start."))
	if not override_reason:
		frappe.throw(_("An override reason is required."))
	if approved_by == meeting.chairperson:
		frappe.throw(_("The quorum override approver must be a different Top Management member than the Chairperson."))

	result = check_quorum(meeting_name)
	result["override"] = {"reason": override_reason, "approved_by": approved_by, "at": str(now_datetime())}

	meeting.quorum_check_log = frappe.as_json(result)
	meeting.quorum_met = 1  # allowed to proceed, but the log makes the override permanently visible
	meeting.status = "Started"
	meeting.actual_start_datetime = now_datetime()
	meeting.flags.ignore_permissions = True
	meeting.save()
	meeting.add_comment(
		"Info", _("Quorum override approved by {0}: {1}. External auditor view must show this override, never silently pass.").format(approved_by, override_reason)
	)
	return result
