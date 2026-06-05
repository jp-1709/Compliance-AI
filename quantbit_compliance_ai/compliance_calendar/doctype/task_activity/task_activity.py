"""
quantbit_compliance_ai/compliance_calendar/doctype/task_activity/task_activity.py

Controller for Task Activity — the immutable audit log for compliance tasks.

Design principles
-----------------
- Append-only: no user (not even System Manager) may modify or delete an existing activity.
- before_insert : captures IP address and user agent from the request context.
- validate      : raises PermissionError on any attempt to modify an existing record.
"""

import frappe
from frappe.model.document import Document


class TaskActivity(Document):

    def before_insert(self):
        """Capture request metadata for forensic purposes."""
        try:
            self.ip_address = frappe.local.request_ip or ""
            self.user_agent = (
                frappe.local.request.headers.get("User-Agent", "") if frappe.local.request else ""
            )
        except Exception:
            # Non-HTTP context (scheduler jobs, tests)
            self.ip_address = "system"
            self.user_agent = "scheduler"

        # Auto-populate actor if not set
        if not self.actor:
            self.actor = frappe.session.user

        # Capture actor's primary role
        if not self.actor_role:
            roles = frappe.get_roles(self.actor)
            # Precedence: most privileged role first
            role_precedence = [
                "System Manager",
                "Compliance Officer",
                "Legal Counsel",
                "Department Manager",
                "Internal Auditor",
                "Group CXO",
                "QMS Administrator",
            ]
            for role in role_precedence:
                if role in roles:
                    self.actor_role = role
                    break
            else:
                self.actor_role = roles[0] if roles else "Unknown"

    def validate(self):
        """
        Immutability enforcement.

        Task Activity records are APPEND-ONLY. Any attempt to save an existing
        record (non-new, i.e. it already has a name that exists in the DB) raises
        a PermissionError regardless of the user's role.

        Note: frappe.ValidationError would be swallowed by some callers; we use
        frappe.PermissionError to signal that this is a policy violation, not a
        data error.
        """
        if not self.is_new():
            frappe.throw(
                "Task Activity records are immutable and cannot be modified. "
                "This is an append-only audit log.",
                frappe.PermissionError,
            )

    def before_save(self):
        """Belt-and-suspenders guard — also checked in validate(), but guard here too."""
        if not self.is_new():
            frappe.throw(
                "Task Activity records are immutable and cannot be modified.",
                frappe.PermissionError,
            )