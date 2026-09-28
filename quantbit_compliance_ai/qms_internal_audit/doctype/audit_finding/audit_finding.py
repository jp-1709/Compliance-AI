import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime, today

from quantbit_compliance_ai.qms_internal_audit.state_engine import check_and_close_audit, validate_finding_transition


class AuditFinding(Document):
	def validate(self):
		if not self.is_new():
			before = self.get_doc_before_save()
			if before:
				validate_finding_transition(before.status, self.status)

		is_nc = self.finding_type == "NC (Non-Conformity)"
		self.corrective_action_required = 1 if is_nc else 0
		if is_nc and not self.severity:
			frappe.throw(_("Severity is required for a non-conformity."))
		if is_nc and not self.target_correction_date:
			frappe.throw(_("Target Correction Date is required for a non-conformity."))
		if self.status not in {"Draft", "Voided"} and len((self.finding_statement or "").strip()) < 150:
			frappe.throw(_("Finding Statement must contain at least 150 characters before issuance."))
		if self.status not in {"Draft", "Pending Lead Approval", "Voided"} and not self.lead_auditor_approved:
			frappe.throw(_("Lead Auditor approval is required before issuing a finding."))
		if self.severity == "Major" and self.status != "Draft" and not self.evidence_files:
			frappe.throw(_("At least one evidence file is required for a Major finding."))
		if self.auditee_disputes and self.status == "Disputed" and not self.auditee_response:
			frappe.throw(_("Auditee Response is required for a disputed finding."))
		if self.status == "Withdrawn" and self.dispute_resolution != "Withdrawn (post-dispute outcome)":
			frappe.throw(_("A withdrawn finding requires the withdrawn dispute outcome."))
		if self.status == "Closed":
			if not self.closure_evidence_summary or not self.closure_verified_by:
				frappe.throw(_("Closure evidence and an independent verifier are required to close a finding."))
			if self.closure_verified_by == self.auditor_raised_by:
				frappe.throw(_("The finding author cannot verify its closure."))
			if self.linked_capa:
				capa = frappe.db.get_value("CAPA Case", self.linked_capa, ["capa_owner", "effectiveness_check"], as_dict=True)
				verifier = frappe.db.get_value("Effectiveness Check", capa.effectiveness_check, "verifier") if capa and capa.effectiveness_check else None
				if capa and self.closure_verified_by in {capa.capa_owner, verifier}:
					frappe.throw(_("Finding closure verifier must differ from the CAPA Owner and Effectiveness Verifier."))
		if self.is_voided and not self.void_reason:
			frappe.throw(_("Void Reason is required for a voided finding."))

	def before_save(self):
		if self.lead_auditor_approved and not self.lead_auditor_approved_at:
			self.lead_auditor_approved_at = now_datetime()
		if self.auditee_acknowledged and not self.auditee_acknowledged_at:
			self.auditee_acknowledged_at = now_datetime()
		if self.status == "Closed" and not self.actual_close_date:
			self.actual_close_date = today()
		if self.status == "Closed" and not self.closure_verified_at:
			self.closure_verified_at = now_datetime()

	def on_update(self):
		# §3.2 auto-transition: Report Issued -> Closed once every finding is
		# resolved. check_and_close_audit() writes via db_set only.
		if self.status in ("Closed", "Withdrawn", "Voided") and self.audit:
			check_and_close_audit(self.audit)
