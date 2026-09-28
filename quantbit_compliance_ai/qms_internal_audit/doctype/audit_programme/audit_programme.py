import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_months, getdate

from quantbit_compliance_ai.qms_validation import as_list


class AuditProgramme(Document):
	def validate(self):
		if self.programme_period_start and self.programme_period_end:
			start, end = getdate(self.programme_period_start), getdate(self.programme_period_end)
			if end <= start:
				frappe.throw(_("Programme Period End must be after Period Start."))
			if end > add_months(start, 24):
				frappe.throw(_("An Audit Programme cannot span more than 24 months."))
		entities = as_list(self.entities_in_scope)
		entity_names = {row.get("business_entity") if isinstance(row, dict) else row for row in entities}
		if self.business_entity and self.business_entity not in entity_names:
			frappe.throw(_("Entities in Scope must include the programme Business Entity."))
		if self.programme_status in {"Approved", "Active", "Completed"} and (not self.approved_by or not self.approved_on):
			frappe.throw(_("Approved By and Approved On are required before activating the programme."))
		if self.programme_status == "Completed" and not self.is_new():
			open_audits = frappe.db.count("Audit", {"audit_programme": self.name, "status": ["not in", ["Closed", "Cancelled"]]})
			if open_audits:
				frappe.throw(_("All audits in the programme must be Closed or Cancelled before completion."))
