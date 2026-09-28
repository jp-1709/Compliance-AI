"""
quantbit_compliance_ai/qms_capa/effectiveness_hooks.py

Effectiveness Check -> CAPA Case state sync (Q2 spec §3.2 transitions:
Effectiveness Check -> Closed / -> Action Planning).

Writes via frappe.db.set_value only, called from EffectivenessCheck's own
on_update() — never raises out of that save.
"""

import frappe
from frappe import _
from frappe.utils import today


def sync_capa_from_ec(ec):
	if not ec.parent_capa:
		return
	if ec.outcome == "Effective":
		frappe.db.set_value("CAPA Case", ec.parent_capa, {"status": "Closed", "actual_close_date": today(), "effectiveness_check": ec.name})
	elif ec.outcome in ("Partially Effective", "Not Effective"):
		frappe.db.set_value("CAPA Case", ec.parent_capa, "status", "Action Planning")
		if ec.outcome == "Not Effective":
			severity = frappe.db.get_value("CAPA Case", ec.parent_capa, "severity")
			if severity in ("Critical", "High"):
				frappe.get_doc("CAPA Case", ec.parent_capa).add_comment(
					"Info",
					_(
						"Effectiveness Check {0} found this CAPA Not Effective. A follow-up CAPA is recommended "
						"(use create_followup_capa) — the system never auto-creates one; a human decides."
					).format(ec.name),
				)


@frappe.whitelist()
def create_followup_capa(ec_name: str) -> dict:
	"""§4.5 rule 7: 'the system automatically prompts to open a follow-up CAPA
	via UI (not auto-creates) — human decides.' This is that human decision,
	made explicit as a callable action rather than an automatic side effect."""
	ec = frappe.get_doc("Effectiveness Check", ec_name)
	if ec.outcome != "Not Effective":
		frappe.throw(_("A follow-up CAPA is only offered when the effectiveness outcome is Not Effective."))
	if ec.followup_capa:
		frappe.throw(_("A follow-up CAPA already exists: {0}").format(ec.followup_capa))

	parent = frappe.get_doc("CAPA Case", ec.parent_capa)
	followup = frappe.get_doc(
		{
			"doctype": "CAPA Case",
			"title": f"Follow-up to {parent.name}: prior treatment ineffective"[:140],
			"organisation": parent.organisation,
			"business_entity": parent.business_entity,
			"capa_type": parent.capa_type,
			"severity": parent.severity,
			"priority": parent.priority,
			"source": "Trend Analysis",
			"source_reference": parent.name,
			"problem_statement": f"Effectiveness Check {ec.name} found prior CAPA {parent.name} Not Effective. Rationale: {ec.outcome_rationale}",
			"capa_owner": parent.capa_owner,
			"qa_approver": parent.qa_approver,
			"target_close_date": frappe.utils.add_days(today(), 60),
			"status": "Draft",
		}
	)
	followup.insert(ignore_permissions=True, ignore_mandatory=True)
	frappe.db.set_value("Effectiveness Check", ec.name, "followup_capa", followup.name)
	return {"followup_capa": followup.name}
