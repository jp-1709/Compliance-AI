"""
Tax Notice — Controller
quantbit_compliance_ai/tax_gst_compliance/doctype/tax_notice/tax_notice.py

Handles:
  - Total demand computation (amount + interest + penalty)
  - Auto-creates a Compliance Calendar Task with the response due date,
    so the notice response deadline shows up on the calendar without a
    separate manual step (this is the explicit "Key Behaviour" from the
    module spec's log_tax_notice() API).

NOTE on scope: the spec's cross-module integration table also calls for
auto-creating a Risk Register Item when amount_demanded_inr >= Rs 50 lakh.
That is intentionally NOT implemented here: Risk Item's identification_source
Select has no "Tax Notice" option, and risk_category/method_profile are Links
to Risk Category / Risk Method Profile records this module has no way to
choose correctly without guessing. Wiring that up would mean either silently
guessing a category or modifying the Risk Register schema from outside its
own module — both riskier than leaving the linked_risk_item field (already
on the DocType) for a human to fill in manually.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate

from quantbit_compliance_ai.foundation.utils import pick_task_reviewer


class TaxNotice(Document):

    def validate(self):
        self.compute_total_demand()

    def on_update(self):
        self.create_response_task()

    # ──────────────────────────────────────────────
    # Total Demand
    # ──────────────────────────────────────────────

    def compute_total_demand(self):
        self.total_demand_inr = (
            (self.amount_demanded_inr or 0)
            + (self.interest_demanded_inr or 0)
            + (self.penalty_demanded_inr or 0)
        )

    # ──────────────────────────────────────────────
    # Response Task (idempotent)
    # ──────────────────────────────────────────────

    def create_response_task(self):
        if not self.response_due_date or self.response_filed_on:
            return

        task_title = f"Respond to {self.notice_type} — {self.notice_number}"[:140]
        if frappe.db.exists(
            "Compliance Calendar Task",
            {"business_entity": self.business_entity, "task_title": task_title},
        ):
            return

        try:
            frappe.get_doc(
                {
                    "doctype": "Compliance Calendar Task",
                    "organisation": self.organisation,
                    "business_entity": self.business_entity,
                    "task_title": task_title,
                    # response_due_date isn't guaranteed to be after received_on
                    # for hand-entered data — bracket both rather than assume.
                    "period_start": min(
                        getdate(self.received_on or self.notice_date), getdate(self.response_due_date)
                    ),
                    "period_end": max(
                        getdate(self.received_on or self.notice_date), getdate(self.response_due_date)
                    ),
                    "due_date": self.response_due_date,
                    "assigned_to": frappe.session.user,
                    "reviewer": pick_task_reviewer(frappe.session.user),
                    "status": "Open",
                    "risk_level": "Critical" if (self.total_demand_inr or 0) > 0 else "High",
                    "category": "Tax",
                    "section_reference": f"Tax Notice: {self.name}",
                }
            ).insert(ignore_permissions=True)
        except Exception as e:
            frappe.log_error(
                f"Failed to create response task for Tax Notice {self.name}: {e}",
                "TaxNotice",
            )
