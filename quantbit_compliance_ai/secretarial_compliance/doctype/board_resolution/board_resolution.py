"""
complyai/compliance/secretarial_compliance/doctype/board_resolution/board_resolution.py

Board Resolution controller:
- Auto-determines MGT-14 requirement from category (§117) or Special Resolution
- On submit, auto-creates MCA E-Form Filing (MGT-14) + Compliance Calendar Task
"""

from datetime import date, timedelta

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate

# §117 / §179 categories that mandate MGT-14 filing
MGT_14_CATEGORIES = frozenset({
    "Appointment of KMP",
    "Appointment of Auditor",
    "Loans / Investments / Guarantees (§186)",
    "Issuance of Securities",
    "Charge Creation / Modification / Satisfaction",
    "Alteration of MOA/AOA",
    "Merger / Demerger / Restructuring",
    "Approval of Borrowings (§180)",
})


def _derive_fy(passed_on) -> str:
    """Return FY string like '2024-25' from a date."""
    d = getdate(passed_on)
    if d.month < 4:
        return f"{d.year - 1}-{str(d.year)[2:]}"
    return f"{d.year}-{str(d.year + 1)[2:]}"


class BoardResolution(Document):
    # ───────────────────────── lifecycle hooks ──────────────────────────

    def validate(self):
        self.determine_mgt_14_requirement()

    def on_submit(self):
        self.auto_create_mgt_14_filing()

    # ───────────────────────── MGT-14 logic ─────────────────────────────

    def determine_mgt_14_requirement(self):
        """Set requires_mgt_14 + mgt_14_due_date based on category and resolution type."""
        needs = (
            self.category in MGT_14_CATEGORIES
            or self.resolution_type == "Special Resolution"
        )
        if needs:
            self.requires_mgt_14 = 1
            if self.passed_on:
                self.mgt_14_due_date = getdate(self.passed_on) + timedelta(days=30)
        else:
            self.requires_mgt_14 = 0
            self.mgt_14_due_date = None

    def auto_create_mgt_14_filing(self):
        """On submit, if MGT-14 required and not already linked, create E-Form Filing record."""
        if not self.requires_mgt_14:
            return
        if self.mgt_14_filing:
            return  # Already linked

        try:
            filing = frappe.get_doc({
                "doctype": "MCA E-Form Filing",
                "organisation": self.organisation,
                "business_entity": self.business_entity,
                "form_code": "MGT-14",
                "filing_status": "Pending",
                "filing_due_date": self.mgt_14_due_date,
                "linked_resolution": self.name,
                "fy": _derive_fy(self.passed_on),
                "filing_period": f"Resolution {self.name}",
            }).insert(ignore_permissions=True)

            # Create linked Compliance Calendar Task
            _create_mgt14_task(filing, self)

            # Back-link filing to this resolution
            frappe.db.set_value("Board Resolution", self.name, "mgt_14_filing", filing.name)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "MGT-14 Auto-Creation Failed")


# ───────────────────────── helpers ──────────────────────────────────────


def _create_mgt14_task(filing, resolution: BoardResolution):
    """Create a Compliance Calendar Task for the new MGT-14 filing."""
    try:
        task = frappe.get_doc({
            "doctype": "Compliance Calendar Task",
            "task_type": "MCA E-Form Filing",
            "reference_doctype": "MCA E-Form Filing",
            "reference_name": filing.name,
            "description": (
                f"File MGT-14 for resolution '{resolution.resolution_title}' "
                f"passed on {resolution.passed_on}. "
                f"Due by {resolution.mgt_14_due_date}."
            ),
            "due_date": resolution.mgt_14_due_date,
            "status": "Open",
            "organisation": resolution.organisation,
            "priority": "High",
        }).insert(ignore_permissions=True)
        # Link task back to filing
        frappe.db.set_value(
            "MCA E-Form Filing", filing.name, "linked_compliance_task", task.name
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "MGT-14 Task Creation Failed")