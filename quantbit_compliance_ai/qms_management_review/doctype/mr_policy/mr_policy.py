import frappe
from frappe import _
from frappe.model.document import Document


class MRPolicy(Document):
	def validate(self):
		if (self.quorum_top_mgmt_min or 0) > (self.quorum_total_min or 0):
			frappe.throw(_("Top Management quorum cannot exceed total quorum."))
		limits = {"Quarterly": 120, "Half-yearly": 210, "Annual": 400}
		if self.frequency in limits and (self.max_gap_days or 0) > limits[self.frequency]:
			frappe.throw(_("Maximum Gap Days cannot exceed {0} for {1} reviews.").format(limits[self.frequency], self.frequency))
		if not self.applicable_standards:
			frappe.throw(_("At least one Applicable Standard is required."))
		if self.brsr_aligned_mode and self.entity:
			meta = frappe.get_meta("Business Entity")
			if meta.has_field("is_listed") and not frappe.db.get_value("Business Entity", self.entity, "is_listed"):
				frappe.throw(_("BRSR-aligned mode requires a listed Business Entity."))
