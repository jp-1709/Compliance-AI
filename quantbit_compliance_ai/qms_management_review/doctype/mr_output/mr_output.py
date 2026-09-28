import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, now_datetime, today


class MROutput(Document):
	def validate(self):
		if self.meeting and self.target_completion_date:
			meeting_date = frappe.db.get_value("MR Meeting", self.meeting, "planned_date")
			if meeting_date and getdate(self.target_completion_date) <= getdate(meeting_date):
				frappe.throw(_("Target Completion Date must be after the meeting date."))
		if self.status not in {"Draft", "Captured in Meeting", "Cancelled"} and not self.linked_input_records:
			frappe.throw(_("At least one linked input record is required for this output."))
		mapping = {
			"Open CAPA": {"CAPA Case"}, "Document Change": {"Document Version"},
			"Open Audit": {"Audit Programme", "Audit"}, "Risk Treatment": {"Risk Item"},
		}
		if self.output_type in mapping and self.downstream_doctype and self.downstream_doctype not in mapping[self.output_type]:
			frappe.throw(_("Downstream DocType is not valid for output type {0}.").format(self.output_type))
		if self.status == "Owner Declined" and not self.owner_decline_reason:
			frappe.throw(_("Owner Decline Reason is required."))
		if self.status in {"Downstream Created", "In Progress", "Completed", "Verified Effective"}:
			if self.downstream_creation_status != "Not Required" and not (self.downstream_doctype and self.downstream_record):
				frappe.throw(_("Downstream DocType and record are required after downstream creation."))
		if self.status in {"Verified Effective", "Closed"} and self.effectiveness_verification_required:
			if not self.effectiveness_verified_by or not self.effectiveness_verified_on:
				frappe.throw(_("Independent effectiveness verification is required."))
			if self.effectiveness_verified_by == self.assigned_to:
				frappe.throw(_("The output owner cannot verify effectiveness."))
		if self.status == "Closed" and not self.closure_summary:
			frappe.throw(_("Closure Summary is required before closing the output."))
		if self.status == "Carried Forward" and not self.carry_forward_to_cycle:
			frappe.throw(_("Carry Forward To Cycle is required."))
		if self.severity_classification == "Critical" and self.entity:
			meta = frappe.get_meta("Business Entity")
			if meta.has_field("is_listed") and frappe.db.get_value("Business Entity", self.entity, "is_listed") and not self.escalated_to_board:
				frappe.throw(_("Critical outputs for listed entities must be escalated to the board."))

	def before_submit(self):
		cycle_status = frappe.db.get_value("MR Cycle", self.cycle, "status")
		meeting_status = frappe.db.get_value("MR Meeting", self.meeting, "status")
		if cycle_status not in {"Minutes Pending", "Minutes Signed"}:
			frappe.throw(_("The cycle must be Minutes Pending or Minutes Signed before submitting an output."))
		if meeting_status not in {"Ended", "Minutes Drafted", "Minutes Signed"}:
			frappe.throw(_("The meeting must have ended before submitting an output."))

	def before_save(self):
		if self.owner_acceptance and not self.owner_acceptance_date:
			self.owner_acceptance_date = now_datetime()
		if self.status == "Closed":
			self.closed_on = self.closed_on or today()
