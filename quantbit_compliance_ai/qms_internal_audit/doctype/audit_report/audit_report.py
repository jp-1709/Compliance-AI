import frappe
from frappe import _
from frappe.model.document import Document


class AuditReport(Document):
	def validate(self):
		if self.is_current_version:
			duplicate = frappe.db.exists("Audit Report", {"audit": self.audit, "is_current_version": 1, "name": ("!=", self.name or "")})
			if duplicate:
				frappe.throw(_("Only one Audit Report version can be current for an audit."))
		if self.status in {"Issued", "Re-Issued"}:
			if not self.lead_auditor_signature:
				frappe.throw(_("Lead Auditor Signature is required before issuing the report."))
			if not self.report_pdf or not self.report_pdf_hash:
				frappe.throw(_("Issued reports require a generated PDF and PDF hash."))
		if self.status == "Re-Issued" and not self.prior_version:
			frappe.throw(_("A Re-Issued report must reference its prior version."))
