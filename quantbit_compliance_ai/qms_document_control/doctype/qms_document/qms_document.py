import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, nowdate

from quantbit_compliance_ai.qms_document_control.engine import refresh_review_schedule
from quantbit_compliance_ai.qms_document_control.utils import clause_list, get_settings, log_activity


class QMSDocument(Document):
	# ── validate() : rules 1-7 of spec §5.1 ────────────────────────────────
	def validate(self):
		if len((self.document_title or "").strip()) > 200:
			frappe.throw(_("Document Title cannot exceed 200 characters."))
		if not 1 <= (self.review_frequency_months or 0) <= 60:
			frappe.throw(_("Review Frequency must be between 1 and 60 months."))
		if not 1 <= (self.retention_years or 0) <= 50:
			frappe.throw(_("Retention Years must be between 1 and 50."))
		if not self.is_new() and self.has_value_changed("document_code"):
			frappe.throw(_("Document Code is immutable after creation."))
		if self.document_code and frappe.db.exists(
			"QMS Document",
			{"business_entity": self.business_entity, "document_code": self.document_code, "name": ("!=", self.name or "")},
		):
			frappe.throw(_("Document Code must be unique within the Business Entity."))

		settings = get_settings()
		if settings.require_clause_mapping and not clause_list(self):
			frappe.throw(_("At least one ISO clause must be linked (rule 1). Disable this in QMS Settings if not required."))
		if self.is_new() and not self.review_frequency_months:
			self.review_frequency_months = settings.default_review_frequency_months

		if self.lifecycle_state == "Archived" and not self.archived_reason:
			frappe.throw(_("Archive Reason is required for archived documents."))
		if self.lifecycle_state == "Withdrawn" and not self.withdrawn_reason:
			frappe.throw(_("Withdraw Reason is required for withdrawn documents."))

		if self.has_value_changed("lifecycle_state") and not self.is_new():
			self._lifecycle_changed_from = self.get_doc_before_save().lifecycle_state if self.get_doc_before_save() else None

	# ── on_update() : rules 8-9 of spec §5.2 ───────────────────────────────
	def on_update(self):
		if getattr(self, "_lifecycle_changed_from", None) and self._lifecycle_changed_from != self.lifecycle_state:
			log_activity(self, _("Lifecycle state changed"), f"{self._lifecycle_changed_from} → {self.lifecycle_state}")
		if self.next_review_date and self.lifecycle_state == "Active":
			refresh_review_schedule(self)

	# ── Actions ─────────────────────────────────────────────────────────────
	@frappe.whitelist()
	def archive(self, reason: str):
		if self.lifecycle_state != "Active":
			frappe.throw(_("Only an Active document can be archived."))
		if not reason:
			frappe.throw(_("Archive Reason is required."))
		self.lifecycle_state = "Archived"
		self.archived_on = nowdate()
		self.archived_reason = reason
		self.save(ignore_permissions=False)

	@frappe.whitelist()
	def withdraw_document(self, reason: str):
		if self.lifecycle_state == "Withdrawn":
			frappe.throw(_("Document is already Withdrawn."))
		if not reason:
			frappe.throw(_("Withdraw Reason is required."))
		self.lifecycle_state = "Withdrawn"
		self.withdrawn_on = nowdate()
		self.withdrawn_reason = reason
		self.save(ignore_permissions=False)
