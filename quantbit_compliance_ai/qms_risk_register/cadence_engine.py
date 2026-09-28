"""
quantbit_compliance_ai/qms_risk_register/cadence_engine.py

Reassessment Cadence Engine (Q6 spec §7).

  - resolve_cadence_days()          : category -> Risk Policy cadence_*_days lookup
  - compute_next_reassessment_due() : last_assessed_on + cadence
  - refresh_overdue_flags()         : sets is_reassessment_overdue / overdue_days,
                                       returns whether the risk has crossed into
                                       "stale" (overdue > 30 days, §7.3)
  - trigger_event_driven_reassessment(): pulls next_reassessment_due forward when
                                       a linked QE/Audit/Reg event fires (§7.2)

NOTE: the shipped schema has no dedicated "Risk Reassessment Trigger" log
DocType, so the trigger is recorded on the Risk Item's own comment timeline
(consistent with how Q1 records activity) rather than inventing a new master.
"""

import frappe
from frappe import _
from frappe.utils import add_days, date_diff, getdate, nowdate

STALE_THRESHOLD_DAYS = 30

# Risk Category.category_name -> Risk Policy cadence field, used only when the
# category record has no explicit default_cadence_field set.
_CATEGORY_NAME_FALLBACK = {
	"strategic": "cadence_strategic_days",
	"operational": "cadence_operational_days",
	"compliance": "cadence_compliance_days",
	"infosec": "cadence_infosec_days",
	"information security": "cadence_infosec_days",
	"oh&s": "cadence_oh_s_days",
	"occupational health": "cadence_oh_s_days",
	"environmental": "cadence_environmental_days",
	"food safety": "cadence_food_safety_days",
	"product safety": "cadence_product_safety_days",
	"business continuity": "cadence_business_continuity_days",
	"financial": "cadence_financial_days",
	"reputational": "cadence_reputational_days",
}


def _get_policy(entity: str):
	name = frappe.db.get_value("Risk Policy", {"entity": entity}, "name", order_by="modified desc")
	return frappe.get_doc("Risk Policy", name) if name else None


def resolve_cadence_days(entity: str, risk_category: str) -> int:
	policy = _get_policy(entity)
	if not policy:
		return 180

	category = frappe.db.get_value(
		"Risk Category", risk_category, ["default_cadence_field", "category_name"], as_dict=True
	)
	fieldname = category.default_cadence_field if category else None
	if not fieldname:
		fieldname = _CATEGORY_NAME_FALLBACK.get((category.category_name or "").strip().lower()) if category else None
	if not fieldname or not hasattr(policy, fieldname):
		fieldname = "cadence_other_days"
	return policy.get(fieldname) or policy.cadence_other_days or 180


def compute_next_reassessment_due(entity: str, risk_category: str, last_assessed_on):
	if not last_assessed_on:
		return None
	days = resolve_cadence_days(entity, risk_category)
	return add_days(getdate(last_assessed_on), days)


def refresh_overdue_flags(risk_item) -> bool:
	"""Mutate risk_item in place. Returns True if the risk is now 'stale'
	(overdue by more than STALE_THRESHOLD_DAYS)."""
	due = risk_item.next_reassessment_due
	if not due:
		risk_item.is_reassessment_overdue = 0
		risk_item.reassessment_overdue_days = 0
		return False
	delta = date_diff(nowdate(), due)
	risk_item.is_reassessment_overdue = 1 if delta > 0 else 0
	risk_item.reassessment_overdue_days = max(delta, 0)
	return delta > STALE_THRESHOLD_DAYS


def trigger_event_driven_reassessment(risk_item_name: str, trigger_type: str, within_days: int):
	"""§7.2 — a new linked QE/Audit finding/Reg change/re-opening pulls the
	reassessment deadline forward. Idempotent: never pushes the date later,
	only earlier."""
	risk = frappe.db.get_value("Risk Item", risk_item_name, "next_reassessment_due")
	proposed = add_days(nowdate(), within_days)
	if not risk or getdate(proposed) < getdate(risk):
		frappe.db.set_value("Risk Item", risk_item_name, "next_reassessment_due", proposed)
	frappe.get_doc("Risk Item", risk_item_name).add_comment(
		"Info", _("Event-driven reassessment triggered by {0}; due by {1}.").format(trigger_type, proposed)
	)
	return proposed
