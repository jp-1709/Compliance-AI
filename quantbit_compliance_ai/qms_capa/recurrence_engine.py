"""
quantbit_compliance_ai/qms_capa/recurrence_engine.py

Recurring-issue detection (Q2 spec §4.3). Same practical substitute used in
Q6's identification_engine: difflib text-similarity against the existing
register rather than a pgvector embedding search, since no embedding infra
is wired into this app.

Algorithm (§4.3):
  1. Compare problem_statement (+ root_cause_summary if present) against
     every Closed CAPA in the same business_entity from the past 18 months.
  2. Cosine-similarity threshold 0.78 in the spec -> difflib ratio >= 0.78 here.
  3. If matched: is_recurring_issue=1, prior_capas populated, severity floor
     raised to High for Critical/High recurring issues (never downgraded).
"""

import difflib

import frappe
from frappe.utils import add_months, getdate, nowdate

RECURRING_THRESHOLD = 0.78
LOOKBACK_MONTHS = 18
_SEVERITY_RANK = {"Low": 0, "Medium": 1, "High": 2, "Critical": 3}


def find_similar_capas(business_entity: str, problem_statement: str, exclude: str | None = None, threshold: float = RECURRING_THRESHOLD, limit: int = 5) -> list:
	candidate = (problem_statement or "").strip().lower()
	if not candidate:
		return []
	cutoff = add_months(getdate(nowdate()), -LOOKBACK_MONTHS)
	filters = {"business_entity": business_entity, "status": "Closed", "actual_close_date": (">=", cutoff)}
	if exclude:
		filters["name"] = ("!=", exclude)
	rows = frappe.get_all("CAPA Case", filters=filters, fields=["name", "problem_statement", "severity", "actual_close_date"])

	scored = []
	for row in rows:
		existing = (row.problem_statement or "").strip().lower()
		if not existing:
			continue
		score = difflib.SequenceMatcher(None, candidate, existing).ratio()
		if score >= threshold:
			scored.append({"name": row.name, "similarity": round(score, 3), "severity": row.severity, "closed_on": str(row.actual_close_date)})
	scored.sort(key=lambda r: -r["similarity"])
	return scored[:limit]


def detect_recurring_issue(capa) -> list:
	"""Mutates `capa` (in-memory doc) in place. Returns the matched prior
	CAPA names, or an empty list. Never downgrades severity — only raises
	Medium/Low up to High when a Critical/High recurrence is found."""
	matches = find_similar_capas(capa.business_entity, capa.problem_statement, exclude=capa.name)
	if not matches:
		return []

	capa.is_recurring_issue = 1
	capa.prior_capas = frappe.as_json([m["name"] for m in matches])

	worst_prior_severity = max((m["severity"] for m in matches), key=lambda s: _SEVERITY_RANK.get(s, 0))
	if _SEVERITY_RANK.get(worst_prior_severity, 0) >= _SEVERITY_RANK["High"] and _SEVERITY_RANK.get(capa.severity, 0) < _SEVERITY_RANK["High"]:
		capa.severity = "High"

	return [m["name"] for m in matches]
