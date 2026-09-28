import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import today

from quantbit_compliance_ai.qms_risk_register.scoring import get_method_profile, resolve_band, validate_and_score

# Risk Item snapshot fields this assessment is authoritative for on submit.
_SNAPSHOT_FIELDS = (
	"inherent_likelihood", "inherent_impact", "inherent_third_dim", "inherent_cia_c", "inherent_cia_i", "inherent_cia_a", "inherent_rating",
	"residual_likelihood", "residual_impact", "residual_third_dim", "residual_cia_c", "residual_cia_i", "residual_cia_a", "residual_rating",
)
# The only two Risk Item states from which an assessment legitimately advances
# status to "Assessed" per the §4.5 transition matrix; any other current
# state (e.g. Treatment In Progress being reassessed) keeps its own status.
_STATUS_ADVANCES_FROM = {"Identified", "Re-opened"}


class RiskAssessment(Document):
	def validate(self):
		profile = get_method_profile(self.method_profile)
		if not profile:
			frappe.throw(_("A valid Risk Method Profile is required."))
		inherent = validate_and_score(self, profile, "inherent")
		residual = validate_and_score(self, profile, "residual")
		if inherent is not None:
			self.inherent_rating = inherent
			self.inherent_band = resolve_band(self.method_profile, inherent)
		if residual is not None:
			self.residual_rating = residual
			self.residual_band = resolve_band(self.method_profile, residual)
		if self.residual_rating and self.inherent_rating and self.residual_rating > self.inherent_rating:
			frappe.throw(_("Residual Rating cannot exceed Inherent Rating."))

		self.is_self_assessment = int(
			self.assessed_by == frappe.db.get_value("Risk Item", self.risk_item, "risk_owner")
		)
		if self.validated_by and self.validated_by == self.assessed_by:
			frappe.throw(_("Assessment validator must differ from the assessor."))
		if (self.inherent_delta or self.residual_delta) and not self.delta_explanation:
			frappe.throw(_("Delta Explanation is required when assessment scores change."))

		policy = frappe.get_all(
			"Risk Policy",
			filters={"entity": self.entity},
			fields=["allow_self_assessment", "require_evidence_for_assessment_change"],
			order_by="modified desc",
			limit=1,
		)
		policy = policy[0] if policy else None
		if (self.inherent_delta or self.residual_delta) and policy and policy.require_evidence_for_assessment_change and not self.evidence_files:
			frappe.throw(_("Evidence is required when assessment scores change."))
		if self.status == "Submitted" and self.is_self_assessment and (not policy or not policy.allow_self_assessment) and not self.validated_by:
			frappe.throw(_("An independent validator is required for this self-assessment."))

	def before_submit(self):
		if not self.prior_assessment:
			prior = frappe.get_all(
				"Risk Assessment", filters={"risk_item": self.risk_item, "docstatus": 1, "name": ("!=", self.name)}, pluck="name", order_by="creation desc", limit=1
			)
			self.prior_assessment = prior[0] if prior else None
		if self.prior_assessment:
			prior = frappe.db.get_value("Risk Assessment", self.prior_assessment, ["inherent_rating", "residual_rating"], as_dict=True)
			self.inherent_delta = (self.inherent_rating or 0) - (prior.inherent_rating or 0)
			self.residual_delta = (self.residual_rating or 0) - (prior.residual_rating or 0)
			if (self.inherent_delta or self.residual_delta) and not self.delta_explanation:
				frappe.throw(_("Delta Explanation is required when scores differ from the prior assessment."))

	def on_submit(self):
		# Route through Risk Item.save() (rather than a raw db_set) so the
		# Reassessment Cadence Engine, the Appetite Resolution Engine, and
		# the trend computation all re-run consistently from one place.
		risk = frappe.get_doc("Risk Item", self.risk_item)
		for fieldname in _SNAPSHOT_FIELDS:
			risk.set(fieldname, self.get(fieldname))
		risk.inherent_band = self.inherent_band
		risk.residual_band = self.residual_band
		risk.last_assessed_on = today()
		if risk.status in _STATUS_ADVANCES_FROM:
			risk.status = "Assessed"
		risk.flags.ignore_permissions = True
		risk.save()  # Risk Item.on_update() refreshes the Risk Action Tracker
