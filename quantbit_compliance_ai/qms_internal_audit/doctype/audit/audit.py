import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, now_datetime, today

from quantbit_compliance_ai.qms_internal_audit.state_engine import validate_audit_transition


class Audit(Document):
	def validate(self):
		self._validate_status_transition()

		if self.planned_start_date and self.planned_end_date and getdate(self.planned_end_date) < getdate(self.planned_start_date):
			frappe.throw(_("Planned End Date cannot be before Planned Start Date."))
		if self.actual_start_date and self.actual_end_date and getdate(self.actual_end_date) < getdate(self.actual_start_date):
			frappe.throw(_("Actual End Date cannot be before Actual Start Date."))
		if self.audit_type == "Internal" and not self.audit_programme:
			frappe.throw(_("Audit Programme is required for an Internal audit."))
		if self.auditee_party != "Self (internal)" and not self.auditee_supplier:
			frappe.throw(_("Auditee Supplier is required for an external-party audit."))
		if self.status == "Cancelled" and len((self.cancellation_reason or "").strip()) < 30:
			frappe.throw(_("Cancellation Reason must contain at least 30 characters."))
		if self.is_voided and not self.void_reason:
			frappe.throw(_("Void Reason is required for a voided audit."))

		team_users = {row.user for row in self.audit_team or [] if row.user}
		leads = [row for row in self.audit_team or [] if row.team_role == "Lead Auditor"]
		if len(leads) != 1:
			frappe.throw(_("The audit team must contain exactly one Lead Auditor."))
		if leads[0].user != self.lead_auditor:
			frappe.throw(_("Lead Auditor must match the Lead Auditor team member."))
		if self.status != "Planned" and self.lead_auditor not in team_users:
			frappe.throw(_("Lead Auditor must be included in the audit team."))
		if self.status in {"Confirmed", "In Progress", "Findings Issued", "Report Issued", "Closed"}:
			for member in self.audit_team or []:
				if member.team_role in {"Lead Auditor", "Auditor"} and not member.independence_confirmed:
					frappe.throw(_("Independence must be confirmed for {0}.").format(member.user or member.external_name))
				if member.team_role == "External Auditor" and not member.confidentiality_agreement_signed:
					frappe.throw(_("External auditors must sign the confidentiality agreement before confirmation."))
			if any(row.team_role == "Auditor in Training" for row in self.audit_team or []) and not any(row.team_role in {"Auditor", "Lead Auditor"} for row in self.audit_team or []):
				frappe.throw(_("An Auditor in Training requires a Lead Auditor or Auditor to shadow."))
		for scope in self.scope_items or []:
			targets = {
				"Process": scope.process_reference, "Department": scope.department, "Location": scope.location,
				"Document": scope.controlled_document, "ISO Clause": scope.iso_clause,
				"Regulatory Section": scope.regulation, "Risk Item": scope.risk_item,
			}
			if scope.scope_type in targets and not targets[scope.scope_type]:
				frappe.throw(_("Scope Item {0} requires its {1} reference.").format(scope.idx, scope.scope_type))
			if scope.assigned_auditor and scope.process_owner_at_audit == scope.assigned_auditor:
				frappe.throw(_("The auditor assigned to scope item {0} cannot be its process owner.").format(scope.idx))
			if scope.process_owner_at_audit == self.lead_auditor:
				frappe.throw(_("Lead Auditor cannot be a process owner within the audit scope."))
			if scope.process_owner_at_audit in team_users:
				frappe.throw(_("Audit team members cannot audit a process they own (Scope Item {0}).").format(scope.idx))

		if self.status == "Findings Issued" and not (self.audit_checklists or self.findings or self.no_findings_attestation_text):
			frappe.throw(_("At least one checklist, finding, or a no-findings attestation is required before findings can be issued."))
		if self.status == "Report Issued" and not self.audit_report:
			frappe.throw(_("An Audit Report is required before the audit can be marked Report Issued."))
		if self.status == "Closed" and self.name and not self.is_new():
			open_findings = frappe.db.count(
				"Audit Finding",
				{"audit": self.name, "status": ["not in", ["Closed", "Withdrawn", "Voided"]]},
			)
			if open_findings:
				frappe.throw(_("All findings must be Closed, Withdrawn, or Voided before closing the audit."))

	def _validate_status_transition(self):
		if self.is_new():
			return
		before = self.get_doc_before_save()
		if before:
			validate_audit_transition(before.status, self.status)

	def before_save(self):
		if self.status == "In Progress":
			self.actual_start_date = self.actual_start_date or today()
			self.opening_meeting_date = self.opening_meeting_date or now_datetime()
		if self.status == "Findings Issued":
			self.actual_end_date = self.actual_end_date or today()
			self.closing_meeting_date = self.closing_meeting_date or now_datetime()
