"""
quantbit_compliance_ai/qms_internal_audit/api.py

Whitelisted API surface for the Q4 Internal Audit module, at the same
"controllers + engines + key APIs" depth used for Q1/Q2/Q5/Q6: no reports/
dashboards/print-format/AI-hook/scheduled-task wiring, and no External
Auditor token-onboarding flow (that needs a distribution-token pattern this
build depth doesn't include — see Q1's Document Distribution Token for the
closest existing analogue if that is built out later).
"""

import frappe
from frappe import _
from frappe.utils import now_datetime, today

from quantbit_compliance_ai.qms_internal_audit.findings_engine import create_capa_for_finding, detect_repeat_finding, issue_audit_findings
from quantbit_compliance_ai.qms_internal_audit.state_engine import advance_audit_states, reopen_finding, validate_audit_transition, validate_finding_transition

MANAGER_ROLES = ("System Manager", "QMS Manager", "Audit Manager")


def _require_role(*roles):
	if not any(r in frappe.get_roles(frappe.session.user) for r in roles):
		frappe.throw(_("Permission denied. Required role(s): {0}.").format(", ".join(roles)), frappe.PermissionError)


# ──────────────────────────────────────────────────────────────────────────────
# AUDIT
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def list_audits(entity: str, status: str | None = None, lead_auditor: str | None = None, limit: int = 50, start: int = 0) -> dict:
	filters = {"lead_business_entity": entity}
	for key, value in (("status", status), ("lead_auditor", lead_auditor)):
		if value:
			filters[key] = value
	rows = frappe.get_all(
		"Audit",
		filters=filters,
		fields=["name", "audit_title", "audit_type", "status", "lead_auditor", "planned_start_date", "planned_end_date", "nc_major_count", "nc_minor_count"],
		order_by="planned_start_date asc",
		limit_page_length=limit,
		limit_start=start,
	)
	return {"items": rows, "total": frappe.db.count("Audit", filters=filters), "limit": limit, "start": start}


@frappe.whitelist()
def confirm_audit(name: str) -> dict:
	audit = frappe.get_doc("Audit", name)
	validate_audit_transition(audit.status, "Confirmed")
	if not audit.audit_team or not audit.auditees_primary_contact:
		frappe.throw(_("The audit team and auditee primary contact must be set before confirming."))
	audit.status = "Confirmed"
	audit.save()
	return {"name": audit.name, "status": audit.status}


@frappe.whitelist()
def start_audit(name: str, opening_meeting_minutes: str | None = None) -> dict:
	audit = frappe.get_doc("Audit", name)
	validate_audit_transition(audit.status, "In Progress")
	audit.status = "In Progress"
	audit.actual_start_date = today()
	audit.opening_meeting_date = now_datetime()
	if opening_meeting_minutes:
		audit.opening_meeting_minutes = opening_meeting_minutes
	audit.save()
	return {"name": audit.name, "status": audit.status, "actual_start_date": str(audit.actual_start_date)}


@frappe.whitelist()
def api_issue_audit_findings(name: str, no_findings_attestation: bool = False, attestation_text: str | None = None) -> dict:
	return issue_audit_findings(name, no_findings_attestation, attestation_text)


@frappe.whitelist()
def close_audit(name: str) -> dict:
	_require_role(*MANAGER_ROLES)
	audit = frappe.get_doc("Audit", name)
	validate_audit_transition(audit.status, "Closed")
	audit.status = "Closed"
	audit.save()
	return {"name": audit.name, "status": audit.status}


@frappe.whitelist()
def cancel_audit(name: str, reason: str) -> dict:
	_require_role(*MANAGER_ROLES)
	if len((reason or "").strip()) < 30:
		frappe.throw(_("Cancellation reason must contain at least 30 characters."))
	audit = frappe.get_doc("Audit", name)
	validate_audit_transition(audit.status, "Cancelled")
	audit.status = "Cancelled"
	audit.cancellation_reason = reason
	audit.save()
	return {"name": audit.name, "status": audit.status}


