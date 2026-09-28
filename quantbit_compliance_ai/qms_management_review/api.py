"""
quantbit_compliance_ai/qms_management_review/api.py

Whitelisted API surface for the Q5 Management Review module, at the same
"controllers + engines + key APIs" depth used for Q1 and Q6: no reports/
dashboards/print-format/AI-hook/scheduled-task wiring here.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime

from quantbit_compliance_ai.qms_management_review.coverage_engine import compute_coverage
from quantbit_compliance_ai.qms_management_review.input_handlers import run_auto_population
from quantbit_compliance_ai.qms_management_review.output_hooks import dispatch_outputs_to_owners
from quantbit_compliance_ai.qms_management_review.quorum_engine import check_quorum, override_quorum, start_meeting

MANAGER_ROLES = ("System Manager", "MR Secretariat", "MR Chairperson")


def _require_role(*roles):
	user_roles = frappe.get_roles(frappe.session.user)
	if not any(r in user_roles for r in roles):
		frappe.throw(_("Permission denied. Required role(s): {0}.").format(", ".join(roles)), frappe.PermissionError)


def _user_from_profile(user_profile: str | None) -> str | None:
	if not user_profile:
		return None
	return frappe.db.get_value("User Profile", user_profile, "user") or user_profile


# ──────────────────────────────────────────────────────────────────────────────
# CYCLE / AUTO-POPULATION / COVERAGE
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def list_cycles(entity: str, status: str | None = None) -> list:
	filters = {"entity": entity}
	if status:
		filters["status"] = status
	return frappe.get_all(
		"MR Cycle", filters=filters, fields=["name", "cycle_label", "status", "target_meeting_date", "input_completeness_pct", "output_count", "output_closure_pct"], order_by="period_start desc"
	)


@frappe.whitelist()
def bulk_auto_pull_inputs(cycle_id: str) -> dict:
	_require_role(*MANAGER_ROLES)
	return run_auto_population(cycle_id)


@frappe.whitelist()
def get_coverage_status(cycle_id: str) -> dict:
	return compute_coverage(cycle_id)


@frappe.whitelist()
def acknowledge_coverage_gap(cycle_id: str, gap_acknowledgement_text: str) -> dict:
	_require_role(*MANAGER_ROLES)
	if not gap_acknowledgement_text:
		frappe.throw(_("Gap acknowledgement text is required."))
	minutes = frappe.db.get_value("MR Minutes", {"cycle": cycle_id}, "name", order_by="creation desc")
	if not minutes:
		frappe.throw(_("No MR Minutes draft exists yet for this cycle."))
	frappe.db.set_value("MR Minutes", minutes, "coverage_gaps_acknowledged", gap_acknowledgement_text)
	return {"minutes": minutes}


# ──────────────────────────────────────────────────────────────────────────────
# MEETING / QUORUM
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def api_check_quorum(meeting_id: str) -> dict:
	return check_quorum(meeting_id)


@frappe.whitelist()
def api_start_meeting(meeting_id: str) -> dict:
	chairperson = frappe.db.get_value("MR Meeting", meeting_id, "chairperson")
	is_chair = _user_from_profile(chairperson) == frappe.session.user
	if not is_chair:
		_require_role(*MANAGER_ROLES)
	return start_meeting(meeting_id)


@frappe.whitelist()
def api_override_quorum(meeting_id: str, override_reason: str, approved_by: str) -> dict:
	_require_role(*MANAGER_ROLES)
	return override_quorum(meeting_id, override_reason, approved_by)


# ──────────────────────────────────────────────────────────────────────────────
# OUTPUTS
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def list_outputs(filters: dict | None = None) -> list:
	filters = filters or {}
	return frappe.get_all(
		"MR Output",
		filters=filters,
		fields=["name", "title", "output_type", "severity_classification", "assigned_to", "status", "target_completion_date", "downstream_doctype", "downstream_record"],
		order_by="target_completion_date asc",
	)


@frappe.whitelist()
def get_output_action_tracker(entity: str, status_filter: str | None = None) -> list:
	filters = {"entity": entity}
	if status_filter:
		filters["status"] = status_filter
	return frappe.get_all(
		"MR Output",
		filters=filters,
		fields=["name", "title", "output_type", "severity_classification", "assigned_to", "status", "target_completion_date", "max_age_breach_date"],
		order_by="target_completion_date asc",
	)


def _assert_output_owner(output) -> None:
	owner_user = _user_from_profile(output.assigned_to)
	if frappe.session.user != owner_user and not any(r in frappe.get_roles(frappe.session.user) for r in MANAGER_ROLES):
		frappe.throw(_("Only the assigned owner (or a Secretariat/Chairperson) can act on this output."), frappe.PermissionError)


@frappe.whitelist()
def accept_output_assignment(output_id: str, signature: str | None = None) -> dict:
	output = frappe.get_doc("MR Output", output_id)
	if output.status != "Pending Owner Acceptance":
		frappe.throw(_("This output is not awaiting owner acceptance."))
	_assert_output_owner(output)
	output.owner_acceptance = 1
	output.owner_acceptance_date = now_datetime()
	output.status = "Owner Accepted"
	output.save()  # on_update() fires the Output Auto-Creation Hooks
	return {"name": output.name, "status": output.status, "downstream_creation_status": output.downstream_creation_status}


@frappe.whitelist()
def decline_output_assignment(output_id: str, reason: str) -> dict:
	output = frappe.get_doc("MR Output", output_id)
	if output.status != "Pending Owner Acceptance":
		frappe.throw(_("This output is not awaiting owner acceptance."))
	_assert_output_owner(output)
	if not reason:
		frappe.throw(_("A decline reason is required."))
	output.status = "Owner Declined"
	output.owner_decline_reason = reason
	output.save()
	return {"name": output.name, "status": output.status, "requires_reassignment": True}


@frappe.whitelist()
def reassign_output(output_id: str, new_owner: str) -> dict:
	_require_role(*MANAGER_ROLES)
	output = frappe.get_doc("MR Output", output_id)
	if output.status != "Owner Declined":
		frappe.throw(_("Only a Declined output can be reassigned."))
	output.assigned_to = new_owner
	output.owner_decline_reason = None
	output.status = "Pending Owner Acceptance"
	output.save()
	return {"name": output.name, "status": output.status, "assigned_to": output.assigned_to}


@frappe.whitelist()
def mark_output_completed(output_id: str, completion_summary: str) -> dict:
	output = frappe.get_doc("MR Output", output_id)
	_assert_output_owner(output)
	if output.status not in ("Downstream Created", "In Progress"):
		frappe.throw(_("Output must be In Progress before it can be marked Completed."))
	output.status = "Completed"
	output.impact_assessment = completion_summary
	output.save()
	return {"name": output.name, "status": output.status}


@frappe.whitelist()
def verify_output_effectiveness(output_id: str, verifier_id: str, evidence_id: str) -> dict:
	_require_role(*MANAGER_ROLES)
	output = frappe.get_doc("MR Output", output_id)
	if output.status != "Completed":
		frappe.throw(_("Output must be Completed before effectiveness can be verified."))
	if verifier_id == output.assigned_to:
		frappe.throw(_("The output owner cannot verify its own effectiveness."))
	output.effectiveness_verified_by = verifier_id
	output.effectiveness_verified_on = frappe.utils.today()
	output.effectiveness_evidence = evidence_id
	output.status = "Verified Effective"
	output.save()
	return {"name": output.name, "status": output.status}


@frappe.whitelist()
def carry_forward_output(output_id: str, target_cycle_id: str, reason: str) -> dict:
	_require_role(*MANAGER_ROLES)
	output = frappe.get_doc("MR Output", output_id)
	output.status = "Carried Forward"
	output.carry_forward_to_cycle = target_cycle_id
	output.save()

	target_cycle = frappe.db.get_value("MR Cycle", target_cycle_id, ["period_start", "period_end"], as_dict=True)
	standard_tag = output.applicable_standards[0] if output.applicable_standards else None
	carried = frappe.new_doc("MR Input Record")
	carried.cycle = target_cycle_id
	carried.entity = output.entity
	carried.input_category = "Status of Prior Actions"
	carried.source_type = "Carried from Prior Cycle"
	carried.source_module = "Q5 Prior Cycle"
	carried.source_period_start = target_cycle.period_start if target_cycle else frappe.utils.today()
	carried.source_period_end = target_cycle.period_end if target_cycle else frappe.utils.today()
	carried.contributed_by = frappe.db.get_value("User Profile", {"user": frappe.session.user}, "name")
	carried.title = f"Carried forward: {output.title}"[:200]
	carried.data_summary = f"Carried forward from {output.cycle} ({output.name}): {reason}"
	carried.reason_for_carry = reason
	carried.status = "Draft"
	carried.append(
		"applicable_standards",
		{"standard": standard_tag.standard if standard_tag else "General", "clause": standard_tag.clause if standard_tag else None, "mandatory_for_clause": 0},
	)
	if not carried.contributed_by:
		carried.flags.ignore_mandatory = True
	carried.insert(ignore_permissions=True)
	return {"name": output.name, "status": output.status, "target_cycle": target_cycle_id, "carried_input": carried.name}


@frappe.whitelist()
def cancel_output(output_id: str, reason: str) -> dict:
	_require_role(*MANAGER_ROLES)
	output = frappe.get_doc("MR Output", output_id)
	if not reason:
		frappe.throw(_("A cancellation reason is required."))
	output.status = "Cancelled"
	output.closure_summary = reason
	output.save()
	return {"name": output.name, "status": output.status}


@frappe.whitelist()
def api_dispatch_outputs_to_owners(cycle_id: str) -> dict:
	_require_role(*MANAGER_ROLES)
	count = dispatch_outputs_to_owners(cycle_id)
	return {"cycle": cycle_id, "dispatched": count}
