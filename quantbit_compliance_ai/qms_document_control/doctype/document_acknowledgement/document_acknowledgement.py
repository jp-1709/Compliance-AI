import frappe
from frappe import _
from frappe.model.document import Document


class DocumentAcknowledgement(Document):
	def validate(self):
		if frappe.db.exists("Document Acknowledgement", {"document_version": self.document_version, "assigned_to": self.assigned_to, "name": ("!=", self.name or "")}):
			frappe.throw(_("An acknowledgement already exists for this user and version."))
		if self.status == "Waived":
			if not self.waived_by or not self.waiver_reason:
				frappe.throw(_("Waived By and Waiver Reason are required."))
			if self.waived_by == self.assigned_to:
				frappe.throw(_("Users cannot waive their own acknowledgement."))
