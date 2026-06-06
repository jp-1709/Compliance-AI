"""
applicability_test_case.py
Controller for Applicability Test Case DocType.

Ensures that test cases authored by non-Legal-Counsel users are blocked,
and provides a convenience method to run a single test inline.
"""

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now


class ApplicabilityTestCase(Document):

    def validate(self):
        self._require_legal_counsel_or_sysmanager()
        self._set_authored_by()

    # ── Hooks ──────────────────────────────────────────────────────────────

    def _require_legal_counsel_or_sysmanager(self):
        roles = frappe.get_roles(frappe.session.user)
        if "Legal Counsel" not in roles and "System Manager" not in roles:
            frappe.throw(
                _("Only Legal Counsel or System Manager can author Applicability Test Cases."),
                frappe.PermissionError,
            )

    def _set_authored_by(self):
        if not self.authored_by:
            self.authored_by = frappe.session.user

    # ── Run helper (used by api.run_applicability_test) ────────────────────

    def run(self) -> dict:
        """
        Execute this test case inline and return result dict.
        Also persists last_run_* fields.
        """
        from complyai.compliance.obligation_register.controllers.applicability_engine import (
            is_obligation_applicable,
        )

        ob = frappe.get_doc("Compliance Obligation", self.obligation).as_dict()
        entity = {
            "state": self.state,
            "industry_code": self.industry_code,
            "employee_count": self.employee_count or 0,
            "contract_worker_count": 0,
            "women_employee_count": 0,
            "turnover_inr_cr": self.turnover_inr_cr or 0,
            "is_listed": bool(self.is_listed),
            "is_hazardous": bool(self.is_hazardous),
            "is_msme": bool(self.is_msme),
            "entity_type": self.entity_type,
            "business_type": self.business_type,
        }

        verdict, trace = is_obligation_applicable(ob, entity)
        expected = bool(self.expected_applicable)
        ok = verdict == expected

        # Persist
        self.last_run_at = now()
        self.last_run_passed = 1 if ok else 0
        self.last_run_actual = 1 if verdict else 0
        self.last_run_trace = json.dumps(trace)
        self.flags.ignore_permissions = True
        self.save()

        return {
            "test_case": self.name,
            "obligation": self.obligation,
            "expected": expected,
            "actual": verdict,
            "passed": ok,
            "trace": trace,
        }