@frappe.whitelist()
def void_audit(name: str, reason: str) -> dict:
	_require_role("System Manager")
	if not reason:
		frappe.throw(_("A void reason is required."))
	frappe.db.set_value("Audit", name, {"is_voided": 1, "void_reason": reason, "voided_by": frappe.session.user, "voided_on": now_datetime()})
	return {"name": name, "is_voided": 1}


@frappe.whitelist()
def confirm_independence(audit_name: str, member_no: int, signature_payload: str | None = None) -> dict:
	audit = frappe.get_doc("Audit", audit_name)
	member = next((m for m in audit.audit_team if m.idx == int(member_no)), None)
	if not member:
		frappe.throw(_("Team member {0} not found.").format(member_no))
	if member.user and member.user != frappe.session.user:
		_require_role(*MANAGER_ROLES)
	member.independence_confirmed = 1
	member.independence_confirmed_at = now_datetime()
	audit.save()
	return {"success": True}


@frappe.whitelist()
def api_advance_audit_states(business_entity: str | None = None) -> dict:
	_require_role(*MANAGER_ROLES)
	return advance_audit_states(business_entity)


# ──────────────────────────────────────────────────────────────────────────────
# FINDINGS
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def create_finding(audit_name: str, payload: dict, source_checklist_item: str | None = None) -> dict:
	audit = frappe.db.get_value("Audit", audit_name, ["organisation", "business_entity"], as_dict=True)
	finding = frappe.get_doc(
		{
			"doctype": "Audit Finding",
			"organisation": audit.organisation,
			"business_entity": audit.business_entity,
			"audit": audit_name,
			"audit_checklist_item": source_checklist_item,
			"auditor_raised_by": frappe.session.user,
			"raised_at": now_datetime(),
			"status": "Draft",
			**payload,
		}
	)
	finding.insert()
	if source_checklist_item:
		frappe.db.set_value("Audit Checklist Item", {"name": source_checklist_item}, "linked_finding", finding.name)
	return {"name": finding.name}


@frappe.whitelist()
def submit_finding_for_lead_approval(name: str) -> dict:
	finding = frappe.get_doc("Audit Finding", name)
	validate_finding_transition(finding.status, "Pending Lead Approval")
	finding.status = "Pending Lead Approval"
	finding.save()
	return {"name": finding.name, "status": finding.status}


@frappe.whitelist()
def approve_finding(name: str, comment: str = "", signature_payload: str | None = None) -> dict:
	_require_role(*MANAGER_ROLES)
	finding = frappe.get_doc("Audit Finding", name)
	validate_finding_transition(finding.status, "Issued")
	finding.status = "Issued"
	finding.lead_auditor_approved = 1
	finding.lead_auditor_approved_at = now_datetime()
	if not finding.linked_quality_event:
		finding.linked_quality_event = "N/A — no Quality Event DocType is shipped in this app; see linked_capa."
	detect_repeat_finding(finding)
	finding.save()
	if finding.finding_type == "NC (Non-Conformity)" and finding.corrective_action_required:
		create_capa_for_finding(finding)
	if comment:
		finding.add_comment("Info", comment)
	return {"name": finding.name, "status": finding.status}


@frappe.whitelist()
def kick_back_finding(name: str, comment: str) -> dict:
	_require_role(*MANAGER_ROLES)
	if not comment:
		frappe.throw(_("A comment is required when kicking back a finding."))
	finding = frappe.get_doc("Audit Finding", name)
	validate_finding_transition(finding.status, "Draft")
	finding.status = "Draft"
	finding.save()
	finding.add_comment("Info", comment)
	return {"name": finding.name, "status": finding.status}


@frappe.whitelist()
def acknowledge_finding(name: str, response: str = "", signature_payload: str | None = None) -> dict:
	finding = frappe.get_doc("Audit Finding", name)
	validate_finding_transition(finding.status, "Acknowledged")
	finding.status = "Acknowledged"
	finding.auditee_acknowledged = 1
	finding.auditee_acknowledged_by = frappe.session.user
	finding.auditee_acknowledged_at = now_datetime()
	if response:
		finding.auditee_response = response
	finding.save()
	return {"name": finding.name, "status": finding.status}


