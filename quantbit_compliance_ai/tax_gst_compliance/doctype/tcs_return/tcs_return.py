"""
TCS Return — Controller
quantbit_compliance_ai/tax_gst_compliance/doctype/tcs_return/tcs_return.py

Handles:
  - Filing due date: 31st of month after quarter end; Q4 (Jan-Mar): 31-May
    (same statutory cycle as TDS returns — Form 27EQ)
  - Delay + late fee (Sec 234E: Rs 200/day, capped at total TCS collected)
"""

from datetime import date

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate

LATE_FEE_PER_DAY = 200

_QUARTER_DUE = {
    "Q1 (Apr-Jun)": (0, 7, 31),
    "Q2 (Jul-Sep)": (0, 10, 31),
    "Q3 (Oct-Dec)": (1, 1, 31),
    "Q4 (Jan-Mar)": (1, 5, 31),
}


class TCSReturn(Document):

    def validate(self):
        self.compute_due_date()
        self.compute_delay()
        self.compute_late_fee()

    def compute_due_date(self):
        if self.filing_due_date:
            return
        fy_start = self._fy_start_year()
        due = _QUARTER_DUE.get(self.period_quarter)
        if fy_start is None or not due:
            return
        year_offset, month, day = due
        self.filing_due_date = date(fy_start + year_offset, month, day)

    def _fy_start_year(self):
        fy = (self.period_fy or "").strip()
        parts = fy.split("-")
        if len(parts) != 2:
            return None
        try:
            return int(parts[0])
        except ValueError:
            return None

    def compute_delay(self):
        if not self.filed_on or not self.filing_due_date:
            self.delay_days = 0
            return
        delta = (getdate(self.filed_on) - getdate(self.filing_due_date)).days
        self.delay_days = max(0, delta)

    def compute_late_fee(self):
        """234E: Rs 200/day, capped at total TCS collected for this return."""
        if not self.delay_days or self.delay_days <= 0:
            self.late_fee_inr = 0
            return
        self.late_fee_inr = min(
            self.delay_days * LATE_FEE_PER_DAY, self.total_tcs_collected_inr or 0
        )
