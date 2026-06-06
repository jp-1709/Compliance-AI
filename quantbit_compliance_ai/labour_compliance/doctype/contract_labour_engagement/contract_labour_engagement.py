"""
complyai/compliance/labour_compliance/doctype/contract_labour_engagement/contract_labour_engagement.py

Controller for Contract Labour Engagement.

Key behaviour:
- validate()            : compute compliance score + block status + perennial work warning.
- compute_block_status(): auto-sets block_new_pos based on licence expiry, score, oversubscription.
- is_contractor_blocked(): static method called by ERP webhook before PO creation.

Block triggers (in precedence order):
  1. Licence expired (any day past expiry)
  2. Licence expiring in < 30 days
  3. Compliance score < 60%
  4. Workers deployed > licence authorisation
"""

import frappe
from frappe.model.document import Document
from frappe.utils import getdate, today
from datetime import date, timedelta


class ContractLabourEngagement(Document):

    # ──────────────────────────────────────────────────────────────────
    # LIFECYCLE HOOKS
    # ──────────────────────────────────────────────────────────────────

    def validate(self):
        self.validate_dates()
        self.update_peak_workers()
        self.compute_compliance_score()
        self.compute_block_status()
        self.alert_perennial_work_violation()

    # ──────────────────────────────────────────────────────────────────
    # VALIDATION
    # ──────────────────────────────────────────────────────────────────

    def validate_dates(self):
        """Engagement end cannot precede engagement start."""
        if self.engagement_start and self.engagement_end:
            if getdate(self.engagement_end) < getdate(self.engagement_start):
                frappe.throw(
                    "Engagement End cannot be before Engagement Start.",
                    frappe.ValidationError,
                )

    def update_peak_workers(self):
        """Track peak workers deployed over the lifetime of the engagement."""
        current = self.current_workers_deployed or 0
        peak = self.max_workers_deployed_ever or 0
        if current > peak:
            self.max_workers_deployed_ever = current

    # ──────────────────────────────────────────────────────────────────
    # COMPLIANCE SCORE
    # ──────────────────────────────────────────────────────────────────

    def compute_compliance_score(self):
        """
        Score: 100 base.
          -40 : PF not compliant
          -30 : ESIC not compliant
          -30 : Min wages not verified
        Note: total possible deduction = 100 (can reach 0).
        """
        score = 100
        if not self.contractor_pf_compliant:
            score -= 40
        if not self.contractor_esic_compliant:
            score -= 30
        if not self.contractor_min_wages_paid:
            score -= 30
        self.compliance_score = max(0, score)

    # ──────────────────────────────────────────────────────────────────
    # BLOCK STATUS (ERP WEBHOOK GATE)
    # ──────────────────────────────────────────────────────────────────

    def compute_block_status(self):
        """
        Block new POs if ANY of:
          1. Licence already expired
          2. Licence expiring in < 30 days (near-expiry buffer)
          3. Compliance score < 60%
          4. Current deployed > licence authorisation (oversubscribed)

        Evaluated in precedence order; first match wins.
        Called nightly by the background job and on every save.
        """
        if not self.licence_expiry_date:
            # Cannot assess without expiry — block conservatively
            self.block_new_pos = 1
            self.block_reason = "Licence expiry date not recorded — cannot issue POs until verified."
            return

        expiry = getdate(self.licence_expiry_date)
        _today = date.today()
        days_to_expiry = (expiry - _today).days

        # 1. Already expired
        if days_to_expiry < 0:
            self.block_new_pos = 1
            self.block_reason = f"Licence expired on {expiry.strftime('%d-%b-%Y')}."
            return

        # 2. Expiring within 30 days
        if days_to_expiry < 30:
            self.block_new_pos = 1
            self.block_reason = (
                f"Licence expires in {days_to_expiry} day(s) "
                f"({expiry.strftime('%d-%b-%Y')}) — renew before issuing new POs."
            )
            return

        # 3. Compliance score below threshold
        if (self.compliance_score or 0) < 60:
            self.block_new_pos = 1
            self.block_reason = (
                f"Compliance score {self.compliance_score}% is below the 60% threshold. "
                "Verify PF/ESIC challans and minimum wages compliance."
            )
            return

        # 4. Oversubscribed
        deployed = self.current_workers_deployed or 0
        authorised = self.licence_workers_authorised or 0
        if authorised > 0 and deployed > authorised:
            self.block_new_pos = 1
            self.block_reason = (
                f"Workers deployed ({deployed}) exceeds licence authorisation ({authorised}). "
                "Apply for licence amendment before deploying more workers."
            )
            return

        # All clear
        self.block_new_pos = 0
        self.block_reason = None

    # ──────────────────────────────────────────────────────────────────
    # PERENNIAL WORK WARNING
    # ──────────────────────────────────────────────────────────────────

    def alert_perennial_work_violation(self):
        """
        CLRA §10 (as amended) restricts contract labour for perennial/core work.
        This is a legal risk that requires mandatory Legal Counsel review.
        Alert shown; does NOT block save (Legal may have an exemption).
        """
        if self.is_perennial_work and self.engagement_status == "Active":
            frappe.msgprint(
                "WARNING: Engaging contract labour for perennial / core work is restricted "
                "under CLRA §10 as amended in many states. Confirm with Legal Counsel before "
                "proceeding. Perennial work = regularly done by permanent workers in same establishment.",
                alert=True,
                indicator="red",
            )

    # ──────────────────────────────────────────────────────────────────
    # ERP WEBHOOK — STATIC METHOD
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def is_blocked(contractor_pan: str, business_entity: str) -> bool:
        """
        Called by ERP PO module as a fast yes/no check before PO creation.
        Target: < 100ms response time.
        Queries DB directly (no doc instantiation) for speed.
        """
        result = frappe.db.get_value(
            "Contract Labour Engagement",
            {
                "contractor_pan": contractor_pan,
                "business_entity": business_entity,
                "engagement_status": "Active",
            },
            "block_new_pos",
        )
        # If no engagement found, default to not blocked (new contractor — allow PO creation)
        return bool(result)