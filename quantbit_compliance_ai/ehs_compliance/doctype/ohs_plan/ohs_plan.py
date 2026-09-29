# Copyright (c) 2026, Quantbit Technologies Pvt Ltd and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_years, getdate

# MSIHC-mandated plans (On-Site Emergency Plan, Safety Report, etc.) follow a
# formal lifecycle: Draft -> Under Review -> Approved -> Active -> Under
# Revision -> Superseded, with Withdrawn reachable from any pre-Active state.
# Draft/Under Review -> Active is also allowed directly: api.approve_ohs_plan()
# combines approval and activation into a single action.
_ALLOWED_TRANSITIONS = {
    None: {"Draft"},
    "Draft": {"Under Review", "Active", "Withdrawn"},
    "Under Review": {"Approved", "Active", "Draft", "Withdrawn"},
    "Approved": {"Active", "Under Review", "Withdrawn"},
    "Active": {"Under Revision", "Superseded", "Withdrawn"},
    "Under Revision": {"Under Review", "Active", "Withdrawn"},
    "Superseded": set(),
    "Withdrawn": set(),
}

REVIEW_CYCLE_YEARS = 1


class OHSPlan(Document):

    def validate(self):
        self._validate_status_transition()
        self._set_next_review_due()

    def _validate_status_transition(self):
        before = self.get_doc_before_save()
        before_status = before.plan_status if before else None
        after_status = self.plan_status

        if before_status == after_status:
            return

        allowed = _ALLOWED_TRANSITIONS.get(before_status, set())
        if after_status not in allowed:
            frappe.throw(
                _("OHS Plan cannot transition from {0} to {1}.").format(
                    before_status or "(new)", after_status
                )
            )

    def _set_next_review_due(self):
        """
        Whenever the plan becomes Active, (re)compute the annual review date
        from approved_on so get_ohs_plans_due_for_review() can find it.
        """
        if self.plan_status == "Active" and self.approved_on:
            approved_on = getdate(self.approved_on)
            self.next_review_due = add_years(approved_on, REVIEW_CYCLE_YEARS)
