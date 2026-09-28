import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class DocumentAccessLog(Document):
	def before_insert(self):
		if not self.accessed_by and not self.external_token:
			frappe.throw(_("Accessed By or External Token is required."))
		self.accessed_on = self.accessed_on or now_datetime()

	def validate(self):
		if not self.is_new():
			frappe.throw(_("Document Access Logs are append-only and cannot be edited."))
