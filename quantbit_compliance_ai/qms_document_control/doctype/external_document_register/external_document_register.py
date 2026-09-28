import frappe
from frappe import _
from frappe.model.document import Document


class ExternalDocumentRegister(Document):
	def validate(self):
		if frappe.db.exists("External Document Register", {"business_entity": self.business_entity, "issuing_authority": self.issuing_authority, "original_reference": self.original_reference, "version_or_edition": self.version_or_edition, "name": ("!=", self.name or "")}):
			frappe.throw(_("This external document edition is already registered."))
