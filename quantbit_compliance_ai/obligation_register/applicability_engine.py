"""
applicability_engine.py
Core applicability-evaluation engine for ComplyAI Obligation Register.

Public API
----------
is_obligation_applicable(obligation, entity)  → (bool, list[dict])
evaluate_single_rule(rule, entity)            → bool
evaluate_custom_expression(expr, rules, entity) → bool
get_applicable_obligations(entity_name, as_of_date, include_trace) → list
"""

import json
from typing import Optional

import frappe
from frappe.utils import getdate


# ─────────────────────────────────────────────────────────────────────────────
# Field map: rule_type → entity profile key
# ─────────────────────────────────────────────────────────────────────────────
_RULE_FIELD_MAP = {
    "State Match":                  "state",
    "Industry Match (NIC)":         "industry_code",
    "Employee Threshold":           "employee_count",
    "Contract Worker Threshold":    "contract_worker_count",
    "Women Employee Threshold":     "women_employee_count",
    "Turnover Threshold":           "turnover_inr_cr",
    "Listed Status":                "is_listed",
    "Hazardous Status":             "is_hazardous",
    "MSME Status":                  "is_msme",
    "Entity Type":                  "entity_type",
    "Business Type":                "business_type",
    "Is Principal Entity":          "is_principal_entity",
    "24x7 Operations":              "operates_24x7",
    "Has Canteen":                  "has_canteen",
    "Shifts Count":                 "shifts_count",
    "GSTIN Present":                "gstin",
    "EPFO Code Present":            "epfo_code",
    "ESIC Code Present":            "esic_code",
    "Factory Licence Present":      "factory_license_no",
    "IEC Present":                  "iec_code",
    "LEI Present":                  "lei",
}

# Safe builtins for custom expression sandbox
_SAFE_BUILTINS = {
    "__builtins__": {
        "True": True,
        "False": False,
        "None": None,
        "all": all,
        "any": any,
        "len": len,
        "bool": bool,
        "int": int,
        "float": float,
        "str": str,
    }
}


# ─────────────────────────────────────────────────────────────────────────────
# Main applicability function
# ─────────────────────────────────────────────────────────────────────────────

