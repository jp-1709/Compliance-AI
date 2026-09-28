"""
quantbit_compliance_ai/qms_risk_register/appetite_engine.py

Risk Appetite Resolution Engine (Q6 spec §9).

Resolution order for a Risk Item with category C:
  1. Active Category-Specific appetite for (entity, C).
  2. If none, cascade up C's parent_category chain (§9.2).
  3. If still none, the active Entity-Wide appetite.
  4. If still none, `applicable_appetite` stays unset and the risk is flagged
     `appetite_unset` via the returned warning.

A method-profile mismatch between the appetite and the risk (different rating
scales) is treated as "no usable appetite" rather than silently comparing
numbers that mean different things (§9.3).
"""

import frappe
from frappe import _


def _active_appetite(entity: str, scope: str, category: str | None):
	filters = {"entity": entity, "scope": scope, "status": "Approved", "effective_to": ["is", "not set"]}
	if scope == "Category-Specific":
		filters["risk_category"] = category
	name = frappe.db.get_value("Risk Appetite", filters, "name", order_by="appetite_version desc")
	if not name:
		return None
	return frappe.db.get_value(
		"Risk Appetite", name, ["name", "max_tolerable_residual_rating", "method_profile"], as_dict=True
	)


def _profile_scale(profile_name: str):
	if not profile_name:
		return None
	row = frappe.db.get_value(
		"Risk Method Profile",
		profile_name,
		["likelihood_scale_max", "impact_scale_max", "has_third_dimension", "third_dimension_scale_max"],
		as_dict=True,
	)
	if not row:
		return None
	scale = (row.likelihood_scale_max or 0) * (row.impact_scale_max or 0)
	if row.has_third_dimension:
		scale *= row.third_dimension_scale_max or 1
	return scale


def _method_compatible(appetite, risk_method_profile: str) -> bool:
	if not appetite.method_profile or appetite.method_profile == risk_method_profile:
		return True
	a_scale = _profile_scale(appetite.method_profile)
	b_scale = _profile_scale(risk_method_profile)
	return a_scale is not None and a_scale == b_scale


def resolve_appetite(entity: str, risk_category: str, risk_method_profile: str):
	"""Return (appetite_dict_or_None, warning_text_or_None)."""
	category = risk_category
	visited = set()
	while category and category not in visited:
		visited.add(category)
		appetite = _active_appetite(entity, "Category-Specific", category)
		if appetite:
			if _method_compatible(appetite, risk_method_profile):
				return appetite, None
			return None, _(
				"Appetite method for category {0} does not match this risk's method profile; "
				"Risk Manager must set a category-specific appetite using the same method."
			).format(category)
		category = frappe.db.get_value("Risk Category", category, "parent_category")

	appetite = _active_appetite(entity, "Entity-Wide", None)
	if appetite:
		if _method_compatible(appetite, risk_method_profile):
			return appetite, None
		return None, _("Entity-wide appetite method does not match this risk's method profile.")

	return None, _("No applicable Risk Appetite is configured for this entity or category.")


def apply_appetite_to_risk(risk_item) -> str | None:
	"""Mutate `risk_item` (in-memory doc, not yet saved) with the resolved
	applicable_appetite / within_appetite / acceptance_required fields.
	Returns a warning string if resolution failed, else None."""
	appetite, warning = resolve_appetite(risk_item.entity, risk_item.risk_category, risk_item.method_profile)

	policy = frappe.db.get_value(
		"Risk Policy", {"entity": risk_item.entity}, "acceptance_required_threshold", order_by="modified desc"
	)

	if appetite:
		risk_item.applicable_appetite = appetite.name
		risk_item.within_appetite = 1 if (risk_item.residual_rating or 0) <= appetite.max_tolerable_residual_rating else 0
	else:
		risk_item.applicable_appetite = None
		risk_item.within_appetite = 0

	needs_acceptance = bool(appetite) and not risk_item.within_appetite
	if policy and (risk_item.residual_rating or 0) >= policy:
		needs_acceptance = True
	risk_item.acceptance_required = 1 if needs_acceptance else 0

	return warning