@frappe.whitelist()
def dispute_finding(name: str, dispute_reason: str) -> dict:
	if not dispute_reason:
		frappe.throw(_("A dispute reason is required."))
	finding = frappe.get_doc("Audit Finding", name)
	validate_finding_transition(finding.status, "Disputed")
	finding.status = "Disputed"
	finding.auditee_disputes = 1
	finding.auditee_response = dispute_reason
	finding.save()
	return {"name": finding.name, "status": finding.status}


@frappe.whitelist()
def resolve_dispute(name: str, resolution: str, rationale: str) -> dict:
	_require_role(*MANAGER_ROLES)
	if resolution not in ("Sustained", "Modified", "Withdrawn (post-dispute outcome)"):
		frappe.throw(_("Invalid resolution."))
	if not rationale:
		frappe.throw(_("A resolution rationale is required."))
	finding = frappe.get_doc("Audit Finding", name)
	finding.dispute_resolution = resolution
	finding.dispute_resolution_rationale = rationale
	target_status = "Withdrawn" if resolution == "Withdrawn (post-dispute outcome)" else "Acknowledged"
	validate_finding_transition(finding.status, target_status)
	finding.status = target_status
	finding.save()
	return {"name": finding.name, "status": finding.status}


@frappe.whitelist()
def verify_finding_closure(name: str, summary: str, evidence_files: list | None = None, signature_payload: str | None = None) -> dict:
	_require_role(*MANAGER_ROLES)
	finding = frappe.get_doc("Audit Finding", name)
	if finding.status != "Action Complete":
		frappe.throw(_("Only a finding with status Action Complete can have its closure verified."))
	if frappe.session.user == finding.auditor_raised_by:
		frappe.throw(_("The finding's author cannot verify its own closure."))
	if finding.linked_capa:
		capa_owner, ec = frappe.db.get_value("CAPA Case", finding.linked_capa, ["capa_owner", "effectiveness_check"], as_dict=True) or (None, None)
		ec_verifier = frappe.db.get_value("Effectiveness Check", ec, "verifier") if ec else None
		if frappe.session.user in {capa_owner, ec_verifier}:
			frappe.throw(_("Closure verifier must differ from the CAPA Owner and Effectiveness Verifier."))
	finding.closure_verified_by = frappe.session.user
	finding.closure_verified_at = now_datetime()
	finding.closure_evidence_summary = summary
	if evidence_files:
		for ev in evidence_files:
			finding.append("evidence_files", ev)
	validate_finding_transition(finding.status, "Closed")
	finding.status = "Closed"
	finding.actual_close_date = today()
	finding.save()
	return {"name": finding.name, "status": finding.status, "audit_status_change": frappe.db.get_value("Audit", finding.audit, "status")}


@frappe.whitelist()
def api_reopen_finding(name: str, reason: str) -> dict:
	_require_role(*MANAGER_ROLES)
	return reopen_finding(name, reason)


@frappe.whitelist()
def void_finding(name: str, reason: str) -> dict:
	_require_role(*MANAGER_ROLES)
	if not reason:
		frappe.throw(_("A void reason is required."))
	finding = frappe.get_doc("Audit Finding", name)
	validate_finding_transition(finding.status, "Voided")
	finding.status = "Voided"
	finding.is_voided = 1
	finding.void_reason = reason
	finding.voided_by = frappe.session.user
	finding.voided_on = now_datetime()
	finding.save()
	return {"name": finding.name, "status": finding.status}


@frappe.whitelist()
def ai_find_similar_findings(name: str) -> list:
	"""Not actually AI — reuses the same difflib similarity search that runs
	automatically when a finding is approved (see detect_repeat_finding)."""
	finding = frappe.get_doc("Audit Finding", name)
	return detect_repeat_finding(finding, threshold=0.6)  # lower bar for exploratory browsing vs. the 0.75 auto-flag
