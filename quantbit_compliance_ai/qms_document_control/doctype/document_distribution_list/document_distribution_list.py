import frappe
from frappe import _
from frappe.model.document import Document


class DocumentDistributionList(Document):
	def validate(self):
		if not self.members:
			frappe.throw(_("At least one distribution member is required."))
		if (self.acknowledgement_window_days or 0) <= 0:
			frappe.throw(_("Acknowledgement Window must be greater than zero."))
		if frappe.db.exists("Document Distribution List", {"business_entity": self.business_entity, "list_name": self.list_name, "name": ("!=", self.name or "")}):
			frappe.throw(_("List Name must be unique within the Business Entity."))
		for row in self.members or []:
			required = {"User": row.user, "Department + Designation Rule": row.department, "Role": row.role}.get(row.member_kind)
			if not required:
				frappe.throw(_("Distribution member {0} is incomplete for kind {1}.").format(row.idx, row.member_kind))
