"""
quantbit_compliance_ai/foundation/utils.py

Foundation spec §5 — the tenancy-scoping primitives every whitelisted API
method across every module (Q1-Q6, C1-C12) is expected to call. Several
controllers already reference `get_user_entities()` in their docstrings
(see User Profile) without it actually existing anywhere; this file is that
missing piece.
"""

import frappe
from frappe import _


def get_current_organisation() -> str:
	"""§5: the single source of truth for tenant scoping."""
	user = frappe.session.user
	if user == "Administrator":
		org = frappe.local.flags.get("organisation_override")
		if not org:
			frappe.throw(_("Administrator must specify organisation context."))
		return org
	org = frappe.db.get_value("User Profile", {"user": user}, "organisation")
	if not org:
		frappe.throw(_("User has no Organisation assigned. Contact your administrator."))
	return org


def get_user_entities(user: str | None = None) -> list:
	"""Returns the list of Business Entity names `user` can access."""
	user = user or frappe.session.user

	profile_name = frappe.db.get_value("User Profile", {"user": user}, "name")
	if not profile_name:
		if "System Manager" in frappe.get_roles(user):
			# Administrator / a System Manager with no User Profile of their own:
			# desk convenience, scoped to the override org if one is set.
			org = frappe.local.flags.get("organisation_override")
			filters = {"is_active": 1}
			if org:
				filters["organisation"] = org
			return frappe.get_all("Business Entity", filters=filters, pluck="name")
		return []

	profile = frappe.get_doc("User Profile", profile_name)
	if profile.all_entities_access:
		return frappe.get_all("Business Entity", filters={"organisation": profile.organisation, "is_active": 1}, pluck="name")
	return [row.business_entity for row in (profile.entity_access or [])]


def assert_entity_access(entity_name: str, user: str | None = None) -> None:
	"""Raise PermissionError if `user` cannot access `entity_name`."""
	user = user or frappe.session.user
	if "System Manager" in frappe.get_roles(user):
		return
	if entity_name not in get_user_entities(user):
		frappe.throw(_("Access denied to entity {0}").format(entity_name), frappe.PermissionError)


def assert_organisation_match(entity_name: str, organisation: str) -> None:
	"""Defence-in-depth companion to link_validation.validate_scoped_links —
	useful in API methods that receive both an entity and an organisation as
	separate arguments and must confirm they actually belong together before
	doing anything with them."""
	entity_org = frappe.db.get_value("Business Entity", entity_name, "organisation")
	if entity_org and entity_org != organisation:
		frappe.throw(
			_("Business Entity {0} belongs to Organisation {1}, not {2}.").format(entity_name, entity_org, organisation),
			frappe.PermissionError,
		)


def pick_task_reviewer(assignee: str) -> str:
	"""
	Compliance Calendar Task enforces maker-checker on Critical/High risk_level
	tasks: `reviewer` must be set and must differ from `assigned_to`. System-
	generated tasks (threshold warnings, renewal reminders, notice-response
	deadlines, etc., created directly by controllers across every compliance
	module) have no natural second human to name as reviewer, so this picks
	a safe distinct fallback: Administrator, or — if the assignee already is
	Administrator — the first other System Manager on the site.
	"""
	if assignee != "Administrator":
		return "Administrator"
	other = frappe.db.get_value(
		"Has Role", {"role": "System Manager", "parent": ["!=", "Administrator"]}, "parent"
	)
	return other or assignee
