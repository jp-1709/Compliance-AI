"""
quantbit_compliance_ai/foundation/permissions.py

Foundation spec §5 — row-level tenancy enforcement, registered via Frappe's
`permission_query_conditions` / `has_permission` hooks.

Frappe calls a permission_query_conditions function with only `(user)` — the
target doctype is never passed in, so one truly doctype-agnostic function
isn't possible the way `doc_events["*"]` is for document hooks. Instead this
module exposes a *factory*: `org_scoped_query_conditions(doctype, org_field)`
returns a closure for that one doctype, so adding tenancy isolation to a new
DocType is a one-line addition in hooks.py, not a new function.

Wired here for the two DocTypes Foundation itself owns that need row-level
scoping (Business Entity, Evidence File). Each of the QMS/Compliance modules
built on top of Foundation should register its own list-worthy DocTypes the
same way as they're finalised — that is a per-module follow-up, not something
this pass silently retrofits across the other five already-shipped modules.
"""

import frappe


def org_scoped_query_conditions(doctype: str, org_field: str = "organisation"):
	"""Factory: returns a permission_query_conditions callable for `doctype`,
	scoping rows to the calling user's Organisation via `org_field`."""

	def _condition(user: str) -> str:
		if "System Manager" in frappe.get_roles(user):
			return ""
		org = frappe.db.get_value("User Profile", {"user": user}, "organisation")
		if not org:
			return "1=0"
		return f"`tab{doctype}`.`{org_field}` = {frappe.db.escape(org)}"

	return _condition


def org_scoped_has_permission(org_field: str = "organisation"):
	"""Factory: returns a has_permission callable enforcing the same rule at
	the single-document level (defence in depth alongside the query condition
	above, and the only thing that guards a direct `frappe.get_doc` load)."""

	def _has_permission(doc, ptype=None, user=None) -> bool:
		user = user or frappe.session.user
		if "System Manager" in frappe.get_roles(user):
			return True
		org = frappe.db.get_value("User Profile", {"user": user}, "organisation")
		return bool(org) and doc.get(org_field) == org

	return _has_permission


# Pre-built closures for hooks.py — see permission_query_conditions / has_permission there.
get_business_entity_query_conditions = org_scoped_query_conditions("Business Entity", "organisation")
has_business_entity_permission = org_scoped_has_permission("organisation")

get_evidence_file_query_conditions = org_scoped_query_conditions("Evidence File", "organisation")
has_evidence_file_permission = org_scoped_has_permission("organisation")
