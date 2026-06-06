"""
ITC Reconciliation — Controller
complyai/compliance/tax_gst_compliance/doctype/itc_reconciliation/itc_reconciliation.py

Handles:
  - Difference (Books − 2B) computation
  - Match percentage computation
  - ITC at risk / additional ITC in 2B
  - Rule 36(4) compliance flag
  - Vendor chase count aggregation from line items
  - Mismatch line diff_inr auto-compute
"""

import frappe
from frappe import _
from frappe.model.document import Document


class ITCReconciliation(Document):

    # ──────────────────────────────────────────────
    # Frappe lifecycle hooks
    # ──────────────────────────────────────────────

    def validate(self):
        self.compute_diff()
        self.compute_match_pct()
        self.compute_rule_36_4_compliance()
        self.compute_line_diffs()
        self.recompute_vendor_chase_counts()

    def on_update(self):
        self.recompute_vendor_chase_counts()

    # ──────────────────────────────────────────────
    # Difference Computation
    # ──────────────────────────────────────────────

    def compute_diff(self):
        """
        diff_total_itc_inr  = books_total_itc_inr - gstr2b_total_itc_inr
        itc_at_risk_inr     = max(0, diff)   [books > 2B → we claimed more than allowed]
        additional_itc_in_2b_inr = max(0, -diff) [2B > books → unclaimed ITC]
        """
        books = self.books_total_itc_inr or 0
        gstr2b = self.gstr2b_total_itc_inr or 0

        self.diff_total_itc_inr       = books - gstr2b
        self.itc_at_risk_inr          = max(0,  books - gstr2b)
        self.additional_itc_in_2b_inr = max(0, gstr2b - books)

    # ──────────────────────────────────────────────
    # Match Percentage
    # ──────────────────────────────────────────────

    def compute_match_pct(self):
        """
        match_percentage = (matched ITC / books ITC) * 100
        matched ITC = books ITC - ITC at risk
        """
        books = self.books_total_itc_inr or 0
        if books > 0:
            matched = books - (self.itc_at_risk_inr or 0)
            self.match_percentage = round((matched / books) * 100, 2)
        else:
            self.match_percentage = 100.0  # no ITC in books = 100% matched

    # ──────────────────────────────────────────────
    # Rule 36(4) Compliance
    # ──────────────────────────────────────────────

    def compute_rule_36_4_compliance(self):
        """
        Rule 36(4) from 1-Jan-2022:
        ITC claimable in GSTR-3B <= ITC available in GSTR-2B (100% cap; no 5% buffer).
        compliant = 1 if books_itc <= gstr2b_itc
        """
        books  = self.books_total_itc_inr  or 0
        gstr2b = self.gstr2b_total_itc_inr or 0
        self.rule_36_4_compliant = 1 if books <= gstr2b else 0

    # ──────────────────────────────────────────────
    # Line-Item Diff Auto-Compute
    # ──────────────────────────────────────────────

    def compute_line_diffs(self):
        """For each mismatch line, set diff_inr = books_itc - gstr2b_itc."""
        for line in (self.mismatched_invoices or []):
            books_val  = line.books_itc_inr  or 0
            gstr2b_val = line.gstr2b_itc_inr or 0
            line.diff_inr = books_val - gstr2b_val

            # Auto-classify mismatch type if not set
            if not line.mismatch_type:
                if books_val > 0 and gstr2b_val == 0:
                    line.mismatch_type = "In Books, Not in 2B"
                elif gstr2b_val > 0 and books_val == 0:
                    line.mismatch_type = "In 2B, Not in Books"
                elif books_val != gstr2b_val:
                    line.mismatch_type = "Value Mismatch"

    # ──────────────────────────────────────────────
    # Vendor Chase Counts
    # ──────────────────────────────────────────────

    def recompute_vendor_chase_counts(self):
        """Aggregate chase status from child table lines."""
        CHASED_STATUSES  = {"Vendor Notified", "Vendor Will Amend"}
        PENDING_STATUSES = {"New"}

        chased  = 0
        pending = 0

        for line in (self.mismatched_invoices or []):
            status = line.action_status or "New"
            if status in CHASED_STATUSES:
                chased += 1
            elif status in PENDING_STATUSES:
                pending += 1

        self.vendor_chase_count   = chased
        self.vendor_chase_pending = pending


class ITCMismatchLine(Document):
    """
    Child table row — no standalone lifecycle needed.
    Validation is driven by the parent ITCReconciliation.
    """
    pass