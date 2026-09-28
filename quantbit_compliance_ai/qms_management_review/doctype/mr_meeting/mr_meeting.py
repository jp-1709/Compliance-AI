import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_time

from quantbit_compliance_ai.qms_validation import require_user_role


class MRMeeting(Document):
	def validate(self):
		if self.planned_start_time and self.planned_end_time and get_time(self.planned_end_time) <= get_time(self.planned_start_time):
			frappe.throw(_("Planned End Time must be after Planned Start Time."))
		if self.chairperson == self.secretariat:
			frappe.throw(_("Chairperson and Secretariat must be different users."))
		require_user_role(self.chairperson, "MR Chairperson", _("Chairperson"))
		require_user_role(self.secretariat, "MR Secretariat", _("Secretariat"))
		if self.meeting_mode in {"Virtual", "Hybrid"} and not self.virtual_link:
			frappe.throw(_("Virtual Link is required for a Virtual or Hybrid meeting."))
		if self.status == "Cancelled" and (not self.cancellation_reason or not self.cancellation_approved_by):
			frappe.throw(_("Cancellation Reason and Cancellation Approved By are required."))
		if self.status in {"Started", "In Progress", "Ended", "Minutes Drafted", "Minutes Signed"} and not self.quorum_met:
			frappe.throw(_("Quorum must be met before the meeting can start."))
		if self.status in {"Minutes Drafted", "Minutes Signed"}:
			if not self.linked_minutes:
				frappe.throw(_("Linked Minutes are required after minutes are drafted."))
			if self.status == "Minutes Drafted" and frappe.db.get_value("MR Minutes", self.linked_minutes, "docstatus") != 0:
				frappe.throw(_("Minutes Drafted must reference draft MR Minutes."))
		if self.status == "Minutes Signed":
			unsigned = [row.user for row in self.attendees or [] if row.attended and row.attendance_mode == "In Person" and not row.signed_attendance]
			if unsigned:
				frappe.throw(_("All in-person attendees must sign attendance before Minutes Signed."))
		if self.recording_url and not self.recording_consent_obtained:
			frappe.throw(_("Recording consent is required when a recording URL is stored."))

	def before_submit(self):
		if self.status != "Minutes Signed":
			frappe.throw(_("MR Meeting can be submitted only after Minutes are Signed."))
		unsigned = [row.user for row in self.attendees or [] if row.attended and row.attendance_mode == "In Person" and not row.signed_attendance]
		if unsigned:
			frappe.throw(_("All attended in-person participants must sign attendance."))
