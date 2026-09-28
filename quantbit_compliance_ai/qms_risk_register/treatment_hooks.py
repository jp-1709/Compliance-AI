"""
quantbit_compliance_ai/qms_risk_register/treatment_hooks.py

Treatment Auto-Creation Hooks (Q6 spec §8) + bidirectional state sync (§8.2).

auto_create_downstream() is called from Risk Treatment.on_submit(). It is
idempotent (re-running on an already-created treatment is a no-op) and never
raises out of the submit transaction — a failure is recorded on the treatment
itself (downstream_creation_status="Failed") for a human to retry manually,
exactly as the spec requires ("Risk Manager review needed").

sync_from_capa() / sync_from_control() close the loop the other way: when the
downstream record's own state changes, the originating Risk Treatment is kept
in sync. sync_from_capa is registered in hooks.py against CAPA Case (a
different module); sync_from_control is called directly from Control's own
controller since Control lives in this module.
"""

import frappe
from frappe import _
from frappe.utils import add_days, now_datetime, today

# treatment_type -> downstream_doctype, used for the doc-model consistency
# check in Risk Treatment.validate() as well as here.
EXPECTED_DOWNSTREAM = {
	"CAPA": "CAPA Case",
	"Pursue (Opportunity)": "CAPA Case",
	"New Control": "Control",
	"Modify Existing Control": "Control",
	"Accept (No Treatment Beyond Existing Controls)": "Risk Acceptance",
}

_CAPA_SEVERITY_FROM_BAND = {
	"Critical": "Critical",
	"High": "High",
	"Medium": "Medium",
	"Low": "Low",
	"Very Low": "Low",
}


def _user_from_profile(user_profile: str | None) -> str | None:
	if not user_profile:
		return None
	return frappe.db.get_value("User Profile", user_profile, "user") or user_profile


def _append_json_link(doctype: str, name: str, fieldname: str, value: str):
	existing = frappe.parse_json(frappe.db.get_value(doctype, name, fieldname) or "[]")
	if value not in existing:
		existing.append(value)
		frappe.db.set_value(doctype, name, fieldname, frappe.as_json(existing), update_modified=False)


# ──────────────────────────────────────────────────────────────────────────────
# PER-TYPE HANDLERS
# ──────────────────────────────────────────────────────────────────────────────

def _create_capa(treatment) -> tuple:
	risk = frappe.get_doc("Risk Item", treatment.risk_item)
	severity = _CAPA_SEVERITY_FROM_BAND.get(risk.residual_band, "Medium")
	assignee = _user_from_profile(treatment.assigned_to)
	capa = frappe.get_doc(
		{
			"doctype": "CAPA Case",
			"title": treatment.title[:140] if treatment.title else f"Treatment for {risk.name}",
			"organisation": frappe.db.get_value("Business Entity", treatment.entity, "organisation"),
			"business_entity": treatment.entity,
			"capa_type": "Preventive" if treatment.treatment_type == "Pursue (Opportunity)" else "Corrective",
			"severity": severity,
			"priority": {"Critical": "P1", "High": "P2", "Medium": "P3", "Low": "P4"}.get(severity, "P3"),
			"source": "Risk Assessment",
			"source_reference": treatment.name,
			"problem_statement": treatment.description or treatment.rationale or risk.risk_description,
			"capa_owner": assignee,
			"qa_approver": assignee,
			"target_close_date": treatment.target_completion_date,
			"status": "Draft",
		}
	)
	capa.insert(ignore_permissions=True, ignore_mandatory=True)
	_append_json_link("Risk Item", risk.name, "linked_capas", capa.name)
	return "CAPA Case", capa.name


def _create_control(treatment) -> tuple:
	control = frappe.get_doc(
		{
			"doctype": "Control",
			"entity": treatment.entity,
			"title": treatment.title[:140] if treatment.title else f"Control for {treatment.risk_item}",
			"control_type": "Preventive",
			"control_category": "Manual",
			"control_frequency": "Quarterly",
			"description": treatment.description,
			"control_objective": treatment.rationale or treatment.description,
			"control_owner": treatment.assigned_to,
			"status": "Designed",
			"source_treatments": frappe.as_json([treatment.name]),
		}
	)
	control.insert(ignore_permissions=True, ignore_mandatory=True)
	return "Control", control.name


def _modify_existing_control(treatment) -> tuple | None:
	if not treatment.linked_existing_control:
		frappe.throw(_("Linked Existing Control is mandatory for a 'Modify Existing Control' treatment."))
	control = frappe.get_doc("Control", treatment.linked_existing_control)
	linked = frappe.parse_json(control.source_treatments or "[]")
	if treatment.name not in linked:
		linked.append(treatment.name)
	control.db_set("source_treatments", frappe.as_json(linked), update_modified=True)
	return "Control", control.name


