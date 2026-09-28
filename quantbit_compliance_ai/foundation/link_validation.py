import frappe
from frappe import _


def validate_scoped_links(doc, method=None):
	"""Prevent cross-organisation links on every tenant-scoped document.

	The check is deliberately metadata-driven so new compliance DocTypes inherit
	the invariant without having to duplicate controller code.
	"""
	if doc.doctype in {"Business Entity", "Organisation"}:
		return
	if not doc.meta.has_field("organisation") or not doc.meta.has_field("business_entity"):
		return
	if not doc.organisation or not doc.business_entity:
		return

	entity_organisation = frappe.db.get_value(
		"Business Entity", doc.business_entity, "organisation", cache=True
	)
	if entity_organisation and entity_organisation != doc.organisation:
		frappe.throw(
			_("Business Entity {0} belongs to Organisation {1}, not {2}.").format(
				frappe.bold(doc.business_entity),
				frappe.bold(entity_organisation),
				frappe.bold(doc.organisation),
			),
			frappe.LinkValidationError,
			title=_("Organisation Mismatch"),
		)
