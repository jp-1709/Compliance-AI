"""
complyai/compliance/secretarial_compliance/doctype/related_party_transaction/related_party_transaction.py

Related Party Transaction controller:
- §188 threshold computation (Rule 15 of Companies (Meetings of Board) Rules 2014)
- LODR Reg 23 materiality (10% of turnover) for listed entities
- Approval chain gate: cannot execute without required approvals
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

# §188 threshold rules (transaction_type -> (% of turnover, abs cap in INR, basis))
# basis: "turnover" or "networth"
_188_THRESHOLD_RULES = {
    "Sale of Goods":         ("turnover", 0.10, 100_00_00_000),
    "Purchase of Goods":     ("turnover", 0.10, 100_00_00_000),
    "Sale of Services":      ("turnover", 0.10, 50_00_00_000),
    "Purchase of Services":  ("turnover", 0.10, 50_00_00_000),
    "Sale of Property":      ("networth", 0.10, 100_00_00_000),
    "Purchase of Property":  ("networth", 0.10, 100_00_00_000),
    "Lease":                 ("networth", 0.10, 100_00_00_000),   # also 10% turnover; use lower
    "Loan Given":            ("networth", 0.10, 100_00_00_000),
    "Loan Taken":            ("networth", 0.10, 100_00_00_000),
    "Guarantee":             ("networth", 0.10, 100_00_00_000),
    "Security":              ("networth", 0.10, 100_00_00_000),
    "Appointment with Remuneration": ("fixed", None, 2_50_000 * 12),  # ₹2.5 lakh/month * 12
    "Agency Arrangement":    ("turnover", 0.10, 100_00_00_000),
}


def _get_financials(business_entity: str) -> tuple:
    """Return (turnover_inr, networth_inr) from latest year's financials.
    Falls back to 0 if not found.
    """
    # Attempt to read from a Financial Data doctype if it exists
    try:
        financials = frappe.db.get_value(
            "Business Entity Financial",
            {"business_entity": business_entity},
            ["turnover_inr", "networth_inr"],
            order_by="fy desc",
            as_dict=True,
        )
        if financials:
            return flt(financials.get("turnover_inr")), flt(financials.get("networth_inr"))
    except Exception:
        pass
    return 0.0, 0.0


def _compute_188_threshold(transaction_type: str, turnover: float, networth: float) -> float | None:
    """Return the §188 approval threshold in INR for a given transaction type.

    Threshold = min(percentage * basis, absolute_cap).
    Returns None if no rule found (no threshold applies).
    """
    rule = _188_THRESHOLD_RULES.get(transaction_type)
    if not rule:
        return None
    basis_type, pct, abs_cap = rule
    if basis_type == "fixed":
        return flt(abs_cap)
    basis = turnover if basis_type == "turnover" else networth
    if not basis:
        return flt(abs_cap)
    pct_threshold = basis * pct
    return min(pct_threshold, flt(abs_cap))


class RelatedPartyTransaction(Document):
    # ───────────────────────── lifecycle hooks ──────────────────────────

    def validate(self):
        self.compute_threshold_status()
        self.validate_approval_chain()

    # ───────────────────────── threshold computation ─────────────────────

    def compute_threshold_status(self):
        """Compute exceeds_188_threshold and exceeds_lodr_material_threshold."""
        turnover, networth = _get_financials(self.business_entity)
        amount = flt(self.transaction_amount_inr)

        # §188 threshold
        threshold_188 = _compute_188_threshold(self.transaction_type, turnover, networth)
        if threshold_188 is not None and amount > threshold_188:
            self.exceeds_188_threshold = 1
        else:
            self.exceeds_188_threshold = 0

        # LODR Reg 23 materiality — 10% of turnover, only for listed entities
        is_listed = frappe.db.get_value("Business Entity", self.business_entity, "is_listed_branch")
        if is_listed and turnover:
            material_threshold = turnover * 0.10
            self.exceeds_lodr_material_threshold = 1 if amount > material_threshold else 0
        else:
            self.exceeds_lodr_material_threshold = 0

    # ───────────────────────── approval chain gate ──────────────────────

    def validate_approval_chain(self):
        """Prevent execution without required approvals.

        Rule:
        - If exceeds §188 threshold AND NOT (ordinary course AND arm's length)
          → Board approval required before execution.
        - If listed and exceeds LODR materiality
          → Shareholder approval required before execution.
        - If ordinary course AND arm's length
          → Only Audit Committee approval required.
        """
        if self.rpt_status != "Executed":
            return  # Gate only applies at execution

        is_ordinary_and_arms_length = (
            self.ordinary_course_of_business and self.arms_length_basis
        )
        needs_board_approval = (
            self.exceeds_188_threshold and not is_ordinary_and_arms_length
        )

        if needs_board_approval and not self.board_approval_date:
            frappe.throw(
                _("RPT cannot be executed: Board approval is required under §188 "
                  "of the Companies Act 2013 for this transaction."),
                title=_("Approval Required"),
            )

        if self.exceeds_lodr_material_threshold and not self.shareholder_approval_date:
            frappe.throw(
                _("Material RPT for a listed entity requires Shareholder approval "
                  "under LODR Regulation 23(4) before execution."),
                title=_("Shareholder Approval Required"),
            )

        # If ordinary course + arms length, at minimum AC approval should be present
        if is_ordinary_and_arms_length and not self.ac_approval_date:
            frappe.msgprint(
                _("Audit Committee approval is recommended for RPTs even when transacted "
                  "in ordinary course and at arm's length basis."),
                indicator="orange",
                alert=True,
            )