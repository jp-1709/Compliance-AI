import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, getdate, now_datetime

from quantbit_compliance_ai.qms_risk_register.aggregation_engine import refresh_action_tracker
from quantbit_compliance_ai.qms_validation import require_user_role


class RiskAcceptance(Document):
	def validate(self):
		if self.effective_from and self.valid_until:
			if getdate(self.valid_until) < getdate(self.effective_from):
				frappe.throw(_("Valid Until cannot be before Effective From."))
			if getdate(self.valid_until) > add_days(getdate(self.effective_from), 365):
				frappe.throw(_("Risk acceptance validity cannot exceed 365 days."))
			self.re_review_due_date = min(getdate(self.valid_until), add_days(getdate(self.effective_from), 180))
		owner = frappe.db.get_value("Risk Item", self.risk_item, "risk_owner")
		if self.accepted_by and self.accepted_by == owner:
			frappe.throw(_("Risk Acceptor must differ from the Risk Owner."))
		require_user_role(self.accepted_by, "Risk Acceptor", _("Accepted By"))
		if self.countersigned_by and self.countersigned_by == self.accepted_by:
			frappe.throw(_("Countersigner must differ from the Risk Acceptor."))
		if self.status == "Active" and not self.acceptance_signature:
			frappe.throw(_("Acceptance Signature is required before activation."))
		if self.status == "Withdrawn" and (not self.withdrawal_reason or not self.withdrawn_by):
			frappe.throw(_("Withdrawal Reason and Withdrawn By are required."))
		if self.appetite_rating_at_acceptance is not None:
			self.delta_above_appetite = self.residual_rating_at_acceptance - self.appetite_rating_at_acceptance

	def before_submit(self):
		if not self.acceptance_signature or not self.acceptance_rationale or not self.alternative_treatments_considered:
			frappe.throw(_("Signature, Acceptance Rationale, and Alternatives Considered are required."))
		band = frappe.db.get_value("Risk Item", self.risk_item, "residual_band")
		if band == "Critical" and (not self.countersigned_by or not self.countersignature):
			frappe.throw(_("Critical residual risks require a countersigner and countersignature."))
		# "Submittable: submit = acceptance signed and active" (§4.9) — submission
		# itself is the activation event, unless the acceptance is being
		# withdrawn as part of the same save.
		if self.status != "Withdrawn":
			self.status = "Active"

	def before_save(self):
		if self.status == "Active" and not self.accepted_on:
			self.accepted_on = now_datetime()
		if self.status == "Withdrawn" and not self.withdrawn_on:
			self.withdrawn_on = now_datetime()

	def on_submit(self):
		frappe.db.set_value("Risk Acceptance", {"risk_item": self.risk_item, "status": "Active", "name": ["!=", self.name]}, "status", "Superseded")
		frappe.db.set_value("Risk Item", self.risk_item, {"active_acceptance": self.name, "status": "Accepted"})
		refresh_action_tracker(self.risk_item)

	@frappe.whitelist()
	def withdraw(self, reason: str):
		if self.status != "Active":
			frappe.throw(_("Only an Active acceptance can be withdrawn."))
		if not reason:
			frappe.throw(_("Withdrawal Reason is required."))
		self.status = "Withdrawn"
		self.withdrawal_reason = reason
		self.withdrawn_by = frappe.session.user
		self.withdrawn_on = now_datetime()
		self.save()
		# Re-evaluation: the risk no longer has a signed-off acceptance, so its
		# acceptance_required posture must be recomputed the next time the
		# Risk Item is saved (validate() re-derives it from residual vs appetite).
		if frappe.db.get_value("Risk Item", self.risk_item, "active_acceptance") == self.name:
			frappe.db.set_value("Risk Item", self.risk_item, "active_acceptance", None)
			frappe.get_doc("Risk Item", self.risk_item).save(ignore_permissions=True)
		refresh_action_tracker(self.risk_item)
