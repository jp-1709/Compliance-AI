"""
PCB Return — Controller
complyai/compliance/ehs_compliance/doctype/pcb_return/pcb_return.py

Handles:
  • filing_due_date auto-computation:
      - Form-V (Annual Environmental Statement): 30-Sep of following year
      - Form-4 (HW Annual Return): 30-Jun of following year
  • delay_days computation (filed_on − filing_due_date)
  • Uniqueness enforcement per (org, entity, return_type, period_fy)
"""

import frappe
from frappe import _
from frappe.model.document import Document
from datetime import date


class PCBReturn(Document):

    # ──────────────────────────────────────────────
    # Lifecycle hooks
    # ──────────────────────────────────────────────

    def validate(self):
        self.validate_period_fy_format()
        self.compute_due_date()
        self.compute_delay()
        self.validate_uniqueness()

    def on_submit(self):
        self.validate_evidence_on_submit()

    # ──────────────────────────────────────────────
    # Period FY format validation
    # ──────────────────────────────────────────────

    def validate_period_fy_format(self):
        """Expect 'YYYY-YY' or 'YY-YY' format, e.g. '2024-25'."""
        if not self.period_fy:
            return
        parts = self.period_fy.split("-")
        if len(parts) != 2:
            frappe.throw(
                _("Financial Year must be in format 'YYYY-YY' (e.g. 2024-25). Got: {0}").format(
                    self.period_fy
                )
            )

    # ──────────────────────────────────────────────
    # Due date computation
    # ──────────────────────────────────────────────

    def compute_due_date(self):
        """
        Form-V (Annual Environmental Statement): due 30-Sep of FY end year + 1
        Form-4 (HW Annual Return): due 30-Jun of FY end year + 1
        Other returns with known quarterly schedule: Q-end + 15 days

        FY format: '2024-25' → end year = 2025, so next year = 2026? No.
        PCB convention: FY 2024-25 → Form-V due 30-Sep-2025, Form-4 due 30-Jun-2025.
        """
        if self.filing_due_date:
            return  # Don't override manually set due dates

        if not self.period_fy or not self.return_type:
            return

        try:
            fy_end_year = _parse_fy_end_year(self.period_fy)
        except (ValueError, IndexError):
            return  # can't parse, skip

        return_type = self.return_type

        if return_type.startswith("Form-V"):
            # Due 30-Sep of the year following FY end
            self.filing_due_date = date(fy_end_year, 9, 30)

        elif return_type.startswith("Form-4"):
            # Due 30-Jun of the year following FY end
            self.filing_due_date = date(fy_end_year, 6, 30)

        elif return_type == "Water Cess Return":
            # Quarterly: month after quarter end
            self.filing_due_date = _compute_quarterly_due(fy_end_year, self.period_quarter)

        elif return_type == "Air Cess Return":
            self.filing_due_date = _compute_quarterly_due(fy_end_year, self.period_quarter)

        elif return_type == "OCEMS Quarterly Calibration":
            self.filing_due_date = _compute_quarterly_due(fy_end_year, self.period_quarter)

    # ──────────────────────────────────────────────
    # Delay computation
    # ──────────────────────────────────────────────

    def compute_delay(self):
        """
        delay_days = filed_on − filing_due_date
        Positive = filed late (days after due date)
        Negative = filed early
        """
        if self.filed_on and self.filing_due_date:
            filed_on = (
                self.filed_on
                if isinstance(self.filed_on, date)
                else frappe.utils.getdate(self.filed_on)
            )
            filing_due = (
                self.filing_due_date
                if isinstance(self.filing_due_date, date)
                else frappe.utils.getdate(self.filing_due_date)
            )
            delta = (filed_on - filing_due).days
            self.delay_days = max(0, delta)  # Only count positive delays

            if self.delay_days > 0 and self.filing_status == "Filed":
                self.filing_status = "Filed with Late Fee"
        else:
            self.delay_days = 0

    # ──────────────────────────────────────────────
    # Uniqueness (one return per entity+type+FY)
    # ──────────────────────────────────────────────

    def validate_uniqueness(self):
        filters = {
            "organisation": self.organisation,
            "business_entity": self.business_entity,
            "return_type": self.return_type,
            "period_fy": self.period_fy,
            "name": ["!=", self.name],
        }
        if self.period_quarter:
            filters["period_quarter"] = self.period_quarter

        existing = frappe.db.exists("PCB Return", filters)
        if existing:
            frappe.throw(
                _(
                    "A PCB Return of type '{0}' for FY '{1}' already exists for this entity: {2}."
                ).format(self.return_type, self.period_fy, existing)
            )

    # ──────────────────────────────────────────────
    # Submit validation
    # ──────────────────────────────────────────────

    def validate_evidence_on_submit(self):
        if not self.filed_return_evidence:
            frappe.throw(
                _("Please attach the Filed Return Evidence before submitting.")
            )
        if not self.filed_on:
            frappe.throw(_("Please set the Filed On date before submitting."))

    # ──────────────────────────────────────────────
    # Status helpers
    # ──────────────────────────────────────────────

    def before_save(self):
        """Auto-set Lapsed status when filing_due_date is past and not yet filed."""
        if not self.filed_on and self.filing_due_date:
            due = (
                self.filing_due_date
                if isinstance(self.filing_due_date, date)
                else frappe.utils.getdate(self.filing_due_date)
            )
            if due < date.today() and self.filing_status in ("Pending", "In Preparation"):
                self.filing_status = "Lapsed"


