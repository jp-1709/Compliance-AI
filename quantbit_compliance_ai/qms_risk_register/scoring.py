import json

import frappe
from frappe import _


def get_method_profile(name):
	fields = [
		"likelihood_scale_max", "impact_scale_max", "has_third_dimension",
		"third_dimension_scale_max", "composition_formula", "composition_weights",
		"is_infosec_cia", "cia_impact_strategy",
	]
	return frappe.db.get_value("Risk Method Profile", name, fields, as_dict=True)


def validate_and_score(doc, profile, prefix):
	likelihood = doc.get(f"{prefix}_likelihood")
	impact = doc.get(f"{prefix}_impact")
	third = doc.get(f"{prefix}_third_dim")
	cia = [doc.get(f"{prefix}_cia_{key}") for key in ("c", "i", "a")]

	if profile.is_infosec_cia:
		if any(value is None for value in cia):
			frappe.throw(_("All {0} CIA scores are required for an InfoSec method.").format(prefix.title()))
		strategy = profile.cia_impact_strategy
		impact = {"Max of CIA": max, "Sum of CIA": sum}.get(strategy, lambda values: sum(values) / len(values))(cia)
		doc.set(f"{prefix}_impact", impact)

	_validate_scale(likelihood, profile.likelihood_scale_max, _("{0} Likelihood").format(prefix.title()))
	_validate_scale(impact, profile.impact_scale_max, _("{0} Impact").format(prefix.title()))
	if profile.has_third_dimension:
		_validate_scale(third, profile.third_dimension_scale_max, _("{0} Third Dimension").format(prefix.title()))

	values = [value for value in (likelihood, impact, third if profile.has_third_dimension else None) if value is not None]
	if len(values) < (3 if profile.has_third_dimension else 2):
		return None
	formula = profile.composition_formula
	if formula in {"Multiply", "Multiply-Divide-Detectability"}:
		rating = likelihood * impact
		if profile.has_third_dimension:
			rating = rating * third if formula == "Multiply" else rating / third
	elif formula == "Add":
		rating = sum(values)
	elif formula == "Weighted":
		weights = json.loads(profile.composition_weights or "{}")
		rating = (
			likelihood * float(weights.get("likelihood", 1))
			+ impact * float(weights.get("impact", 1))
			+ (third or 0) * float(weights.get("third_dimension", 0))
		)
	else:
		return None
	return round(rating)


def _validate_scale(value, maximum, label):
	if value is not None and (value < 1 or value > maximum):
		frappe.throw(_("{0} must be between 1 and {1}.").format(label, maximum))


def resolve_band(profile_name, rating):
	"""Look up the Risk Method Band whose [min,max] contains `rating`."""
	if not profile_name or rating is None:
		return None
	bands = frappe.get_all(
		"Risk Method Band",
		filters={"parent": profile_name, "parenttype": "Risk Method Profile"},
		fields=["band_label", "band_min_rating", "band_max_rating"],
		order_by="band_min_rating asc",
	)
	for band in bands:
		if (band.band_min_rating or 0) <= rating <= (band.band_max_rating or 0):
			return band.band_label
	return None
