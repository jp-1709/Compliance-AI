"""
quantbit_compliance_ai/foundation/api.py

Whitelisted API surface for Foundation (spec §8), at the same "controllers +
engines + key APIs" depth used for every other module in this build: no
reports/dashboards/print-format/scheduled-task wiring here (the evidence
expiry jobs already exist as plain functions in evidence_file.py but are not
registered into hooks.py `scheduler_events`, consistent with that same choice
made for every other module).
"""

import frappe
from frappe import _

from quantbit_compliance_ai.foundation.utils import assert_entity_access, get_current_organisation, get_user_entities


@frappe.whitelist()
def get_organisation_summary(organisation: str | None = None) -> dict:
	org_name = organisation or get_current_organisation()
	org = frappe.get_doc("Organisation", org_name)
	entity_count = frappe.db.count("Business Entity", {"organisation": org_name, "is_active": 1})
	user_count = frappe.db.count("User Profile", {"organisation": org_name})
	return {
		"organisation": org.name,
		"organisation_name": org.organisation_name,
		"is_active": org.is_active,
		"subscription_plan": org.subscription_plan,
		"is_subscription_valid": org.is_subscription_valid(),
		"days_until_subscription_expires": org.days_until_subscription_expires(),
		"entity_count": entity_count,
		"user_count": user_count,
		"max_users": org.max_users,
		"max_entities": org.max_entities,
	}


@frappe.whitelist()
def list_my_entities() -> list:
	entities = get_user_entities()
	if not entities:
		return []
	return frappe.get_all(
		"Business Entity",
		filters={"name": ("in", entities)},
		fields=["name", "entity_name", "entity_type", "state", "is_principal_entity", "is_active"],
		order_by="entity_name asc",
	)


@frappe.whitelist()
def upload_evidence(business_entity: str, file_url: str, evidence_type: str, title: str, **kwargs) -> dict:
	assert_entity_access(business_entity)
	organisation = kwargs.pop("organisation", None) or frappe.db.get_value("Business Entity", business_entity, "organisation")
	evidence = frappe.get_doc(
		{
			"doctype": "Evidence File",
			"organisation": organisation,
			"business_entity": business_entity,
			"evidence_title": title,
			"evidence_type": evidence_type,
			"file": file_url,
			**kwargs,
		}
	)
	evidence.insert()
	return {"name": evidence.name, "sha256_hash": evidence.sha256_hash, "is_expired": evidence.is_expired}


@frappe.whitelist()
def link_evidence(evidence_file: str, linked_doctype: str, linked_name: str, link_purpose: str = "Primary Evidence") -> dict:
	if not frappe.has_permission("Evidence File", "write", evidence_file):
		frappe.throw(_("Insufficient permissions on the Evidence File."), frappe.PermissionError)
	if not frappe.has_permission(linked_doctype, "read", linked_name):
		frappe.throw(_("Insufficient permissions on {0} {1}.").format(linked_doctype, linked_name), frappe.PermissionError)
	doc = frappe.get_doc("Evidence File", evidence_file)
	doc.link_to_record(linked_doctype, linked_name, link_purpose)
	return {"success": True, "evidence_file": evidence_file, "linked_doctype": linked_doctype, "linked_name": linked_name}


@frappe.whitelist()
def search_iso_clauses(query: str, iso_standard: str | None = None) -> list:
	from quantbit_compliance_ai.foundation.doctype.iso_clause.iso_clause import ISOClause

	return ISOClause.search_clauses(query, iso_standard)


@frappe.whitelist()
def search_regulations(query: str, regulator: str | None = None, jurisdiction: str | None = None, state: str | None = None) -> list:
	filters = {"is_active": 1, "extraction_status": "Published"}
	if regulator:
		filters["regulator"] = regulator
	if jurisdiction:
		filters["jurisdiction"] = jurisdiction

	regulations = frappe.get_all(
		"Regulation",
		filters=filters,
		fields=["name", "regulation_title", "short_title", "regulation_type", "regulator", "jurisdiction", "effective_date"],
		or_filters={
			"regulation_title": ("like", f"%{query}%"),
			"short_title": ("like", f"%{query}%"),
			"summary": ("like", f"%{query}%"),
		},
		order_by="short_title asc",
		limit=50,
	)

	if state:
		# Central regs apply everywhere; State regs only if this state is tagged.
		state_tagged = set(
			frappe.get_all("Regulation State", filters={"state": state}, pluck="parent")
		)
		regulations = [r for r in regulations if r.jurisdiction != "State" or r.name in state_tagged]

	return regulations


@frappe.whitelist()
def get_user_profile() -> dict:
	profile_name = frappe.db.get_value("User Profile", {"user": frappe.session.user}, "name")
	if not profile_name:
		frappe.throw(_("No User Profile found for the current user."))
	profile = frappe.get_doc("User Profile", profile_name)
	return {
		"user": profile.user,
		"organisation": profile.organisation,
		"primary_persona": profile.primary_persona,
		"designation": profile.designation,
		"department": profile.department,
		"accessible_entities": profile.get_accessible_entities(),
		"notification_channels": profile.get_notification_channels(),
	}


@frappe.whitelist()
def update_notification_preferences(email: bool, whatsapp: bool, inapp: bool) -> dict:
	profile_name = frappe.db.get_value("User Profile", {"user": frappe.session.user}, "name")
	if not profile_name:
		frappe.throw(_("No User Profile found for the current user."))
	frappe.db.set_value(
		"User Profile",
		profile_name,
		{
			"notification_pref_email": 1 if frappe.utils.cint(email) else 0,
			"notification_pref_whatsapp": 1 if frappe.utils.cint(whatsapp) else 0,
			"notification_pref_inapp": 1 if frappe.utils.cint(inapp) else 0,
		},
	)
	return {"success": True}
