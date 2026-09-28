import frappe
from frappe import _
from frappe.model.document import Document

from quantbit_compliance_ai.qms_validation import text_length


class AuditChecklist(Document):
	def validate(self):
		items = self.items or []
		self.total_items = len(items)
		self.items_completed = sum(1 for row in items if row.auditor_response)
		self.items_with_findings = sum(1 for row in items if row.linked_finding)
		self.completion_pct = (self.items_completed * 100 / self.total_items) if self.total_items else 0
		for row in items:
			if row.auditor_response == "Non-Conformant" and not row.linked_finding:
				frappe.throw(_("Checklist Item {0} requires a linked Audit Finding when Non-Conformant.").format(row.item_no or row.idx))
			if row.auditor_response == "Not Applicable" and text_length(row.auditor_notes) < 30:
				frappe.throw(_("Checklist Item {0} requires at least 30 characters explaining Not Applicable.").format(row.item_no or row.idx))
			if row.risk_weighting == "High" and row.auditor_response != "Skipped" and not row.evidence_files:
				frappe.throw(_("High-risk Checklist Item {0} requires evidence.").format(row.item_no or row.idx))
		if self.status in {"Complete", "Reviewed"} and any(not row.auditor_response for row in items):
			frappe.throw(_("Every checklist item requires a response before completion."))
		if self.status == "Reviewed" and not self.reviewed_by:
			frappe.throw(_("Reviewed By is required for a Reviewed checklist."))
