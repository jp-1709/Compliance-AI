"""
quantbit_compliance_ai/compliance_calendar/applicability_engine.py

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

import ast
import json
import frappe
from frappe.utils import cint, flt

# Fields on Business Entity a Custom Expression rule is allowed to read.
# Keeping this an explicit allowlist (rather than "any attribute") is what
# makes the safe-eval below actually safe — see _safe_eval_custom_expression.
_ALLOWED_ENTITY_ATTRS = {
	"state", "industry_code", "entity_type", "employee_count", "contract_worker_count",
	"women_employee_count", "differently_abled_count", "shifts_count", "operates_24x7",
	"has_canteen", "is_listed", "is_hazardous", "is_msme", "is_principal_entity",
}

# Only these AST node types may appear in a Custom Expression. Notably absent:
# ast.Call (no function calls), ast.Subscript, ast.Lambda, ast.Import, etc. —
# there is no way to reach __class__/__subclasses__-style sandbox escapes
# without at least one Attribute-on-arbitrary-object or Call node, and every
# Attribute node is additionally checked against _ALLOWED_ENTITY_ATTRS below.
_ALLOWED_EXPR_NODES = (
	ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
	ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
	ast.In, ast.NotIn, ast.Name, ast.Load, ast.Attribute, ast.Constant,
	ast.List, ast.Tuple,
)


class _EntityAttrProxy:
	"""Read-only view over a Business Entity doc exposing only the allowlisted
	attributes to a Custom Expression — a second layer of defence alongside
	the AST allowlist below."""

	def __init__(self, entity):
		self._entity = entity

	def __getattr__(self, item):
		if item not in _ALLOWED_ENTITY_ATTRS:
			raise AttributeError(item)
		return getattr(self._entity, item, None)


def _safe_eval_custom_expression(expr: str, entity) -> bool:
	"""Evaluate a Custom Expression rule without ever calling Python's eval()
	on unvetted code. Parses to an AST, rejects anything outside a small
	allowlist of node types and entity attributes, then evaluates the
	pre-vetted tree. Any rejection or error fails closed (returns False —
	an obligation that can't be safely evaluated is never applied)."""
	try:
		tree = ast.parse(expr, mode="eval")
	except SyntaxError:
		frappe.log_error(f"Custom Expression syntax error: {expr!r}", "ApplicabilityEngine")
		return False

	for node in ast.walk(tree):
		if not isinstance(node, _ALLOWED_EXPR_NODES):
			frappe.log_error(f"Custom Expression rejected (disallowed syntax {type(node).__name__}): {expr!r}", "ApplicabilityEngine")
			return False
		if isinstance(node, ast.Name) and node.id != "entity":
			frappe.log_error(f"Custom Expression rejected (disallowed name {node.id!r}): {expr!r}", "ApplicabilityEngine")
			return False
		if isinstance(node, ast.Attribute) and node.attr not in _ALLOWED_ENTITY_ATTRS:
			frappe.log_error(f"Custom Expression rejected (disallowed attribute {node.attr!r}): {expr!r}", "ApplicabilityEngine")
			return False

	try:
		code = compile(tree, "<custom_expression>", "eval")
		return bool(eval(code, {"__builtins__": {}}, {"entity": _EntityAttrProxy(entity)}))  # noqa: S307 — pre-vetted AST only
	except Exception as e:
		frappe.log_error(f"Custom Expression evaluation error ({e}): {expr!r}", "ApplicabilityEngine")
		return False


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

        # Industry filter (Obligation Industry rows and Business Entity both use
        # `industry_code` — there is no plain `industry` field on either).
        if obligation.applicable_industries:
            allowed_industries = [r.industry_code for r in obligation.applicable_industries]
            if allowed_industries and getattr(self.entity, "industry_code", None) not in allowed_industries:
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
            "Industry Match":       "industry_code",
            "Employee Threshold":   "employee_count",
            "Turnover Threshold":   "annual_turnover",  # not a Business Entity field yet — see docstring
            "Listed Status":        "is_listed",
            "Hazardous Status":     "is_hazardous",
            "MSME Status":          "is_msme",
            "Entity Type":          "entity_type",
        }

        if rule_type == "Custom Expression":
            return _safe_eval_custom_expression(str(value), entity)

        attr = attr_map.get(rule_type)
        if not attr:
            return True  # Unknown rule type — pass through
        if not hasattr(entity, attr):
            # Fail closed rather than silently matching on a None vs None
            # comparison — a rule referencing a field the entity doesn't
            # carry (e.g. Turnover Threshold, until Business Entity grows an
            # annual_turnover field) should never be treated as satisfied.
            frappe.log_error(
                f"Applicability rule '{rule_type}' references entity attribute '{attr}', "
                f"which Business Entity does not have. Rule treated as not satisfied.",
                "ApplicabilityEngine",
            )
            return False

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