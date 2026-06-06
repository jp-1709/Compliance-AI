"""
complyai/compliance/secretarial_compliance/doctype/director_kyc_filing/director_kyc_filing.py

Director KYC Filing controller — delay computation, late-flag, back-sync to Director Profile.
"""

from datetime import date

import frappe
from frappe import _
from frappe.model.document import Document


class DirectorKycFiling(Document):
    # ───────────────────────── lifecycle hooks ──────────────────────────

    def validate(self):
        self.compute_delay()
        self.set_late_flag()

    def on_submit(self):
        self.sync_director_profile()
        self.mark_compliance_task_complete()

    # ───────────────────────── computations ─────────────────────────────

    def compute_delay(self):
        """Delay = (filed_on - filing_due_date) in days if positive, else 0."""
        if self.filed_on and self.filing_due_date:
            delta = (self.filed_on - self.filing_due_date).days
            self.delay_days = max(0, delta)
        else:
            self.delay_days = 0

    def set_late_flag(self):
        """Mark is_late_filing based on delay_days and fee logic (₹5,000 if late)."""
        self.is_late_filing = 1 if (self.delay_days or 0) > 0 else 0
        if self.is_late_filing and not self.fee_paid_inr:
            # Minimum late fee hint — actual amount set by user after MCA confirmation
            frappe.msgprint(
                _("This is a late KYC filing. A fee of ₹5,000 is applicable on MCA portal."),
                indicator="orange",
                alert=True,
            )

    # ───────────────────────── back-sync ────────────────────────────────

    def sync_director_profile(self):
        """After approval, update Director Profile with latest SRN and filing date."""
        if self.filing_status == "Approved" and self.director:
            director = frappe.get_doc("Director Profile", self.director)
            director.last_kyc_filing = self.filed_on or date.today()
            director.last_kyc_srn = self.srn
            # Reactivate DIN if it was deactivated
            if self.din_was_deactivated and director.din_status == "Deactivated (KYC pending)":
                director.din_status = "Active"
            director.save(ignore_permissions=True)

    def mark_compliance_task_complete(self):
        """Close linked Compliance Calendar Task when filing is submitted as Approved."""
        if self.filing_status == "Approved" and self.linked_compliance_task:
            try:
                task = frappe.get_doc("Compliance Calendar Task", self.linked_compliance_task)
                task.status = "Completed"
                task.completion_pct = 100
                task.completed_on = self.filed_on or date.today()
                task.save(ignore_permissions=True)
            except Exception:
                frappe.log_error(frappe.get_traceback(), "KYC Task Completion Failed")