def is_obligation_applicable(obligation: dict, entity: dict) -> tuple:
    """
    Evaluate whether *obligation* applies to *entity*.

    Parameters
    ----------
    obligation : dict
        Flattened obligation record (from frappe.get_doc().as_dict() or pre-fetched cache).
    entity : dict
        Flattened Business Entity / location profile with applicable keys.

    Returns
    -------
    (verdict: bool, trace: list[dict])
        trace contains one entry per rule evaluated with keys:
        rule, passed, expected (optional), actual (optional).
    """
    trace = []

    # ── 1. Cheap flag checks (fail-fast, O(1)) ────────────────────────────
    if obligation.get("applies_to_listed_only") and not entity.get("is_listed"):
        return False, [{"rule": "Listed only", "passed": False,
                        "expected": True, "actual": entity.get("is_listed")}]

    if obligation.get("applies_to_hazardous_only") and not entity.get("is_hazardous"):
        return False, [{"rule": "Hazardous only", "passed": False,
                        "expected": True, "actual": entity.get("is_hazardous")}]

    if obligation.get("applies_to_msme_only") and not entity.get("is_msme"):
        return False, [{"rule": "MSME only", "passed": False,
                        "expected": True, "actual": entity.get("is_msme")}]

    if obligation.get("exempt_for_msme") and entity.get("is_msme"):
        return False, [{"rule": "MSME exempt", "passed": False,
                        "expected": False, "actual": entity.get("is_msme")}]

    # ── 2. State filter ───────────────────────────────────────────────────
    applicable_states = obligation.get("applicable_states") or []
    if applicable_states:
        state_list = [s.get("state") for s in applicable_states]
        entity_state = entity.get("state")
        if entity_state not in state_list:
            return False, [{"rule": "State filter", "passed": False,
                            "expected": state_list, "actual": entity_state}]
        trace.append({"rule": "State filter", "passed": True,
                      "expected": state_list, "actual": entity_state})

    # ── 3. Industry filter ────────────────────────────────────────────────
    applicable_industries = obligation.get("applicable_industries") or []
    if applicable_industries:
        industry_list = [i.get("industry_code") for i in applicable_industries]
        entity_industry = entity.get("industry_code")
        if entity_industry not in industry_list:
            return False, [{"rule": "Industry filter", "passed": False,
                            "expected": industry_list, "actual": entity_industry}]
        trace.append({"rule": "Industry filter", "passed": True,
                      "expected": industry_list, "actual": entity_industry})

    # ── 4. Employee threshold ─────────────────────────────────────────────
    employees = (entity.get("employee_count") or 0) + (entity.get("contract_worker_count") or 0)
    min_emp = obligation.get("min_employees")
    max_emp = obligation.get("max_employees")

    if min_emp and employees < int(min_emp):
        return False, [{"rule": "Employee min", "passed": False,
                        "expected": f">={min_emp}", "actual": employees}]
    if max_emp and employees > int(max_emp):
        return False, [{"rule": "Employee max", "passed": False,
                        "expected": f"<={max_emp}", "actual": employees}]
    trace.append({"rule": "Employee thresholds", "passed": True, "actual": employees})

    # ── 5. Turnover threshold ─────────────────────────────────────────────
    turnover = entity.get("turnover_inr_cr") or 0
    min_turn = obligation.get("min_turnover_inr_cr")
    max_turn = obligation.get("max_turnover_inr_cr")

    if min_turn and float(turnover) < float(min_turn):
        return False, [{"rule": "Turnover min", "passed": False,
                        "expected": f">={min_turn}", "actual": turnover}]
    if max_turn and float(turnover) > float(max_turn):
        return False, [{"rule": "Turnover max", "passed": False,
                        "expected": f"<={max_turn}", "actual": turnover}]
    trace.append({"rule": "Turnover thresholds", "passed": True, "actual": turnover})

    # ── 6. Business-type filter ───────────────────────────────────────────
    allowed_biz_types = _csv_to_list(obligation.get("applicable_business_types"))
    if allowed_biz_types:
        entity_biz = (entity.get("business_type") or "").strip()
        if entity_biz and entity_biz not in allowed_biz_types:
            return False, [{"rule": "Business type filter", "passed": False,
                            "expected": allowed_biz_types, "actual": entity_biz}]
        trace.append({"rule": "Business type filter", "passed": True, "actual": entity_biz})

    # ── 7. Entity-type filter ─────────────────────────────────────────────
    allowed_ent_types = _csv_to_list(obligation.get("applicable_entity_types"))
    if allowed_ent_types:
        entity_type = (entity.get("entity_type") or "").strip()
        if entity_type and entity_type not in allowed_ent_types:
            return False, [{"rule": "Entity type filter", "passed": False,
                            "expected": allowed_ent_types, "actual": entity_type}]
        trace.append({"rule": "Entity type filter", "passed": True, "actual": entity_type})

    # ── 8. Custom applicability rules ─────────────────────────────────────
    rule_rows = obligation.get("applicability_rules") or []
    rule_results = []
    for rule in rule_rows:
        result = evaluate_single_rule(rule, entity)
        rule_results.append(result)
        trace.append({
            "rule": f"Rule {rule.get('rule_index', '?')}: {rule.get('rule_type')}",
            "operator": rule.get("operator"),
            "value": rule.get("value_text"),
            "passed": result,
        })

    # ── 9. Combine results ────────────────────────────────────────────────
    logic = obligation.get("applicability_logic") or "ALL (AND)"
    if rule_results:
        if logic == "ALL (AND)":
            verdict = all(rule_results)
        elif logic == "ANY (OR)":
            verdict = any(rule_results)
        else:  # Custom Expression
            verdict = evaluate_custom_expression(
                obligation.get("applicability_expression", ""),
                rule_results,
                entity,
            )
    else:
        # No custom rules — passed all quick checks above
        verdict = True

    return verdict, trace


