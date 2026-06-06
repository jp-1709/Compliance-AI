"""
Operating Licence — Controller
complyai/compliance/ehs_compliance/doctype/operating_licence/operating_licence.py

Handles:
  • days_to_expiry computation
  • next_action_due computation (valid_until − renewal_lead_days)
  • Oversubscription detection (Factory Licence — workers + HP)
  • Renewal Compliance Calendar Task creation (idempotent)
"""

import frappe
from frappe import _
from frappe.model.document import Document
from datetime import date, timedelta


class OperatingLicence(Document):

    # ──────────────────────────────────────────────
    # Lifecycle hooks
    # ──────────────────────────────────────────────

    def validate(self):
        self.validate_dates()
        self.compute_days_to_expiry()
        self.compute_next_action_due()
        self.check_oversubscription()

    def on_update(self):
        self.maybe_create_renewal_task()

    # ──────────────────────────────────────────────
    # Validation helpers
    # ──────────────────────────────────────────────

    def validate_dates(self):
        """valid_from must be ≤ valid_until; issued_on must be ≤ valid_from."""
        if self.valid_from and self.valid_until:
            if self.valid_from > self.valid_until:
                frappe.throw(
                    _("Valid From ({0}) cannot be after Valid Until ({1}).").format(
                        self.valid_from, self.valid_until
                    )
                )
        if self.issued_on and self.valid_from:
            if self.issued_on > self.valid_from:
                frappe.throw(
                    _("Issued On ({0}) cannot be after Valid From ({1}).").format(
                        self.issued_on, self.valid_from
                    )
                )

    # ──────────────────────────────────────────────
    # Computed fields
    # ──────────────────────────────────────────────

    def compute_days_to_expiry(self):
        """Positive = days remaining; negative = days overdue."""
        if self.valid_until:
            self.days_to_expiry = (
                self.valid_until
                if isinstance(self.valid_until, date)
                else frappe.utils.getdate(self.valid_until)
            ) - date.today()
            self.days_to_expiry = self.days_to_expiry.days

    def compute_next_action_due(self):
        """next_action_due = valid_until − renewal_lead_days."""
        if self.valid_until and self.renewal_lead_days:
            valid_until = (
                self.valid_until
                if isinstance(self.valid_until, date)
                else frappe.utils.getdate(self.valid_until)
            )
            self.next_action_due = valid_until - timedelta(days=int(self.renewal_lead_days))

    # ──────────────────────────────────────────────
    # Oversubscription detection
    # ──────────────────────────────────────────────

    def check_oversubscription(self):
        """
        Factory Licence only:
          actual workers > authorised  →  is_oversubscribed
          actual HP > authorised HP    →  is_oversubscribed
        Amendment required under Factories Act §6.
        """
        if self.licence_type != "Factory Licence":
            self.is_oversubscribed = 0
            return

        oversub_workers = (
            (self.current_workers_actual or 0) > (self.max_workers_authorised or 0)
            if self.max_workers_authorised
            else False
        )
        oversub_hp = (
            (self.current_horsepower_actual or 0) > (self.max_horsepower_authorised or 0)
            if self.max_horsepower_authorised
            else False
        )

        self.is_oversubscribed = 1 if (oversub_workers or oversub_hp) else 0

        if self.is_oversubscribed:
            reasons = []
            if oversub_workers:
                reasons.append(
                    _("Workers: {0} actual > {1} authorised").format(
                        self.current_workers_actual, self.max_workers_authorised
                    )
                )
            if oversub_hp:
                reasons.append(
                    _("HP: {0} actual > {1} authorised").format(
                        self.current_horsepower_actual, self.max_horsepower_authorised
                    )
                )
            frappe.msgprint(
                _(
                    "⚠️ Factory Licence oversubscribed — licence amendment required under Factories Act §6.<br>{0}"
                ).format("<br>".join(reasons)),
                title=_("Oversubscription Warning"),
                indicator="orange",
            )

    # ──────────────────────────────────────────────
    # Renewal task creation
    # ──────────────────────────────────────────────

    def maybe_create_renewal_task(self):
        """
        When next_action_due ≤ today and licence is Active and renewal not yet filed →
        create a Compliance Calendar Task (idempotent).
        """
        if not self.next_action_due:
            return
        if self.licence_status != "Active":
            return
        if self.renewal_application_filed_on:
            return  # already filed

        action_due = (
            self.next_action_due
            if isinstance(self.next_action_due, date)
            else frappe.utils.getdate(self.next_action_due)
        )
        if action_due > date.today():
            return  # not yet due

        task_title_pattern = f"%Renew {self.licence_type}%{self.licence_number}%"
        existing = frappe.db.exists(
            "Compliance Calendar Task",
            {
                "business_entity": self.business_entity,
                "task_title": ["like", task_title_pattern],
                "status": ["in", ["Open", "In Progress"]],
            },
        )
        if existing:
            return  # idempotent — task already open

        _create_renewal_task(self)

    # ──────────────────────────────────────────────
    # Status auto-update
    # ──────────────────────────────────────────────

    def before_save(self):
        """Auto-set status to Expired when days_to_expiry < 0."""
        if self.days_to_expiry is not None and self.days_to_expiry < 0:
            if self.licence_status == "Active":
                self.licence_status = "Expired"


