"""
quantbit_compliance_ai/qms_document_control/engine.py

The Q1 QMS Document Control state-machine engines. Every function here is the
single place a given business rule from `13_qms_document_control.md` §5/§6/§7
lives — controllers call into this module rather than re-implementing the
transaction inline, so the "atomic publish" and "atomic withdraw" invariants
can never drift between the DocType controller and the API layer.

Engines:
  - publish_version()            : Approved-version atomic transaction (§5.5)
  - withdraw_version()           : Withdraw/cancel atomic transaction (§5.6, TP-23)
  - fan_out_acknowledgements()   : distribution → Document Acknowledgement fan-out
  - refresh_review_schedule()    : cadence engine — creates Document Review Schedule
                                    entries inside the fan-out horizon (§5.10)
  - confirm_review_no_change()   : periodic-review confirm without a new version
  - mark_overdue_acknowledgements() / mark_overdue_review_schedules():
                                    idempotent sweeps, safe to call from a
                                    scheduler once one is wired up.
"""

import frappe
from frappe import _
from frappe.utils import add_days, date_diff, getdate, now_datetime, nowdate

from quantbit_compliance_ai.qms_document_control.utils import (
	compute_next_review_date,
	get_settings,
	log_activity,
	resolve_distribution_lists,
)


# ──────────────────────────────────────────────────────────────────────────────
# APPROVAL / PUBLISH  (ISO 9001 §7.5.3.2(c)/(e) — control of changes, obsolete info)
# ──────────────────────────────────────────────────────────────────────────────

def publish_version(version) -> dict:
	"""Atomically promote `version` (already status=Approved, docstatus=1) to be
	the QMS Document's current, in-force version. Called from
	Document Version.on_submit(). Every side effect happens in one DB
	transaction so the register can never observe two Approved versions of
	the same document, nor zero (TP-22)."""
	qdoc_name = version.qms_document

	# Row-lock the parent for the duration of this transaction: two concurrent
	# approvals of two different draft versions of the same document must be
	# serialised, not interleaved (TP-22).
	locked = frappe.db.sql(
		"SELECT name, review_frequency_months FROM `tabQMS Document` WHERE name=%s FOR UPDATE",
		(qdoc_name,),
		as_dict=True,
	)
	if not locked:
		frappe.throw(_("QMS Document {0} no longer exists.").format(qdoc_name))
	review_months = locked[0].review_frequency_months

	# Supersede whatever was previously Approved for this document.
	prior_name = frappe.db.get_value(
		"Document Version",
		{"qms_document": qdoc_name, "status": "Approved", "name": ("!=", version.name)},
		"name",
	)
	if prior_name:
		frappe.db.set_value(
			"Document Version",
			prior_name,
			{"status": "Superseded", "superseded_on": now_datetime(), "superseded_by": version.name},
			update_modified=False,
		)
		frappe.publish_realtime(
			"document_version_superseded", {"version": prior_name, "by": version.name}
		)

	effective_date = (
		getdate(version.effective_date_override)
		if version.effective_date_override
		else getdate(version.approved_on)
	)
	next_review_date = compute_next_review_date(effective_date, review_months)
	frappe.db.set_value(
		"QMS Document",
		qdoc_name,
		{
			"current_version": version.name,
			"effective_date": effective_date,
			"next_review_date": next_review_date,
		},
	)

	# Any acknowledgement still Pending against the superseded version is moot.
	if prior_name:
		for name in frappe.get_all(
			"Document Acknowledgement",
			filters={"document_version": prior_name, "status": "Pending", "docstatus": 0},
			pluck="name",
		):
			ack = frappe.get_doc("Document Acknowledgement", name)
			ack.status = "Waived"
			ack.waived_by = "Administrator"
			ack.waiver_reason = "Revision superseded"
			ack.flags.ignore_permissions = True
			ack.save()

	qdoc = frappe.get_doc("QMS Document", qdoc_name)
	created = fan_out_acknowledgements(qdoc, version, effective_date)

	log_activity(qdoc, _("Version approved"), f"{version.version_number} by {version.approved_by}")
	refresh_review_schedule(qdoc)
	frappe.publish_realtime("qms_document_approved", {"document": qdoc_name, "version": version.name})

	return {
		"supersedes": prior_name,
		"acknowledgements_created": created,
		"effective_date": str(effective_date),
		"next_review_date": str(next_review_date) if next_review_date else None,
	}


