"""
quantbit_compliance_ai/qms_risk_register/api.py

Whitelisted API surface for the Q6 Risk Register module, narrowed to the same
"controllers + engines + key APIs" depth used for Q1: no reports/dashboards/
print-format/AI-hook/scheduled-task wiring here.

Every mutating call routes through the engine modules (appetite_engine,
cadence_engine, identification_engine, aggregation_engine, treatment_hooks)
so the same logic runs whether triggered here, from the Desk UI, or a future
background job.
"""

import frappe
from frappe import _

from quantbit_compliance_ai.qms_risk_register.aggregation_engine import compute_aggregate_posture, recompute_top_n_ranks
from quantbit_compliance_ai.qms_risk_register.appetite_engine import resolve_appetite
from quantbit_compliance_ai.qms_risk_register.cadence_engine import trigger_event_driven_reassessment
from quantbit_compliance_ai.qms_risk_register.identification_engine import create_risk_from_event, find_potential_duplicates

MANAGER_ROLES = ("System Manager", "Risk Manager")


def _require_role(*roles):
	user_roles = frappe.get_roles(frappe.session.user)
	if not any(r in user_roles for r in roles):
		frappe.throw(_("Permission denied. Required role(s): {0}.").format(", ".join(roles)), frappe.PermissionError)


# ──────────────────────────────────────────────────────────────────────────────
# IDENTIFICATION
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def api_find_potential_duplicates(entity: str, title: str, description: str = "") -> list:
	return find_potential_duplicates(entity, title, description)


@frappe.whitelist()
def api_create_risk_from_event(
	source_doctype: str, source_name: str, entity: str, risk_category: str, method_profile: str, risk_owner: str, overrides: dict | None = None
) -> dict:
	return create_risk_from_event(source_doctype, source_name, entity, risk_category, method_profile, risk_owner, overrides)


@frappe.whitelist()
def merge_risks(target_risk_id: str, source_risk_ids: list, merge_strategy: str = "supersede") -> dict:
	"""§5.2: dedup resolution — fold source risks into the target. The sources
	are withdrawn (never hard-deleted, matching the module's soft-delete
	invariant) with a reason pointing at the surviving record."""
	_require_role(*MANAGER_ROLES)
	if isinstance(source_risk_ids, str):
		source_risk_ids = frappe.parse_json(source_risk_ids)
	if not frappe.db.exists("Risk Item", target_risk_id):
		frappe.throw(_("Target Risk Item {0} does not exist.").format(target_risk_id))
	merged = []
	for source in source_risk_ids:
		if source == target_risk_id:
			continue
		source_doc = frappe.get_doc("Risk Item", source)
		if source_doc.status == "Withdrawn":
			continue
		source_doc.status = "Withdrawn"
		source_doc.withdrawal_reason = _("Merged into {0} ({1}).").format(target_risk_id, merge_strategy)
		source_doc.save(ignore_permissions=True)
		merged.append(source)
	return {"target": target_risk_id, "merged": merged}


# ──────────────────────────────────────────────────────────────────────────────
# APPETITE
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def api_resolve_appetite(entity: str, risk_category: str, method_profile: str) -> dict:
	appetite, warning = resolve_appetite(entity, risk_category, method_profile)
	return {"appetite": appetite, "warning": warning}


# ──────────────────────────────────────────────────────────────────────────────
# CADENCE
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def api_trigger_reassessment(risk_item: str, trigger_type: str, within_days: int = 14) -> dict:
	proposed = trigger_event_driven_reassessment(risk_item, trigger_type, int(within_days))
	return {"risk_item": risk_item, "next_reassessment_due": str(proposed)}


@frappe.whitelist()
def list_stale_risks(entity: str) -> list:
	return frappe.get_all(
		"Risk Item",
		filters={"entity": entity, "status": ("not in", ["Closed", "Withdrawn", "Archived"]), "reassessment_overdue_days": (">", 30)},
		fields=["name", "title", "risk_owner", "last_assessed_on", "next_reassessment_due", "reassessment_overdue_days"],
		order_by="reassessment_overdue_days desc",
	)


