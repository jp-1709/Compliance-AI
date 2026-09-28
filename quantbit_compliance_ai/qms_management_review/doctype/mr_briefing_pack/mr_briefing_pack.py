import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, getdate

from quantbit_compliance_ai.qms_validation import as_list, row_value


class MRBriefingPack(Document):
	def validate(self):
		if not self.included_input_records:
			frappe.throw(_("At least one Input Record is required in a briefing pack."))
		recipients = {
			row_value(row, "user") for row in as_list(self.recipients) if row_value(row, "user")
		}
		invited = {row.user for row in frappe.get_doc("MR Meeting", self.meeting).attendees if row.invited and row.user} if self.meeting else set()
		missing = invited - recipients
		if missing:
			frappe.throw(_("Briefing Pack recipients must include every invited attendee."))
		if self.dispatched_on:
			if frappe.db.get_value("MR Cycle", self.cycle, "status") != "Inputs Locked":
				frappe.throw(_("A Briefing Pack can be dispatched only while cycle Inputs are Locked."))
			meeting_date = frappe.db.get_value("MR Meeting", self.meeting, "planned_date")
			policy = frappe.db.get_value("MR Cycle", self.cycle, "policy")
			lead_days = frappe.db.get_value("MR Policy", policy, "briefing_pack_lead_days") or 0
			if meeting_date and getdate(self.dispatched_on) > add_days(getdate(meeting_date), -(max(lead_days - 1, 0))):
				frappe.throw(_("Briefing Pack was dispatched later than the permitted lead-time grace."))

	def before_submit(self):
		if not self.generated_pdf_hash or not self.recipients:
			frappe.throw(_("A generated PDF hash and recipients are required before submission."))
