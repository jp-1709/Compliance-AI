"""
quantbit_compliance_ai/qms_risk_register/aggregation_engine.py

Aggregation Engine (Q6 spec §6) + Risk Action Tracker maintenance (§4.12) +
trend computation (used by the Risk Item snapshot fields).

  - compute_aggregate_posture() : Sum-of-Top-N / Weighted Avg / Max rollup
  - recompute_top_n_ranks()     : sets is_top_n_at_entity / aggregation_rank
  - refresh_action_tracker()    : upserts the denormalised dashboard row
  - compute_trend()             : Improving/Stable/Worsening/Volatile over the
                                    last 3 submitted assessments
"""

import frappe
from frappe.utils import now_datetime

OPEN_STATUSES_EXCLUDED = ("Closed", "Withdrawn", "Archived")


def _get_policy(entity: str):
	return frappe.db.get_value(
		"Risk Policy", {"entity": entity}, ["aggregate_method", "aggregate_top_n"], as_dict=True, order_by="modified desc"
	)


def compute_aggregate_posture(entity: str, method: str | None = None, top_n: int | None = None) -> dict:
	policy = _get_policy(entity)
	method = method or (policy.aggregate_method if policy else "Sum of Top-N")
	top_n = top_n or (policy.aggregate_top_n if policy else 10)

	risks = frappe.get_all(
		"Risk Item",
		filters={"entity": entity, "status": ("not in", OPEN_STATUSES_EXCLUDED)},
		fields=["name", "residual_rating", "residual_band", "within_appetite", "acceptance_required", "is_reassessment_overdue", "risk_category"],
	)
	ratings = sorted((r.residual_rating or 0 for r in risks), reverse=True)

	if method == "Max":
		score = ratings[0] if ratings else 0
	elif method == "Weighted Avg":
		score = round(sum(ratings) / len(ratings), 2) if ratings else 0
	else:  # "Sum of Top-N" and "Custom" fallback (a bespoke Python formula is out of scope here)
		score = sum(ratings[: int(top_n)])

	by_band: dict = {}
	by_category: dict = {}
	for r in risks:
		band = r.residual_band or "Unrated"
		by_band[band] = by_band.get(band, 0) + 1
		cat = r.risk_category or "Uncategorised"
		by_category.setdefault(cat, {"open_count": 0, "top_residual": 0})
		by_category[cat]["open_count"] += 1
		by_category[cat]["top_residual"] = max(by_category[cat]["top_residual"], r.residual_rating or 0)

	return {
		"entity": entity,
		"method": method,
		"top_n": top_n,
		"aggregate_score": score,
		"open_count": len(risks),
		"above_appetite_count": sum(1 for r in risks if r.within_appetite == 0),
		"acceptance_required_count": sum(1 for r in risks if r.acceptance_required),
		"reassessment_overdue_count": sum(1 for r in risks if r.is_reassessment_overdue),
		"by_band": by_band,
		"by_category": by_category,
	}


def recompute_top_n_ranks(entity: str) -> int:
	policy = _get_policy(entity)
	top_n = policy.aggregate_top_n if policy else 10
	risks = frappe.get_all(
		"Risk Item",
		filters={"entity": entity, "status": ("not in", OPEN_STATUSES_EXCLUDED)},
		fields=["name", "residual_rating", "inherent_rating", "last_assessed_on"],
		order_by="residual_rating desc, inherent_rating desc, last_assessed_on desc",
	)
	for idx, row in enumerate(risks, start=1):
		is_top = idx <= top_n
		frappe.db.set_value(
			"Risk Item",
			row.name,
			{"is_top_n_at_entity": 1 if is_top else 0, "aggregation_rank": idx if is_top else 0},
			update_modified=False,
		)
	return len(risks)


def compute_trend(risk_item_name: str) -> tuple:
	"""Returns (trend_direction, delta_last_3) from up to the 3 most recent
	*submitted* assessments, newest first."""
	assessments = frappe.get_all(
		"Risk Assessment",
		filters={"risk_item": risk_item_name, "docstatus": 1},
		fields=["residual_rating"],
		order_by="creation desc",
		limit=3,
	)
	ratings = [a.residual_rating for a in assessments if a.residual_rating is not None]
	if len(ratings) < 2:
		return "Insufficient Data", 0

	delta = ratings[0] - ratings[-1]  # latest minus oldest in the window
	if len(ratings) == 3:
		step_a, step_b = ratings[0] - ratings[1], ratings[1] - ratings[2]
		if step_a * step_b < 0 and abs(step_a) >= 1 and abs(step_b) >= 1:
			return "Volatile", delta

	if delta < 0:
		return "Improving", delta
	if delta > 0:
		return "Worsening", delta
	return "Stable", delta


def refresh_action_tracker(risk_item_name: str) -> str:
	risk = frappe.get_doc("Risk Item", risk_item_name)
	open_treatments = frappe.db.count(
		"Risk Treatment", {"risk_item": risk_item_name, "status": ("not in", ["Completed", "Verified Effective", "Cancelled"])}
	)
	completed_treatments = frappe.db.count(
		"Risk Treatment", {"risk_item": risk_item_name, "status": ("in", ["Completed", "Verified Effective"])}
	)
	acceptance_valid_until = (
		frappe.db.get_value("Risk Acceptance", risk.active_acceptance, "valid_until") if risk.active_acceptance else None
	)

	values = {
		"entity": risk.entity,
		"risk_item": risk.name,
		"risk_category": risk.risk_category,
		"title": risk.title,
		"risk_owner": risk.risk_owner,
		"inherent_rating": risk.inherent_rating,
		"residual_rating": risk.residual_rating,
		"residual_band": risk.residual_band,
		"within_appetite": risk.within_appetite,
		"acceptance_required": risk.acceptance_required,
		"active_acceptance": risk.active_acceptance,
		"acceptance_valid_until": acceptance_valid_until,
		"treatment_status": risk.treatment_status,
		"open_treatment_count": open_treatments,
		"completed_treatment_count": completed_treatments,
		"last_assessed_on": risk.last_assessed_on,
		"next_reassessment_due": risk.next_reassessment_due,
		"is_reassessment_overdue": risk.is_reassessment_overdue,
		"reassessment_overdue_days": risk.reassessment_overdue_days,
		"top_n_at_entity": risk.is_top_n_at_entity,
		"aggregation_rank": risk.aggregation_rank,
		"trend_direction": risk.trend_direction,
		"last_synced_on": now_datetime(),
	}

	existing = frappe.db.get_value("Risk Action Tracker", {"risk_item": risk_item_name}, "name")
	if existing:
		frappe.db.set_value("Risk Action Tracker", existing, values, update_modified=False)
		return existing

	tracker = frappe.get_doc({"doctype": "Risk Action Tracker", **values})
	tracker.insert(ignore_permissions=True)
	return tracker.name
