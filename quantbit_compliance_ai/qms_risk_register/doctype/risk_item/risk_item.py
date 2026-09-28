import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, today

from quantbit_compliance_ai.qms_risk_register.scoring import get_method_profile, validate_and_score


class RiskItem(Document):
	def validate(self):
		profile = get_method_profile(self.method_profile)
		if not profile:
			frappe.throw(_("A valid Risk Method Profile is required."))
		inherent = validate_and_score(self, profile, "inherent")
		residual = validate_and_score(self, profile, "residual") if self.residual_likelihood else None
		if inherent is not None:
			self.inherent_rating = inherent
		if residual is not None:
			self.residual_rating = residual
		if self.target_residual_rating and self.inherent_rating and self.target_residual_rating >= self.inherent_rating:
			frappe.throw(_("Target Residual Rating must be lower than Inherent Rating."))
		if self.treatment_strategy == "Accept" and not (self.active_acceptance or self.within_appetite):
			frappe.throw(_("An active Risk Acceptance is required to accept a risk above appetite."))
		if self.status == "Closed":
			if not self.closure_summary:
				frappe.throw(_("Closure Summary is required before closing a risk."))
			if self.acceptance_required and not self.active_acceptance:
				frappe.throw(_("An active Risk Acceptance is required before closing this risk."))
		if self.status == "Withdrawn" and not self.withdrawal_reason:
			frappe.throw(_("Withdrawal Reason is required."))
		if self.status == "Re-opened" and not self.status_reason:
			frappe.throw(_("Status Reason is required when reopening a risk."))
		if self.next_reassessment_due:
			delta = (getdate(today()) - getdate(self.next_reassessment_due)).days
			self.is_reassessment_overdue = 1 if delta > 0 else 0
			self.reassessment_overdue_days = max(delta, 0)

	def before_save(self):
		if self.status == "Closed" and not self.closed_on:
			self.closed_on = today()

	def before_submit(self):
		if not self.name or not frappe.db.exists("Risk Assessment", {"risk_item": self.name}):
			frappe.throw(_("At least one Risk Assessment is required before submitting a Risk Item."))
