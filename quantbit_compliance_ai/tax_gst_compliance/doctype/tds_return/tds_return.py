"""
TDS Return — Controller
quantbit_compliance_ai/tax_gst_compliance/doctype/tds_return/tds_return.py

Handles:
  - Filing due date: 31st of month after quarter end; Q4 (Jan-Mar): 31-May
  - Delay + late fee (Sec 234E: Rs 200/day, capped at total TDS deducted)
  - Challan-count sanity check (challan_count/matched/unmatched are plain
    Int fields with no child-table line items to reconcile against in this
    schema, so this is a count consistency check, not a real line-item match)
"""

from datetime import date

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate

LATE_FEE_PER_DAY = 200

# quarter select value -> (due_year_offset_from_fy_start, month, day)
_QUARTER_DUE = {
    "Q1 (Apr-Jun)": (0, 7, 31),
    "Q2 (Jul-Sep)": (0, 10, 31),
    "Q3 (Oct-Dec)": (1, 1, 31),
    "Q4 (Jan-Mar)": (1, 5, 31),
}


class TDSReturn(Document):

    def validate(self):
        self.compute_due_date()
        self.compute_delay()
        self.compute_late_fee()
        self.compute_challan_match()

    # ──────────────────────────────────────────────
    # Due Date
    # ──────────────────────────────────────────────

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

    # ──────────────────────────────────────────────
    # Delay / Late Fee
    # ──────────────────────────────────────────────

    def compute_delay(self):
        if not self.filed_on or not self.filing_due_date:
            self.delay_days = 0
            return
        delta = (getdate(self.filed_on) - getdate(self.filing_due_date)).days
        self.delay_days = max(0, delta)

    def compute_late_fee(self):
        """234E: Rs 200/day, capped at the total TDS deducted for this return."""
        if not self.delay_days or self.delay_days <= 0:
            self.late_fee_inr = 0
            return
        self.late_fee_inr = min(
            self.delay_days * LATE_FEE_PER_DAY, self.total_tds_deducted_inr or 0
        )

    # ──────────────────────────────────────────────
    # Challan Count Sanity Check
    # ──────────────────────────────────────────────

    def compute_challan_match(self):
        """unmatched = challans recorded but not yet marked matched. Floored at 0."""
        total = self.challan_count or 0
        matched = self.matched_challans_count or 0
        self.unmatched_challans_count = max(0, total - matched)
