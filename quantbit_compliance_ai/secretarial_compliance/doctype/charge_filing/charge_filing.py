"""
complyai/compliance/secretarial_compliance/doctype/charge_filing/charge_filing.py

Charge Filing controller:
- CHG-1 due = charge_creation_date + 30 days
- CHG-4 due = satisfaction_date + 30 days
- Warn loudly if CHG-1 deadline missed (charge may become unenforceable in liquidation)
- Auto-create MCA E-Form Filing obligations for CHG-1 and CHG-4
"""

from datetime import date, timedelta

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate


class ChargeFiling(Document):
    # ───────────────────────── lifecycle hooks ──────────────────────────

    def validate(self):
        self.compute_filing_due_dates()
        self.warn_if_due_date_breached()

    def on_submit(self):
        self.auto_create_chg_filing_obligations()

    # ───────────────────────── due date computation ─────────────────────

    def compute_filing_due_dates(self):
        """CHG-1 due 30 days from creation; CHG-4 due 30 days from satisfaction."""
        if self.charge_creation_date:
            self.chg_1_due_date = getdate(self.charge_creation_date) + timedelta(days=30)
        if self.satisfaction_date:
            self.chg_4_due_date = getdate(self.satisfaction_date) + timedelta(days=30)

    # ───────────────────────── deadline breach warning ──────────────────

    def warn_if_due_date_breached(self):
        """Raise a bold alert if CHG-1 or CHG-4 deadline is already missed."""
        today = date.today()
        if (
            self.chg_1_due_date
            and today > getdate(self.chg_1_due_date)
            and not self.chg_1_filed_on
        ):
            frappe.msgprint(
                _(
                    f"⚠ CHG-1 DEADLINE MISSED for {self.charge_holder}. "
                    "The charge may become unenforceable against a liquidator. "
                    "Seek NCLT condonation immediately if beyond 300 days."
                ),
                indicator="red",
                alert=True,
            )
        if (
            self.chg_4_due_date
            and today > getdate(self.chg_4_due_date)
            and self.charge_status == "Satisfied (Closed)"
            and not frappe.db.get_value("Charge Filing", self.name, "chg_4_filing")
        ):
            frappe.msgprint(
                _(
                    f"⚠ CHG-4 DEADLINE MISSED for {self.charge_holder}. "
                    "File immediately to avoid penalties."
                ),
                indicator="red",
                alert=True,
            )

    # ───────────────────────── auto-create filings ──────────────────────

    def auto_create_chg_filing_obligations(self):
        """Create MCA E-Form Filing placeholder + task for CHG-1 (and CHG-4 if satisfied)."""
        self._ensure_chg1_filing()
        if self.charge_status == "Satisfied (Closed)":
            self._ensure_chg4_filing()

    def _ensure_chg1_filing(self):
        if self.chg_1_filing:
            return
        try:
            filing = frappe.get_doc({
                "doctype": "MCA E-Form Filing",
                "organisation": self.organisation,
                "business_entity": self.business_entity,
                "form_code": "CHG-1",
                "filing_status": "Pending",
                "filing_due_date": self.chg_1_due_date,
                "linked_charge": self.name,
                "fy": _derive_fy(self.charge_creation_date),
            }).insert(ignore_permissions=True)
            frappe.db.set_value("Charge Filing", self.name, "chg_1_filing", filing.name)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "CHG-1 Filing Creation Failed")

    def _ensure_chg4_filing(self):
        if self.chg_4_filing:
            return
        if not self.satisfaction_date:
            return
        try:
            filing = frappe.get_doc({
                "doctype": "MCA E-Form Filing",
                "organisation": self.organisation,
                "business_entity": self.business_entity,
                "form_code": "CHG-4",
                "filing_status": "Pending",
                "filing_due_date": self.chg_4_due_date,
                "linked_charge": self.name,
                "fy": _derive_fy(self.satisfaction_date),
            }).insert(ignore_permissions=True)
            frappe.db.set_value("Charge Filing", self.name, "chg_4_filing", filing.name)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "CHG-4 Filing Creation Failed")


# ───────────────────────── helpers ──────────────────────────────────────


def _derive_fy(d) -> str:
    d = getdate(d)
    if d.month < 4:
        return f"{d.year - 1}-{str(d.year)[2:]}"
    return f"{d.year}-{str(d.year + 1)[2:]}"