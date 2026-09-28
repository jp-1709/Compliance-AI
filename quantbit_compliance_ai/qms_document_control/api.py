"""
quantbit_compliance_ai/qms_document_control/api.py

Whitelisted API surface for the Q1 QMS Document Control module (spec §8,
narrowed to the "controllers + engines + key APIs" build depth: no reports/
dashboards/print-format/AI-hook/scheduled-task wiring here — those consume
these primitives later).

Every mutating call routes through the engine module (see engine.py) so the
atomic publish/withdraw invariants are enforced identically whether triggered
from the API, the Desk UI, or a future background job.
"""

import json
import statistics

import frappe
from frappe import _
from frappe.utils import add_days, date_diff, getdate, now_datetime, nowdate

from quantbit_compliance_ai.qms_document_control.engine import (
	confirm_review_no_change as _confirm_review_no_change,
	mark_overdue_acknowledgements,
	mark_overdue_review_schedules,
	refan_acknowledgements as _refan_acknowledgements,
)
from quantbit_compliance_ai.qms_document_control.utils import (
	clause_list,
	clause_matches,
	get_settings,
	log_activity,
)

MANAGER_ROLES = ("System Manager", "QMS Administrator", "QMS Manager")
ADMIN_ROLES = ("System Manager", "QMS Administrator")


def _has_role(*roles) -> bool:
	user_roles = frappe.get_roles(frappe.session.user)
	return any(r in user_roles for r in roles)


def _require_role(*roles):
	if not _has_role(*roles):
		frappe.throw(_("Permission denied. Required role(s): {0}.").format(", ".join(roles)), frappe.PermissionError)


def _can_author(qdoc) -> bool:
	if _has_role(*MANAGER_ROLES):
		return True
	user = frappe.session.user
	if qdoc.document_owner == user:
		return True
	drafters = qdoc.get("document_drafters")
	if isinstance(drafters, str):
		drafters = json.loads(drafters or "[]")
	return user in (drafters or [])


# ──────────────────────────────────────────────────────────────────────────────
# 1. DOCUMENT MASTER
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def get_settings_api() -> dict:
	return get_settings().as_dict()


@frappe.whitelist()
def list_documents(
	business_entity: str,
	document_type: str | None = None,
	document_owner: str | None = None,
	lifecycle_state: str = "Active",
	search: str | None = None,
	limit: int = 50,
	start: int = 0,
) -> dict:
	filters = {"business_entity": business_entity, "lifecycle_state": lifecycle_state}
	if document_type:
		filters["document_type"] = document_type
	if document_owner:
		filters["document_owner"] = document_owner
	or_filters = None
	if search:
		or_filters = [
			["document_title", "like", f"%{search}%"],
			["document_code", "like", f"%{search}%"],
			["keywords", "like", f"%{search}%"],
		]

	rows = frappe.get_all(
		"QMS Document",
		filters=filters,
		or_filters=or_filters,
		fields=[
			"name",
			"document_code",
			"document_title",
			"document_type",
			"current_version",
			"effective_date",
			"next_review_date",
			"document_owner",
			"lifecycle_state",
		],
		order_by="next_review_date asc",
		limit_page_length=limit,
		limit_start=start,
	)
	today_ = nowdate()
	for row in rows:
		if not row.next_review_date:
			row["review_status"] = "No Version" if not row.current_version else "Current"
		elif getdate(row.next_review_date) < getdate(today_):
			row["review_status"] = "Overdue"
		elif date_diff(row.next_review_date, today_) <= 30:
			row["review_status"] = "Due Soon"
		else:
			row["review_status"] = "Current"

	total = frappe.db.count("QMS Document", filters=filters)
	return {"items": rows, "total": total, "limit": limit, "start": start}


@frappe.whitelist()
def get_document_detail(name: str) -> dict:
	qdoc = frappe.get_doc("QMS Document", name)
	versions = frappe.get_all(
		"Document Version",
		filters={"qms_document": name},
		fields=["name", "version_number", "status", "prepared_by", "reviewed_by", "approved_by", "approved_on"],
		order_by="creation asc",
	)
	ack_counts = {"pending": 0, "acknowledged": 0, "overdue": 0, "waived": 0}
	if qdoc.current_version:
		for row in frappe.get_all(
			"Document Acknowledgement", filters={"document_version": qdoc.current_version}, fields=["status"]
		):
			key = row.status.lower()
			if key in ack_counts:
				ack_counts[key] += 1
	return {
		"document": qdoc.as_dict(),
		"versions": versions,
		"acknowledgement_summary": ack_counts,
		"applicable_clauses": clause_list(qdoc),
	}


