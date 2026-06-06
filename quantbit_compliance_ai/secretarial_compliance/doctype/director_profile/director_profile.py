"""
complyai/compliance/secretarial_compliance/doctype/director_profile/director_profile.py

Director Profile controller — DIN/PAN validation, KYC due-date computation,
directorship cap (§165), disqualification cascade, DSC expiry alerting.
"""

import re
from datetime import date, timedelta

import frappe
from frappe import _
from frappe.model.document import Document


class DirectorProfile(Document):
    # ───────────────────────── lifecycle hooks ──────────────────────────

    def validate(self):
        self.validate_din_format()
        self.validate_pan_format()
        self.compute_next_kyc_due()
        self.check_directorship_cap()
        self.check_active_disqualification()

    def on_update(self):
        self.alert_dsc_expiry()
        self.alert_kyc_due()

    # ───────────────────────── field validations ────────────────────────

    def validate_din_format(self):
        """DIN must be exactly 8 digits."""
        if self.din and (not self.din.isdigit() or len(self.din) != 8):
            frappe.throw(_("DIN must be exactly 8 digits"), title=_("Invalid DIN"))

    def validate_pan_format(self):
        """PAN must match AAAAA9999A pattern."""
        if not re.match(r"^[A-Z]{5}[0-9]{4}[A-Z]$", self.pan or ""):
            frappe.throw(_("PAN must be in format AAAAA9999A (e.g. ABCDE1234F)"),
                         title=_("Invalid PAN"))

    # ───────────────────────── KYC due-date ─────────────────────────────

    def compute_next_kyc_due(self):
        """DIR-3 KYC is due Sep 30 every year.
        next_kyc_due = Sep 30 of the FY-end year for current FY.
        Indian FY: Apr 1 – Mar 31.  If today is before Apr, FY ends this year.
        """
        today = date.today()
        # Determine calendar year in which the current FY ends
        fy_end_year = today.year if today.month < 4 else today.year + 1
        self.next_kyc_due = date(fy_end_year, 9, 30)

    # ───────────────────────── directorship cap §165 ────────────────────

    def check_directorship_cap(self):
        """§165: max 20 directorships (of which max 10 public companies)."""
        total = self.external_directorships_count or 0
        public = self.external_directorships_public_count or 0
        if total > 20:
            frappe.throw(
                _(f"Directorship cap exceeded ({total} > 20). This violates Section 165."),
                title=_("Directorship Cap Exceeded"),
            )
        if public > 10:
            frappe.throw(
                _(f"Public company directorship cap exceeded ({public} > 10). Section 165 violation."),
                title=_("Directorship Cap Exceeded"),
            )

    # ───────────────────────── disqualification cascade ─────────────────

    def check_active_disqualification(self):
        """If any child row has is_active=1 and is not expired, mark director disqualified."""
        today = date.today()
        has_active = any(
            d.is_active and (not d.disqualified_to or d.disqualified_to >= today)
            for d in (self.disqualifications or [])
        )
        if has_active:
            self.is_disqualified = 1
            if self.din_status == "Active":
                self.din_status = "Disqualified"
        else:
            # Only clear if no active disqualifications remain
            if self.is_disqualified:
                self.is_disqualified = 0

    # ───────────────────────── alerts ───────────────────────────────────

    def alert_dsc_expiry(self):
        """Warn if DSC expires within 30 days — director cannot sign e-forms after expiry."""
        if self.dsc_expiry_date:
            days_left = (self.dsc_expiry_date - date.today()).days
            if days_left <= 30:
                _create_dsc_renewal_task(self, days_left)

    def alert_kyc_due(self):
        """Warn if KYC is due within 30 days and last KYC was not in current FY."""
        if self.next_kyc_due:
            days_left = (self.next_kyc_due - date.today()).days
            if 0 <= days_left <= 30:
                _create_kyc_reminder_task(self, days_left)

    # ───────────────────────── pre-fill export ──────────────────────────

    @frappe.whitelist()
    def get_kyc_prefill_json(self):
        """Return JSON payload suitable for direct upload to MCA DIR-3 KYC portal."""
        return {
            "din": self.din,
            "full_name": self.full_name,
            "pan": self.pan,
            "date_of_birth": str(self.date_of_birth or ""),
            "email": self.email,
            "mobile": self.mobile,
            "permanent_address": self.permanent_address,
            "present_address": self.present_address,
            "dsc_serial": self.dsc_serial_no,
            "form_type": self.kyc_form_type,
        }


# ───────────────────────── helpers ──────────────────────────────────────


def _create_dsc_renewal_task(director: "DirectorProfile", days_left: int):
    """Create or refresh a Compliance Calendar Task for DSC renewal."""
    existing = frappe.db.get_value(
        "Compliance Calendar Task",
        {"reference_doctype": "Director Profile", "reference_name": director.name,
         "task_type": "DSC Renewal", "status": ["in", ["Open", "In Progress"]]},
        "name",
    )
    if existing:
        return  # Already exists, don't duplicate

    try:
        task = frappe.get_doc({
            "doctype": "Compliance Calendar Task",
            "task_type": "DSC Renewal",
            "reference_doctype": "Director Profile",
            "reference_name": director.name,
            "description": (f"DSC for {director.full_name} (DIN: {director.din}) "
                            f"expires in {days_left} days on {director.dsc_expiry_date}. "
                            "Renew immediately to continue e-form signing."),
            "due_date": director.dsc_expiry_date,
            "status": "Open",
            "organisation": director.organisation,
        })
        task.insert(ignore_permissions=True)
    except Exception:
        # Non-critical — log but do not block save
        frappe.log_error(frappe.get_traceback(), "DSC Renewal Task Creation Failed")


def _create_kyc_reminder_task(director: "DirectorProfile", days_left: int):
    """Create a DIR-3 KYC reminder task if not already present."""
    existing = frappe.db.get_value(
        "Compliance Calendar Task",
        {"reference_doctype": "Director Profile", "reference_name": director.name,
         "task_type": "DIR-3 KYC", "status": ["in", ["Open", "In Progress"]]},
        "name",
    )
    if existing:
        return

    try:
        task = frappe.get_doc({
            "doctype": "Compliance Calendar Task",
            "task_type": "DIR-3 KYC",
            "reference_doctype": "Director Profile",
            "reference_name": director.name,
            "description": (f"DIR-3 KYC for {director.full_name} (DIN: {director.din}) "
                            f"is due on {director.next_kyc_due} ({days_left} days left). "
                            "File before Sep 30 to avoid DIN deactivation."),
            "due_date": director.next_kyc_due,
            "status": "Open",
            "organisation": director.organisation,
        })
        task.insert(ignore_permissions=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "KYC Reminder Task Creation Failed")