# ──────────────────────────────────────────────────────
# Module-level helpers
# ──────────────────────────────────────────────────────

def _create_renewal_task(licence: "OperatingLicence") -> None:
    """Create a Compliance Calendar Task for licence renewal."""
    try:
        task = frappe.get_doc(
            {
                "doctype": "Compliance Calendar Task",
                "organisation": licence.organisation,
                "business_entity": licence.business_entity,
                "task_title": f"Renew {licence.licence_type} — {licence.licence_number}",
                "task_type": "Licence Renewal",
                "due_date": licence.next_action_due,
                "status": "Open",
                "priority": _derive_priority(licence.days_to_expiry),
                "assigned_to": licence.responsible_person,
                "reference_doctype": "Operating Licence",
                "reference_name": licence.name,
                "description": (
                    f"Renewal due for {licence.licence_type} — {licence.licence_number} "
                    f"issued by {licence.issuing_authority}. "
                    f"Valid until: {licence.valid_until}. "
                    f"Days remaining: {licence.days_to_expiry}."
                ),
            }
        )
        task.insert(ignore_permissions=True)
        frappe.db.set_value("Operating Licence", licence.name, "amended_from", licence.name)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Operating Licence — renewal task creation failed")


def _derive_priority(days_to_expiry: int) -> str:
    if days_to_expiry is None:
        return "Medium"
    if days_to_expiry <= 30:
        return "Urgent"
    if days_to_expiry <= 90:
        return "High"
    return "Medium"


# ──────────────────────────────────────────────────────
# Scheduled task: recompute expiry daily
# ──────────────────────────────────────────────────────

def recompute_licence_expiry():
    """
    Scheduled daily at 02:00.
    Refresh days_to_expiry and next_action_due for all non-expired licences.
    """
    licences = frappe.get_all(
        "Operating Licence",
        filters={"licence_status": ["not in", ["Revoked", "Surrendered"]]},
        fields=["name", "valid_until", "renewal_lead_days"],
    )
    today = date.today()
    for lic in licences:
        if not lic.valid_until:
            continue
        valid_until = frappe.utils.getdate(lic.valid_until)
        days_to_expiry = (valid_until - today).days
        next_action_due = (
            valid_until - timedelta(days=int(lic.renewal_lead_days or 180))
        )
        updates = {
            "days_to_expiry": days_to_expiry,
            "next_action_due": next_action_due,
        }
        if days_to_expiry < 0:
            updates["licence_status"] = "Expired"
        frappe.db.set_value("Operating Licence", lic.name, updates, update_modified=False)

    frappe.db.commit()