# ─────────────────────────────────────────────────────────────────────────────
# Single-rule evaluator
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_single_rule(rule: dict, entity: dict) -> bool:
    """
    Evaluate one Applicability Rule row against an entity profile.
    Never raises — returns False on bad/missing data.
    """
    rule_type = rule.get("rule_type") or ""
    op = rule.get("operator") or ""
    value_text = rule.get("value_text") or ""

    # Parse value
    try:
        value = json.loads(value_text) if value_text.strip() else None
    except (json.JSONDecodeError, AttributeError):
        return False

    # Resolve entity field
    if rule_type == "Custom Field Match":
        field = rule.get("custom_field")
    else:
        field = _RULE_FIELD_MAP.get(rule_type)

    if not field:
        return False

    actual = entity.get(field)

    # Presence-only operators (no value needed)
    if op == "is_set":
        return bool(actual)
    if op == "is_not_set":
        return not bool(actual)

    # Numeric/string comparisons
    try:
        if op == "equals":
            return actual == value
        if op == "not_equals":
            return actual != value
        if op == "greater_than":
            return (actual or 0) > value
        if op == "less_than":
            return (actual or 0) < value
        if op == "greater_or_equal":
            return (actual or 0) >= value
        if op == "less_or_equal":
            return (actual or 0) <= value
        if op == "in":
            return actual in (value or [])
        if op == "not_in":
            return actual not in (value or [])
        if op == "between":
            lo = value.get("min") if isinstance(value, dict) else None
            hi = value.get("max") if isinstance(value, dict) else None
            if lo is None or hi is None:
                return False
            return lo <= (actual or 0) <= hi
    except (TypeError, AttributeError):
        return False

    return False


# ─────────────────────────────────────────────────────────────────────────────
# Custom expression evaluator (sandboxed)
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_custom_expression(expression: str, rule_results: list, entity: dict) -> bool:
    """
    Evaluate a custom Python expression in a restricted sandbox.

    Available variables:
        rules  — list of booleans (one per rule row, in order)
        entity — dict of entity profile fields

    SECURITY: __import__, open, exec, and all builtins not in _SAFE_BUILTINS
    are blocked. Never call with untrusted expressions outside Legal Counsel
    / System Manager authorship context.
    """
    expression = (expression or "").strip()
    if not expression:
        return False

    safe_globals = {
        **_SAFE_BUILTINS,
        "rules": rule_results,
        "entity": entity,
    }

    # Extra guard: reject obvious injection patterns
    _assert_no_dangerous_tokens(expression)

    try:
        compiled = compile(expression, "<applicability>", "eval")
        return bool(eval(compiled, safe_globals, {}))  # nosec B307
    except Exception as exc:
        frappe.log_error(
            message=f"Applicability expression error: {exc}\n{expression}",
            title="Applicability Engine",
        )
        return False


