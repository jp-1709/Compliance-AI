import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, today

from quantbit_compliance_ai.qms_validation import text_length


class CAPACase(Document):
	def validate(self):
		self.priority = {"Critical": "P1", "High": "P2", "Medium": "P3", "Low": "P4"}.get(
			self.severity, self.priority
		)
		if self.target_close_date and getdate(self.target_close_date) < getdate(today()) and self.status == "Draft":
			frappe.throw(_("Target Close Date cannot be in the past."))
		if self.capa_owner and self.capa_owner == self.qa_approver:
			frappe.throw(_("CAPA Owner and QA Approver must be different users."))
		if self.status in {"Submitted", "Pending Review"} and (
			text_length(self.problem_statement) < 50 or not self.source or not self.severity
		):
			frappe.throw(_("Pending Review requires Source, Severity, and a Problem Statement of at least 50 characters."))
		if self.status in {"In RCA", "Action Planning", "Plan Approved", "In Progress", "Effectiveness Pending", "Pending Effectiveness", "Effectiveness In Progress", "Effectiveness Check", "Closed"} and not self.rca_record:
			frappe.throw(_("An RCA Record is required from In RCA onward."))
		if self.status in {"Action Planning", "Plan Approved", "In Progress", "Effectiveness Pending", "Pending Effectiveness", "Effectiveness In Progress", "Effectiveness Check", "Closed"}:
			if text_length(self.root_cause_summary) < 50:
				frappe.throw(_("Root Cause Summary must contain at least 50 characters from Action Planning onward."))
			if self.rca_record and frappe.db.get_value("RCA Record", self.rca_record, "status") != "Reviewed":
				frappe.throw(_("The RCA Record must be Reviewed before Action Planning."))
		if self.status in {"In Progress", "Effectiveness Pending", "Effectiveness In Progress", "Closed"} and not self.actions:
			frappe.throw(_("At least one CAPA Action is required from In Progress onward."))
		if self.status in {"Plan Approved", "In Progress", "Effectiveness Pending", "Pending Effectiveness", "Effectiveness In Progress", "Effectiveness Check", "Closed"} and not [row for row in self.actions or [] if row.status != "Cancelled"]:
			frappe.throw(_("At least one non-cancelled CAPA Action is required after Action Planning."))
		if self.status == "Closed":
			if not self.effectiveness_check:
				frappe.throw(_("An Effectiveness Check is required before closing the CAPA."))
			if frappe.db.get_value("Effectiveness Check", self.effectiveness_check, "outcome") != "Effective":
				frappe.throw(_("CAPA can close only after an Effectiveness Check outcome of Effective."))
		if self.is_voided and not self.void_reason:
			frappe.throw(_("Void Reason is required for a voided CAPA."))
		quality_events = [row.quality_event for row in self.quality_events or [] if row.quality_event]
		if quality_events:
			if len(quality_events) != len(set(quality_events)):
				frappe.throw(_("A Quality Event can be linked only once."))
			if sum(1 for row in self.quality_events if row.is_primary) != 1:
				frappe.throw(_("Exactly one linked Quality Event must be marked Primary."))
		verifier = None
		if self.effectiveness_check:
			verifier = frappe.db.get_value("Effectiveness Check", self.effectiveness_check, "verifier")
			if verifier in {self.capa_owner, self.qa_approver}:
				frappe.throw(_("Effectiveness Verifier must differ from CAPA Owner and QA Approver."))
		for action in self.actions or []:
			if text_length(action.acceptance_criteria) < 20:
				frappe.throw(_("CAPA Action {0} acceptance criteria must contain at least 20 characters.").format(action.idx))
			if verifier and action.owner == verifier:
				frappe.throw(_("CAPA Action {0} owner cannot be the Effectiveness Verifier.").format(action.idx))
			if action.target_date and self.target_close_date and action.target_date > self.target_close_date:
				frappe.throw(_("CAPA Action {0} target date exceeds the CAPA target close date.").format(action.idx))
			if action._ai_generated and not action._human_reviewed and action.status != "Open":
				frappe.throw(_("AI-generated CAPA Action {0} must be human reviewed before progressing.").format(action.idx))
			if action.status == "Completed" and self.severity in {"High", "Critical"} and not action.evidence_files:
				frappe.throw(_("Evidence is required to complete Action {0} for a High or Critical CAPA.").format(action.idx))
			if action.status == "Blocked" and not action.blocking_reason:
				frappe.throw(_("Blocking Reason is required for blocked CAPA Action {0}.").format(action.idx))

	def before_save(self):
		if self.status == "Closed" and not self.actual_close_date:
			self.actual_close_date = today()
