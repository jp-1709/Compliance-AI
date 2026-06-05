"""
quantbit_compliance_ai/compliance_calendar/doctype/compliance_obligation/compliance_obligation.py

Controller for Compliance Obligation — the global library of statutory requirements.

Design principles
-----------------
- Global doctype: no organisation field. All tenants share the same obligation library.
- Only PUBLISHED obligations (is_published=1) are picked up by the Applicability Engine.
- Obligation code is immutable once any task has been generated from it.
- Validation on lawyer-validation consistency.
"""

import frappe
from frappe.model.document import Document


class ComplianceObligation(Document):

    def validate(self):
        self.validate_obligation_code_immutability()
        self.validate_employee_thresholds()
        self.validate_validation_fields()
        self.validate_applicability_rules()

    def validate_obligation_code_immutability(self):
        """
        Obligation code cannot change once a Compliance Calendar Task
        has been created from this obligation.
        """
        if not self.is_new() and self.has_value_changed("obligation_code"):
            task_count = frappe.db.count(
                "Compliance Calendar Task", {"obligation": self.name}
            )
            if task_count > 0:
                frappe.throw(
                    f"Obligation code cannot be changed — {task_count} task(s) have been "
                    "generated from this obligation. Change the code on a new obligation and "
                    "use the 'Supersedes' field to retire this one.",
                    frappe.ValidationError,
                )

    def validate_employee_thresholds(self):
        """max_employees must be >= min_employees when both are set."""
        if self.min_employees and self.max_employees:
            if self.max_employees < self.min_employees:
                frappe.throw(
                    "Maximum employees threshold cannot be less than the minimum threshold.",
                    frappe.ValidationError,
                )

    def validate_validation_fields(self):
        """If validated_by_lawyer is set, validated_on must also be set and vice versa."""
        if self.validated_by_lawyer and not self.validated_on:
            frappe.throw(
                "Please set the 'Validated On' date when a lawyer validation is recorded.",
                frappe.ValidationError,
            )
        if self.validated_on and not self.validated_by_lawyer:
            frappe.throw(
                "Please set the 'Validated by Lawyer' field when a validation date is recorded.",
                frappe.ValidationError,
            )

    def validate_applicability_rules(self):
        """Each applicability rule row must have rule_type and operator."""
        for i, rule in enumerate(self.applicability_rules or [], start=1):
            if not rule.rule_type:
                frappe.throw(
                    f"Applicability Rule row {i}: Rule Type is required.",
                    frappe.ValidationError,
                )
            if not rule.operator:
                frappe.throw(
                    f"Applicability Rule row {i}: Operator is required.",
                    frappe.ValidationError,
                )

    def on_update(self):
        """
        When an obligation is updated (e.g., regulation changed), flag all open
        tasks linked to this obligation so Compliance Officers can review them.
        This is a lightweight banner flag; full re-generation is done via API.
        """
        if not self.is_new() and self.has_value_changed("regulation"):
            self._flag_linked_open_tasks()

    def _flag_linked_open_tasks(self):
        """
        Mark open tasks as needing review when their source obligation changes.
        Stores a Task Activity entry per affected task.
        """
        open_statuses = ("Open", "In Progress", "Under Review", "Overdue", "Deferred")
        affected_tasks = frappe.get_all(
            "Compliance Calendar Task",
            filters={
                "obligation": self.name,
                "status": ("in", open_statuses),
                "docstatus": 0,
            },
            pluck="name",
        )
        for task_name in affected_tasks:
            frappe.get_doc(
                {
                    "doctype": "Task Activity",
                    "task": task_name,
                    "activity_type": "Status Changed",
                    "actor": frappe.session.user,
                    "remarks": f"Source obligation updated — regulation changed. Review required.",
                    "from_value": "Active",
                    "to_value": "Regulation Updated",
                }
            ).insert(ignore_permissions=True)