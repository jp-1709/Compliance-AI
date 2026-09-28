"""
quantbit_compliance_ai/qms_document_control/utils.py

Shared helpers for the Q1 QMS Document Control module.

Covers:
  - get_settings()              : QMS Settings singleton with safe defaults
  - parse_version()             : vX.Y version-string parsing (single source of truth)
  - is_version_greater()        : monotonic version comparison
  - compute_sha256()            : content hash for uploaded controlled files
  - resolve_distribution_lists(): flatten Document Distribution List membership rules
                                   (User / Department+Designation / Role) into a user set
  - clause_list()                : normalise the JSON applicable_clauses field
  - clause_matches()             : match a (standard, clause) pair against a document's clauses
  - log_activity()               : append-only activity trail via the comment timeline
"""

import hashlib
import json
import re

import frappe
from frappe.utils import add_months, getdate

VERSION_PATTERN = re.compile(r"^v(\d+)\.(\d+)$")

_SETTINGS_DEFAULTS = {
	"default_review_frequency_months": 12,
	"default_acknowledgement_window_days": 14,
	"allow_approver_equal_reviewer": 0,
	"require_clause_mapping": 1,
	"obsolete_pdf_watermark_text": "OBSOLETE — DO NOT USE",
	"review_confirm_window_days": 60,
	"review_fanout_horizon_days": 60,
}


def get_settings():
	"""Return the QMS Settings singleton, tolerating a fresh install where it
	has never been saved (Frappe still returns a doc with schema defaults,
	but we guard explicitly so business logic never has to check for None)."""
	settings = frappe.get_single("QMS Settings")
	for fieldname, default in _SETTINGS_DEFAULTS.items():
		if settings.get(fieldname) in (None, ""):
			settings.set(fieldname, default)
	return settings


# ──────────────────────────────────────────────────────────────────────────────
# VERSION NUMBERING
# ──────────────────────────────────────────────────────────────────────────────

def parse_version(version_number: str):
	"""Return (major, minor) int tuple for a 'vX.Y' string, or None if invalid."""
	match = VERSION_PATTERN.fullmatch((version_number or "").strip())
	if not match:
		return None
	return tuple(int(part) for part in match.groups())


def is_version_greater(candidate: str, existing: list) -> bool:
	"""True if `candidate` is strictly greater than every version_number in `existing`."""
	parsed_candidate = parse_version(candidate)
	if not parsed_candidate:
		return False
	parsed_existing = [parse_version(v) for v in existing]
	parsed_existing = [v for v in parsed_existing if v]
	if not parsed_existing:
		return True
	return parsed_candidate > max(parsed_existing)


# ──────────────────────────────────────────────────────────────────────────────
# FILE INTEGRITY
# ──────────────────────────────────────────────────────────────────────────────

def compute_sha256(file_url: str) -> tuple:
	"""Return (sha256_hex, size_bytes) for a Frappe file:// / /files/ URL.
	Reads in 8 KB chunks so large controlled documents (drawings, MBRs) don't
	blow up memory."""
	if not file_url:
		return None, None
	file_doc = frappe.get_doc("File", {"file_url": file_url})
	path = file_doc.get_full_path()
	digest = hashlib.sha256()
	size = 0
	with open(path, "rb") as fh:
		for chunk in iter(lambda: fh.read(8192), b""):
			digest.update(chunk)
			size += len(chunk)
	return digest.hexdigest(), size


# ──────────────────────────────────────────────────────────────────────────────
# DISTRIBUTION RESOLUTION  (ISO 9001 §7.5.3.2(a) — distribution, access, retrieval)
# ──────────────────────────────────────────────────────────────────────────────

def _loads(value, default=None):
	if not value:
		return default if default is not None else []
	if isinstance(value, (list, dict)):
		return value
	try:
		return json.loads(value)
	except (TypeError, ValueError):
		return default if default is not None else []


def resolve_distribution_list_members(distribution_list_name: str) -> set:
	"""Resolve one Document Distribution List's member rules into a flat set
	of user emails. Non-recursive: a Role rule pulls users with that role
	directly; it does not chase Role → Role rules (TP-16)."""
	dl = frappe.get_doc("Document Distribution List", distribution_list_name)
	if not dl.is_active:
		return set()

	users = set()
	for row in dl.members or []:
		if row.member_kind == "User" and row.user:
			users.add(row.user)
		elif row.member_kind == "Role" and row.role:
			users.update(
				frappe.get_all(
					"Has Role",
					filters={"role": row.role, "parenttype": "User"},
					pluck="parent",
				)
			)
		elif row.member_kind == "Department + Designation Rule" and row.department:
			filters = {"department": row.department}
			if row.designation:
				filters["designation"] = row.designation
			users.update(
				frappe.get_all("User Profile", filters=filters, pluck="user")
			)
	users.discard(None)
	return users


def resolve_distribution_lists(qms_document) -> dict:
	"""Resolve every Document Distribution List linked on a QMS Document
	(stored as a JSON array of list names in `distribution_lists`) into a
	single {user: acknowledgement_window_days} map, deduplicated by user
	(TP-16: a user matched by multiple rules/lists gets one entry — the
	shortest window wins, since that is the more conservative deadline)."""
	list_names = _loads(qms_document.get("distribution_lists"))
	resolved: dict = {}
	for name in list_names:
		if not frappe.db.exists("Document Distribution List", name):
			continue
		window_days = frappe.db.get_value(
			"Document Distribution List", name, "acknowledgement_window_days"
		) or 14
		for user in resolve_distribution_list_members(name):
			if user not in resolved or window_days < resolved[user]:
				resolved[user] = window_days
	return resolved


# ──────────────────────────────────────────────────────────────────────────────
# ISO CLAUSE MAPPING
# ──────────────────────────────────────────────────────────────────────────────

def clause_list(qms_document) -> list:
	"""Normalise `applicable_clauses` (JSON) into a list of
	{"standard": ..., "clause": ...} dicts, tolerating legacy plain strings."""
	raw = _loads(qms_document.get("applicable_clauses"))
	normalised = []
	for item in raw:
		if isinstance(item, dict):
			normalised.append(
				{"standard": item.get("standard", ""), "clause": item.get("clause", "")}
			)
		elif isinstance(item, str):
			normalised.append({"standard": "", "clause": item})
	return normalised


def clause_matches(qms_document, standard: str, clause_number: str) -> bool:
	for row in clause_list(qms_document):
		if clause_number and clause_number not in (row.get("clause") or ""):
			continue
		if standard and standard.lower() not in (row.get("standard") or "").lower():
			continue
		return True
	return False


# ──────────────────────────────────────────────────────────────────────────────
# CADENCE
# ──────────────────────────────────────────────────────────────────────────────

def compute_next_review_date(effective_date, review_frequency_months: int):
	if not effective_date:
		return None
	return add_months(getdate(effective_date), int(review_frequency_months or 12))


# ──────────────────────────────────────────────────────────────────────────────
# ACTIVITY LOG  (append-only trail; we reuse Frappe's comment timeline rather
# than introduce a new child DocType not present in the shipped schema)
# ──────────────────────────────────────────────────────────────────────────────

def log_activity(doc, event: str, details: str = ""):
	text = event if not details else f"{event} — {details}"
	doc.add_comment("Info", text)