def _create_acceptance(treatment) -> tuple:
	risk = frappe.get_doc("Risk Item", treatment.risk_item)
	appetite_rating = (
		frappe.db.get_value("Risk Appetite", risk.applicable_appetite, "max_tolerable_residual_rating")
		if risk.applicable_appetite
		else None
	)
	acceptance = frappe.get_doc(
		{
			"doctype": "Risk Acceptance",
			"risk_item": risk.name,
			"entity": treatment.entity,
			"residual_rating_at_acceptance": risk.residual_rating or 0,
			"applicable_appetite_at_acceptance": risk.applicable_appetite,
			"appetite_rating_at_acceptance": appetite_rating,
			"acceptance_rationale": treatment.rationale or treatment.description or "Drafted from treatment; Top Management to complete.",
			"alternative_treatments_considered": treatment.cost_benefit_rationale or "Pending Top Management input.",
			"residual_treatment_after_acceptance": treatment.description,
			"re_review_trigger_conditions": "Any material change to residual rating or control environment.",
			"effective_from": today(),
			"valid_until": add_days(today(), 180),
			"status": "Draft",
		}
	)
	acceptance.insert(ignore_permissions=True, ignore_mandatory=True)
	return "Risk Acceptance", acceptance.name


def _document_change(treatment) -> tuple | None:
	"""No Document Change Request DocType ships in this app's Q1 module (only
	QMS Document / Document Version exist, and a version needs an existing
	QMS Document link this treatment doesn't carry). Left Pending for a human
	to raise the Document Version manually via the Q1 API."""
	frappe.db.set_value(
		"Risk Treatment",
		treatment.name,
		"downstream_creation_attempt_log",
		frappe.as_json({"note": "Document Change treatments require manual linkage to a QMS Document; raise the revision via Q1's create_draft_version API.", "at": str(now_datetime())}),
	)
	return None


_HANDLERS = {
	"CAPA": _create_capa,
	"Pursue (Opportunity)": _create_capa,
	"New Control": _create_control,
	"Modify Existing Control": _modify_existing_control,
	"Accept (No Treatment Beyond Existing Controls)": _create_acceptance,
	"Document Change (Procedure / Policy / Work Instruction)": _document_change,
}

# treatment types with no downstream artefact by design (data lives on the
# treatment record itself): Insurance Cover, Outsource / Transfer
# Contractual, Avoid (Discontinue Activity) fall through to "Not Required".


def auto_create_downstream(treatment):
	if treatment.downstream_creation_status in ("Auto-Created", "Manually Created"):
		return  # idempotent

	handler = _HANDLERS.get(treatment.treatment_type)
	if not handler:
		frappe.db.set_value("Risk Treatment", treatment.name, "downstream_creation_status", "Not Required")
		return

	try:
		result = handler(treatment)
	except Exception as exc:
		frappe.log_error(title=f"Risk Treatment downstream auto-create failed: {treatment.name}", message=frappe.get_traceback())
		frappe.db.set_value(
			"Risk Treatment",
			treatment.name,
			{
				"downstream_creation_status": "Failed",
				"downstream_creation_attempt_log": frappe.as_json({"error": str(exc), "at": str(now_datetime())}),
			},
		)
		return

	if result:
		doctype, name = result
		frappe.db.set_value(
			"Risk Treatment", treatment.name, {"downstream_doctype": doctype, "downstream_record": name, "downstream_creation_status": "Auto-Created"}
		)
	else:
		frappe.db.set_value("Risk Treatment", treatment.name, "downstream_creation_status", "Pending")


# ──────────────────────────────────────────────────────────────────────────────
# BIDIRECTIONAL STATE SYNC (§8.2)
# ──────────────────────────────────────────────────────────────────────────────

def sync_from_capa(capa_doc, method=None):
	"""Registered in hooks.py against CAPA Case's on_update event."""
	if capa_doc.source != "Risk Assessment" or not capa_doc.source_reference:
		return
	treatment_status = frappe.db.get_value("Risk Treatment", capa_doc.source_reference, "status")
	if not treatment_status:
		return
	new_status = None
	if capa_doc.status == "Closed" and treatment_status not in ("Verified Effective", "Cancelled"):
		new_status = "Completed"
	elif capa_doc.status == "Voided" and treatment_status != "Cancelled":
		new_status = "Cancelled"
	if new_status and new_status != treatment_status:
		frappe.db.set_value("Risk Treatment", capa_doc.source_reference, "status", new_status)


def sync_from_control(control_doc):
	"""Called directly from Control.on_update() (same module)."""
	linked = frappe.parse_json(control_doc.source_treatments or "[]")
	for treatment_name in linked:
		current = frappe.db.get_value("Risk Treatment", treatment_name, "status")
		if not current or current in ("Cancelled",):
			continue
		if control_doc.status == "Tested-Effective" and current != "Verified Effective":
			frappe.db.set_value("Risk Treatment", treatment_name, "status", "Verified Effective")
		elif control_doc.status == "Tested-Not Effective" and current != "Re-treatment Required":
			frappe.db.set_value("Risk Treatment", treatment_name, "status", "Re-treatment Required")