# ──────────────────────────────────────────────────────────────────────────────
# 2. VERSION LIFECYCLE
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def create_draft_version(
	qms_document: str, version_number: str, change_summary: str = "", file_url: str = "", is_major_revision: bool = False
) -> dict:
	qdoc = frappe.get_doc("QMS Document", qms_document)
	if not _can_author(qdoc):
		frappe.throw(_("You are not authorised to draft versions of this document."), frappe.PermissionError)

	version = frappe.get_doc(
		{
			"doctype": "Document Version",
			"qms_document": qms_document,
			"business_entity": qdoc.business_entity,
			"version_number": version_number,
			"is_major_revision": is_major_revision,
			"change_summary": change_summary,
			"prepared_by": frappe.session.user,
			"prepared_on": nowdate(),
			"file_attachment": file_url,
			"status": "Draft",
		}
	)
	version.insert()
	return version.as_dict()


@frappe.whitelist()
def send_for_review(version_name: str, reviewer: str) -> dict:
	version = frappe.get_doc("Document Version", version_name)
	if version.status != "Draft":
		frappe.throw(_("Only a Draft version can be sent for review."))
	if version.prepared_by != frappe.session.user and not _has_role(*MANAGER_ROLES):
		frappe.throw(_("Only the preparer can send this version for review."), frappe.PermissionError)
	if reviewer == version.prepared_by:
		frappe.throw(_("Reviewer cannot be the same person as the preparer."))

	version.reviewed_by = reviewer
	version.review_outcome = None
	version.review_comments = None
	version.status = "In Review"
	version.append("approval_steps", {"step_role": "Reviewer", "acted_by": None, "outcome": "Submitted"})
	version.save()

	frappe.sendmail(
		recipients=[reviewer],
		subject=_("Review request — {0} {1}").format(version.qms_document, version.version_number),
		message=_("You have been asked to review {0} version {1}.").format(version.qms_document, version.version_number),
		now=False,
	)
	return {"name": version.name, "status": version.status}


@frappe.whitelist()
def reviewer_action(version_name: str, outcome: str, comments: str = "") -> dict:
	version = frappe.get_doc("Document Version", version_name)
	if version.status != "In Review":
		frappe.throw(_("This version is not awaiting review."))
	if frappe.session.user != version.reviewed_by and not _has_role(*MANAGER_ROLES):
		frappe.throw(_("Only the assigned reviewer can act on this version."), frappe.PermissionError)
	if outcome not in ("Approved", "Returned"):
		frappe.throw(_("Outcome must be Approved or Returned."))
	if outcome == "Returned" and not comments:
		frappe.throw(_("Comments are required when returning a version."))

	version.review_outcome = outcome
	version.review_comments = comments
	version.reviewed_on = now_datetime()
	version.append(
		"approval_steps",
		{"step_role": "Reviewer", "acted_by": frappe.session.user, "acted_on": now_datetime(), "outcome": outcome, "comments": comments},
	)
	if outcome == "Returned":
		version.status = "Draft"
	version.save()
	return {"name": version.name, "status": version.status, "review_outcome": version.review_outcome}


@frappe.whitelist()
def approve_version(version_name: str, effective_date_override: str | None = None) -> dict:
	version = frappe.get_doc("Document Version", version_name)
	if version.status != "In Review" or version.review_outcome != "Approved":
		frappe.throw(_("This version has not been review-approved yet."))
	if not _has_role(*MANAGER_ROLES):
		frappe.throw(_("Only a QMS Manager can approve a version."), frappe.PermissionError)
	if effective_date_override:
		version.effective_date_override = getdate(effective_date_override)
	version.approved_by = frappe.session.user
	version.save()
	version.submit()
	result = getattr(version, "_publish_result", {})
	result.update({"name": version.name, "status": version.status})
	return result


@frappe.whitelist()
def withdraw_version(version_name: str, reason: str) -> dict:
	_require_role(*ADMIN_ROLES)
	version = frappe.get_doc("Document Version", version_name)
	if version.status != "Approved":
		frappe.throw(_("Only the currently Approved version can be withdrawn."))
	version.flags.withdrawal_reason = reason
	version.cancel()
	return {"name": version.name, "status": version.status}


