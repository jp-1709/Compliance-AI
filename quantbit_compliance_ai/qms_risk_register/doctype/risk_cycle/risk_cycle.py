import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, now_datetime


class RiskCycle(Document):
	def validate(self):
		if self.period_start and self.period_end and getdate(self.period_end) < getdate(self.period_start):
			frappe.throw(_("Period End cannot be before Period Start."))
		risks = self.included_risks or []
		if len([row.risk_item for row in risks if row.risk_item]) != len({row.risk_item for row in risks if row.risk_item}):
			frappe.throw(_("A Risk Item can appear only once in a Risk Cycle."))
		self.target_assessment_count = len(risks)
		self.completed_assessment_count = sum(1 for row in risks if row.assessment_completed)
		self.completion_pct = self.completed_assessment_count * 100 / self.target_assessment_count if self.target_assessment_count else 0
		if self.status == "Closed" and self.completed_assessment_count != self.target_assessment_count:
			frappe.throw(_("Every included risk assessment must be completed before closing the cycle."))

	def before_submit(self):
		if self.status != "Closed" or not self.closing_snapshot or not self.cycle_summary:
			frappe.throw(_("A Risk Cycle requires Closed status, Closing Snapshot, and Cycle Summary before submission."))
		self.closed_on = self.closed_on or now_datetime()
