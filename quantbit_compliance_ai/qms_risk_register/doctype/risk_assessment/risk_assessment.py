import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import today

from quantbit_compliance_ai.qms_risk_register.scoring import get_method_profile, validate_and_score


class RiskAssessment(Document):
	def validate(self):
		profile = get_method_profile(self.method_profile)
		if not profile:
			frappe.throw(_("A valid Risk Method Profile is required."))
		inherent = validate_and_score(self, profile, "inherent")
		residual = validate_and_score(self, profile, "residual")
		if inherent is not None:
			self.inherent_rating = inherent
		if residual is not None:
			self.residual_rating = residual
		if self.residual_rating and self.inherent_rating and self.residual_rating > self.inherent_rating:
			frappe.throw(_("Residual Rating cannot exceed Inherent Rating."))
		self.is_self_assessment = int(
			self.assessed_by == frappe.db.get_value("Risk Item", self.risk_item, "risk_owner")
		)
		if self.validated_by and self.validated_by == self.assessed_by:
			frappe.throw(_("Assessment validator must differ from the assessor."))
		if (self.inherent_delta or self.residual_delta) and not self.delta_explanation:
			frappe.throw(_("Delta Explanation is required when assessment scores change."))
		policy = frappe.get_all("Risk Policy", filters={"entity": self.entity}, fields=["allow_self_assessment", "require_evidence_for_assessment_change"], order_by="modified desc", limit=1)
		policy = policy[0] if policy else None
		if (self.inherent_delta or self.residual_delta) and policy and policy.require_evidence_for_assessment_change and not self.evidence_files:
			frappe.throw(_("Evidence is required when assessment scores change."))
		if self.status == "Submitted" and self.is_self_assessment and (not policy or not policy.allow_self_assessment) and not self.validated_by:
			frappe.throw(_("An independent validator is required for this self-assessment."))

	def before_submit(self):
		if not self.prior_assessment:
			prior = frappe.get_all("Risk Assessment", filters={"risk_item": self.risk_item, "docstatus": 1, "name": ("!=", self.name)}, pluck="name", order_by="creation desc", limit=1)
			self.prior_assessment = prior[0] if prior else None
		if self.prior_assessment:
			prior = frappe.db.get_value("Risk Assessment", self.prior_assessment, ["inherent_rating", "residual_rating"], as_dict=True)
			self.inherent_delta = (self.inherent_rating or 0) - (prior.inherent_rating or 0)
			self.residual_delta = (self.residual_rating or 0) - (prior.residual_rating or 0)
			if (self.inherent_delta or self.residual_delta) and not self.delta_explanation:
				frappe.throw(_("Delta Explanation is required when scores differ from the prior assessment."))

	def on_submit(self):
		frappe.db.set_value(
			"Risk Item", self.risk_item,
			{
				"inherent_rating": self.inherent_rating,
				"residual_rating": self.residual_rating,
				"last_assessed_on": today(),
				"status": "Assessed",
			},
		)
