import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, today

from quantbit_compliance_ai.qms_risk_register.aggregation_engine import refresh_action_tracker
from quantbit_compliance_ai.qms_risk_register.scoring import get_method_profile
from quantbit_compliance_ai.qms_risk_register.treatment_hooks import EXPECTED_DOWNSTREAM, auto_create_downstream


class RiskTreatment(Document):
	def validate(self):
		created_on = getdate(self.creation) if self.creation else getdate(today())
		if self.target_completion_date and getdate(self.target_completion_date) <= created_on:
			frappe.throw(_("Target Completion Date must be after the treatment creation date."))
		if self.expected_reduction_pct is not None and not 0 <= self.expected_reduction_pct <= 100:
			frappe.throw(_("Expected Reduction must be between 0 and 100 percent."))
		if self.effectiveness_verified_by and self.effectiveness_verified_by == self.assigned_to:
			frappe.throw(_("Treatment owner cannot verify treatment effectiveness."))

		expected_target = EXPECTED_DOWNSTREAM.get(self.treatment_type)
		if expected_target and self.downstream_doctype and self.downstream_doctype != expected_target:
			frappe.throw(_("Downstream DocType must be {0} for this treatment type.").format(expected_target))

		if self.treatment_type == "Modify Existing Control" and not self.linked_existing_control:
			frappe.throw(_("Linked Existing Control is mandatory for a 'Modify Existing Control' treatment."))
		if self.treatment_type == "Insurance Cover":
			if not all((self.insurance_policy_no, self.insurance_carrier, self.insurance_sum_insured_inr)):
				frappe.throw(_("Policy number, carrier, and sum insured are required for insurance treatment."))
		if self.treatment_type == "Outsource / Transfer Contractual" and not self.transfer_counterparty:
			frappe.throw(_("Transfer Counterparty is required for contractual transfer."))

		risk = frappe.db.get_value("Risk Item", self.risk_item, ["risk_category", "method_profile"], as_dict=True)
		if risk:
			category = (frappe.db.get_value("Risk Category", risk.risk_category, "category_name") or "").lower()
			if ("oh&s" in category or "occupational" in category) and self.hierarchy_of_control == "Not Applicable":
				frappe.throw(_("OH&S risk treatments must select a hierarchy of control."))
			profile = get_method_profile(risk.method_profile)
			if profile:
				if self.target_residual_likelihood and not 1 <= self.target_residual_likelihood <= profile.likelihood_scale_max:
					frappe.throw(_("Target Residual Likelihood is outside the method scale."))
				if self.target_residual_impact and not 1 <= self.target_residual_impact <= profile.impact_scale_max:
					frappe.throw(_("Target Residual Impact is outside the method scale."))

		if self.treatment_strategy == "Accept":
			self.downstream_creation_status = "Not Required"
			self.effectiveness_verification_required = 0
		if self.status == "Verified Effective":
			if self.effectiveness_verification_required and not (
				self.effectiveness_verified_by and self.effectiveness_verified_on and self.effectiveness_evidence
			):
				frappe.throw(_("Verifier, verification date, and evidence are required."))
			if not self.residual_at_verification_assessment:
				frappe.throw(_("A residual assessment is required to verify treatment effectiveness."))

	def before_save(self):
		if self.status in {"Completed", "Verified Effective"} and not self.actual_completion_date:
			self.actual_completion_date = today()

	def on_submit(self):
		# Treatment Auto-Creation Hooks (§8): CAPA / Control / Risk Acceptance
		# are created here; failures are recorded on the treatment, never
		# raised out of the submit transaction (§8.2 error handling).
		auto_create_downstream(self)
		if self.status == "Draft":
			frappe.db.set_value("Risk Treatment", self.name, "status", "Plan Approved")
		frappe.db.set_value("Risk Item", self.risk_item, "treatment_status", "Plan Approved")
		refresh_action_tracker(self.risk_item)