# ──────────────────────────────────────────────────────
# Module-level helpers
# ──────────────────────────────────────────────────────

def _parse_fy_end_year(period_fy: str) -> int:
    """
    Parse FY string and return the end calendar year.
    '2024-25' → 2025
    '24-25'   → 2025
    """
    parts = period_fy.strip().split("-")
    end_part = parts[1].strip()
    start_part = parts[0].strip()

    if len(end_part) == 2:
        # e.g. '25' → prefix with century from start year
        start_year = int(start_part)
        century = (start_year // 100) * 100
        return century + int(end_part)
    else:
        return int(end_part)


def _compute_quarterly_due(fy_end_year: int, quarter: str) -> date:
    """Return the quarterly filing due date (15th of month after quarter end)."""
    quarter_end_months = {
        "Q1": (6, 30),  # Q1 Apr–Jun → due 15-Jul
        "Q2": (9, 30),  # Q2 Jul–Sep → due 15-Oct
        "Q3": (12, 31), # Q3 Oct–Dec → due 15-Jan
        "Q4": (3, 31),  # Q4 Jan–Mar → due 15-Apr
    }
    if not quarter or quarter not in quarter_end_months:
        return date(fy_end_year, 6, 30)  # default

    end_month, _ = quarter_end_months[quarter]
    due_month = end_month + 1 if end_month < 12 else 1
    due_year = fy_end_year if end_month < 12 else fy_end_year + 1
    return date(due_year, due_month, 15)


# ──────────────────────────────────────────────────────
# Scheduled: alert PCB returns approaching
# ──────────────────────────────────────────────────────

def alert_pcb_return_due():
    """
    Monthly scheduled job.
    Alert for pending returns due within 30 / 60 days.
    """
    today = date.today()
    from datetime import timedelta

    upcoming = frappe.get_all(
        "PCB Return",
        filters={
            "filing_status": ["in", ["Pending", "In Preparation"]],
            "filing_due_date": ["between", [today, today + timedelta(days=60)]],
            "docstatus": ["!=", 2],
        },
        fields=[
            "name", "business_entity", "organisation",
            "return_type", "period_fy", "filing_due_date"
        ],
    )

    for ret in upcoming:
        due = frappe.utils.getdate(ret.filing_due_date)
        days_left = (due - today).days
        frappe.sendmail(
            subject=f"PCB Return Due: {ret.return_type} — {ret.period_fy} in {days_left} days",
            message=(
                f"<b>{ret.return_type}</b> for FY {ret.period_fy} is due on "
                f"{ret.filing_due_date} ({days_left} days remaining).<br>"
                f"Business Entity: {ret.business_entity}<br>"
                f"Record: {ret.name}"
            ),
            now=True,
        )