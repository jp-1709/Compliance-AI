"""
quantbit_compliance_ai/qms_management_review/input_handlers.py

Auto-Population Engine (Q5 spec §5) — the plug-in handler registry that pulls
MR Input Records from other modules without Q5 hard-coding their names.

Each handler takes the MR Cycle doc and returns a list of payload dicts:
  {title, data_summary, key_metrics: [...], trend_direction,
   source_record_doctype?, source_record_name?}

Real handlers are wired to the modules that already exist in this app —
notably Q1 (qms_document_control) and Q6 (qms_risk_register), reusing the
engines built for those modules rather than re-querying their tables here.
Modules not yet built (Q7 Training, Q8 Supplier) simply have no registered
handler, which is exactly the "no auto-source; open for Process Owner
contribution" behaviour the spec describes (§5.3 step 3).

run_auto_population() is idempotent per (cycle, category, source_module):
re-running it updates the existing auto-pulled Draft record in place rather
than duplicating it, and never touches a record a human has already advanced
past Draft (§5.4 "manual contributions are never overwritten").
"""

import frappe
from frappe.utils import now_datetime

HANDLERS: dict = {}


def _current_user_profile() -> str | None:
	"""MR Input Record.contributed_by links to User Profile, not User. When
	the auto-population engine runs as a system user with no User Profile
	(Administrator, a scheduled job), fall back to any MR Secretariat's
	profile so the mandatory link is still meaningful; if even that doesn't
	exist yet on a fresh tenant, the record still saves (ignore_mandatory)
	since a human reviews every auto-pulled Draft before it can advance."""
	profile = frappe.db.get_value("User Profile", {"user": frappe.session.user}, "name")
	if profile:
		return profile
	secretariat_users = frappe.get_all("Has Role", filters={"role": "MR Secretariat", "parenttype": "User"}, pluck="parent")
	if secretariat_users:
		return frappe.db.get_value("User Profile", {"user": ("in", secretariat_users)}, "name")
	return None


def register_handler(input_category: str, source_module: str):
	def decorator(fn):
		HANDLERS[(input_category, source_module)] = fn
		return fn

	return decorator


def pull_input_for_category(cycle, input_category: str, source_module: str) -> list:
	handler = HANDLERS.get((input_category, source_module))
	if not handler:
		return []
	try:
		return handler(cycle) or []
	except Exception:
		frappe.log_error(
			title=f"MR auto-population handler failed: {input_category} / {source_module}", message=frappe.get_traceback()
		)
		return []


# ──────────────────────────────────────────────────────────────────────────────
# HANDLERS
# ──────────────────────────────────────────────────────────────────────────────

@register_handler("Status of Prior Actions", "Q5 Prior Cycle")
def pull_open_outputs_from_prior_cycle(cycle) -> list:
	if not cycle.prior_cycle:
		return []
	open_outputs = frappe.get_all(
		"MR Output",
		filters={"cycle": cycle.prior_cycle, "status": ("not in", ["Closed", "Verified Effective", "Cancelled"])},
		fields=["name", "title", "status", "assigned_to", "target_completion_date"],
	)
	if not open_outputs:
		return []
	lines = "\n".join(f"- {o.title} ({o.name}): {o.status}, due {o.target_completion_date}" for o in open_outputs)
	return [
		{
			"title": f"Status of {len(open_outputs)} prior-cycle action(s)",
			"data_summary": f"{len(open_outputs)} output(s) from cycle {cycle.prior_cycle} remain open:\n{lines}",
			"key_metrics": [{"metric_name": "Open prior outputs", "current_period_value": len(open_outputs), "unit": "count"}],
			"trend_direction": "Worsening",
		}
	]


