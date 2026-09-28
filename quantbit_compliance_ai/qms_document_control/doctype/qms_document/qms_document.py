import frappe
from frappe import _
from frappe.model.document import Document


class QMSDocument(Document):
	def validate(self):
		if len((self.document_title or "").strip()) > 200:
			frappe.throw(_("Document Title cannot exceed 200 characters."))
		if not 1 <= (self.review_frequency_months or 0) <= 60:
			frappe.throw(_("Review Frequency must be between 1 and 60 months."))
		if not 1 <= (self.retention_years or 0) <= 50:
			frappe.throw(_("Retention Years must be between 1 and 50."))
		if not self.is_new() and self.has_value_changed("document_code"):
			frappe.throw(_("Document Code is immutable after creation."))
		if self.document_code and frappe.db.exists("QMS Document", {"business_entity": self.business_entity, "document_code": self.document_code, "name": ("!=", self.name or "")}):
			frappe.throw(_("Document Code must be unique within the Business Entity."))
		if self.lifecycle_state == "Archived" and not self.archived_reason:
			frappe.throw(_("Archive Reason is required for archived documents."))
		if self.lifecycle_state == "Withdrawn" and not self.withdrawn_reason:
			frappe.throw(_("Withdraw Reason is required for withdrawn documents."))
