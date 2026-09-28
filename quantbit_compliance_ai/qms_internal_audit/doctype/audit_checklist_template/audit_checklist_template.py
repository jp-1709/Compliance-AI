import frappe
from frappe import _
from frappe.model.document import Document

from quantbit_compliance_ai.qms_validation import text_length


class AuditChecklistTemplate(Document):
	def validate(self):
		duplicate = frappe.db.exists("Audit Checklist Template", {
			"organisation": self.organisation, "template_name": self.template_name,
			"template_version": self.template_version, "name": ("!=", self.name or ""),
		})
		if duplicate:
			frappe.throw(_("Template Version must be unique for this Organisation and Template Name."))
		for row in self.items or []:
			if text_length(row.question) < 20:
				frappe.throw(_("Checklist Item {0} question must contain at least 20 characters.").format(row.idx))
		self.total_items = len(self.items or [])
		if not self.is_new() and (self.times_used or 0) > 0:
			old = self.get_doc_before_save()
			if old and (old.items != self.items or old.template_name != self.template_name or old.template_version != self.template_version):
				frappe.throw(_("A used checklist template is immutable; create a new version instead."))