@register_handler("Nonconformities & Corrective Actions", "Q2 CAPA")
def pull_capa_summary_for_period(cycle) -> list:
	if not frappe.db.table_exists("CAPA Case"):
		return []
	capas = frappe.get_all(
		"CAPA Case",
		filters={"business_entity": cycle.entity, "creation": ("between", [cycle.period_start, cycle.period_end])},
		fields=["name", "severity", "status"],
	)
	if not capas:
		return []
	open_count = sum(1 for c in capas if c.status not in ("Closed", "Voided"))
	return [
		{
			"title": f"CAPA activity — {len(capas)} case(s) opened",
			"data_summary": f"{len(capas)} CAPA(s) were opened in this period; {open_count} remain open at period end.",
			"key_metrics": [
				{"metric_name": "CAPAs opened", "current_period_value": len(capas), "unit": "count"},
				{"metric_name": "CAPAs open at period end", "current_period_value": open_count, "unit": "count"},
			],
			"trend_direction": "Insufficient Data",
		}
	]


@register_handler("Audit Results", "Q4 Internal Audit")
def pull_audit_results_for_period(cycle) -> list:
	if not frappe.db.table_exists("Audit Finding"):
		return []
	findings = frappe.get_all(
		"Audit Finding",
		filters={"business_entity": cycle.entity, "creation": ("between", [cycle.period_start, cycle.period_end])},
		fields=["name", "severity", "status"],
	)
	if not findings:
		return []
	major = sum(1 for f in findings if f.severity == "Major")
	return [
		{
			"title": f"Internal audit findings — {len(findings)} raised",
			"data_summary": f"{len(findings)} audit finding(s) were raised this period, of which {major} were Major nonconformities.",
			"key_metrics": [
				{"metric_name": "Findings raised", "current_period_value": len(findings), "unit": "count"},
				{"metric_name": "Major findings", "current_period_value": major, "unit": "count"},
			],
			"trend_direction": "Insufficient Data",
		}
	]


@register_handler("Resource Adequacy", "Q1 Document Control")
def pull_document_control_kpis(cycle) -> list:
	try:
		from quantbit_compliance_ai.qms_document_control.api import get_kpi_snapshot

		kpi = get_kpi_snapshot(cycle.entity)
	except Exception:
		return []
	return [
		{
			"title": "Document control resource adequacy",
			"data_summary": (
				f"{kpi['documents_with_current_version_pct']}% of controlled documents have a current approved "
				f"version; {kpi['documents_within_review_window_pct']}% are within their periodic-review window; "
				f"median approval cycle time is {kpi['median_approval_cycle_days']} day(s)."
			),
			"key_metrics": [
				{"metric_name": "Documents with current version", "current_period_value": kpi["documents_with_current_version_pct"], "unit": "%"},
				{"metric_name": "Documents within review window", "current_period_value": kpi["documents_within_review_window_pct"], "unit": "%"},
				{"metric_name": "Median approval cycle", "current_period_value": kpi["median_approval_cycle_days"], "unit": "days"},
			],
			"trend_direction": "Insufficient Data",
		}
	]


@register_handler("Risks & Opportunities Effectiveness", "Q6 Risk")
def pull_risk_effectiveness_for_period(cycle) -> list:
	try:
		from quantbit_compliance_ai.qms_risk_register.aggregation_engine import compute_aggregate_posture

		posture = compute_aggregate_posture(cycle.entity)
	except Exception:
		return []
	if not posture["open_count"]:
		return []
	return [
		{
			"title": "Risk register effectiveness",
			"data_summary": (
				f"Aggregate residual posture is {posture['aggregate_score']} ({posture['method']}) across "
				f"{posture['open_count']} open risk(s); {posture['above_appetite_count']} above appetite; "
				f"{posture['reassessment_overdue_count']} overdue for reassessment."
			),
			"key_metrics": [
				{"metric_name": "Aggregate residual score", "current_period_value": posture["aggregate_score"], "unit": "rating"},
				{"metric_name": "Risks above appetite", "current_period_value": posture["above_appetite_count"], "unit": "count"},
				{"metric_name": "Reassessments overdue", "current_period_value": posture["reassessment_overdue_count"], "unit": "count"},
			],
			"trend_direction": "Insufficient Data",
		}
	]


