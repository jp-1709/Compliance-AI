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
- on_change()                       : redact permlevel-1 field values from the Version
                                       (audit trail) record Frappe just wrote for this save.

Frappe's `track_changes` / Version log does NOT respect `permlevel` — it snapshots
full before/after field values regardless, so without this the audit trail itself
would leak complainant/respondent identity. `on_change()` runs after Frappe's own
`save_version()` call (see frappe/model/document.py: on_update -> save_version ->
on_change), so the Version record already exists by the time we scrub it.
"""

import json

import frappe
from frappe.model.document import Document
from frappe.utils import getdate, today
from datetime import date, timedelta


# Roles that may access permlevel-1 (confidential) fields
_CONFIDENTIAL_ROLES = frozenset(["POSH IC Member", "System Manager"])

# permlevel-1 fieldnames on POSH Complaint (kept in sync with posh_complaint.json).
# Table fields (case_file_evidence) are redacted wholesale in added/removed/row_changed.
_CONFIDENTIAL_FIELDS = frozenset([
    "complainant_id", "complainant_user", "complainant_relationship",
    "respondent_id", "respondent_user", "respondent_designation",
    "is_respondent_senior_to_complainant", "interim_relief_details",
    "action_recommended", "action_taken_by_employer",
])
_CONFIDENTIAL_TABLE_FIELDS = frozenset(["case_file_evidence"])


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

    def on_change(self):
        self._redact_confidential_version_diff()

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
    # AUDIT TRAIL REDACTION
    # ──────────────────────────────────────────────────────────────────

    def _redact_confidential_version_diff(self):
        """
        Scrub permlevel-1 field values out of the Version record Frappe just
        created for this save. Frappe's diff (frappe.core.doctype.version)
        stores field-level before/after values keyed by fieldname under
        'changed', and whole child-row dicts under 'added'/'removed'/
        'row_changed' for table fields — none of it is permlevel-aware.
        """
        version_name = frappe.db.get_value(
            "Version",
            {"ref_doctype": "POSH Complaint", "docname": self.name},
            "name",
            order_by="creation desc",
        )
        if not version_name:
            return

        try:
            version = frappe.get_doc("Version", version_name)
            data = json.loads(version.data or "{}")
            redacted = False

            changed = data.get("changed") or []
            for row in changed:
                if row and row[0] in _CONFIDENTIAL_FIELDS:
                    row[1] = "[REDACTED]"
                    row[2] = "[REDACTED]"
                    redacted = True

            for key in ("added", "removed"):
                rows = data.get(key) or []
                for row in rows:
                    if row and row[0] in _CONFIDENTIAL_TABLE_FIELDS:
                        row[1] = {"redacted": True}
                        redacted = True

            row_changed = data.get("row_changed") or []
            for row in row_changed:
                if row and row[0] in _CONFIDENTIAL_TABLE_FIELDS:
                    row[3] = [["redacted", "[REDACTED]", "[REDACTED]"]]
                    redacted = True

            if redacted:
                frappe.db.set_value(
                    "Version", version_name, "data", json.dumps(data), update_modified=False
                )
        except Exception as e:
            frappe.log_error(
                f"Failed to redact Version diff for POSH Complaint {self.name}: {e}",
                "POSHComplaint",
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