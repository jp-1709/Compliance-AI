"""
Environmental Clearance — Controller
complyai/compliance/ehs_compliance/doctype/environmental_clearance/environmental_clearance.py

Handles:
  • days_to_expiry computation (skipped for perpetual)
  • next_renewal_action_due = valid_until − renewal_lead_days
  • Compliance score from specific_conditions table
  • CTO < 365 days → critical task auto-creation
"""

import frappe
from frappe import _
from frappe.model.document import Document
from datetime import date, timedelta


class EnvironmentalClearance(Document):

    # ──────────────────────────────────────────────
    # Lifecycle hooks
    # ──────────────────────────────────────────────

    def validate(self):
        self.validate_clearance_number_unique()
        self.compute_days_to_expiry()
        self.compute_next_renewal_action()
        self.compute_compliance_score()
        self.validate_cto_renewal_lead()

    def on_update(self):
        self.alert_critical_thresholds()

    # ──────────────────────────────────────────────
    # Unique clearance number
    # ──────────────────────────────────────────────

    def validate_clearance_number_unique(self):
        if not self.clearance_number:
            return
        existing = frappe.db.get_value(
            "Environmental Clearance",
            {"clearance_number": self.clearance_number, "name": ["!=", self.name]},
            "name",
        )
        if existing:
            frappe.throw(
                _("Clearance Number {0} already exists in record {1}.").format(
                    self.clearance_number, existing
                )
            )

    # ──────────────────────────────────────────────
    # Computed fields
    # ──────────────────────────────────────────────

    def compute_days_to_expiry(self):
        """Skip computation for perpetual/lifetime clearances (e.g. forest clearances)."""
        if self.is_perpetual:
            self.days_to_expiry = None
            return
        if self.valid_until:
            valid_until = (
                self.valid_until
                if isinstance(self.valid_until, date)
                else frappe.utils.getdate(self.valid_until)
            )
            self.days_to_expiry = (valid_until - date.today()).days

    def compute_next_renewal_action(self):
        """next_renewal_action_due = valid_until − renewal_lead_days."""
        if self.is_perpetual:
            self.next_renewal_action_due = None
            return
        if self.valid_until and self.renewal_lead_days:
            valid_until = (
                self.valid_until
                if isinstance(self.valid_until, date)
                else frappe.utils.getdate(self.valid_until)
            )
            self.next_renewal_action_due = valid_until - timedelta(
                days=int(self.renewal_lead_days)
            )

    # ──────────────────────────────────────────────
    # Compliance score from conditions table
    # ──────────────────────────────────────────────

    def compute_compliance_score(self):
        """
        % of specific conditions in 'Compliant' state.
        Non-compliant count = conditions in 'Non-Compliant' or 'Partially Compliant'.
        """
        conditions = self.specific_conditions or []
        total = len(conditions)

        if total == 0:
            self.compliance_score = 100.0
            self.non_compliance_open_count = 0
            self.specific_conditions_count = 0
            return

        compliant = sum(1 for c in conditions if c.compliance_status == "Compliant")
        non_compliant = sum(
            1
            for c in conditions
            if c.compliance_status in ("Non-Compliant", "Partially Compliant")
        )

        self.compliance_score = round((compliant / total) * 100, 2)
        self.non_compliance_open_count = non_compliant
        self.specific_conditions_count = total

    # ──────────────────────────────────────────────
    # CTO-specific validations
    # ──────────────────────────────────────────────

    def validate_cto_renewal_lead(self):
        """
        For CTO types: warn if renewal_lead_days < 365.
        PCB may reject renewal applications filed < 365 days before expiry.
        """
        if self.clearance_type and self.clearance_type.startswith("Consent to Operate"):
            if (self.renewal_lead_days or 0) < 365:
                frappe.msgprint(
                    _(
                        "⚠️ For Consent to Operate (CTO), renewal lead time should be ≥ 365 days. "
                        "State PCBs may reject applications filed less than 365 days before expiry."
                    ),
                    title=_("CTO Renewal Lead Warning"),
                    indicator="orange",
                )

    # ──────────────────────────────────────────────
    # Alert threshold: CTO < 365 days
    # ──────────────────────────────────────────────

    def alert_critical_thresholds(self):
        """
        CTO with < 365 days to expiry → create a critical Compliance Calendar Task.
        This is THE most consequential alert in the EHS module.
        """
        if not self.clearance_type:
            return
        if not self.clearance_type.startswith("Consent to Operate"):
            return
        if self.is_perpetual:
            return
        if self.days_to_expiry is None:
            return
        if self.days_to_expiry >= 365:
            return

        # Only create if no open critical task exists
        existing = frappe.db.exists(
            "Compliance Calendar Task",
            {
                "business_entity": self.business_entity,
                "task_title": ["like", f"%CTO {self.clearance_number}%"],
                "status": ["in", ["Open", "In Progress"]],
                "priority": "Urgent",
            },
        )
        if existing:
            return

        _create_cto_critical_task(self)

    def before_save(self):
        """Auto-set status to Expired when past valid_until."""
        if (
            not self.is_perpetual
            and self.days_to_expiry is not None
            and self.days_to_expiry < 0
            and self.clearance_status not in ("Suspended", "Revoked")
        ):
            self.clearance_status = "Expired"


# ──────────────────────────────────────────────────────
# Module-level helpers
# ──────────────────────────────────────────────────────

def _create_cto_critical_task(clearance: "EnvironmentalClearance") -> None:
    try:
        task = frappe.get_doc(
            {
                "doctype": "Compliance Calendar Task",
                "organisation": clearance.organisation,
                "business_entity": clearance.business_entity,
                "task_title": (
                    f"🚨 CTO {clearance.clearance_number} expires in "
                    f"{clearance.days_to_expiry} days — START RENEWAL NOW"
                ),
                "task_type": "CTO Renewal",
                "due_date": clearance.next_renewal_action_due,
                "status": "Open",
                "priority": "Urgent",
                "assigned_to": clearance.responsible_person,
                "reference_doctype": "Environmental Clearance",
                "reference_name": clearance.name,
                "description": (
                    f"CRITICAL: Consent to Operate {clearance.clearance_number} "
                    f"({clearance.clearance_type}) expires on {clearance.valid_until} "
                    f"({clearance.days_to_expiry} days remaining). "
                    "A lapsed CTO = factory closure. Start renewal application immediately. "
                    "PCB requires 365-day lead time."
                ),
            }
        )
        task.insert(ignore_permissions=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Environmental Clearance — CTO task creation failed")


def update_clearance_condition_status(
    clearance_name: str, condition_no: str, new_status: str, evidence: str = None
) -> dict:
    """
    Update a specific condition's compliance status.
    Called from whitelisted API.
    """
    doc = frappe.get_doc("Environmental Clearance", clearance_name)
    updated = False
    for condition in doc.specific_conditions:
        if condition.condition_no == condition_no:
            condition.compliance_status = new_status
            if evidence:
                condition.evidence = evidence
            updated = True
            break

    if not updated:
        frappe.throw(
            _("Condition No. {0} not found in clearance {1}.").format(
                condition_no, clearance_name
            )
        )

    doc.compute_compliance_score()
    doc.save(ignore_permissions=True)
    return {
        "clearance": clearance_name,
        "condition_no": condition_no,
        "new_status": new_status,
        "compliance_score": doc.compliance_score,
    }