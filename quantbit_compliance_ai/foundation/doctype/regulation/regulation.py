"""
regulation.py — Frappe DocType controller for Regulation (F6)
Quantbit Compliance AI · Foundation Module

Implements the Regulation Lifecycle workflow (spec §6):

    Not Started -> AI Extracted -> Under Legal Review -> Published -> (Superseded)

`extraction_status` is the workflow-state field (the spec's diagram calls the
initial state "Draft"; the shipped Select options use "Not Started" for the
same state — both refer to the same class of record: nothing extracted yet).

Business rules:
  - Cannot jump straight to Published; must pass through Under Legal Review.
  - Published requires validated_by + validated_on (a Legal Counsel sign-off).
  - validated_by must actually hold a role capable of legal review (Legal
    Counsel or System Manager) — mirrors the independence pattern used
    throughout the QMS modules (reviewer/approver must be a real person with
    the right role, not just any user).
  - Once Published, the record is locked except by System Manager (Legal
    Counsel cannot silently rewrite a published regulation).
  - Setting `supersedes` on a newly Published regulation atomically marks the
    prior regulation `is_active = 0` (§7 tricky point 6: the old obligations
    must still be retrievable for audit history, so this is a soft flag, not
    a delete).
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, today

_TRANSITIONS = {
	"Not Started": {"AI Extracted"},
	"AI Extracted": {"Under Legal Review"},
	"Under Legal Review": {"Published", "AI Extracted"},
	"Published": set(),  # terminal; superseded via a *new* Regulation record, not a status change
}

_LEGAL_REVIEW_ROLES = ("Legal Counsel", "System Manager")


class Regulation(Document):
	def validate(self):
		self._validate_transition()

		if self.effective_date and self.enacted_date and getdate(self.effective_date) < getdate(self.enacted_date):
			frappe.throw(_("Effective Date cannot be before Enacted Date."))
		if self.last_amended and self.effective_date and getdate(self.last_amended) < getdate(self.effective_date):
			frappe.throw(_("Last Amended cannot be before Effective Date."))

		if self.extraction_status == "Published":
			if not self.validated_by or not self.validated_on:
				frappe.throw(_("A Published regulation requires Validated By and Validated On."))
			if getdate(self.validated_on) > getdate(today()):
				frappe.throw(_("Validated On cannot be in the future."))
			roles = frappe.get_roles(self.validated_by)
			if not any(r in roles for r in _LEGAL_REVIEW_ROLES):
				frappe.throw(_("Validated By ({0}) must hold the Legal Counsel role.").format(self.validated_by))

		if self.supersedes and self.supersedes == self.name:
			frappe.throw(_("A regulation cannot supersede itself."))

		if not self.is_new() and self.get_doc_before_save():
			before = self.get_doc_before_save()
			if before.extraction_status == "Published" and self.extraction_status == "Published":
				# Published is locked: only System Manager may still edit substantive fields
				# (e.g. to fix a typo); everyone else must raise a new regulation record.
				if "System Manager" not in frappe.get_roles(frappe.session.user):
					frappe.throw(_("Published regulations are locked. Only a System Manager may amend one directly; otherwise supersede it with a new record."))

	def _validate_transition(self):
		if self.is_new():
			return
		before = self.get_doc_before_save()
		if not before or before.extraction_status == self.extraction_status:
			return
		allowed = _TRANSITIONS.get(before.extraction_status, set())
		if self.extraction_status not in allowed:
			frappe.throw(
				_("Cannot move Regulation from {0} to {1} directly. Allowed next states: {2}.").format(
					before.extraction_status, self.extraction_status, ", ".join(sorted(allowed)) or "(terminal)"
				)
			)

	def on_update(self):
		# §7 tricky point 6: publishing a superseding regulation retires the
		# old one (soft flag only — history stays queryable for audit).
		if self.extraction_status == "Published" and self.supersedes and self.has_value_changed("extraction_status"):
			if frappe.db.get_value("Regulation", self.supersedes, "is_active"):
				frappe.db.set_value("Regulation", self.supersedes, "is_active", 0)
				frappe.get_doc("Regulation", self.supersedes).add_comment(
					"Info", _("Superseded by {0}, published {1}.").format(self.name, self.validated_on)
				)

	# ─── API helpers ─────────────────────────────────────────────────────────

	def get_superseding_chain(self) -> list:
		"""Walk forward through supersession links, most recent last."""
		chain = [self.name]
		current = self.name
		seen = {current}
		while True:
			nxt = frappe.db.get_value("Regulation", {"supersedes": current}, "name")
			if not nxt or nxt in seen:
				break
			chain.append(nxt)
			seen.add(nxt)
			current = nxt
		return chain
