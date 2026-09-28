import frappe
from frappe import _
from frappe.model.document import Document

from quantbit_compliance_ai.qms_validation import require_user_role


class RiskPolicy(Document):
	def validate(self):
		cadence_fields = [field.fieldname for field in self.meta.fields if field.fieldname.startswith("cadence_")]
		for fieldname in cadence_fields:
			if (self.get(fieldname) or 0) <= 0:
				frappe.throw(_("{0} must be greater than zero.").format(self.meta.get_label(fieldname)))
		if (self.acceptance_required_threshold or 0) < (self.treatment_required_threshold or 0):
			frappe.throw(_("Acceptance Required Threshold must be greater than or equal to Treatment Required Threshold."))
		if not self.default_method_profile and not self.method_profile_per_category:
			frappe.throw(_("At least one default or category-specific Risk Method Profile is required."))
		if self.appetite_signing_required:
			users = frappe.get_all("Has Role", filters={"role": "Risk Acceptor", "parenttype": "User"}, pluck="parent", limit=1)
			if not users:
				frappe.throw(_("Appetite signing requires at least one enabled User with the Risk Acceptor role."))
			for user in users:
				require_user_role(user, "Risk Acceptor")