@frappe.whitelist()
def reassign_reviewer(version_name: str, new_reviewer: str) -> dict:
	"""TP-06: the assigned reviewer left mid-review."""
	_require_role(*MANAGER_ROLES)
	version = frappe.get_doc("Document Version", version_name)
	if version.status != "In Review":
		frappe.throw(_("Only a version currently In Review can have its reviewer reassigned."))
	if new_reviewer == version.prepared_by:
		frappe.throw(_("Reviewer cannot be the same person as the preparer."))
	old_reviewer = version.reviewed_by
	version.reviewed_by = new_reviewer
	version.review_outcome = None
	version.reviewed_on = None
	version.append(
		"approval_steps",
		{"step_role": "Reviewer", "acted_by": frappe.session.user, "outcome": "Submitted", "comments": f"Reassigned from {old_reviewer} to {new_reviewer}"},
	)
	version.save()
	return {"name": version.name, "reviewed_by": version.reviewed_by}


@frappe.whitelist()
def refan_acknowledgements(version_name: str) -> dict:
	_require_role(*MANAGER_ROLES)
	created = _refan_acknowledgements(version_name)
	return {"created": created}


# ──────────────────────────────────────────────────────────────────────────────
# 3. PERIODIC REVIEW
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def confirm_review_no_change(qms_document: str, peer_reviewer: str, notes: str = "") -> dict:
	qdoc = frappe.get_doc("QMS Document", qms_document)
	if frappe.session.user != qdoc.document_owner and not _has_role(*MANAGER_ROLES):
		frappe.throw(_("Only the Document Owner (or a QMS Manager) can confirm this review."), frappe.PermissionError)
	return _confirm_review_no_change(qms_document, peer_reviewer, notes)


@frappe.whitelist()
def list_due_reviews(business_entity: str, horizon_days: int = 30) -> list:
	cutoff = add_days(nowdate(), int(horizon_days))
	return frappe.get_all(
		"QMS Document",
		filters={
			"business_entity": business_entity,
			"lifecycle_state": "Active",
			"next_review_date": ("<=", cutoff),
		},
		fields=["name", "document_code", "document_title", "document_owner", "next_review_date"],
		order_by="next_review_date asc",
	)


# ──────────────────────────────────────────────────────────────────────────────
# 4. DISTRIBUTION & ACKNOWLEDGEMENT
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def list_pending_acknowledgements(user: str | None = None) -> list:
	user = user or frappe.session.user
	return frappe.get_all(
		"Document Acknowledgement",
		filters={"assigned_to": user, "status": ("in", ["Pending", "Overdue"])},
		fields=["name", "qms_document", "document_version", "due_on", "status"],
		order_by="due_on asc",
	)


@frappe.whitelist()
def acknowledge_document(version_name: str) -> dict:
	user = frappe.session.user
	ack_name = frappe.db.get_value(
		"Document Acknowledgement", {"document_version": version_name, "assigned_to": user}, "name"
	)
	if not ack_name:
		frappe.throw(_("No acknowledgement is assigned to you for this version."))
	ack = frappe.get_doc("Document Acknowledgement", ack_name)
	if ack.status in ("Acknowledged", "Waived"):
		frappe.throw(_("This acknowledgement has already been resolved."))
	opened = frappe.db.exists(
		"Document Access Log",
		{"document_version": version_name, "accessed_by": user, "access_type": ("in", ["View", "Download"])},
	)
	if not opened:
		frappe.throw(_("You must open the document at least once before acknowledging it."))
	ack.status = "Acknowledged"
	ack.acknowledged_on = now_datetime()
	ack.save()
	ack.submit()
	return {"name": ack.name, "status": ack.status}


@frappe.whitelist()
def waive_acknowledgement(ack_name: str, reason: str) -> dict:
	_require_role(*MANAGER_ROLES)
	ack = frappe.get_doc("Document Acknowledgement", ack_name)
	if ack.status not in ("Pending", "Overdue"):
		frappe.throw(_("Only a Pending or Overdue acknowledgement can be waived."))
	if ack.assigned_to == frappe.session.user:
		frappe.throw(_("You cannot waive your own acknowledgement."))
	ack.status = "Waived"
	ack.waived_by = frappe.session.user
	ack.waiver_reason = reason
	ack.save()
	ack.submit()
	return {"name": ack.name, "status": ack.status}


# ──────────────────────────────────────────────────────────────────────────────
# 5. READING & ACCESS LOG
# ──────────────────────────────────────────────────────────────────────────────

def _log_access(version, access_type: str):
	try:
		ip_address = frappe.local.request_ip
	except Exception:
		ip_address = None
	try:
		user_agent = frappe.get_request_header("User-Agent") if frappe.request else None
	except Exception:
		user_agent = None
	frappe.get_doc(
		{
			"doctype": "Document Access Log",
			"qms_document": version.qms_document,
			"document_version": version.name,
			"accessed_by": frappe.session.user,
			"access_type": access_type,
			"business_entity": version.business_entity,
			"ip_address": ip_address,
			"user_agent": user_agent,
		}
	).insert(ignore_permissions=True)


