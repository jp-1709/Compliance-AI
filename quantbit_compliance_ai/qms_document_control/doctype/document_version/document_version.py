import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, now_datetime

from quantbit_compliance_ai.qms_document_control.engine import publish_version, withdraw_version
from quantbit_compliance_ai.qms_document_control.utils import (
	compute_sha256,
	get_settings,
	is_version_greater,
	parse_version,
)


class DocumentVersion(Document):
	# ── validate() : rules 10-19 of spec §5.3 ──────────────────────────────
	def validate(self):
		if not parse_version(self.version_number):
			frappe.throw(_("Version must use the vX.Y format, for example v0.1 or v1.0."))

		siblings = frappe.get_all(
			"Document Version",
			filters={"qms_document": self.qms_document, "name": ("!=", self.name or "")},
			fields=["name", "version_number", "status"],
		)

		# TP-03/rule 11: strictly increasing version numbers, no gaps required.
		if siblings and not is_version_greater(self.version_number, [s.version_number for s in siblings]):
			frappe.throw(_("Version Number must increase monotonically for this document."))

		# rule 16 / US Q1-US-02: only one Draft version per document at a time.
		if self.status == "Draft":
			other_draft = [s for s in siblings if s.status == "Draft"]
			if other_draft:
				frappe.throw(
					_("Only one Draft version may exist at a time. Submit or discard {0} first.").format(
						other_draft[0].name
					)
				)

		if siblings and not self.change_summary:
			frappe.throw(_("Summary of Changes is required for revisions."))

		# rule 13/14/15: maker-checker independence.
		if self.reviewed_by and self.reviewed_by == self.prepared_by:
			frappe.throw(_("Reviewer cannot be the same person as the preparer."))
		if self.approved_by and self.approved_by == self.prepared_by:
			frappe.throw(_("Approver cannot be the same person as the preparer."))
		settings = get_settings()
		if (
			self.approved_by
			and self.reviewed_by
			and self.approved_by == self.reviewed_by
			and not settings.allow_approver_equal_reviewer
		):
			frappe.throw(_("Approver cannot be the same person as the reviewer."))

		if self.review_outcome == "Returned" and not self.review_comments:
			frappe.throw(_("Reviewer Comments are required when a version is returned."))

		if self.effective_date_override and self.approved_on and getdate(self.effective_date_override) < getdate(
			self.approved_on
		):
			frappe.throw(_("Effective Date Override cannot precede the approval date."))

		# TP-18: a Withdrawn/Archived document cannot receive new drafts.
		lifecycle_state = frappe.db.get_value("QMS Document", self.qms_document, "lifecycle_state")
		if self.is_new() and lifecycle_state in ("Archived", "Withdrawn"):
			frappe.throw(_("Cannot draft new versions for an Archived or Withdrawn document."))

		# rule 19: recompute file hash whenever the attachment changes (TP-08 guard).
		if self.file_attachment and self.has_value_changed("file_attachment"):
			digest, size = compute_sha256(self.file_attachment)
			self.hash_sha256 = digest
			self.file_size_bytes = size
		elif not self.is_new() and self.has_value_changed("file_attachment") and self.status == "Approved":
			frappe.throw(_("The controlled file of an Approved version cannot be replaced (TP-08). Draft a new version instead."))

	# ── before_submit() : the "Approve" action, rules 20-23 ────────────────
	def before_submit(self):
		if self.status != "In Review" or self.review_outcome != "Approved":
			frappe.throw(_("Cannot approve a version that has not been reviewed-and-approved by the reviewer."))
		if not self.approved_by:
			self.approved_by = frappe.session.user
		if self.approved_by == self.prepared_by:
			frappe.throw(_("Approver cannot be the same person as the preparer."))
		if not self.file_attachment or not self.hash_sha256:
			frappe.throw(_("A controlled file with a computed SHA-256 hash is required to approve."))
		self.approved_on = self.approved_on or now_datetime()
		self.status = "Approved"
		self.append(
			"approval_steps",
			{
				"step_role": "Approver",
				"acted_by": self.approved_by,
				"acted_on": self.approved_on,
				"outcome": "Approved",
			},
		)

	# ── on_submit() : the atomic publish transaction, §5.5 ─────────────────
	def on_submit(self):
		self._publish_result = publish_version(self)

	# ── before_cancel() : the "Withdraw" action, §5.6 / TP-23 ──────────────
	def before_cancel(self):
		if not frappe.has_permission(self.doctype, "cancel", self):
			frappe.throw(_("Insufficient permissions to withdraw a controlled document version."), frappe.PermissionError)
		reason = (self.flags.withdrawal_reason or "").strip()
		if not reason:
			frappe.throw(_("A withdrawal reason is required."))
		self.status = "Withdrawn"

	def on_cancel(self):
		withdraw_version(self, self.flags.withdrawal_reason)
