"""
complyai/compliance/labour_compliance/doctype/posh_complaint/posh_complaint.py

Controller for POSH Complaint.

CONFIDENTIALITY: permlevel=1 fields (parties, evidence, action) are NEVER
logged in plain text, never appear in notification subjects, never in error logs.
All logging must use complaint ID (e.g. POSH-C-2025-000142) — never party names.

Key behaviour:
- validate()                        : compute inquiry target (90 days); check filing window.
- on_update()                       : roll up complaint counters to parent POSH Committee.
- check_ic_permission()             : hard-block if caller is not POSH IC Member.
- Audit logs auto-redact permlevel-1 fields (enforced in hooks.py doc_events).
"""

import frappe
from frappe.model.document import Document
from frappe.utils import getdate, today
from datetime import date, timedelta


# Roles that may access permlevel-1 (confidential) fields
_CONFIDENTIAL_ROLES = frozenset(["POSH IC Member", "System Manager"])


class POSHComplaint(Document):

    # ──────────────────────────────────────────────────────────────────
    # LIFECYCLE HOOKS
    # ──────────────────────────────────────────────────────────────────

    def validate(self):
        self.compute_inquiry_target()
        self.check_filing_window()
        self.validate_ic_belongs_to_entity()

    def on_update(self):
        self.update_committee_complaint_counters()

    def before_insert(self):
        """Reject complaint creation by non-IC roles."""
        self._require_ic_permission("create a POSH Complaint")

    # ──────────────────────────────────────────────────────────────────
    # INQUIRY TIMELINE
    # ──────────────────────────────────────────────────────────────────

    def compute_inquiry_target(self):
        """
        POSH Act §11: The IC must complete its inquiry within 90 days of receipt of complaint.
        Report must be submitted within 10 days of inquiry completion (§13).
        An alert fires at 75 days to give 15 days' warning.
        """
        if self.complaint_received_on:
            received = getdate(self.complaint_received_on)
            self.inquiry_target_completion = received + timedelta(days=90)

    # ──────────────────────────────────────────────────────────────────
    # FILING WINDOW
    # ──────────────────────────────────────────────────────────────────

    def check_filing_window(self):
        """
        Statutory limitation: complaint must be filed within 3 months of incident.
        IC may extend by another 3 months if cause is shown.
        'complaint_filed_within_3m' is informational — does NOT block save,
        as IC needs to accept and then decide on the limitation question.
        """
        # complaint_received_on is the only available date in this DocType.
        # The incident date would be in the case file (permlevel-1).
        # We cannot check the actual 3-month window without the incident date,
        # so we set the field to 1 by default and let the IC override.
        if self.complaint_received_on and not self.extension_granted:
            self.complaint_filed_within_3m = 1  # Presumed timely; IC to verify

    # ──────────────────────────────────────────────────────────────────
    # CROSS-ENTITY VALIDATION
    # ──────────────────────────────────────────────────────────────────

    def validate_ic_belongs_to_entity(self):
        """The POSH Committee must belong to the same Business Entity."""
        if self.posh_committee and self.business_entity:
            ic_entity = frappe.db.get_value(
                "POSH Committee", self.posh_committee, "business_entity"
            )
            if ic_entity != self.business_entity:
                frappe.throw(
                    f"The selected POSH Committee belongs to entity '{ic_entity}', "
                    f"not '{self.business_entity}'. Select the correct IC.",
                    frappe.ValidationError,
                )

    # ──────────────────────────────────────────────────────────────────
    # COMMITTEE COUNTERS ROLLUP
    # ──────────────────────────────────────────────────────────────────

    def update_committee_complaint_counters(self):
        """
        Roll up YTD complaint counts to the parent POSH Committee.
        Used in the POSH Annual Report generation.
        Counts by calendar year (not FY — POSH Annual Report is Jan 31).
        """
        if not self.posh_committee:
            return

        cal_year = getdate(today()).year
        ytd_start = f"{cal_year}-01-01"

        received_ytd = frappe.db.count(
            "POSH Complaint",
            {
                "posh_committee": self.posh_committee,
                "complaint_received_on": (">=", ytd_start),
            },
        )
        disposed_ytd = frappe.db.count(
            "POSH Complaint",
            {
                "posh_committee": self.posh_committee,
                "complaint_status": ("like", "Disposed%"),
                "complaint_received_on": (">=", ytd_start),
            },
        )

        frappe.db.set_value(
            "POSH Committee",
            self.posh_committee,
            {
                "complaints_received_ytd": received_ytd,
                "complaints_disposed_ytd": disposed_ytd,
            },
            update_modified=False,
        )

    # ──────────────────────────────────────────────────────────────────
    # PERMISSION GUARD
    # ──────────────────────────────────────────────────────────────────

    def _require_ic_permission(self, action: str) -> None:
        """
        Hard-block: only POSH IC Members and System Manager may create or
        access permlevel-1 fields. This is a belt-and-suspenders check
        in addition to Frappe's permlevel mechanism.
        """
        user_roles = set(frappe.get_roles(frappe.session.user))
        if not user_roles.intersection(_CONFIDENTIAL_ROLES):
            frappe.throw(
                f"Permission denied: only POSH IC Members may {action}. "
                "Contact the IC Presiding Officer.",
                frappe.PermissionError,
            )

    # ──────────────────────────────────────────────────────────────────
    # SLA STATUS HELPER
    # ──────────────────────────────────────────────────────────────────

    def days_until_sla_breach(self) -> int:
        """
        Returns days remaining before the 90-day inquiry SLA is breached.
        Negative = already breached.
        """
        if not self.inquiry_target_completion:
            return 90
        return (getdate(self.inquiry_target_completion) - date.today()).days

    def is_sla_at_risk(self) -> bool:
        """Returns True if SLA breach is within 15 days (alert at 75 days)."""
        return self.days_until_sla_breach() <= 15