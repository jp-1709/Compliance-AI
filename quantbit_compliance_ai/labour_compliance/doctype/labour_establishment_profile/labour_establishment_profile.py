"""
complyai/compliance/labour_compliance/doctype/labour_establishment_profile/labour_establishment_profile.py

Controller for Labour Establishment Profile.

Drives applicability of all labour obligations for a Business Entity.
Key behaviour:
- validate()    : workforce arithmetic, factory power choice, completeness.
- on_update()   : threshold crossing → recompute obligations; committee warnings.
"""

import frappe
from frappe.model.document import Document
from frappe.utils import today, add_days, getdate
from dateutil.relativedelta import relativedelta

WORKER_THRESHOLDS = [10, 20, 30, 50, 100, 250, 500, 1000]

# Key fields for completeness calculation
_COMPLETENESS_FIELDS = [
    "establishment_type", "total_workers", "direct_employees", "contract_workers",
    "women_workers", "shifts_count", "epfo_registered", "esic_registered",
    "professional_tax_registered", "shops_estab_registered", "posh_ic_constituted",
    "operations_start_date", "weekly_off_day",
]


class LabourEstablishmentProfile(Document):

    # ──────────────────────────────────────────────────────────────────
    # LIFECYCLE HOOKS
    # ──────────────────────────────────────────────────────────────────

    def validate(self):
        self.validate_workforce_arithmetic()
        self.validate_factory_power_choice()
        self.compute_review_due_date()
        self.compute_profile_completeness()

    def on_update(self):
        self.maybe_recompute_obligations()
        self.warn_threshold_committees()

    # ──────────────────────────────────────────────────────────────────
    # VALIDATION METHODS
    # ──────────────────────────────────────────────────────────────────

    def validate_workforce_arithmetic(self):
        """
        total_workers should roughly equal direct + contract + apprentices.
        Warn on mismatch > 5 (tolerance for flex workers).
        Women workers cannot exceed direct + contract.
        """
        direct = self.direct_employees or 0
        contract = self.contract_workers or 0
        apprentices = self.apprentices or 0
        computed_total = direct + contract + apprentices

        if self.total_workers and abs(self.total_workers - computed_total) > 5:
            frappe.msgprint(
                f"Total workers ({self.total_workers}) doesn't match sum of components "
                f"(direct {direct} + contract {contract} + apprentices {apprentices} = {computed_total}). "
                "Please verify — interns/trainees may need to be included separately.",
                alert=True,
                indicator="orange",
            )

        if (self.women_workers or 0) > (direct + contract):
            frappe.throw(
                "Women workers cannot exceed direct employees + contract workers.",
                frappe.ValidationError,
            )

    def validate_factory_power_choice(self):
        """
        A factory must declare whether it uses power or not — but not both.
        Factories Act triggers at 10+ workers (with power) or 20+ workers (without power).
        """
        if self.establishment_type == "Factory":
            if self.uses_power and self.without_power_workers_only:
                frappe.throw(
                    "A factory cannot be both 'manufacturing with power' and 'manufacturing without power'. "
                    "Choose one — this determines the Factories Act threshold (10 vs 20 workers).",
                    frappe.ValidationError,
                )
            if not (self.uses_power or self.without_power_workers_only):
                frappe.throw(
                    "For Factory establishment type, you must specify whether manufacturing uses power. "
                    "This determines which Factories Act threshold applies.",
                    frappe.ValidationError,
                )

    def compute_review_due_date(self):
        """Profile should be reviewed at least annually. review_due_on = last_reviewed_on + 365 days."""
        if self.last_reviewed_on:
            self.review_due_on = getdate(self.last_reviewed_on) + relativedelta(years=1)

    def compute_profile_completeness(self):
        """Compute % of key profile fields that are filled."""
        filled = sum(1 for f in _COMPLETENESS_FIELDS if getattr(self, f, None) not in (None, 0, ""))
        self.profile_completeness = int((filled / len(_COMPLETENESS_FIELDS)) * 100)

    # ──────────────────────────────────────────────────────────────────
    # THRESHOLD CROSSING
    # ──────────────────────────────────────────────────────────────────

    def maybe_recompute_obligations(self):
        """
        When total_workers crosses a threshold (10, 20, 30, 50, 100, 250, 500, 1000),
        enqueue a background job to re-run the Applicability Engine for this entity.
        Works in BOTH directions: thresholds crossed upward add obligations;
        thresholds crossed downward may remove obligations (marked Not Applicable).
        """
        prev = self.get_doc_before_save()
        old_total = prev.total_workers if prev else 0
        new_total = self.total_workers or 0

        crossed = [
            t for t in WORKER_THRESHOLDS
            if (old_total < t <= new_total) or (new_total < t <= old_total)
        ]
        if crossed:
            frappe.enqueue(
                "complyai.compliance.compliance_calendar.api.generate_calendar_for_entity",
                queue="long",
                job_name=f"recompute_obligations_{self.business_entity}",
                business_entity=self.business_entity,
                timeout=600,
            )
            frappe.msgprint(
                f"Workforce crossed threshold(s): {crossed}. "
                "Calendar obligations will be recomputed in the background.",
                alert=True,
            )

    # ──────────────────────────────────────────────────────────────────
    # COMMITTEE & FACILITY THRESHOLD WARNINGS
    # ──────────────────────────────────────────────────────────────────

    def warn_threshold_committees(self):
        """
        Generate Compliance Calendar Task warnings when statutory requirements
        are triggered by workforce thresholds but not yet fulfilled.

        Thresholds and their requirements:
         10+  workers : POSH IC mandatory
         30+  women   : Crèche required (Maternity Benefit Act)
        100+  workers : Works Committee mandatory (ID Act §3)
        250+  workers : Canteen + Safety Committee
        500+  workers : Welfare Officer
        1000+ workers : Safety Officer
        """
        total = self.total_workers or 0
        women = self.women_workers or 0

        checks = [
            (total >= 10 and not self.posh_ic_constituted,
             "POSH IC must be constituted — mandatory at 10+ employees (any gender). Fine: ₹50,000."),
            (women >= 30 and not self.has_creche,
             "Crèche required — 30+ women workers (Maternity Benefit Act §11A). Install or obtain exemption."),
            (total >= 100 and not self.works_committee_constituted,
             "Works Committee mandatory — 100+ workers (Industrial Disputes Act §3)."),
            (total >= 250 and not self.has_canteen,
             "Canteen required — 250+ workers (Factories Act §46). Set up or apply for exemption."),
            (total >= 250 and not self.safety_committee_constituted,
             "Safety Committee recommended — 250+ workers or hazardous factory."),
            (total >= 500 and not self.has_welfare_officer,
             "Welfare Officer required — 500+ workers (Factories Act §49)."),
            (total >= 1000 and not self.has_safety_officer,
             "Safety Officer required — 1000+ workers (Factories Act §40B)."),
        ]

        for condition, message in checks:
            if condition:
                _create_warning_task(self.business_entity, message)


def _create_warning_task(business_entity: str, message: str) -> None:
    """
    Create a Compliance Calendar Task warning if one doesn't already exist
    for this exact message + entity (idempotent).
    """
    if frappe.db.exists(
        "Compliance Calendar Task",
        {"business_entity": business_entity, "task_title": message, "status": ("!=", "Completed")},
    ):
        return

    org = frappe.db.get_value("Business Entity", business_entity, "organisation")
    if not org:
        return

    try:
        frappe.get_doc(
            {
                "doctype": "Compliance Calendar Task",
                "organisation": org,
                "business_entity": business_entity,
                "task_title": message,
                "period_start": today(),
                "period_end": today(),
                "due_date": today(),
                "assigned_to": frappe.session.user,
                "status": "Open",
                "risk_level": "High",
                "category": "Labour",
            }
        ).insert(ignore_permissions=True)
    except Exception as e:
        frappe.log_error(f"Warning task creation failed for {business_entity}: {e}", "LabourProfile")