def fan_out_acknowledgements(qdoc, version, effective_date) -> int:
	"""Create Pending Document Acknowledgement rows for every user resolved
	from the document's distribution lists who does not already have one for
	this version (idempotent — safe to call again after adding members)."""
	created = 0
	for user, window_days in resolve_distribution_lists(qdoc).items():
		if frappe.db.exists(
			"Document Acknowledgement", {"document_version": version.name, "assigned_to": user}
		):
			continue
		frappe.get_doc(
			{
				"doctype": "Document Acknowledgement",
				"qms_document": qdoc.name,
				"document_version": version.name,
				"assigned_to": user,
				"due_on": add_days(effective_date, window_days),
				"status": "Pending",
				"business_entity": qdoc.business_entity,
			}
		).insert(ignore_permissions=True)
		created += 1
	return created


def refan_acknowledgements(version_name: str) -> int:
	"""Manual re-fan for users added to a distribution list after a version
	was already published (TP-13). Never back-dates or duplicates existing
	acknowledgements."""
	version = frappe.get_doc("Document Version", version_name)
	if version.status != "Approved":
		frappe.throw(_("Only the currently Approved version can be re-fanned."))
	qdoc = frappe.get_doc("QMS Document", version.qms_document)
	return fan_out_acknowledgements(qdoc, version, qdoc.effective_date or getdate())


# ──────────────────────────────────────────────────────────────────────────────
# WITHDRAWAL  (Document Version cancel / Approved → Withdrawn, TP-23)
# ──────────────────────────────────────────────────────────────────────────────

def withdraw_version(version, reason: str):
	"""Atomically withdraw an Approved version. Promotes the most recently
	Superseded version back to Approved if one exists; otherwise the document
	is left with no current version. Auto-waives any still-Pending
	acknowledgements for the withdrawn version."""
	if not reason:
		frappe.throw(_("A withdrawal reason is required."))

	qdoc_name = version.qms_document
	frappe.db.sql("SELECT name FROM `tabQMS Document` WHERE name=%s FOR UPDATE", (qdoc_name,))

	promoted = frappe.db.get_value(
		"Document Version",
		{"qms_document": qdoc_name, "status": "Superseded", "name": ("!=", version.name)},
		"name",
		order_by="superseded_on desc",
	)
	if promoted:
		frappe.db.set_value(
			"Document Version", promoted, {"status": "Approved", "superseded_on": None, "superseded_by": None}
		)
		promoted_row = frappe.db.get_value(
			"Document Version", promoted, ["approved_on", "effective_date_override"], as_dict=True
		)
		effective_date = (
			getdate(promoted_row.effective_date_override)
			if promoted_row.effective_date_override
			else getdate(promoted_row.approved_on)
		)
		review_months = frappe.db.get_value("QMS Document", qdoc_name, "review_frequency_months")
		frappe.db.set_value(
			"QMS Document",
			qdoc_name,
			{
				"current_version": promoted,
				"effective_date": effective_date,
				"next_review_date": compute_next_review_date(effective_date, review_months),
			},
		)
	else:
		frappe.db.set_value(
			"QMS Document", qdoc_name, {"current_version": None, "effective_date": None, "next_review_date": None}
		)

	for name in frappe.get_all(
		"Document Acknowledgement",
		filters={"document_version": version.name, "status": "Pending", "docstatus": 0},
		pluck="name",
	):
		ack = frappe.get_doc("Document Acknowledgement", name)
		ack.status = "Waived"
		ack.waived_by = frappe.session.user
		ack.waiver_reason = "Version withdrawn"
		ack.flags.ignore_permissions = True
		ack.save()

	log_activity(
		frappe.get_doc("QMS Document", qdoc_name), _("Version withdrawn"), f"{version.version_number}: {reason}"
	)


# ──────────────────────────────────────────────────────────────────────────────
# CADENCE ENGINE  (ISO 9001 §7.5.3.1 — documented information is maintained)
# ──────────────────────────────────────────────────────────────────────────────

