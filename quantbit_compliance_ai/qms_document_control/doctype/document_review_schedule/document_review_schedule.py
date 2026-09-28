import frappe
from frappe import _
from frappe.model.document import Document


class DocumentReviewSchedule(Document):
	def validate(self):
		if self.status == "Completed" and (not self.outcome or not self.completed_on):
			frappe.throw(_("Completed reviews require Outcome and Completed On."))
		if self.outcome == "Revised" and not self.linked_new_version:
			frappe.throw(_("A revised review must link the new Document Version."))
