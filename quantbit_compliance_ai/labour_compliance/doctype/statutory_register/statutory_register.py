"""
complyai/compliance/labour_compliance/doctype/statutory_register/statutory_register.py

Controller for Statutory Register.

Key behaviour:
- on_update()      : compute destroy_after from retention + earliest entry.
- is_stale()       : returns True if active register has no entry in 90+ days.
- Unique index on (organisation, business_entity, register_code) enforced at DB level.
"""

import frappe
from frappe.model.document import Document
from frappe.utils import today, getdate
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta


class StatutoryRegister(Document):

    # ──────────────────────────────────────────────────────────────────
    # LIFECYCLE HOOKS
    # ──────────────────────────────────────────────────────────────────

    def validate(self):
        self.validate_register_code_unique()

    def on_update(self):
        self.compute_destroy_after()
        self.refresh_earliest_entry_date()

    # ──────────────────────────────────────────────────────────────────
    # VALIDATION
    # ──────────────────────────────────────────────────────────────────

    def validate_register_code_unique(self):
        """
        Enforce unique (business_entity, register_code) at application level
        in addition to the DB unique index, to give a cleaner user-facing error.
        """
        if self.is_new():
            existing = frappe.db.exists(
                "Statutory Register",
                {
                    "business_entity": self.business_entity,
                    "register_code": self.register_code,
                },
            )
            if existing:
                frappe.throw(
                    f"Register '{self.register_code}' already exists for entity "
                    f"'{self.business_entity}'. Each register code must be unique per entity.",
                    frappe.UniqueValidationError,
                )

    # ──────────────────────────────────────────────────────────────────
    # COMPUTED FIELDS
    # ──────────────────────────────────────────────────────────────────

    def compute_destroy_after(self):
        """destroy_after = earliest_entry_date + retention_years."""
        if self.earliest_entry_date and self.retention_years:
            self.destroy_after = getdate(self.earliest_entry_date) + relativedelta(
                years=self.retention_years
            )

    def refresh_earliest_entry_date(self):
        """
        Fetch the oldest Register Entry date for this register.
        Updates last_entry_date as well (most recent entry).
        Called on_update; also called by Register Entry on_insert.
        """
        result = frappe.db.sql(
            """
            SELECT MIN(entry_date) as earliest, MAX(entry_date) as latest
            FROM `tabRegister Entry`
            WHERE register = %s
            """,
            self.name,
            as_dict=True,
        )
        if result and result[0].earliest:
            self.db_set("earliest_entry_date", result[0].earliest, update_modified=False)
            self.db_set("last_entry_date", result[0].latest, update_modified=False)
            self.compute_destroy_after()
            self.db_set("destroy_after", self.destroy_after, update_modified=False)

    # ──────────────────────────────────────────────────────────────────
    # BUSINESS LOGIC
    # ──────────────────────────────────────────────────────────────────

    def is_stale(self) -> bool:
        """
        A register is stale if it is active but has had no entry in the last 90 days.
        Used by the daily staleness detection job.
        """
        if not self.is_active_register:
            return False
        if not self.last_entry_date:
            # Never had an entry — stale from day 1 if register is supposed to be active
            return True
        return (date.today() - getdate(self.last_entry_date)) > timedelta(days=90)