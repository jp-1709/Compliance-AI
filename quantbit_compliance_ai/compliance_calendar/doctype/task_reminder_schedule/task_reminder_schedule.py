"""
complyai/compliance/compliance_calendar/doctype/task_reminder_schedule/task_reminder_schedule.py

Controller for Task Reminder Schedule — per-organisation reminder and escalation configuration.

Design principles
-----------------
- One record per organisation (autoname = field:organisation).
- Reminder day strings are validated to be comma-separated non-negative integers.
- Quiet hours must be valid times and start != end.
- Escalation thresholds must be positive integers and L2 > L1.
"""

import frappe
from frappe.model.document import Document


class TaskReminderSchedule(Document):

    def validate(self):
        self.validate_reminder_day_strings()
        self.validate_escalation_thresholds()
        self.validate_quiet_hours()

    def validate_reminder_day_strings(self):
        """
        All reminder fields must be comma-separated non-negative integers.
        E.g. '30,14,7,3,1,0' is valid. '30, 14, seven' is not.
        """
        fields = {
            "reminder_critical": self.reminder_critical,
            "reminder_high": self.reminder_high,
            "reminder_medium": self.reminder_medium,
            "reminder_low": self.reminder_low,
        }
        for field_name, value in fields.items():
            if not value:
                continue
            parts = [p.strip() for p in value.split(",")]
            for part in parts:
                if not part.isdigit():
                    frappe.throw(
                        f"Field '{field_name}' must be a comma-separated list of non-negative integers. "
                        f"Got invalid value: '{part}'.",
                        frappe.ValidationError,
                    )
            # Also validate they are in descending order (best practice, soft warning only)
            int_parts = [int(p) for p in parts]
            if int_parts != sorted(int_parts, reverse=True):
                frappe.msgprint(
                    f"Field '{field_name}': reminder days should be in descending order "
                    f"(e.g. 14,7,3,1,0) for predictable behaviour.",
                    indicator="orange",
                    alert=True,
                )

    def validate_escalation_thresholds(self):
        """L2 (CXO) threshold must be strictly greater than L1 (Manager) threshold."""
        l1 = self.escalate_after_days_overdue or 0
        l2 = self.escalate_to_cxo_after_days or 0
        if l1 < 0 or l2 < 0:
            frappe.throw(
                "Escalation thresholds must be non-negative integers.",
                frappe.ValidationError,
            )
        if l2 <= l1:
            frappe.throw(
                f"'Escalate to CXO after' ({l2} days) must be greater than "
                f"'Escalate to Manager after' ({l1} days).",
                frappe.ValidationError,
            )

    def validate_quiet_hours(self):
        """Quiet hours start and end must not be equal."""
        if self.quiet_hours_start and self.quiet_hours_end:
            if str(self.quiet_hours_start) == str(self.quiet_hours_end):
                frappe.throw(
                    "Quiet hours start and end times cannot be the same.",
                    frappe.ValidationError,
                )

    @staticmethod
    def get_for_organisation(organisation: str) -> "TaskReminderSchedule | None":
        """
        Fetch the reminder schedule for an organisation, or None if not configured.
        Callers should fall back to system defaults when None is returned.
        """
        if frappe.db.exists("Task Reminder Schedule", organisation):
            return frappe.get_doc("Task Reminder Schedule", organisation)
        return None

    def get_reminder_days(self, risk_level: str) -> list[int]:
        """
        Parse the reminder day string for the given risk level.
        Returns a sorted descending list of integers.
        """
        mapping = {
            "Critical": self.reminder_critical or "30,14,7,3,1,0",
            "High": self.reminder_high or "14,7,3,1,0",
            "Medium": self.reminder_medium or "7,3,0",
            "Low": self.reminder_low or "3,0",
        }
        raw = mapping.get(risk_level, "7,3,0")
        return sorted([int(d.strip()) for d in raw.split(",") if d.strip().isdigit()], reverse=True)