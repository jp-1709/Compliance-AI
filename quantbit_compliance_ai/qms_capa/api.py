"""
quantbit_compliance_ai/qms_capa/api.py

Whitelisted API surface for the Q2 CAPA module, at the same "controllers +
engines + key APIs" depth used for Q1/Q5/Q6: no reports/dashboards/print-
format/AI-hook/scheduled-task wiring here.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime, today

from quantbit_compliance_ai.qms_capa.effectiveness_hooks import create_followup_capa  # noqa: F401 (re-exported)
from quantbit_compliance_ai.qms_capa.recurrence_engine import find_similar_capas
from quantbit_compliance_ai.qms_capa.state_engine import advance_states, extend_target_date, reopen_capa, validate_transition

MANAGER_ROLES = ("System Manager", "QMS Manager")


def _require_role(*roles):
	if not any(r in frappe.get_roles(frappe.session.user) for r in roles):
		frappe.throw(_("Permission denied. Required role(s): {0}.").format(", ".join(roles)), frappe.PermissionError)


# ──────────────────────────────────────────────────────────────────────────────
# CAPA CASE
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def list_capas(entity: str, status: str | None = None, severity: str | None = None, capa_owner: str | None = None, limit: int = 50, start: int = 0) -> dict:
	filters = {"business_entity": entity}
	for key, value in (("status", status), ("severity", severity), ("capa_owner", capa_owner)):
		if value:
			filters[key] = value
	rows = frappe.get_all(
		"CAPA Case",
		filters=filters,
		fields=["name", "title", "status", "severity", "priority", "capa_owner", "target_close_date", "is_recurring_issue", "due_date_extensions"],
		order_by="target_close_date asc",
		limit_page_length=limit,
		limit_start=start,
	)
	return {"items": rows, "total": frappe.db.count("CAPA Case", filters=filters), "limit": limit, "start": start}


@frappe.whitelist()
def get_capa(name: str) -> dict:
	capa = frappe.get_doc("CAPA Case", name)
	return capa.as_dict()


@frappe.whitelist()
def transition_status(name: str, target_status: str, comment: str = "") -> dict:
	capa = frappe.get_doc("CAPA Case", name)
	validate_transition(capa.status, target_status)
	capa.status = target_status
	capa.save()
	if comment:
		capa.add_comment("Info", comment)
	return {"name": capa.name, "status": capa.status}


@frappe.whitelist()
def api_extend_target_date(name: str, new_date: str, reason: str) -> dict:
	return extend_target_date(name, new_date, reason)


@frappe.whitelist()
def api_reopen_capa(name: str, reason: str) -> dict:
	_require_role(*MANAGER_ROLES)
	return reopen_capa(name, reason)


@frappe.whitelist()
def void_capa(name: str, reason: str) -> dict:
	_require_role("System Manager")
	if not reason:
		frappe.throw(_("A void reason is required."))
	capa = frappe.get_doc("CAPA Case", name)
	validate_transition(capa.status, "Voided")
	capa.is_voided = 1
	capa.void_reason = reason
	capa.voided_by = frappe.session.user
	capa.voided_on = now_datetime()
	capa.status = "Voided"
	capa.save()
	return {"name": capa.name, "status": capa.status}


@frappe.whitelist()
def ai_find_similar(name: str) -> list:
	"""Not actually AI — reuses the same difflib-based similarity search that
	runs automatically on Draft/Pending Review saves (§10.6 in the spec calls
	this a pure-embedding lookup with no LLM call; this app's stand-in is the
	same recurrence_engine used there)."""
	capa = frappe.db.get_value("CAPA Case", name, ["business_entity", "problem_statement"], as_dict=True)
	return find_similar_capas(capa.business_entity, capa.problem_statement, exclude=name)


@frappe.whitelist()
def api_advance_states(business_entity: str | None = None) -> dict:
	_require_role(*MANAGER_ROLES)
	return advance_states(business_entity)


# ──────────────────────────────────────────────────────────────────────────────
# CAPA ACTION (child table)
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def add_action(capa_name: str, action_payload: dict) -> dict:
	capa = frappe.get_doc("CAPA Case", capa_name)
	capa.append("actions", action_payload)
	capa.save()
	return {"action_no": capa.actions[-1].idx}


@frappe.whitelist()
def mark_action_complete(capa_name: str, action_no: int, evidence_files: list | None = None) -> dict:
	capa = frappe.get_doc("CAPA Case", capa_name)
	row = next((a for a in capa.actions if a.idx == int(action_no)), None)
	if not row:
		frappe.throw(_("Action {0} not found on {1}.").format(action_no, capa_name))
	row.status = "Completed"
	row.actual_completion_date = today()
	if evidence_files:
		row.evidence_files = frappe.as_json(evidence_files)
	capa.save()  # on_update() runs check_and_advance -> may auto-move to Pending Effectiveness
	return {"success": True, "capa_status": frappe.db.get_value("CAPA Case", capa_name, "status")}


@frappe.whitelist()
def mark_action_blocked(capa_name: str, action_no: int, reason: str) -> dict:
	if not reason:
		frappe.throw(_("A blocking reason is required."))
	capa = frappe.get_doc("CAPA Case", capa_name)
	row = next((a for a in capa.actions if a.idx == int(action_no)), None)
	if not row:
		frappe.throw(_("Action {0} not found on {1}.").format(action_no, capa_name))
	row.status = "Blocked"
	row.blocking_reason = reason
	capa.save()
	return {"success": True}


@frappe.whitelist()
def reassign_action(capa_name: str, action_no: int, new_owner: str) -> dict:
	_require_role(*MANAGER_ROLES)
	capa = frappe.get_doc("CAPA Case", capa_name)
	row = next((a for a in capa.actions if a.idx == int(action_no)), None)
	if not row:
		frappe.throw(_("Action {0} not found on {1}.").format(action_no, capa_name))
	row.owner = new_owner
	capa.save()
	return {"success": True}


# ──────────────────────────────────────────────────────────────────────────────
# RCA
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def create_rca(capa_name: str, methods: list, analyst: str, problem_definition: str) -> dict:
	capa = frappe.db.get_value("CAPA Case", capa_name, ["organisation", "business_entity"], as_dict=True)
	rca = frappe.get_doc(
		{
			"doctype": "RCA Record",
			"organisation": capa.organisation,
			"business_entity": capa.business_entity,
			"parent_capa": capa_name,
			"analyst": analyst,
			"methods_used": frappe.as_json(methods),
			"problem_definition": problem_definition,
			"identified_root_causes": "Pending analysis.",
			"status": "Draft",
		}
	)
	rca.flags.ignore_mandatory = True
	rca.insert()
	frappe.db.set_value("CAPA Case", capa_name, "rca_record", rca.name)
	return {"rca_name": rca.name}


@frappe.whitelist()
def submit_rca_for_review(rca_name: str) -> dict:
	rca = frappe.get_doc("RCA Record", rca_name)
	if rca.status != "Draft" and rca.status != "In Analysis":
		frappe.throw(_("RCA must be Draft or In Analysis to submit for review."))
	rca.status = "Complete"
	rca.save()
	return {"name": rca.name, "status": rca.status}


@frappe.whitelist()
def approve_rca(rca_name: str, comment: str = "") -> dict:
	_require_role(*MANAGER_ROLES)
	rca = frappe.get_doc("RCA Record", rca_name)
	if rca.status != "Complete":
		frappe.throw(_("Only a Complete RCA can be approved."))
	rca.status = "Reviewed"
	rca.reviewed_by = frappe.session.user
	rca.reviewed_on = now_datetime()
	rca.save()
	if comment:
		rca.add_comment("Info", comment)
	return {"name": rca.name, "status": rca.status}


@frappe.whitelist()
def reject_rca(rca_name: str, reason: str) -> dict:
	_require_role(*MANAGER_ROLES)
	if not reason:
		frappe.throw(_("A rejection reason is required."))
	rca = frappe.get_doc("RCA Record", rca_name)
	rca.status = "Draft"
	rca.save()
	rca.add_comment("Info", _("Sent back for revision: {0}").format(reason))
	return {"name": rca.name, "status": rca.status}


# ──────────────────────────────────────────────────────────────────────────────
# EFFECTIVENESS CHECK
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def create_effectiveness_check(capa_name: str, verifier: str, criteria: str, window_days: int, method: str) -> dict:
	capa = frappe.get_doc("CAPA Case", capa_name)
	if verifier in {capa.capa_owner, capa.qa_approver}:
		frappe.throw(_("Effectiveness Verifier must differ from CAPA Owner and QA Approver."))
	last_completion = max((a.actual_completion_date for a in capa.actions if a.actual_completion_date), default=today())
	earliest = frappe.utils.add_days(last_completion, int(window_days))
	ec = frappe.get_doc(
		{
			"doctype": "Effectiveness Check",
			"organisation": capa.organisation,
			"business_entity": capa.business_entity,
			"parent_capa": capa_name,
			"verifier": verifier,
			"verification_method": method,
			"effectiveness_criteria": criteria,
			"verification_window_days": window_days,
			"earliest_check_date": earliest,
			"status": "Scheduled",
		}
	)
	ec.insert()
	frappe.db.set_value("CAPA Case", capa_name, "effectiveness_check", ec.name)
	return {"ec_name": ec.name, "earliest_check_date": str(earliest)}


@frappe.whitelist()
def record_ec_outcome(ec_name: str, outcome: str, rationale: str, evidence_files: list | None = None) -> dict:
	ec = frappe.get_doc("Effectiveness Check", ec_name)
	if frappe.session.user != ec.verifier:
		_require_role(*MANAGER_ROLES)
	ec.outcome = outcome
	ec.outcome_rationale = rationale
	ec.actual_check_date = today()
	if evidence_files:
		ec.evidence_files = frappe.as_json(evidence_files)
	ec.status = "Verified"
	ec.save()  # on_update() syncs the parent CAPA via effectiveness_hooks
	return {"name": ec.name, "status": ec.status, "capa_status": frappe.db.get_value("CAPA Case", ec.parent_capa, "status")}


@frappe.whitelist()
def reschedule_ec(ec_name: str, new_date: str, reason: str) -> dict:
	_require_role(*MANAGER_ROLES)
	if not reason:
		frappe.throw(_("A reschedule reason is required."))
	ec = frappe.get_doc("Effectiveness Check", ec_name)
	ec.earliest_check_date = new_date
	ec.save()
	ec.add_comment("Info", reason)
	return {"name": ec.name, "earliest_check_date": str(ec.earliest_check_date)}