# ──────────────────────────────────────────────────────────────────────────────
# AGGREGATION
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def get_aggregate_posture(entity: str, method: str | None = None, top_n: int | None = None) -> dict:
	return compute_aggregate_posture(entity, method, int(top_n) if top_n else None)


@frappe.whitelist()
def get_top_n(entity: str, n: int = 10) -> list:
	return frappe.get_all(
		"Risk Item",
		filters={"entity": entity, "is_top_n_at_entity": 1},
		fields=["name", "title", "risk_category", "residual_rating", "residual_band", "risk_owner", "treatment_status", "aggregation_rank"],
		order_by="aggregation_rank asc",
		limit_page_length=int(n),
	)


@frappe.whitelist()
def refresh_top_n_ranks(entity: str) -> dict:
	_require_role(*MANAGER_ROLES)
	count = recompute_top_n_ranks(entity)
	return {"entity": entity, "ranked_count": count}


@frappe.whitelist()
def get_heat_map_data(entity: str, dimension: str = "residual") -> dict:
	fieldset = ("residual_likelihood", "residual_impact") if dimension == "residual" else ("inherent_likelihood", "inherent_impact")
	rows = frappe.get_all(
		"Risk Item",
		filters={"entity": entity, "status": ("not in", ["Closed", "Withdrawn", "Archived"])},
		fields=["name", "title", *fieldset],
	)
	cells: dict = {}
	for row in rows:
		likelihood, impact = row.get(fieldset[0]), row.get(fieldset[1])
		if likelihood is None or impact is None:
			continue
		key = f"{likelihood}x{impact}"
		cells.setdefault(key, {"likelihood": likelihood, "impact": impact, "count": 0, "risks": []})
		cells[key]["count"] += 1
		cells[key]["risks"].append(row.name)
	return {"dimension": dimension, "cells": list(cells.values())}


# ──────────────────────────────────────────────────────────────────────────────
# LIST / GET
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def list_risks(entity: str, status: str | None = None, risk_category: str | None = None, risk_owner: str | None = None, limit: int = 50, start: int = 0) -> dict:
	filters = {"entity": entity}
	for key, value in (("status", status), ("risk_category", risk_category), ("risk_owner", risk_owner)):
		if value:
			filters[key] = value
	rows = frappe.get_all(
		"Risk Item",
		filters=filters,
		fields=["name", "title", "risk_category", "risk_owner", "inherent_rating", "residual_rating", "residual_band", "within_appetite", "acceptance_required", "status", "next_reassessment_due", "is_reassessment_overdue"],
		order_by="residual_rating desc",
		limit_page_length=limit,
		limit_start=start,
	)
	return {"items": rows, "total": frappe.db.count("Risk Item", filters=filters), "limit": limit, "start": start}


@frappe.whitelist()
def get_risk(risk_item: str) -> dict:
	risk = frappe.get_doc("Risk Item", risk_item)
	assessments = frappe.get_all(
		"Risk Assessment", filters={"risk_item": risk_item}, fields=["name", "assessment_seq", "inherent_rating", "residual_rating", "status", "creation"], order_by="creation asc"
	)
	treatments = frappe.get_all(
		"Risk Treatment", filters={"risk_item": risk_item}, fields=["name", "treatment_type", "status", "downstream_doctype", "downstream_record", "target_completion_date"], order_by="creation asc"
	)
	acceptances = frappe.get_all(
		"Risk Acceptance", filters={"risk_item": risk_item}, fields=["name", "status", "valid_until", "residual_rating_at_acceptance"], order_by="creation desc"
	)
	return {"risk": risk.as_dict(), "assessments": assessments, "treatments": treatments, "acceptances": acceptances}
