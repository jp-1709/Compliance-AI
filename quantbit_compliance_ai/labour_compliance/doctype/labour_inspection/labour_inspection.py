"""
complyai/compliance/labour_compliance/doctype/labour_inspection/labour_inspection.py

Controller for Labour Inspection.

Key behaviour:
- validate()    : validate response due date; warn on missing observation letter.
- on_update()   : create follow-up Compliance Calendar Task for open observations.
- on_submit()   : if penalty > 0, create QMS Quality Event for CAPA tracking.
"""

import frappe
from frappe.model.document import Document
from frappe.utils import getdate, today
from datetime import date, timedelta


class LabourInspection(Document):

    # ──────────────────────────────────────────────────────────────────
    # LIFECYCLE HOOKS
    # ──────────────────────────────────────────────────────────────────

    def validate(self):
        self.validate_response_due_date()
        self.warn_missing_observation_letter()

    def on_update(self):
        if self.inspection_status == "Observations Issued":
            self.create_response_task()

    def on_submit(self):
        if (self.penalty_imposed_inr or 0) > 0:
            self.create_qms_quality_event()

    # ──────────────────────────────────────────────────────────────────
    # VALIDATION
    # ──────────────────────────────────────────────────────────────────

    def validate_response_due_date(self):
        """Response due date must be after inspection date."""
        if self.response_due_date and self.inspection_date:
            if getdate(self.response_due_date) < getdate(self.inspection_date):
                frappe.throw(
                    "Response due date cannot be before the inspection date.",
                    frappe.ValidationError,
                )

    def warn_missing_observation_letter(self):
        """If observations exist but no observation letter, warn."""
        if self.observations and not self.observation_letter_evidence:
            frappe.msgprint(
                "Observations are recorded but no observation letter/citation has been attached. "
                "Request and upload the official observation letter from the inspector.",
                alert=True,
                indicator="orange",
            )

    # ──────────────────────────────────────────────────────────────────
    # FOLLOW-UP TASK
    # ──────────────────────────────────────────────────────────────────

    def create_response_task(self):
        """
        When observations are issued, create a Compliance Calendar Task
        to track the response. Idempotent.
        """
        task_title = f"Respond to {self.inspection_type} Inspection — {self.inspection_date}"
        if frappe.db.exists(
            "Compliance Calendar Task",
            {"business_entity": self.business_entity, "task_title": task_title},
        ):
            return

        org = frappe.db.get_value("Business Entity", self.business_entity, "organisation")
        if not org:
            return

        due = self.response_due_date or (date.today() + timedelta(days=15))

        try:
            frappe.get_doc(
                {
                    "doctype": "Compliance Calendar Task",
                    "organisation": org,
                    "business_entity": self.business_entity,
                    "task_title": task_title,
                    "period_start": str(self.inspection_date),
                    "period_end": str(due),
                    "due_date": str(due),
                    "assigned_to": frappe.session.user,
                    "status": "Open",
                    "risk_level": "High",
                    "category": "Labour",
                    "linked_qms_event": None,
                }
            ).insert(ignore_permissions=True)
        except Exception as e:
            frappe.log_error(
                f"Failed to create inspection response task for {self.name}: {e}",
                "LabourInspection",
            )

    # ──────────────────────────────────────────────────────────────────
    # QMS QUALITY EVENT
    # ──────────────────────────────────────────────────────────────────

    def create_qms_quality_event(self):
        """
        When an inspection results in a penalty, create a QMS Quality Event
        to track the non-conformance and trigger a CAPA.
        Only creates if QMS Quality Event DocType exists (C6 module).
        """
        if not frappe.db.exists("DocType", "QMS Quality Event"):
            return

        try:
            event = frappe.get_doc(
                {
                    "doctype": "QMS Quality Event",
                    "title": (
                        f"Labour Inspection Penalty — {self.inspection_type} — "
                        f"{self.inspection_date}"
                    ),
                    "event_type": "Regulatory Non-Compliance",
                    "business_entity": self.business_entity,
                    "organisation": self.organisation,
                    "severity": "Major",
                    "description": (
                        f"Penalty of ₹{self.penalty_imposed_inr:,.0f} imposed by "
                        f"{self.inspecting_authority} during {self.inspection_mode} "
                        f"inspection on {self.inspection_date}. "
                        f"Inspection record: {self.name}."
                    ),
                    "linked_compliance_task": self.name,
                }
            )
            event.insert(ignore_permissions=True)

            # Link CAPA back to inspection
            if not self.linked_capa:
                self.db_set("linked_capa", None)  # Placeholder; real CAPA created from event

            frappe.msgprint(
                f"QMS Quality Event {event.name} created for inspection penalty. "
                "Assign a CAPA to track corrective action.",
                alert=True,
            )
        except Exception as e:
            frappe.log_error(
                f"QMS Quality Event creation failed for inspection {self.name}: {e}",
                "LabourInspection",
            )

    # ──────────────────────────────────────────────────────────────────
    # PENDING OBSERVATIONS COUNT
    # ──────────────────────────────────────────────────────────────────

    def pending_observations_count(self) -> int:
        """Count observations with status 'Pending'."""
        return sum(
            1 for obs in (self.observations or [])
            if obs.observation_status == "Pending"
        )