"""
Tax Registration — Controller
complyai/compliance/tax_gst_compliance/doctype/tax_registration/tax_registration.py

Handles:
  - GSTIN / TAN / PAN format validation
  - State-code match for GSTIN
  - Eligibility flag computation (e-invoice, QRMP)
  - Consecutive non-filing counter
  - Auto-suspension at 6 consecutive non-filings
"""

import re
from datetime import date, timedelta

import frappe
from frappe import _
from frappe.model.document import Document

# ─── Regex patterns ──────────────────────────────────────────────────────────
GSTIN_REGEX  = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$")
TAN_REGEX    = re.compile(r"^[A-Z]{4}[0-9]{5}[A-Z]$")
PAN_REGEX    = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
IEC_REGEX    = re.compile(r"^[0-9]{10}$")

# Turnover thresholds (INR)
EINVOICE_THRESHOLD_INR = 5_00_00_000   # ₹5 crore
QRMP_THRESHOLD_INR     = 5_00_00_000   # ₹5 crore
GSTR9C_THRESHOLD_INR   = 5_00_00_000   # ₹5 crore

# Non-filing suspension threshold (GST Rule 21A)
SUSPENSION_THRESHOLD = 6


class TaxRegistration(Document):

    # ──────────────────────────────────────────────
    # Frappe lifecycle hooks
    # ──────────────────────────────────────────────

    def validate(self):
        self.validate_format()
        self.check_gstin_state_match()
        self.compute_eligibility_flags()

    def on_update(self):
        self.recompute_consecutive_non_filings()
        self.maybe_suspend_gstin()

    # ──────────────────────────────────────────────
    # Format Validation
    # ──────────────────────────────────────────────

    def validate_format(self):
        """Validate registration number format based on registration type."""
        reg = (self.registration_number or "").strip().upper()
        rt  = self.registration_type or ""

        if rt.startswith("GSTIN"):
            if not GSTIN_REGEX.match(reg):
                frappe.throw(
                    _("Invalid GSTIN format: <b>{0}</b>. Expected 15-character format "
                      "(e.g. 27AABCU9603R1ZX).").format(reg)
                )

        elif rt == "TAN":
            if not TAN_REGEX.match(reg):
                frappe.throw(
                    _("Invalid TAN format: <b>{0}</b>. Expected 10-character format "
                      "(e.g. PUNE07177F).").format(reg)
                )

        elif rt == "PAN":
            if not PAN_REGEX.match(reg):
                frappe.throw(
                    _("Invalid PAN format: <b>{0}</b>. Expected 10-character format "
                      "(e.g. AABCU9603R).").format(reg)
                )

        elif rt == "IEC":
            if not IEC_REGEX.match(reg):
                frappe.throw(
                    _("Invalid IEC format: <b>{0}</b>. Expected 10-digit number.").format(reg)
                )

    # ──────────────────────────────────────────────
    # GSTIN State-Code Check
    # ──────────────────────────────────────────────

    def check_gstin_state_match(self):
        """First 2 digits of GSTIN must match the GST state code of the selected state."""
        if not self.registration_type or not self.registration_type.startswith("GSTIN"):
            return
        if not self.state or not self.registration_number:
            return

        state_code = frappe.db.get_value("State", self.state, "gst_state_code")
        if not state_code:
            return  # State master not yet configured; skip silently

        gstin_prefix = (self.registration_number or "")[:2]
        if gstin_prefix != str(state_code):
            frappe.throw(
                _("GSTIN {0} starts with state code <b>{1}</b>, but the selected state "
                  "<b>{2}</b> has GST state code <b>{3}</b>. Please verify.").format(
                    self.registration_number, gstin_prefix, self.state, state_code
                )
            )

    # ──────────────────────────────────────────────
    # Eligibility Flag Computation
    # ──────────────────────────────────────────────

    def compute_eligibility_flags(self):
        """
        Recomputes:
          - is_eligible_einvoice : turnover >= ₹5 cr
          - is_eligible_qrmp     : turnover <= ₹5 cr
          - is_eligible_ewaybill : always 1 for GSTIN
        """
        if not self.registration_type or not self.registration_type.startswith("GSTIN"):
            return

        turnover = self.annual_turnover_inr or 0

        self.is_eligible_einvoice = 1 if turnover >= EINVOICE_THRESHOLD_INR else 0
        self.is_eligible_qrmp     = 1 if turnover <= QRMP_THRESHOLD_INR     else 0
        self.is_eligible_ewaybill = 1  # EWB always required for GSTIN entities

    # ──────────────────────────────────────────────
    # Consecutive Non-Filing Counter
    # ──────────────────────────────────────────────

    def recompute_consecutive_non_filings(self):
        """
        Counts how many consecutive months (walking backwards from today)
        have no Filed/Nil-Filed GSTR-3B for this GSTIN.
        Stops at 12 months max.
        """
        if not self.registration_type or not self.registration_type.startswith("GSTIN"):
            self.consecutive_non_filings = 0
            self.db_set("consecutive_non_filings", 0, update_modified=False)
            return

        FILED_STATUSES = ("Filed", "Filed with Late Fee", "Nil Filed")
        count      = 0
        cursor_dt  = date.today().replace(day=1)  # first of current month

        for _ in range(13):  # check up to 12 prior months
            period_month  = cursor_dt.strftime("%B")   # e.g. "January"
            period_fy     = _derive_fy(cursor_dt)

            filed = frappe.db.exists(
                "GSTR Filing",
                {
                    "tax_registration": self.name,
                    "return_type":      "GSTR-3B",
                    "period_month":     period_month,
                    "period_fy":        period_fy,
                    "filing_status":    ["in", list(FILED_STATUSES)],
                }
            )

            if not filed:
                count += 1
            else:
                break

            if count > 12:
                break

            # step back one month
            cursor_dt = (cursor_dt - timedelta(days=1)).replace(day=1)

        self.consecutive_non_filings = count
        self.db_set("consecutive_non_filings", count, update_modified=False)

    # ──────────────────────────────────────────────
    # GSTIN Auto-Suspension
    # ──────────────────────────────────────────────

    def maybe_suspend_gstin(self):
        """
        GST Rule 21A — auto-suspend GSTIN after >= 6 consecutive non-filings.
        Triggers a critical notification to the responsible person and CFO.
        """
        if self.consecutive_non_filings >= SUSPENSION_THRESHOLD and not self.gstin_suspended:
            self.gstin_suspended = 1
            self.db_set("gstin_suspended", 1, update_modified=False)
            _notify_critical_users(
                self.organisation,
                self.business_entity,
                _("GSTIN {0} auto-suspended: {1} consecutive non-filings. "
                  "File pending GSTR-3B returns immediately to restore.").format(
                    self.registration_number, self.consecutive_non_filings
                )
            )


# ─── Module-level helpers ────────────────────────────────────────────────────

def _derive_fy(dt: date) -> str:
    """Return FY string (e.g. '2024-25') for the given date."""
    year = dt.year
    if dt.month >= 4:
        return f"{year}-{str(year + 1)[2:]}"
    else:
        return f"{year - 1}-{str(year)[2:]}"


def _notify_critical_users(organisation: str, business_entity: str, message: str):
    """
    Send a Frappe notification to Compliance Officers and the responsible person.
    In a real deployment this could also send email/WhatsApp.
    """
    try:
        users = frappe.get_all(
            "Has Role",
            filters={"role": ["in", ["Compliance Officer", "Tax Head", "CFO"]]},
            pluck="parent",
        )
        users = list(set(users))
        for user in users:
            frappe.publish_realtime(
                event="eval_js",
                message=f"frappe.show_alert({{message: {repr(message)}, indicator: 'red'}}, 60)",
                user=user,
            )
        frappe.log_error(
            message=message,
            title=f"GSTIN Suspension — {business_entity}",
        )
    except Exception:
        pass  # non-critical; don't break the save