@frappe.whitelist()
def open_for_reading(version_name: str) -> dict:
	version = frappe.get_doc("Document Version", version_name)
	_log_access(version, "View")
	needs_ack = frappe.db.exists(
		"Document Acknowledgement",
		{"document_version": version_name, "assigned_to": frappe.session.user, "status": ("in", ["Pending", "Overdue"])},
	)
	qdoc = frappe.db.get_value("QMS Document", version.qms_document, "confidentiality")
	return {
		"file_url": version.file_attachment,
		"confidentiality": qdoc,
		"needs_acknowledgement": bool(needs_ack),
		"watermark_required": qdoc == "Confidential",
		"status": version.status,
	}


@frappe.whitelist()
def download_document(version_name: str) -> dict:
	version = frappe.get_doc("Document Version", version_name)
	_log_access(version, "Download")
	# NOTE: dynamic per-user watermarking (TP-30) is out of scope for this pass;
	# the immutable file is returned as-is and every download is still logged.
	return {"file_url": version.file_attachment, "hash_sha256": version.hash_sha256}


# ──────────────────────────────────────────────────────────────────────────────
# 6. KPIs & REPORTS
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def get_kpi_snapshot(business_entity: str) -> dict:
	active_docs = frappe.get_all(
		"QMS Document",
		filters={"business_entity": business_entity, "lifecycle_state": "Active"},
		fields=["name", "current_version", "next_review_date", "applicable_clauses"],
	)
	total = len(active_docs) or 1
	with_version = sum(1 for d in active_docs if d.current_version)
	within_window = sum(
		1 for d in active_docs if not d.next_review_date or getdate(d.next_review_date) >= getdate(nowdate())
	)
	with_clauses = sum(1 for d in active_docs if clause_list(d))

	cycle_days = frappe.db.sql(
		"""
		SELECT DATEDIFF(approved_on, prepared_on) AS days
		FROM `tabDocument Version`
		WHERE business_entity=%s AND status IN ('Approved', 'Superseded') AND approved_on IS NOT NULL
		""",
		(business_entity,),
		as_dict=True,
	)
	days = [row.days for row in cycle_days if row.days is not None]
	median_cycle = round(statistics.median(days), 1) if days else 0

	obsolete_in_distribution = frappe.db.sql(
		"""
		SELECT COUNT(*) AS c
		FROM `tabDocument Acknowledgement` a
		JOIN `tabDocument Version` v ON v.name = a.document_version
		WHERE a.status = 'Pending' AND v.status != 'Approved'
		""",
		as_dict=True,
	)[0].c

	return {
		"documents_with_current_version_pct": round(100.0 * with_version / total, 1),
		"documents_within_review_window_pct": round(100.0 * within_window / total, 1),
		"median_approval_cycle_days": median_cycle,
		"obsolete_in_distribution_count": obsolete_in_distribution,
		"documents_with_clause_mapping_pct": round(100.0 * with_clauses / total, 1),
	}


@frappe.whitelist()
def stale_documents_report(business_entity: str, horizon_days: int = 60) -> list:
	docs = frappe.get_all(
		"QMS Document",
		filters={"business_entity": business_entity, "lifecycle_state": "Active", "next_review_date": ("is", "set")},
		fields=["name", "document_code", "document_title", "document_owner", "next_review_date"],
	)
	today_ = getdate(nowdate())
	out = []
	for doc in docs:
		days = date_diff(doc.next_review_date, today_)
		if days > horizon_days:
			continue
		band = "Overdue" if days < 0 else ("Due 30" if days <= 30 else "Due 60")
		out.append({**doc, "days": days, "band": band})
	out.sort(key=lambda r: r["days"])
	return out


@frappe.whitelist()
def documents_by_clause(business_entity: str, standard: str, clause_number: str) -> list:
	docs = frappe.get_all(
		"QMS Document",
		filters={"business_entity": business_entity, "lifecycle_state": "Active"},
		fields=["name", "document_code", "document_title", "current_version", "applicable_clauses", "effective_date", "next_review_date"],
	)
	return [
		{k: v for k, v in d.items() if k != "applicable_clauses"}
		for d in docs
		if clause_matches(d, standard, clause_number)
	]


# ──────────────────────────────────────────────────────────────────────────────
# 7. SWEEPS (callable now; wire to hooks.py scheduler_events later)
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def run_overdue_sweep() -> dict:
	_require_role(*ADMIN_ROLES)
	return {
		"acknowledgements_marked_overdue": mark_overdue_acknowledgements(),
		"review_schedules_marked_overdue": mark_overdue_review_schedules(),
	}
