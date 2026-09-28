import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, now_datetime


class DocumentVersion(Document):
	def validate(self):
		match = re.fullmatch(r"v(\\d+)\\.(\\d+)", self.version_number or "")
		if not match:
			frappe.throw(_("Version must use the vX.Y format, for example v1.0."))
		if self.reviewed_by and self.reviewed_by == self.prepared_by:
			frappe.throw(_("Reviewer must differ from Prepared By."))
		if self.approved_by and self.approved_by == self.prepared_by:
			frappe.throw(_("Approver must differ from Prepared By."))
		if self.review_outcome == "Returned" and not self.review_comments:
			frappe.throw(_("Review Comments are required when a version is returned."))
		prior = frappe.get_all("Document Version", filters={"qms_document": self.qms_document, "name": ("!=", self.name or "")}, pluck="version_number")
		versions = [tuple(map(int, re.fullmatch(r"v(\\d+)\\.(\\d+)", value).groups())) for value in prior if re.fullmatch(r"v(\\d+)\\.(\\d+)", value or "")]
		if versions and tuple(map(int, match.groups())) <= max(versions):
			frappe.throw(_("Version Number must increase monotonically."))
		if prior and not self.change_summary:
			frappe.throw(_("Summary of Changes is required after the first version."))
		if self.effective_date_override and self.approved_on and getdate(self.effective_date_override) < getdate(self.approved_on):
			frappe.throw(_("Effective Date Override cannot precede approval."))

	def before_submit(self):
		if not self.reviewed_by or self.review_outcome != "Approved":
			frappe.throw(_("An approved independent review is required before submission."))
		if not self.approved_by or not self.hash_sha256 or not self.file_attachment:
			frappe.throw(_("Approver, controlled file, and SHA-256 hash are required."))
		self.approved_on = self.approved_on or now_datetime()
		self.status = "Approved"
