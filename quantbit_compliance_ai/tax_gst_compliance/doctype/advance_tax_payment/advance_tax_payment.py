"""
Advance Tax Payment — Controller
quantbit_compliance_ai/tax_gst_compliance/doctype/advance_tax_payment/advance_tax_payment.py

Handles:
  - Cumulative required % / amount from instalment
  - Shortfall computation
  - Interest u/s 234C (1%/month on shortfall; instalments 1-3 carry 3 months'
    interest, instalment 4 carries only 1 month — see Tricky Point #14)
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate

INSTALMENT_PCT = {
    "1 — 15 Jun (15%)": 15,
    "2 — 15 Sep (45% cum.)": 45,
    "3 — 15 Dec (75% cum.)": 75,
    "4 — 15 Mar (100% cum.)": 100,
}

# 234C: 1% per month. Instalments 1-3 shortfalls carry 3 months' interest;
# the final instalment (4) only carries 1 month, since there's no further
# instalment left to true-up against.
INSTALMENT_INTEREST_MONTHS = {
    "1 — 15 Jun (15%)": 3,
    "2 — 15 Sep (45% cum.)": 3,
    "3 — 15 Dec (75% cum.)": 3,
    "4 — 15 Mar (100% cum.)": 1,
}


class AdvanceTaxPayment(Document):

    def validate(self):
        self.compute_required_amounts()
        self.compute_shortfall_and_interest()
        self.compute_delay()

    def compute_required_amounts(self):
        """cumulative_required_pct/inr derived from the selected instalment."""
        pct = INSTALMENT_PCT.get(self.instalment, 0)
        self.cumulative_required_pct = pct
        self.cumulative_required_inr = (self.estimated_total_tax_inr or 0) * pct / 100

    def compute_shortfall_and_interest(self):
        """
        shortfall_inr = max(0, cumulative_required - cumulative_paid_till_now)
        interest_234c_inr = shortfall × 1% × applicable months for this instalment
        """
        required = self.cumulative_required_inr or 0
        paid = self.cumulative_paid_till_now_inr or 0
        self.shortfall_inr = max(0, required - paid)

        if self.shortfall_inr > 0:
            months = INSTALMENT_INTEREST_MONTHS.get(self.instalment, 3)
            self.interest_234c_inr = round(self.shortfall_inr * 0.01 * months, 2)
        else:
            self.interest_234c_inr = 0

    def compute_delay(self):
        """delay_days = paid_on - due_date (0 if on time or unpaid)."""
        if not self.paid_on or not self.due_date:
            self.delay_days = 0
            return
        delta = (getdate(self.paid_on) - getdate(self.due_date)).days
        self.delay_days = max(0, delta)
