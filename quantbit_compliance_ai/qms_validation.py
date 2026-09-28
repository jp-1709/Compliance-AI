import json

import frappe
from frappe import _


def text_length(value):
	return len((value or "").strip())


def as_list(value):
	"""Return JSON/child-table values as a list without making Code fields brittle."""
	if not value:
		return []
	if isinstance(value, (list, tuple)):
		return list(value)
	if isinstance(value, str):
		try:
			parsed = json.loads(value)
		except (TypeError, ValueError):
			return [part.strip() for part in value.split(",") if part.strip()]
		return parsed if isinstance(parsed, list) else [parsed]
	return [value]


def row_value(row, fieldname, default=None):
	if isinstance(row, dict):
		return row.get(fieldname, default)
	return getattr(row, fieldname, default)


def require_unique_rows(rows, fieldname, label):
	seen = set()
	for row in rows or []:
		value = row_value(row, fieldname)
		if value and value in seen:
			frappe.throw(_("{0} contains duplicate value {1}.").format(label, frappe.bold(value)))
		if value:
			seen.add(value)


def require_user_role(user, role, label=None):
	"""Validate a role when the selected identity resolves to a Frappe User.

	Some QMS fields link to the app's User Profile abstraction. Those profiles are
	not assumed to expose ERPNext fields; when the value is a User we enforce the
	standard Frappe role assignment directly.
	"""
	if not user or not frappe.db.exists("User", user):
		return
	roles = set(frappe.get_roles(user))
	if role not in roles and "System Manager" not in roles:
		frappe.throw(_("{0} must have the {1} role.").format(label or user, frappe.bold(role)))


def ensure_reference_exists(doctype, name, message=None):
	if doctype and name and not frappe.db.exists(doctype, name):
		frappe.throw(message or _("Referenced {0} {1} does not exist.").format(doctype, frappe.bold(name)))
