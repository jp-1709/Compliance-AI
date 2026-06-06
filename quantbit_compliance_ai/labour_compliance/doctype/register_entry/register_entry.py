"""
complyai/compliance/labour_compliance/doctype/register_entry/register_entry.py

Controller for Register Entry.

Key behaviour:
- validate()   : validate entry_payload is valid JSON; run per-register schema check.
- on_insert()  : refresh parent register's last_entry_date and earliest_entry_date.
"""

import json
import frappe
from frappe.model.document import Document
from frappe.utils import getdate

# Per-register schema: required keys whose values must be non-null/non-zero.
# Add more register codes as schemas are defined.
REGISTER_SCHEMAS: dict = {
    "FORM-A-MW": {
        "required_keys": ["basic", "da", "gross", "net", "skill_category"],
        "description": "Form A — Register of Wages (Minimum Wages Act): basic, da, gross, net, skill_category required.",
    },
    "FORM-B-MW": {
        "required_keys": ["wage_card_no", "employee_name", "department"],
        "description": "Form B — Wage Cards (Minimum Wages Act).",
    },
    "FORM-C-MW": {
        "required_keys": ["gross", "deductions", "net", "signature"],
        "description": "Form C — Wage Slip.",
    },
    "FORM-D-MW": {
        "required_keys": ["fine_amount", "reason", "date_of_offence"],
        "description": "Form D — Register of Fines.",
    },
    "FORM-E-MW": {
        "required_keys": ["deduction_type", "amount", "reason"],
        "description": "Form E — Register of Deductions.",
    },
    "FORM-12-FA": {
        "required_keys": ["date_of_entry", "attendance", "hours_worked"],
        "description": "Form 12 — Register of Adult Workers (Factories Act).",
    },
    "FORM-13-FA": {
        "required_keys": ["dob", "school_cert_no", "work_start_time", "work_end_time"],
        "description": "Form 13 — Register of Adolescent Workers (Factories Act). DOB and school certificate required.",
    },
    "FORM-14-FA": {
        "required_keys": ["medical_exam_date", "fitness_certificate_no"],
        "description": "Form 14 — Health Register (Factories Act).",
    },
    "FORM-15-FA": {
        "required_keys": ["date", "attendance"],
        "description": "Form 15 — Muster Roll (Factories Act).",
    },
    "FORM-XII-CL": {
        "required_keys": ["contractor_name", "date_of_entry", "attendance"],
        "description": "Form XII — Register of Workers (Contract Labour Act).",
    },
    "FORM-XIV-CL": {
        "required_keys": ["basic", "gross", "net"],
        "description": "Form XIV — Register of Wages (Contract Labour Act).",
    },
    "FORM-A-PB": {
        "required_keys": ["allocable_surplus", "bonus_payable", "set_on", "set_off"],
        "description": "Form A — Register of Bonus (Payment of Bonus Act).",
    },
}


class RegisterEntry(Document):

    # ──────────────────────────────────────────────────────────────────
    # LIFECYCLE HOOKS
    # ──────────────────────────────────────────────────────────────────

    def validate(self):
        self.validate_payload_is_json()
        self.validate_payload_schema()

    def on_insert(self):
        self.refresh_parent_register()

    def on_update(self):
        self.refresh_parent_register()

    # ──────────────────────────────────────────────────────────────────
    # VALIDATION
    # ──────────────────────────────────────────────────────────────────

    def validate_payload_is_json(self):
        """entry_payload must be valid JSON."""
        if not self.entry_payload:
            return
        if isinstance(self.entry_payload, dict):
            return  # Already parsed (e.g. during programmatic insert)
        try:
            parsed = json.loads(self.entry_payload)
            if not isinstance(parsed, dict):
                frappe.throw(
                    "Entry payload must be a JSON object (dict), not an array or scalar.",
                    frappe.ValidationError,
                )
        except (json.JSONDecodeError, TypeError) as e:
            frappe.throw(
                f"Entry payload is not valid JSON: {e}. "
                "See register schema documentation for the required format.",
                frappe.ValidationError,
            )

    def validate_payload_schema(self):
        """
        Check that all required keys for the register type are present and non-empty.
        Flags entry as anomaly if required keys are missing.
        """
        if not self.entry_payload:
            return

        try:
            payload = (
                json.loads(self.entry_payload)
                if isinstance(self.entry_payload, str)
                else self.entry_payload
            )
        except Exception:
            return  # Already caught in validate_payload_is_json

        register_code = frappe.db.get_value(
            "Statutory Register", self.register, "register_code"
        )
        schema = REGISTER_SCHEMAS.get(register_code)
        if not schema:
            return  # No schema defined — pass through

        missing_keys = [
            k for k in schema["required_keys"]
            if k not in payload or payload[k] is None or payload[k] == ""
        ]

        if missing_keys:
            self.is_anomaly = 1
            self.anomaly_reason = (
                f"Missing required fields for {register_code}: {', '.join(missing_keys)}. "
                f"Schema: {schema['description']}"
            )
        else:
            # Clear anomaly if previously flagged and now corrected
            if self.is_anomaly and "Missing required fields" in (self.anomaly_reason or ""):
                self.is_anomaly = 0
                self.anomaly_reason = None

    # ──────────────────────────────────────────────────────────────────
    # PARENT REFRESH
    # ──────────────────────────────────────────────────────────────────

    def refresh_parent_register(self):
        """
        Update last_entry_date on the parent Statutory Register.
        Lightweight — only updates if this entry date is newer than the stored one.
        """
        if not self.register or not self.entry_date:
            return
        try:
            reg = frappe.get_doc("Statutory Register", self.register)
            if not reg.last_entry_date or getdate(self.entry_date) > getdate(reg.last_entry_date):
                frappe.db.set_value(
                    "Statutory Register",
                    self.register,
                    "last_entry_date",
                    self.entry_date,
                    update_modified=False,
                )
            # Update earliest_entry_date
            if not reg.earliest_entry_date or getdate(self.entry_date) < getdate(reg.earliest_entry_date):
                frappe.db.set_value(
                    "Statutory Register",
                    self.register,
                    "earliest_entry_date",
                    self.entry_date,
                    update_modified=False,
                )
        except Exception as e:
            frappe.log_error(f"Failed to refresh register {self.register}: {e}", "RegisterEntry")