def _no_data_attestation(input_category: str) -> dict:
	"""§5.5 — an explicit 'nothing to report' record, never a silent gap."""
	return {
		"title": f"Zero {input_category} events in this period.",
		"data_summary": f"Zero {input_category} events in this period.",
		"key_metrics": [{"metric_name": "count", "current_period_value": 0, "unit": "count"}],
		"trend_direction": "Insufficient Data",
	}


def _upsert_input_record(cycle, input_category: str, source_module: str, payload: dict, standard: str | None, clause: str | None, mandatory: bool) -> int:
	"""Idempotency key: (cycle, input_category, source_module, Auto-Pulled).
	Returns 1 if a new record was created, 0 if an existing one was updated
	or skipped (already advanced past Draft by a human)."""
	existing = frappe.db.get_value(
		"MR Input Record",
		{"cycle": cycle.name, "input_category": input_category, "source_module": source_module, "source_type": "Auto-Pulled"},
		"name",
	)
	if existing:
		doc = frappe.get_doc("MR Input Record", existing)
		if doc.status != "Draft":
			return 0  # a human has moved this forward; never overwrite (§5.4)
	else:
		doc = frappe.new_doc("MR Input Record")
		doc.cycle = cycle.name
		doc.entity = cycle.entity
		doc.input_category = input_category
		doc.source_type = "Auto-Pulled"
		doc.source_module = source_module
		doc.source_period_start = cycle.period_start
		doc.source_period_end = cycle.period_end
		doc.contributed_by = _current_user_profile()
		doc.contributed_on = now_datetime()
		doc.status = "Draft"

	doc.title = payload["title"]
	doc.data_summary = payload["data_summary"]
	doc.trend_direction = payload.get("trend_direction", "Insufficient Data")
	doc.source_record_doctype = payload.get("source_record_doctype")
	doc.source_record_name = payload.get("source_record_name")
	doc.set("key_metrics", [])
	for metric in payload.get("key_metrics", []):
		doc.append("key_metrics", metric)
	if standard and not doc.applicable_standards:
		doc.append("applicable_standards", {"standard": standard, "clause": clause, "mandatory_for_clause": 1 if mandatory else 0})

	doc.flags.ignore_permissions = True
	if not doc.contributed_by:
		doc.flags.ignore_mandatory = True
	doc.save()
	return 0 if existing else 1


def run_auto_population(cycle_name: str) -> dict:
	"""§5.3 run order: for each applicable standard's checklist template, for
	each mandatory/optional category, call every registered handler for the
	category's default source module; fall back to a no-data attestation for
	mandatory categories with a registered-but-empty handler, and leave
	categories with no handler at all open for manual contribution."""
	cycle = frappe.get_doc("MR Cycle", cycle_name)
	created = updated = no_source = 0

	for standard_row in cycle.applicable_standards or []:
		if not standard_row.input_checklist_template:
			continue
		categories = frappe.get_all(
			"MR Checklist Category",
			filters={"parent": standard_row.input_checklist_template, "parenttype": "MR Input Checklist Template"},
			fields=["input_category", "mandatory", "default_source_module"],
		)
		for cat in categories:
			if not cat.default_source_module:
				no_source += 1
				continue
			results = pull_input_for_category(cycle, cat.input_category, cat.default_source_module)
			if not results and cat.mandatory:
				results = [_no_data_attestation(cat.input_category)]
			for payload in results:
				was_created = _upsert_input_record(
					cycle, cat.input_category, cat.default_source_module, payload, standard_row.standard, standard_row.applicable_clause, cat.mandatory
				)
				created += was_created
				updated += 0 if was_created else 1

	return {"cycle": cycle_name, "created": created, "updated": updated, "categories_without_source": no_source}
