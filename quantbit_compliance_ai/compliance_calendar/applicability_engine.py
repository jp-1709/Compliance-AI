"""
complyai/compliance/compliance_calendar/applicability_engine.py

Applicability Engine — decides which Compliance Obligations apply to a Business Entity.

How it works
------------
1. Load all published Compliance Obligations.
2. For each obligation, evaluate its Applicability Rules against the entity's profile.
3. Apply top-level threshold checks (states, industries, employee count, flags).
4. Return the list of applicable obligations.

Rule evaluation
---------------
Each `Applicability Rule` row is evaluated in order.
The `logical_operator` column (AND/OR) determines how each row combines with the next.
The final result is the accumulated boolean.

Idempotency: calling this function multiple times for the same entity returns the same list.
"""

import json
import frappe
from frappe.utils import cint, flt


class ApplicabilityEngine:

    def __init__(self, entity):
        """
        entity: frappe.Document instance of Business Entity
        """
        self.entity = entity

    def get_applicable_obligations(self) -> list:
        """
        Return a list of published Compliance Obligation documents
        that apply to this entity.
        """
        obligations = frappe.get_all(
            "Compliance Obligation",
            filters={"is_published": 1},
            fields=["name"],
        )

        applicable = []
        for row in obligations:
            try:
                obligation = frappe.get_doc("Compliance Obligation", row.name)
                if self._applies(obligation):
                    applicable.append(obligation)
            except Exception as e:
                frappe.log_error(
                    f"Applicability Engine error for {row.name}: {e}",
                    "ApplicabilityEngine",
                )
        return applicable

    def _applies(self, obligation) -> bool:
        """
        Return True if this obligation applies to the entity.

        Evaluation order:
        1. Top-level threshold checks (employee count, states, industries, flags).
        2. Applicability Rule chain (if any rules defined).
        """
        # ── 1. Top-level threshold checks ──────────────────────────────────

        # Employee count range
        emp = cint(getattr(self.entity, "employee_count", 0) or 0)
        if obligation.min_employees and emp < cint(obligation.min_employees):
            return False
        if obligation.max_employees and emp > cint(obligation.max_employees):
            return False

        # State filter
        if obligation.applicable_states:
            allowed_states = [r.state for r in obligation.applicable_states]
            if allowed_states and getattr(self.entity, "state", None) not in allowed_states:
                return False

        # Industry filter
        if obligation.applicable_industries:
            allowed_industries = [r.industry for r in obligation.applicable_industries]
            if allowed_industries and getattr(self.entity, "industry", None) not in allowed_industries:
                return False

        # Listed companies only
        if obligation.applies_to_listed_only and not getattr(self.entity, "is_listed", 0):
            return False

        # Hazardous operations only
        if obligation.applies_to_hazardous_only and not getattr(self.entity, "is_hazardous", 0):
            return False

        # MSME filter: if obligation is not MSME-applicable, skip MSMEs
        if not obligation.applies_to_msme and getattr(self.entity, "is_msme", 0):
            return False

        # ── 2. Applicability Rule chain ─────────────────────────────────────

        rules = obligation.applicability_rules or []
        if not rules:
            return True  # No rules = universally applicable (subject to threshold checks above)

        result = self._evaluate_rule(rules[0])
        for i in range(len(rules) - 1):
            current_rule = rules[i]
            next_rule = rules[i + 1]
            next_result = self._evaluate_rule(next_rule)
            if current_rule.logical_operator == "OR":
                result = result or next_result
            else:  # AND (default)
                result = result and next_result

        return result

    def _evaluate_rule(self, rule) -> bool:
        """
        Evaluate a single Applicability Rule row against the entity.

        rule_type maps to an entity attribute:
          State Match         → entity.state
          Industry Match      → entity.industry
          Employee Threshold  → entity.employee_count
          Turnover Threshold  → entity.annual_turnover
          Listed Status       → entity.is_listed
          Hazardous Status    → entity.is_hazardous
          MSME Status         → entity.is_msme
          Entity Type         → entity.entity_type
          Custom Expression   → eval() — DANGEROUS, only allowed for trusted content

        value_text is JSON-encoded. Supported formats:
          "Maharashtra"                    → plain string
          ["Maharashtra", "Goa"]           → list
          {"min": 10, "max": 499}          → range dict
        """
        entity = self.entity
        rule_type = rule.rule_type
        operator = rule.operator

        try:
            value = json.loads(rule.value_text) if rule.value_text else None
        except (json.JSONDecodeError, TypeError):
            value = rule.value_text  # Treat as plain string

        # Map rule_type to entity attribute
        attr_map = {
            "State Match":          "state",
            "Industry Match":       "industry",
            "Employee Threshold":   "employee_count",
            "Turnover Threshold":   "annual_turnover",
            "Listed Status":        "is_listed",
            "Hazardous Status":     "is_hazardous",
            "MSME Status":          "is_msme",
            "Entity Type":          "entity_type",
        }

        if rule_type == "Custom Expression":
            # Custom expressions are evaluated in a sandboxed context.
            # Only system-managed obligations should use this.
            try:
                return bool(eval(  # noqa: S307
                    str(value),
                    {"__builtins__": {}},
                    {"entity": entity},
                ))
            except Exception:
                return False

        attr = attr_map.get(rule_type)
        if not attr:
            return True  # Unknown rule type — pass through

        entity_value = getattr(entity, attr, None)

        return self._compare(entity_value, operator, value)

    def _compare(self, entity_value, operator: str, rule_value) -> bool:
        """
        Apply the operator to compare entity_value against rule_value.
        """
        try:
            ev = flt(entity_value) if isinstance(entity_value, (int, float)) else entity_value
            rv = rule_value

            if operator == "equals":
                return str(ev) == str(rv)
            elif operator == "not_equals":
                return str(ev) != str(rv)
            elif operator == "greater_than":
                return flt(ev) > flt(rv)
            elif operator == "less_than":
                return flt(ev) < flt(rv)
            elif operator == "greater_equal":
                return flt(ev) >= flt(rv)
            elif operator == "less_equal":
                return flt(ev) <= flt(rv)
            elif operator == "in":
                return str(ev) in (rv if isinstance(rv, list) else [str(rv)])
            elif operator == "not_in":
                return str(ev) not in (rv if isinstance(rv, list) else [str(rv)])
            elif operator == "between":
                # rv expected: {"min": x, "max": y} or [x, y]
                if isinstance(rv, dict):
                    return flt(rv.get("min", 0)) <= flt(ev) <= flt(rv.get("max", float("inf")))
                elif isinstance(rv, list) and len(rv) == 2:
                    return flt(rv[0]) <= flt(ev) <= flt(rv[1])
                return False
            else:
                frappe.log_error(f"Unknown operator: {operator}", "ApplicabilityEngine")
                return False
        except Exception as e:
            frappe.log_error(str(e), "ApplicabilityEngine._compare")
            return False