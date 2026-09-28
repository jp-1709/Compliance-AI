import re

import frappe
from frappe import _
from frappe.model.document import Document


class RiskMethodProfile(Document):
	def validate(self):
		if (self.likelihood_scale_max or 0) < 2 or (self.impact_scale_max or 0) < 2:
			frappe.throw(_("Likelihood and Impact scale maximums must each be at least 2."))
		if self.has_third_dimension and (self.third_dimension_scale_max or 0) < 2:
			frappe.throw(_("Third Dimension scale maximum must be at least 2."))
		anchors = {
			"Likelihood": (self.likelihood_anchors or [], self.likelihood_scale_max),
			"Impact": (self.impact_anchors or [], self.impact_scale_max),
		}
		if self.has_third_dimension:
			anchors["Third Dimension"] = (self.third_dimension_anchors or [], self.third_dimension_scale_max)
		for label, (rows, maximum) in anchors.items():
			values = sorted(row.scale_value for row in rows if row.scale_value is not None)
			if values != list(range(1, (maximum or 0) + 1)):
				frappe.throw(_("{0} anchors must cover every scale value from 1 to {1}, without gaps or duplicates.").format(label, maximum))
		maximum_rating = (self.likelihood_scale_max or 0) * (self.impact_scale_max or 0)
		if self.has_third_dimension:
			maximum_rating *= self.third_dimension_scale_max or 0
		bands = sorted(self.severity_bands or [], key=lambda row: row.band_min_rating or 0)
		expected = 1
		for row in bands:
			if row.band_min_rating != expected or (row.band_max_rating or 0) < row.band_min_rating:
				frappe.throw(_("Severity Bands must cover the rating range without gaps or overlaps."))
			expected = row.band_max_rating + 1
		if expected != maximum_rating + 1:
			frappe.throw(_("Severity Bands must cover ratings 1 through {0}.").format(maximum_rating))
		if (self.treatment_required_default or 0) > (self.acceptance_required_default or 0):
			frappe.throw(_("Treatment threshold cannot exceed the acceptance threshold."))
		if self.composition_formula == "Custom" and (
			not self.custom_formula_python or self.is_seed_data or "custom" not in (self.profile_name or "").lower()
		):
			frappe.throw(_("Custom formulas require code, a profile name containing 'Custom', and a non-seed profile."))
		for fieldname in ("heat_map_colour_band_a", "heat_map_colour_band_b", "heat_map_colour_band_c", "heat_map_colour_band_d", "heat_map_colour_band_e"):
			value = self.get(fieldname)
			if value and not re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
				frappe.throw(_("{0} must be a valid six-digit hex colour.").format(self.meta.get_label(fieldname)))
