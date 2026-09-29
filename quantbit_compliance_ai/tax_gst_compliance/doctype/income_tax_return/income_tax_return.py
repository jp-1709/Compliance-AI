"""
Income Tax Return — Controller
quantbit_compliance_ai/tax_gst_compliance/doctype/income_tax_return/income_tax_return.py

Handles:
  - AY derivation from FY
  - Filing due date: 31-Jul (non-audit) / 31-Oct (audit) / 30-Nov (TP case)
  - Belated-filing detection and 234F late fee
"""

from datetime import date

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate


class IncomeTaxReturn(Document):

    def validate(self):
        self.derive_ay()
        self.compute_due_date()
        self.compute_belated_status()
        self.compute_delay()
        self.compute_late_fee_234f()

    # ──────────────────────────────────────────────
    # Assessment Year
    # ──────────────────────────────────────────────

    def derive_ay(self):
        """FY 2024-25 -> AY 2025-26."""
        fy = (self.period_fy or "").strip()
        parts = fy.split("-")
        if len(parts) != 2:
            return
        try:
            start_year = int(parts[0])
        except ValueError:
            return
        self.ay = f"{start_year + 1}-{str(start_year + 2)[2:]}"

    # ──────────────────────────────────────────────
    # Due Date
    # ──────────────────────────────────────────────

    def compute_due_date(self):
        """
        31-Jul of (FY end year) for non-audit cases.
        31-Oct of (FY end year) for tax-audit cases (Sec 44AB).
        30-Nov of (FY end year) for transfer-pricing cases (Sec 92E).
        TP takes precedence over plain audit; audit takes precedence over neither.
        """
        if self.filing_due_date:
            return  # don't overwrite a manually set / already-computed due date

        fy_end_year = self._fy_end_year()
        if not fy_end_year:
            return

        if self.transfer_pricing_required:
            self.filing_due_date = date(fy_end_year, 11, 30)
        elif self.tax_audit_required:
            self.filing_due_date = date(fy_end_year, 10, 31)
        else:
            self.filing_due_date = date(fy_end_year, 7, 31)

    def _fy_end_year(self):
        """'2024-25' -> 2025 (the calendar year the ITR is filed in)."""
        fy = (self.period_fy or "").strip()
        parts = fy.split("-")
        if len(parts) != 2:
            return None
        try:
            start_year = int(parts[0])
        except ValueError:
            return None
        return start_year + 1

    # ──────────────────────────────────────────────
    # Belated Filing
    # ──────────────────────────────────────────────

    def compute_belated_status(self):
        """is_belated = 1 if filed_on is after filing_due_date."""
        if self.filed_on and self.filing_due_date:
            self.is_belated = 1 if getdate(self.filed_on) > getdate(self.filing_due_date) else 0

    def compute_delay(self):
        if not self.filed_on or not self.filing_due_date:
            self.delay_days = 0
            return
        delta = (getdate(self.filed_on) - getdate(self.filing_due_date)).days
        self.delay_days = max(0, delta)

    # ──────────────────────────────────────────────
    # 234F Late Fee
    # ──────────────────────────────────────────────

    def compute_late_fee_234f(self):
        """
        234F: ₹5,000 if filed belated.
        ₹1,000 if total income < ₹5 lakh (concessional cap).
        """
        if not self.is_belated:
            self.late_fee_234f_inr = 0
            return
        self.late_fee_234f_inr = 1000 if (self.total_income_inr or 0) < 5_00_000 else 5000
