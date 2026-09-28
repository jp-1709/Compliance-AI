import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, getdate, now_datetime

from quantbit_compliance_ai.qms_management_review.coverage_engine import compute_coverage
from quantbit_compliance_ai.qms_management_review.input_handlers import run_auto_population

# Q5 spec §3.1. The Approved -> Superseded-style "Inputs Locked -> In Meeting"
# transition is nominally system-driven (fires when the linked MR Meeting
# starts) but is included here too since it is also a legitimate manual
# catch-up transition when a meeting was started out of band.
_ALLOWED_TRANSITIONS = {
	"Draft": {"Planning", "Skipped"},
	"Planning": {"Inputs Open", "Skipped"},
	"Inputs Open": {"Inputs Locked"},
	"Inputs Locked": {"In Meeting", "Inputs Open"},
	"In Meeting": {"Minutes Pending"},
	"Minutes Pending": {"Minutes Signed"},
	"Minutes Signed": {"Closed"},
	"Closed": set(),
	"Skipped": set(),
}


class MRCycle(Document):
	def validate(self):
		self._validate_status_transition()

		if self.period_start and self.period_end and getdate(self.period_end) < getdate(self.period_start):
			frappe.throw(_("Period End cannot be before Period Start."))
		if self.period_end and self.target_meeting_date:
			meeting_date = getdate(self.target_meeting_date)
			period_end = getdate(self.period_end)
			max_gap = frappe.db.get_value("MR Policy", self.policy, "max_gap_days") if self.policy else 30
			if meeting_date < period_end or meeting_date > add_days(period_end, max_gap or 30):
				frappe.throw(_("Target Meeting Date must fall between Period End and the policy maximum gap."))
		duplicate = frappe.db.exists("MR Cycle", {"entity": self.entity, "cycle_label": self.cycle_label, "name": ("!=", self.name or "")})
		if duplicate:
			frappe.throw(_("Cycle Label must be unique within the Business Entity."))
		if self.status == "Inputs Locked" and (self.input_completeness_pct or 0) < 95:
			frappe.throw(_("At least 95% input completeness is required before inputs can be locked."))
		if self.status == "Skipped" and (not self.skip_reason or not self.skip_approved_by):
			frappe.throw(_("Skip Reason and Skip Approved By are required for a skipped cycle."))
		if self.status == "Closed" and (not self.closing_snapshot or not self.cycle_summary):
			frappe.throw(_("Closing Snapshot and Cycle Summary are required before closing the cycle."))
		if self.status == "Minutes Signed":
			minutes = frappe.db.get_value("MR Minutes", {"cycle": self.name}, ["docstatus", "chair_signed"], as_dict=True)
			if not minutes or minutes.docstatus != 1 or not minutes.chair_signed:
				frappe.throw(_("Minutes Signed requires submitted MR Minutes with the Chairperson signature."))
		if self.status == "Closed" and not self.is_new():
			open_outputs = frappe.db.count("MR Output", {"cycle": self.name, "status": ["not in", ["Owner Accepted", "Downstream Created", "In Progress", "Completed", "Verified Effective", "Closed", "Carried Forward", "Cancelled"]]})
			if open_outputs:
				frappe.throw(_("All Management Review outputs must be accepted or resolved before closing the cycle."))

	def _validate_status_transition(self):
		if self.is_new():
			return
		before = self.get_doc_before_save()
		if not before or before.status == self.status:
			return
		allowed = _ALLOWED_TRANSITIONS.get(before.status, set())
		if self.status not in allowed:
			frappe.throw(
				_("Cannot move MR Cycle from {0} to {1} directly. Allowed next states: {2}.").format(
					before.status, self.status, ", ".join(sorted(allowed)) or "(terminal)"
				)
			)

	def before_save(self):
		if self.status == "Closed" and not self.closed_on:
			self.closed_on = now_datetime()

	def on_update(self):
		# Auto-Population Engine (§5.3): fire once, the moment the cycle opens
		# for input contribution.
		if self.has_value_changed("status") and self.status == "Inputs Open":
			run_auto_population(self.name)
		# Multi-Standard Coverage Check (§7): keep it fresh while inputs are
		# being contributed and right before the secretariat locks them.
		if self.status in ("Inputs Open", "Inputs Locked") and self.applicable_standards:
			compute_coverage(self.name)

	def before_submit(self):
		if self.status != "Closed":
			frappe.throw(_("MR Cycle can be submitted only in Closed status."))
		if not self.closing_snapshot or not self.cycle_summary:
			frappe.throw(_("Closing Snapshot and Cycle Summary are required before submission."))
