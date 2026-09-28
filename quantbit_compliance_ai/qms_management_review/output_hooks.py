"""
quantbit_compliance_ai/qms_management_review/output_hooks.py

Output Auto-Creation Hooks (Q5 spec §8) + bidirectional sync (§8.4) + loop
detection (§8.3) + the Minutes-Signed -> owner dispatch step (§4.7 on_submit).

Where the shipped schema has no clean downstream target for an output type
(Document Change / Policy Change need a QMS Document link this record never
carries; Quality Event isn't a shipped DocType in this app), the handler
leaves the output "Pending" with an explanatory note rather than silently
mis-mapping it — the same honesty rule applied in Q6's treatment_hooks.py.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime

EXPECTED_DOWNSTREAM = {
	"Open CAPA": "CAPA Case",
	"Open Audit": "Audit",
	"Risk Treatment": "Risk Item",
}


def _user_from_profile(user_profile: str | None) -> str | None:
	if not user_profile:
		return None
	return frappe.db.get_value("User Profile", user_profile, "user") or user_profile


# ──────────────────────────────────────────────────────────────────────────────
# PER-TYPE HANDLERS
# ──────────────────────────────────────────────────────────────────────────────

def _create_capa(output) -> tuple:
	assignee = _user_from_profile(output.assigned_to)
	capa = frappe.get_doc(
		{
			"doctype": "CAPA Case",
			"title": output.title[:140],
			"organisation": frappe.db.get_value("Business Entity", output.entity, "organisation"),
			"business_entity": output.entity,
			"capa_type": "Corrective",
			"severity": output.severity_classification or "Medium",
			"priority": {"Critical": "P1", "High": "P2", "Medium": "P3", "Low": "P4"}.get(output.severity_classification, "P3"),
			"source": "Management Review",
			"source_reference": output.name,
			"problem_statement": output.description or output.rationale,
			"capa_owner": assignee,
			"qa_approver": assignee,
			"target_close_date": output.target_completion_date,
			"status": "Draft",
		}
	)
	capa.insert(ignore_permissions=True, ignore_mandatory=True)
	return "CAPA Case", capa.name


def _create_risk(output):
	"""§8.1 'Risk Treatment' -> Risk Register Item. Reuses Q6's identification
	engine so dedupe/loop-warning behaviour is identical to a manually
	triggered create-from-event. Falls through to Pending if the entity has
	no Risk Category / Method Profile configured yet."""
	from quantbit_compliance_ai.qms_risk_register.identification_engine import create_risk_from_event

	category = frappe.db.get_value("Risk Category", {"is_archived": 0}, "name", order_by="creation asc")
	profile = frappe.db.get_value("Risk Method Profile", {"is_archived": 0}, "name", order_by="is_seed_data desc")
	if not category or not profile:
		return None

	assignee_email = _user_from_profile(output.assigned_to)
	owner_profile = frappe.db.get_value("User Profile", {"user": assignee_email}, "name") if assignee_email else None

	result = create_risk_from_event(
		"MR Output", output.name, output.entity, category, profile, owner_profile or output.assigned_to, {"title": output.title}
	)
	return "Risk Item", result["risk_item"]


def _create_audit(output) -> tuple:
	assignee = _user_from_profile(output.assigned_to)
	audit = frappe.get_doc(
		{
			"doctype": "Audit",
			"audit_title": output.title[:200],
			"organisation": frappe.db.get_value("Business Entity", output.entity, "organisation"),
			"audit_type": "For-Cause",
			"audit_classification": "Process (one process)",
			"lead_business_entity": output.entity,
			"auditee_party": "Self (internal)",
			"objectives": output.description or output.rationale or "Raised from Management Review.",
			"criteria": "Per Management Review output rationale.",
			"lead_auditor": assignee,
			"auditees_primary_contact": assignee,
			"planned_start_date": output.target_completion_date,
			"planned_end_date": output.target_completion_date,
			"status": "Planned",
		}
	)
	audit.insert(ignore_permissions=True, ignore_mandatory=True)
	return "Audit", audit.name


def _create_todo(output, note: str = "") -> tuple:
	assignee = _user_from_profile(output.assigned_to)
	todo = frappe.get_doc(
		{
			"doctype": "ToDo",
			"allocated_to": assignee,
			"description": (f"{output.title}\n\n{output.description or ''}\n{note}").strip(),
			"date": output.target_completion_date,
			"priority": {"Critical": "High", "High": "High"}.get(output.severity_classification, "Medium"),
			"reference_type": "MR Output",
			"reference_name": output.name,
		}
	)
	todo.insert(ignore_permissions=True, ignore_mandatory=True)
	return "ToDo", todo.name


def _no_clean_target(output, reason: str):
	frappe.db.set_value(
		"MR Output",
		output.name,
		"downstream_creation_attempt_log",
		frappe.as_json({"note": reason, "at": str(now_datetime())}),
	)
	return None


_HANDLERS = {
	"Open CAPA": _create_capa,
	"Risk Treatment": _create_risk,
	"Open Audit": _create_audit,
	"Training Initiative": lambda o: _create_todo(o),
	"Resource Allocation": lambda o: _create_todo(o, "Tag: MR Resource Need"),
	"Objective Revision": lambda o: _create_todo(o),
	"Communication Action": lambda o: _create_todo(o),
	"Stakeholder Engagement": lambda o: _create_todo(o),
	"Continual Improvement Project": lambda o: _create_todo(o),
	"Suspend / Withdraw / Recall Action": lambda o: _create_todo(
		o, "Recall/Suspend action — no Quality Event DocType ships in this app; tracked as a ToDo instead."
	),
	"Document Change": lambda o: _no_clean_target(o, "Document Change outputs need an existing QMS Document link this record doesn't carry; raise the revision manually via Q1's create_draft_version API."),
	"Policy Change": lambda o: _no_clean_target(o, "Policy Change outputs need an existing QMS Document link this record doesn't carry; raise the revision manually via Q1's create_draft_version API."),
	"Carry Forward to Next Cycle": lambda o: _no_clean_target(o, "Handled at cycle close, not at owner acceptance."),
}
# "Acknowledgement", "Strategic Direction", "Other" fall through to the
# default branch below: Not Required, auto-closed.


def _check_loop(output) -> list:
	chain = frappe.parse_json(output.get("source_chain") or "[]")
	if output.name in chain:
		frappe.throw(
			_("Loop detected: MR Output {0} already appears in its own downstream chain. Manual approval required to proceed.").format(output.name)
		)
	return chain + [output.name]


def on_mr_output_accepted(output):
	"""Called when an MR Output transitions to Owner Accepted. Idempotent."""
	if output.downstream_creation_status in ("Auto-Created", "Manually Created"):
		return

	handler = _HANDLERS.get(output.output_type)
	if not handler:
		frappe.db.set_value(
			"MR Output",
			output.name,
			{
				"downstream_creation_status": "Not Required",
				"status": "Closed" if not output.effectiveness_verification_required else "Verified Effective",
			},
		)
		return

	chain = _check_loop(output)
	try:
		result = handler(output)
	except Exception as exc:
		frappe.log_error(title=f"MR Output downstream auto-create failed: {output.name}", message=frappe.get_traceback())
		frappe.db.set_value(
			"MR Output",
			output.name,
			{"downstream_creation_status": "Failed", "downstream_creation_attempt_log": frappe.as_json({"error": str(exc), "at": str(now_datetime())})},
		)
		return

	if result:
		doctype, name = result
		frappe.db.set_value(
			"MR Output",
			output.name,
			{
				"downstream_doctype": doctype,
				"downstream_record": name,
				"downstream_creation_status": "Auto-Created",
				"status": "Downstream Created",
				"source_chain": frappe.as_json(chain),
			},
		)
	else:
		frappe.db.set_value("MR Output", output.name, "downstream_creation_status", "Pending")


# ──────────────────────────────────────────────────────────────────────────────
# BIDIRECTIONAL SYNC (§8.4)
# ──────────────────────────────────────────────────────────────────────────────

def sync_output_from_capa(capa_doc, method=None):
	"""Registered in hooks.py against CAPA Case's on_update event, alongside
	Q6's own sync_from_capa for treatment-sourced CAPAs."""
	if capa_doc.source != "Management Review" or not capa_doc.source_reference:
		return
	output_name = capa_doc.source_reference
	current = frappe.db.get_value("MR Output", output_name, "status")
	if not current:
		return
	new_status = None
	if capa_doc.status == "Closed" and current not in ("Verified Effective", "Closed"):
		new_status = "Verified Effective"
	elif capa_doc.status == "Voided" and current != "Cancelled":
		new_status = "Cancelled"
	if new_status:
		frappe.db.set_value("MR Output", output_name, "status", new_status)


# ──────────────────────────────────────────────────────────────────────────────
# MINUTES SIGNED -> OWNER DISPATCH (§4.7 on_submit)
# ──────────────────────────────────────────────────────────────────────────────

def dispatch_outputs_to_owners(cycle_name: str) -> int:
	outputs = frappe.get_all("MR Output", filters={"cycle": cycle_name, "status": "Captured in Meeting"}, pluck="name")
	for name in outputs:
		frappe.db.set_value("MR Output", name, "status", "Pending Owner Acceptance")
	return len(outputs)
