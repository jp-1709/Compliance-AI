"""
GSTR Filing — Controller
complyai/compliance/tax_gst_compliance/doctype/gstr_filing/gstr_filing.py

Handles:
  - Due date computation for all return types
  - Delay calculation
  - Late fee calculation (3B cap ₹5k, NIL ₹500, GSTR-9 uncapped at ₹200/day)
  - Tax Registration last-filed status sync
"""

from datetime import date, timedelta
import calendar

import frappe
from frappe import _
from frappe.model.document import Document


# ─── Month name → month number ───────────────────────────────────────────────
MONTH_MAP = {
    "January": 1,  "February": 2,  "March": 3,    "April": 4,
    "May": 5,      "June": 6,      "July": 7,      "August": 8,
    "September": 9,"October": 10,  "November": 11, "December": 12,
}

# ─── Late fee constants ───────────────────────────────────────────────────────
LATE_FEE_PER_DAY_NORMAL = 50    # CGST ₹25 + SGST ₹25
LATE_FEE_PER_DAY_NIL    = 20
LATE_FEE_CAP_NORMAL     = 5000
LATE_FEE_CAP_NIL        = 500
LATE_FEE_GSTR9_PER_DAY  = 200   # uncapped (but limited to 0.5% turnover in theory)


class GSTRFiling(Document):

    # ──────────────────────────────────────────────
    # Frappe lifecycle hooks
    # ──────────────────────────────────────────────

    def validate(self):
        self.normalise_period_fy()
        self.compute_due_date()
        self.compute_delay()
        self.compute_late_fee()

    def on_submit(self):
        self.update_tax_registration_status()

    # ──────────────────────────────────────────────
    # Normalise FY
    # ──────────────────────────────────────────────

    def normalise_period_fy(self):
        """Ensure period_fy is in 'YYYY-YY' format, e.g. '2024-25'."""
        fy = (self.period_fy or "").strip()
        if len(fy) == 7 and fy[4] == "-":
            self.period_fy = fy
        # else: leave as-is; validation elsewhere will surface bad FY

    # ──────────────────────────────────────────────
    # Due Date Computation
    # ──────────────────────────────────────────────

    def compute_due_date(self):
        """
        Compute filing_due_date based on return_type, period, and filing frequency.

        GSTR-1 (Monthly)  : 11th of next month
        GSTR-1 (QRMP)     : 13th of month after quarter end
        GSTR-3B           : 20th of next month (turnover > ₹5 cr)
                            22nd or 24th (turnover <= ₹5 cr, state-dependent)
        GSTR-9 / GSTR-9C  : 31-Dec of (FY end year + 1)
        GSTR-4            : 30-Apr of next FY
        CMP-08            : 18th of month after quarter
        TDS/TCS forms     : handled in TDS/TCS Return controllers
        """
        if self.filing_due_date:
            return  # already set; don't overwrite

        rt = self.return_type or ""

        if rt == "GSTR-1":
            self.filing_due_date = self._gstr1_due_date()

        elif rt == "GSTR-3B":
            self.filing_due_date = self._gstr3b_due_date()

        elif rt in ("GSTR-9 (Annual)", "GSTR-9C (Audit/Reconciliation)"):
            self.filing_due_date = self._gstr9_due_date()

        elif rt == "GSTR-4 (Composition)":
            self.filing_due_date = self._gstr4_due_date()

        elif rt == "CMP-08 (Composition Quarterly)":
            self.filing_due_date = self._cmp08_due_date()

        # Other types: manual entry expected

    def _gstr1_due_date(self) -> date:
        tr = self._get_tax_registration()
        freq = (tr.filing_frequency if tr else None) or "Monthly"

        if freq == "Monthly":
            period_end = self._period_end_date()
            # 11th of the following month
            y, m = _next_month(period_end.year, period_end.month)
            return date(y, m, 11)
        else:
            # QRMP: 13th of month after quarter end
            q_end = self._quarter_end_date()
            y, m = _next_month(q_end.year, q_end.month)
            return date(y, m, 13)

    def _gstr3b_due_date(self) -> date:
        """
        20th for large taxpayers (turnover > ₹5 cr).
        22nd for Group A states (MH, GJ, KA, ...) for small taxpayers.
        24th for Group B states for small taxpayers.
        Default to 20th when state information is unavailable.
        """
        tr = self._get_tax_registration()
        turnover = tr.annual_turnover_inr if tr else 0
        period_end = self._period_end_date()
        y, m = _next_month(period_end.year, period_end.month)

        if (turnover or 0) > 5_00_00_000:
            return date(y, m, 20)

        # Determine state group
        state_name = tr.state if tr else None
        group_a_states = {
            "Maharashtra", "Gujarat", "Karnataka", "Tamil Nadu",
            "Telangana", "Andhra Pradesh", "Kerala", "West Bengal",
            "Delhi", "Odisha",
        }
        if state_name and state_name in group_a_states:
            return date(y, m, 22)
        return date(y, m, 24)

    def _gstr9_due_date(self) -> date:
        fy_end_year = _fy_end_year(self.period_fy)
        # 31-Dec of next calendar year after FY end
        return date(fy_end_year + 1, 12, 31)

    def _gstr4_due_date(self) -> date:
        """GSTR-4 (Composition): 30-Apr of next FY."""
        fy_end_year = _fy_end_year(self.period_fy)
        return date(fy_end_year + 1, 4, 30)

    def _cmp08_due_date(self) -> date:
        """CMP-08: 18th of month after quarter end."""
        q_end = self._quarter_end_date()
        y, m = _next_month(q_end.year, q_end.month)
        return date(y, m, 18)

    # ──────────────────────────────────────────────
    # Delay Calculation
    # ──────────────────────────────────────────────

    def compute_delay(self):
        """Compute delay_days = filed_on - filing_due_date (0 if not yet filed or on time)."""
        if not self.filed_on or not self.filing_due_date:
            self.delay_days = 0
            return
        delta = (self.filed_on - self.filing_due_date).days
        self.delay_days = max(0, delta)

    # ──────────────────────────────────────────────
    # Late Fee Calculation
    # ──────────────────────────────────────────────

    def compute_late_fee(self):
        """
        GSTR-1 / GSTR-3B:
          - Normal : ₹50/day capped at ₹5,000
          - NIL    : ₹20/day capped at ₹500
        GSTR-9 / GSTR-9C:
          - ₹200/day, uncapped (effectively capped at 0.5% of turnover by law, but
            we store raw ₹200/day here; turnover cap must be applied at payment time)
        """
        if not self.delay_days or self.delay_days <= 0:
            self.late_fee_inr = 0
            return

        rt = self.return_type or ""

        if rt in ("GSTR-1", "GSTR-1A (Amendment)", "GSTR-3B"):
            if self.is_nil_return:
                self.late_fee_inr = min(self.delay_days * LATE_FEE_PER_DAY_NIL, LATE_FEE_CAP_NIL)
            else:
                self.late_fee_inr = min(self.delay_days * LATE_FEE_PER_DAY_NORMAL, LATE_FEE_CAP_NORMAL)

        elif rt in ("GSTR-9 (Annual)", "GSTR-9C (Audit/Reconciliation)"):
            # ₹200/day; turnover cap applied externally
            self.late_fee_inr = self.delay_days * LATE_FEE_GSTR9_PER_DAY

        else:
            self.late_fee_inr = 0

    # ──────────────────────────────────────────────
    # Tax Registration Status Update
    # ──────────────────────────────────────────────

    def update_tax_registration_status(self):
        """On submit, update last-filed period/date on the Tax Registration record."""
        if not self.tax_registration:
            return

        rt = self.return_type or ""
        updates = {}

        if rt == "GSTR-1":
            updates = {
                "last_gstr1_period":   f"{self.period_month} {self.period_fy}",
                "last_gstr1_filed_on": self.filed_on,
            }
        elif rt == "GSTR-3B":
            updates = {
                "last_gstr3b_period":   f"{self.period_month} {self.period_fy}",
                "last_gstr3b_filed_on": self.filed_on,
            }

        if updates:
            frappe.db.set_value("Tax Registration", self.tax_registration, updates)
            # Trigger non-filing counter recomputation
            tr_doc = frappe.get_doc("Tax Registration", self.tax_registration)
            tr_doc.on_update()

    # ──────────────────────────────────────────────
    # Private helpers
    # ──────────────────────────────────────────────

    def _get_tax_registration(self):
        if not self.tax_registration:
            return None
        try:
            return frappe.get_cached_doc("Tax Registration", self.tax_registration)
        except Exception:
            return None

    def _period_end_date(self) -> date:
        """Return the last day of the period month."""
        if not self.period_month or not self.period_fy:
            return date.today()
        month_num  = MONTH_MAP.get(self.period_month, date.today().month)
        fy_start_year = int(self.period_fy.split("-")[0])
        # FY Apr–Mar: months Apr–Dec belong to fy_start_year, Jan–Mar to fy_start_year+1
        year = fy_start_year if month_num >= 4 else fy_start_year + 1
        last_day = calendar.monthrange(year, month_num)[1]
        return date(year, month_num, last_day)

    def _quarter_end_date(self) -> date:
        """Return the last day of the selected quarter."""
        q = self.period_quarter or "Q1"
        fy_start = int(self.period_fy.split("-")[0]) if self.period_fy else date.today().year
        quarter_ends = {
            "Q1": date(fy_start,     6,  30),
            "Q2": date(fy_start,     9,  30),
            "Q3": date(fy_start,    12,  31),
            "Q4": date(fy_start + 1, 3, 31),
        }
        return quarter_ends.get(q, date.today())


# ─── Module-level helpers ─────────────────────────────────────────────────────

def _next_month(year: int, month: int):
    """Return (year, month) tuple for the calendar month after (year, month)."""
    if month == 12:
        return year + 1, 1
    return year, month + 1


def _fy_end_year(period_fy: str) -> int:
    """From '2024-25' return 2025."""
    if not period_fy:
        return date.today().year
    parts = period_fy.split("-")
    if len(parts) == 2:
        short_year = int(parts[1])
        if short_year < 100:
            century = (int(parts[0]) // 100) * 100
            return century + short_year
        return short_year
    return int(parts[0]) + 1