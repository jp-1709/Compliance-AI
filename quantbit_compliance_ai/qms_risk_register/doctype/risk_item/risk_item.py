import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import today

from quantbit_compliance_ai.qms_risk_register.aggregation_engine import compute_trend, refresh_action_tracker
from quantbit_compliance_ai.qms_risk_register.appetite_engine import apply_appetite_to_risk
from quantbit_compliance_ai.qms_risk_register.cadence_engine import compute_next_reassessment_due, refresh_overdue_flags
from quantbit_compliance_ai.qms_risk_register.scoring import get_method_profile, resolve_band, validate_and_score

# Q6 spec §4.5 state machine. Engine-driven transitions (Risk Assessment /
# Risk Treatment / Risk Acceptance on_submit hooks) write `status` via
# frappe.db.set_value and intentionally bypass this matrix — it exists to
# stop an out-of-band manual edit in the Desk UI from skipping steps.
_ALLOWED_TRANSITIONS = {
	"Draft": {"Identified", "Withdrawn"},
	"Identified": {"Assessed", "Withdrawn"},
	"Assessed": {"Treatment Planned", "Accepted", "Withdrawn", "Re-opened"},
	"Treatment Planned": {"Treatment In Progress", "Accepted", "Withdrawn"},
	"Treatment In Progress": {"Treatment Effective", "Accepted", "Withdrawn"},
	"Treatment Effective": {"Closed", "Accepted"},
	"Accepted": {"Closed"},
	"Closed": {"Re-opened", "Archived"},
	"Re-opened": {"Assessed"},
	"Archived": set(),
	"Withdrawn": set(),
}


class RiskItem(Document):
	def validate(self):
		self._validate_status_transition()

		profile = get_method_profile(self.method_profile)
		if not profile:
			frappe.throw(_("A valid Risk Method Profile is required."))
		inherent = validate_and_score(self, profile, "inherent")
		residual = validate_and_score(self, profile, "residual") if self.residual_likelihood else None
		if inherent is not None:
			self.inherent_rating = inherent
			self.inherent_band = resolve_band(self.method_profile, inherent)
		if residual is not None:
			self.residual_rating = residual
			self.residual_band = resolve_band(self.method_profile, residual)

		if self.target_residual_rating and self.inherent_rating and self.target_residual_rating >= self.inherent_rating:
			frappe.throw(_("Target Residual Rating must be lower than Inherent Rating."))

		# Risk Appetite Resolution Engine (§9): resolves applicable_appetite,
		# within_appetite, acceptance_required from entity/category/parent cascade.
		warning = apply_appetite_to_risk(self)
		if warning and self.residual_rating:
			frappe.msgprint(warning, indicator="orange", alert=True)

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

		# Reassessment Cadence Engine (§7): recompute the due date whenever the
		# last assessment date moves, then refresh the overdue/stale flags.
		if self.has_value_changed("last_assessed_on") or not self.next_reassessment_due:
			self.next_reassessment_due = compute_next_reassessment_due(self.entity, self.risk_category, self.last_assessed_on)
		refresh_overdue_flags(self)

		self.trend_direction, self.trend_residual_delta_last_3 = compute_trend(self.name) if self.name else ("Insufficient Data", 0)

	def _validate_status_transition(self):
		if self.is_new():
			return
		before = self.get_doc_before_save()
		if not before or before.status == self.status:
			return
		allowed = _ALLOWED_TRANSITIONS.get(before.status, set())
		if self.status not in allowed:
			frappe.throw(
				_("Cannot move Risk Item from {0} to {1} directly. Allowed next states: {2}.").format(
					before.status, self.status, ", ".join(sorted(allowed)) or "(terminal)"
				)
			)

	def before_save(self):
		if self.status == "Closed" and not self.closed_on:
			self.closed_on = today()
			self.closed_by = self.closed_by or frappe.session.user

	def on_update(self):
		refresh_action_tracker(self.name)

	def before_submit(self):
		if not self.name or not frappe.db.exists("Risk Assessment", {"risk_item": self.name}):
			frappe.throw(_("At least one Risk Assessment is required before submitting a Risk Item."))
