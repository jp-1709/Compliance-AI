import frappe
from frappe import _
from frappe.model.document import Document

from quantbit_compliance_ai.qms_validation import as_list, text_length


class RCARecord(Document):
	def validate(self):
		methods = {str(value).lower().replace(" ", "_") for value in as_list(self.methods_used)}
		if any("why" in method for method in methods):
			levels = [row.level for row in self.five_whys or []]
			if len(levels) < 5 or sorted(levels) != list(range(1, len(levels) + 1)):
				frappe.throw(_("5 Whys must contain at least five consecutive levels starting at 1."))
			if sum(1 for row in self.five_whys if row.is_root) != 1:
				frappe.throw(_("5 Whys must identify exactly one root cause."))
		if any("fishbone" in method for method in methods):
			categories = {row.category for row in self.fishbone_causes or [] if row.category}
			if len(categories) < 3:
				frappe.throw(_("Fishbone analysis must cover at least three cause categories."))
		if self.status in {"Complete", "Reviewed"} and text_length(self.identified_root_causes) < 50:
			frappe.throw(_("Identified Root Causes must contain at least 50 characters before completion."))
		if self.status == "Reviewed":
			if not self.reviewed_by:
				frappe.throw(_("Reviewed By is required for a Reviewed RCA."))
			if self.reviewed_by == self.analyst:
				frappe.throw(_("RCA reviewer must differ from the analyst."))
