"""
BOCW Compliance — Controller
complyai/compliance/ehs_compliance/doctype/bocw_compliance/bocw_compliance.py

Handles:
  • cess_amount_estimated_inr = estimated_construction_cost_inr × (cess_rate_pct / 100)
  • registration_compliance_pct = registered / total_workers × 100
  • duration_days computation
  • BOCW ≥₹10 lakh threshold check
  • 10-worker registration obligation warning
  • Form-I obligation on project start
"""

import frappe
from frappe import _
from frappe.model.document import Document
from datetime import date

BOCW_COST_THRESHOLD_INR: float = 10_00_000   # ₹10 lakh
BOCW_WORKER_THRESHOLD: int = 10              # 10 workers → registration mandatory
SITE_SAFETY_OFFICER_COST_THRESHOLD: float = 10_00_00_000  # ₹10 crore


class BOCWCompliance(Document):

    # ──────────────────────────────────────────────
    # Lifecycle hooks
    # ──────────────────────────────────────────────

    def validate(self):
        self.validate_cost_threshold()
        self.compute_cess_amount()
        self.compute_registration_compliance()
        self.compute_duration()
        self.check_worker_registration_obligation()
        self.check_site_safety_officer()

    def before_save(self):
        self._auto_update_project_status()

    def on_submit(self):
        self.validate_evidence_on_submit()

    # ──────────────────────────────────────────────
    # BOCW threshold check
    # ──────────────────────────────────────────────

    def validate_cost_threshold(self):
        """
        BOCW applies only to construction ≥ ₹10 lakh.
        Warn if below threshold — record may not be necessary.
        """
        if (self.estimated_construction_cost_inr or 0) < BOCW_COST_THRESHOLD_INR:
            frappe.msgprint(
                _(
                    "Estimated construction cost (₹{0:,.0f}) is below the BOCW threshold "
                    "of ₹10 lakh. BOCW cess obligations may not apply."
                ).format(self.estimated_construction_cost_inr or 0),
                title=_("BOCW Threshold Check"),
                indicator="blue",
            )

    # ──────────────────────────────────────────────
    # Cess computation
    # ──────────────────────────────────────────────

    def compute_cess_amount(self):
        """
        cess_amount_estimated_inr = estimated_construction_cost_inr × (cess_rate_pct / 100)
        Default cess rate: 1% (statutory).
        """
        cost = self.estimated_construction_cost_inr or 0
        rate = self.cess_rate_pct or 1.0
        self.cess_amount_estimated_inr = round(cost * (rate / 100), 2)

    # ──────────────────────────────────────────────
    # Worker registration compliance
    # ──────────────────────────────────────────────

    def compute_registration_compliance(self):
        """
        registration_compliance_pct = (registered / total) × 100
        If no workers engaged → 100% (vacuous truth).
        """
        total = self.total_workers_engaged or 0
        registered = self.workers_registered_with_welfare_board or 0

        if total <= 0:
            self.registration_compliance_pct = 100.0
        else:
            self.registration_compliance_pct = round((registered / total) * 100, 2)

    # ──────────────────────────────────────────────
    # Duration computation
    # ──────────────────────────────────────────────

    def compute_duration(self):
        """
        duration_days = (actual_end or estimated_end or today) − start_date
        """
        if not self.project_start_date:
            self.duration_days = 0
            return

        start = (
            self.project_start_date
            if isinstance(self.project_start_date, date)
            else frappe.utils.getdate(self.project_start_date)
        )
        end = (
            self.project_actual_end_date
            or self.project_estimated_end_date
        )

        if end:
            end = end if isinstance(end, date) else frappe.utils.getdate(end)
        else:
            end = date.today()

        self.duration_days = max(0, (end - start).days)

    # ──────────────────────────────────────────────
    # Worker registration obligation warning
    # ──────────────────────────────────────────────

    def check_worker_registration_obligation(self):
        """
        If 10+ workers on site → all must be registered with State BOCW Welfare Board.
        Threshold is 10 workers (not 11).
        """
        total = self.total_workers_engaged or 0
        registered = self.workers_registered_with_welfare_board or 0

        if total >= BOCW_WORKER_THRESHOLD and registered < total:
            unregistered = total - registered
            frappe.msgprint(
                _(
                    "⚠️ {0} worker(s) not yet registered with BOCW Welfare Board. "
                    "Registration is mandatory for all {1} workers (10+ on site threshold)."
                ).format(unregistered, total),
                title=_("BOCW Worker Registration"),
                indicator="orange",
            )

    # ──────────────────────────────────────────────
    # Site Safety Officer check
    # ──────────────────────────────────────────────

    def check_site_safety_officer(self):
        """Mandatory for projects ≥ ₹10 crore."""
        cost = self.estimated_construction_cost_inr or 0
        if cost >= SITE_SAFETY_OFFICER_COST_THRESHOLD:
            if not self.site_safety_officer_appointed:
                frappe.msgprint(
                    _(
                        "⚠️ Site Safety Officer is mandatory for construction projects ≥ ₹10 crore. "
                        "Please appoint one and update this record."
                    ),
                    title=_("Site Safety Officer Required"),
                    indicator="orange",
                )

    # ──────────────────────────────────────────────
    # Status auto-update
    # ──────────────────────────────────────────────

    def _auto_update_project_status(self):
        if self.project_actual_end_date:
            actual_end = frappe.utils.getdate(self.project_actual_end_date)
            if actual_end <= date.today() and self.project_status == "In Progress":
                self.project_status = "Completed"

    # ──────────────────────────────────────────────
    # Submit validation
    # ──────────────────────────────────────────────

    def validate_evidence_on_submit(self):
        errors = []
        if not self.cess_paid_evidence and (self.cess_amount_estimated_inr or 0) > 0:
            errors.append(_("Cess Payment Receipt is required"))
        if not self.labour_dept_intimation_filed:
            errors.append(_("Form-I (Labour Department Intimation) must be filed before submission"))
        if errors:
            frappe.throw("<br>".join(errors), title=_("Submission Validation Failed"))


# ──────────────────────────────────────────────────────
# Whitelisted API
# ──────────────────────────────────────────────────────

@frappe.whitelist()
def record_cess_payment(project: str, amount_inr: float, payment_evidence: str) -> dict:
    """
    Log cess payment against a BOCW project.
    Updates cess_amount_paid_inr and attaches evidence.
    """
    amount_inr = float(amount_inr)
    doc = frappe.get_doc("BOCW Compliance", project)

    current_paid = doc.cess_amount_paid_inr or 0
    doc.cess_amount_paid_inr = current_paid + amount_inr
    doc.cess_paid_evidence = payment_evidence

    doc.save(ignore_permissions=True)

    remaining = max(0, (doc.cess_amount_estimated_inr or 0) - doc.cess_amount_paid_inr)
    return {
        "project": project,
        "total_paid": doc.cess_amount_paid_inr,
        "cess_estimated": doc.cess_amount_estimated_inr,
        "remaining": remaining,
        "fully_paid": remaining == 0,
    }