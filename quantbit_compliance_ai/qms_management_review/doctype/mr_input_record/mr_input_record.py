import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, getdate

from quantbit_compliance_ai.qms_validation import ensure_reference_exists


class MRInputRecord(Document):
	def validate(self):
		if not self.applicable_standards:
			frappe.throw(_("At least one Applicable Standard is required."))
		if self.source_period_start and self.source_period_end:
			start, end = getdate(self.source_period_start), getdate(self.source_period_end)
			if start >= end:
				frappe.throw(_("Source Period Start must be before Source Period End."))
			cycle = frappe.db.get_value("MR Cycle", self.cycle, ["period_start", "period_end"], as_dict=True)
			if cycle and (start < add_days(getdate(cycle.period_start), -30) or end > getdate(cycle.period_end)):
				frappe.throw(_("Source period must be within 30 days before Cycle Start through Cycle End."))
		if self.source_type == "Auto-Pulled":
			if not self.source_record_doctype or not self.source_record_name:
				frappe.throw(_("Auto-Pulled inputs require Source Record DocType and Name."))
			ensure_reference_exists(self.source_record_doctype, self.source_record_name)
		if self.carried_from and (self.source_module != "Q5 Prior Cycle" or not self.reason_for_carry):
			frappe.throw(_("Carried inputs require Source Module Q5 Prior Cycle and a Reason for Carry."))
		if self.input_category == "Status of Prior Actions" and not self.meeting_decisions_referenced:
			frappe.throw(_("Status of Prior Actions must reference prior meeting decisions."))
		if self.status not in {"Draft", "Withdrawn"}:
			if not self.data_summary or not self.key_metrics:
				frappe.throw(_("Submitted inputs require Data Summary and Key Metrics."))
			if not self.evidence_files and len((self.data_summary or "").strip()) < 100:
				frappe.throw(_("Provide evidence or document an evidence justification of at least 100 characters in Data Summary."))

	def before_submit(self):
		if not self.data_summary or not self.key_metrics:
			frappe.throw(_("Data Summary and Key Metrics are required before submission."))
		if not self.evidence_files and len((self.data_summary or "").strip()) < 100:
			frappe.throw(_("Provide evidence or a documented evidence justification before submission."))