def _assert_no_dangerous_tokens(expr: str):
    """Raise if obviously dangerous tokens appear in the expression."""
    forbidden = ["__import__", "__builtins__", "open(", "exec(", "eval(", "os.", "sys."]
    for token in forbidden:
        if token in expr:
            frappe.throw(
                f"Expression contains forbidden token: '{token}'",
                frappe.PermissionError,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Bulk applicability — the hot-path called by Compliance Calendar
# ─────────────────────────────────────────────────────────────────────────────

def get_applicable_obligations(
    entity_name: str,
    as_of_date: Optional[str] = None,
    include_trace: bool = False,
) -> list:
    """
    Return all published obligations applicable to *entity_name* as of *as_of_date*.

    Performance target: < 500 ms for 5 000 obligations.
    Strategy:
      1. Load entity once.
      2. Load all published, current obligations in a single DB query (fields only).
      3. Apply cheap flag / state / industry pre-filters in Python.
      4. Run custom rules only on the survivors.
    """
    check_date = getdate(as_of_date) if as_of_date else getdate()

    # ── Load entity profile ───────────────────────────────────────────────
    entity = _load_entity_profile(entity_name)

    # ── Load published obligations (lean fetch) ───────────────────────────
    obligations = frappe.get_all(
        "Compliance Obligation",
        filters={
            "is_published": 1,
            "is_current_version": 1,
            "curation_status": "Published",
            "effective_from": ["<=", check_date],
        },
        fields=[
            "name", "obligation_title", "obligation_code", "obligation_type",
            "category", "sub_category", "regulation", "section_reference",
            "frequency", "due_timing_rule", "due_timing_machine",
            "default_risk_level", "complexity", "estimated_effort_hours",
            "applies_to_listed_only", "applies_to_hazardous_only",
            "applies_to_msme_only", "exempt_for_msme",
            "min_employees", "max_employees",
            "min_turnover_inr_cr", "max_turnover_inr_cr",
            "applicable_business_types", "applicable_entity_types",
            "applicability_logic", "applicability_expression",
            "effective_from", "effective_until",
        ],
        order_by="name asc",
        limit_page_length=0,
    )

    # Filter out obligations whose effective_until has passed
    obligations = [
        o for o in obligations
        if not o.get("effective_until") or getdate(o["effective_until"]) >= check_date
    ]

    # ── Enrich each obligation with child-table data ───────────────────────
    # Batch-load child rows to avoid N+1 queries
    ob_names = [o["name"] for o in obligations]

    states_map = _batch_child(
        "Obligation State", "parent", ob_names, ["parent", "state"]
    )
    industries_map = _batch_child(
        "Obligation Industry", "parent", ob_names, ["parent", "industry_code"]
    )
    rules_map = _batch_child(
        "Applicability Rule", "parent", ob_names,
        ["parent", "rule_index", "rule_type", "operator", "value_text", "custom_field"]
    )

    results = []
    for ob in obligations:
        ob["applicable_states"] = states_map.get(ob["name"], [])
        ob["applicable_industries"] = industries_map.get(ob["name"], [])
        ob["applicability_rules"] = rules_map.get(ob["name"], [])

        verdict, trace = is_obligation_applicable(ob, entity)
        if verdict:
            row = dict(ob)
            if include_trace:
                row["_trace"] = trace
            results.append(row)

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Internal utilities
# ─────────────────────────────────────────────────────────────────────────────

def _load_entity_profile(entity_name: str) -> dict:
    """Load a Business Entity and flatten into a profile dict."""
    doc = frappe.get_doc("Business Entity", entity_name)
    return {
        "name": doc.name,
        "state": doc.get("state"),
        "industry_code": doc.get("industry_code"),
        "employee_count": doc.get("employee_count") or 0,
        "contract_worker_count": doc.get("contract_worker_count") or 0,
        "women_employee_count": doc.get("women_employee_count") or 0,
        "turnover_inr_cr": doc.get("turnover_inr_cr") or 0,
        "is_listed": bool(doc.get("is_listed")),
        "is_hazardous": bool(doc.get("is_hazardous")),
        "is_msme": bool(doc.get("is_msme")),
        "entity_type": doc.get("entity_type"),
        "business_type": doc.get("business_type"),
        "is_principal_entity": bool(doc.get("is_principal_entity")),
        "operates_24x7": bool(doc.get("operates_24x7")),
        "has_canteen": bool(doc.get("has_canteen")),
        "shifts_count": doc.get("shifts_count") or 0,
        "gstin": doc.get("gstin"),
        "epfo_code": doc.get("epfo_code"),
        "esic_code": doc.get("esic_code"),
        "factory_license_no": doc.get("factory_license_no"),
        "iec_code": doc.get("iec_code"),
        "lei": doc.get("lei"),
    }


def _batch_child(doctype: str, parent_field: str, parent_names: list, fields: list) -> dict:
    """Return {parent_name: [rows]} for a child table, loaded in one DB query."""
    if not parent_names:
        return {}
    rows = frappe.get_all(
        doctype,
        filters={parent_field: ["in", parent_names]},
        fields=fields,
        limit_page_length=0,
    )
    result: dict = {}
    for row in rows:
        key = row[parent_field]
        result.setdefault(key, []).append(row)
    return result


def _csv_to_list(value: Optional[str]) -> list:
    """Convert comma-separated string to stripped list, ignoring empty entries."""
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]