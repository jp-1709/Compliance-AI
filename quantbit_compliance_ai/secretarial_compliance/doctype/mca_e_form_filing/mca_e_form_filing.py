"""
complyai/compliance/secretarial_compliance/doctype/mca_e_form_filing/mca_e_form_filing.py

MCA E-Form Filing controller:
- Delay computation (filed_on - filing_due_date)
- Late fee ₹100/day uncapped for AOC-4, MGT-7, MGT-14, etc.
- On submit (Approved status), marks linked Compliance Calendar Task complete
"""

from datetime import date

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate

# Forms subject to ₹100/day uncapped additional fee (Apr 2018 amendment)
UNCAPPED_FORMS = frozenset({
    "AOC-4",
    "AOC-4 XBRL",
    "AOC-4 CFS",
    "MGT-7",
    "MGT-7A",
    "MGT-14",
})


class McaEFormFiling(Document):
    # ───────────────────────── lifecycle hooks ──────────────────────────

    def validate(self):
        self.compute_delay()
        self.compute_late_fee()

    def on_submit(self):
        self.close_linked_task()

    # ───────────────────────── computations ─────────────────────────────

    def compute_delay(self):
        """Delay = max(0, filed_on - filing_due_date) in days."""
        if self.filed_on and self.filing_due_date:
            delta = (getdate(self.filed_on) - getdate(self.filing_due_date)).days
            self.delay_days = max(0, delta)
        else:
            self.delay_days = 0

    def compute_late_fee(self):
        """For uncapped forms: additional_fee = delay_days * ₹100."""
        delay = self.delay_days or 0
        if delay > 0 and self.form_code in UNCAPPED_FORMS:
            self.additional_fee_inr = delay * 100
        elif delay == 0:
            self.additional_fee_inr = 0
        # For capped / event-based forms, fee is manually entered

    # ───────────────────────── task management ──────────────────────────

    def close_linked_task(self):
        """Mark linked Compliance Calendar Task complete when filing status = Approved."""
        if self.filing_status == "Approved" and self.linked_compliance_task:
            try:
                task = frappe.get_doc("Compliance Calendar Task", self.linked_compliance_task)
                task.status = "Completed"
                task.completion_pct = 100
                task.completed_on = self.filed_on or date.today()
                task.submission_reference = self.srn
                task.save(ignore_permissions=True)
            except Exception:
                frappe.log_error(frappe.get_traceback(), "E-Form Filing Task Completion Failed")