def refresh_review_schedule(qdoc):
	"""Create a Document Review Schedule entry once the document's
	next_review_date falls inside the fan-out horizon, unless an Open/In
	Progress one already exists for that due date (idempotent)."""
	settings = get_settings()
	if not qdoc.next_review_date:
		return None
	if date_diff(qdoc.next_review_date, nowdate()) > settings.review_fanout_horizon_days:
		return None
	if frappe.db.exists(
		"Document Review Schedule",
		{"qms_document": qdoc.name, "due_on": qdoc.next_review_date, "status": ("in", ["Open", "In Progress"])},
	):
		return None
	schedule = frappe.get_doc(
		{
			"doctype": "Document Review Schedule",
			"qms_document": qdoc.name,
			"due_on": qdoc.next_review_date,
			"assigned_to": qdoc.document_owner,
			"status": "Open",
			"business_entity": qdoc.business_entity,
		}
	)
	schedule.insert(ignore_permissions=True)
	return schedule.name


def confirm_review_no_change(qms_document_name: str, peer_reviewer: str, notes: str = "") -> dict:
	"""US Q1-US-05 / TP-18,19,20: confirm a periodic review with no new
	version, rolling next_review_date forward without forcing a meaningless
	revision. Enforces a second pair of eyes (peer_reviewer != document_owner)
	and blocks confirmation more than review_confirm_window_days early."""
	qdoc = frappe.get_doc("QMS Document", qms_document_name)

	if not qdoc.current_version or frappe.db.get_value("Document Version", qdoc.current_version, "status") != "Approved":
		frappe.throw(_("Only documents with a currently Approved version can have their review confirmed."))
	if not qdoc.next_review_date:
		frappe.throw(_("This document has no scheduled review date."))
	if peer_reviewer == qdoc.document_owner:
		frappe.throw(_("The peer reviewer must be a different person from the Document Owner (TP-20)."))

	settings = get_settings()
	days_to_due = date_diff(qdoc.next_review_date, nowdate())
	if days_to_due > settings.review_confirm_window_days:
		frappe.throw(
			_("Periodic review can only be confirmed within {0} days of the due date.").format(
				settings.review_confirm_window_days
			)
		)

	open_schedule = frappe.db.get_value(
		"Document Review Schedule",
		{"qms_document": qdoc.name, "status": ("in", ["Open", "In Progress", "Overdue"])},
		"name",
	)
	if open_schedule:
		schedule = frappe.get_doc("Document Review Schedule", open_schedule)
		schedule.status = "Completed"
		schedule.outcome = "No change"
		schedule.completed_on = now_datetime()
		schedule.flags.ignore_permissions = True
		schedule.save()

	# TP-19: even a late (overdue) confirmation is allowed — it is logged, not blocked.
	base_date = nowdate() if days_to_due < 0 else qdoc.next_review_date
	new_next_review = compute_next_review_date(base_date, qdoc.review_frequency_months)
	frappe.db.set_value("QMS Document", qdoc.name, "next_review_date", new_next_review)

	late_note = " (review was overdue)" if days_to_due < 0 else ""
	log_activity(
		qdoc, _("Periodic review confirmed — no change"), f"peer reviewer {peer_reviewer}{late_note}. {notes}"
	)
	return {"next_review_date": str(new_next_review)}


# ──────────────────────────────────────────────────────────────────────────────
# IDEMPOTENT SWEEPS  (not yet wired to hooks.py scheduler_events by design —
# call these from a management command or a future scheduled task)
# ──────────────────────────────────────────────────────────────────────────────

def mark_overdue_acknowledgements() -> int:
	names = frappe.get_all(
		"Document Acknowledgement",
		filters={"status": "Pending", "due_on": ("<", nowdate())},
		pluck="name",
	)
	for name in names:
		frappe.db.set_value("Document Acknowledgement", name, "status", "Overdue")
	return len(names)


def mark_overdue_review_schedules() -> int:
	names = frappe.get_all(
		"Document Review Schedule",
		filters={"status": ("in", ["Open", "In Progress"]), "due_on": ("<", nowdate())},
		pluck="name",
	)
	for name in names:
		frappe.db.set_value("Document Review Schedule", name, "status", "Overdue")
	return len(names)
