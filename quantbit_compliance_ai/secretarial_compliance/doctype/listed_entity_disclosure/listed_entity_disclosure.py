"""
complyai/compliance/secretarial_compliance/doctype/listed_entity_disclosure/listed_entity_disclosure.py

Listed Entity Disclosure controller:
- Delay computation
- Reg 30 material-event 24-hour countdown warning
- Validate at least one exchange is selected when filing
"""

from datetime import date, datetime, timedelta

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, now_datetime


class ListedEntityDisclosure(Document):
    # ───────────────────────── lifecycle hooks ──────────────────────────

    def validate(self):
        self.compute_delay()
        self.check_material_event_24h()
        self.validate_exchange_selection()

    def on_submit(self):
        self.close_linked_task()

    # ───────────────────────── computations ─────────────────────────────

    def compute_delay(self):
        if self.filed_on and self.filing_due_date:
            delta = (getdate(self.filed_on) - getdate(self.filing_due_date)).days
            self.delay_days = max(0, delta)
        else:
            self.delay_days = 0

    def check_material_event_24h(self):
        """Reg 30: material events must be disclosed within 24 hours of occurrence.
        If is_material=1, disclosure_status is still Pending, and filing_due_date is past → warn.
        """
        if not self.is_material:
            return
        if self.disclosure_status in ("Filed with Exchange", "Public"):
            return
        if self.filing_due_date:
            hours_left = (
                datetime.combine(getdate(self.filing_due_date), datetime.min.time())
                + timedelta(hours=24)
                - now_datetime()
            ).total_seconds() / 3600
            if hours_left < 0:
                frappe.msgprint(
                    _("⚠ Material Event (Reg 30) disclosure OVERDUE. "
                      "SEBI requires filing within 24 hours of occurrence. "
                      "File immediately with BSE/NSE."),
                    indicator="red",
                    alert=True,
                )
            elif hours_left < 6:
                frappe.msgprint(
                    _(f"⚠ Material Event (Reg 30): only {hours_left:.1f} hours remaining to file. "
                      "Notify CFO and CS immediately."),
                    indicator="orange",
                    alert=True,
                )

    def validate_exchange_selection(self):
        """When status moves to 'Filed with Exchange', at least one exchange must be ticked."""
        if self.disclosure_status == "Filed with Exchange":
            if not any([self.filed_with_bse, self.filed_with_nse,
                        self.filed_with_msei, self.filed_with_other_exchange]):
                frappe.throw(
                    _("Please select at least one exchange where the disclosure was filed."),
                    title=_("Exchange Not Selected"),
                )

    # ───────────────────────── task management ──────────────────────────

    def close_linked_task(self):
        if self.disclosure_status in ("Filed with Exchange", "Public") \
                and self.linked_compliance_task:
            try:
                task = frappe.get_doc("Compliance Calendar Task", self.linked_compliance_task)
                task.status = "Completed"
                task.completion_pct = 100
                task.completed_on = self.filed_on or date.today()
                task.save(ignore_permissions=True)
            except Exception:
                frappe.log_error(frappe.get_traceback(), "LED Task Completion Failed")							