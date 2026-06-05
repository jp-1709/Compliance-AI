"""
business_entity.py — Frappe DocType controller for Business Entity (F2)
Quantbit Compliance AI · Foundation Module

Validations:
  - Only one is_principal_entity per Organisation
  - pincode must be 6 digits
  - women_employee_count + differently_abled_count <= employee_count + contract_worker_count
  - is_hazardous triggers notification to Compliance Officers
  - parent_entity must belong to the same Organisation
  - Hard-delete blocked if any compliance records exist (soft-delete only)
  - MSME number format if is_msme is checked
  - GSTIN format if provided
"""

import re
import frappe
from frappe import _
from frappe.model.document import Document

# ─── Regex constants ────────────────────────────────────────────────────────
PINCODE_RE   = re.compile(r"^\d{6}$")
GSTIN_RE     = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$")
UDYAM_RE     = re.compile(r"^UDYAM-[A-Z]{2}-\d{2}-\d{7}$")
EPFO_RE      = re.compile(r"^[A-Z]{2}/[A-Z]+/[0-9]+/[0-9]+$")   # loose format

# DocTypes that scope to business_entity (block hard-delete if any linked)
LINKED_DOCTYPES = [
    "Evidence File",
    "Compliance Calendar Task",
]


class BusinessEntity(Document):
    # ─── Lifecycle hooks ────────────────────────────────────────────────────

    def validate(self):
        self.validate_only_one_principal_entity()
        self.validate_pincode()
        self.validate_workforce_counts()
        self.validate_msme()
        self.validate_gstin()
        self.validate_parent_entity()
        self.validate_latitude_longitude()

    def before_insert(self):
        self.check_hazardous_notification()

    def before_delete(self):
        self.block_hard_delete_if_linked()

    def on_update(self):
        if self.has_value_changed("is_hazardous") and self.is_hazardous:
            self.check_hazardous_notification()

    # ─── Field validators ───────────────────────────────────────────────────

    def validate_only_one_principal_entity(self):
        """
        Business rule: Only one Principal Entity (HQ) is allowed per Organisation.
        Enforces F-US-1 / F2 spec.
        """
        if not self.is_principal_entity:
            return

        existing = frappe.db.get_value(
            "Business Entity",
            {
                "organisation": self.organisation,
                "is_principal_entity": 1,
                "name": ("!=", self.name or "__new__"),
                "is_active": 1,
            },
            "name",
        )
        if existing:
            frappe.throw(
                _(
                    "There can be only one Principal Entity per Organisation. "
                    "'{0}' is already the Principal Entity for '{1}'. "
                    "Please unset it first."
                ).format(existing, self.organisation),
                frappe.ValidationError,
                title=_("Duplicate Principal Entity"),
            )

    def validate_pincode(self):
        """PIN Code must be exactly 6 digits."""
        if not self.pincode:
            frappe.throw(_("PIN Code is mandatory."), frappe.MandatoryError)
        if not PINCODE_RE.match(str(self.pincode).strip()):
            frappe.throw(
                _("PIN Code '{0}' must be exactly 6 digits.").format(self.pincode),
                frappe.ValidationError,
                title=_("Invalid PIN Code"),
            )

    def validate_workforce_counts(self):
        """
        Sanity check: subset counts cannot exceed total headcount.
        women + differently_abled <= employees + contract_workers
        """
        employees  = int(self.employee_count or 0)
        contracts  = int(self.contract_worker_count or 0)
        women      = int(self.women_employee_count or 0)
        d_abled    = int(self.differently_abled_count or 0)
        total      = employees + contracts

        if women + d_abled > total:
            frappe.throw(
                _(
                    "Women Employees ({0}) + Differently-abled ({1}) = {2}, "
                    "which exceeds Total Employees ({3}) + Contract Workers ({4}) = {5}. "
                    "Please recheck the workforce data."
                ).format(women, d_abled, women + d_abled, employees, contracts, total),
                frappe.ValidationError,
                title=_("Workforce Count Mismatch"),
            )

        # Individual sanity: subsets cannot be negative
        for label, val in [
            ("Total Employees", employees),
            ("Contract Workers", contracts),
            ("Women Employees", women),
            ("Differently-abled", d_abled),
            ("No. of Shifts", int(self.shifts_count or 1)),
        ]:
            if val < 0:
                frappe.throw(
                    _("{0} cannot be negative.").format(label),
                    frappe.ValidationError,
                )

    def validate_msme(self):
        """
        If MSME checkbox is ticked, Udyam Number is mandatory and must match pattern.
        Format: UDYAM-MH-00-0000000
        """
        if not self.is_msme:
            return
        if not self.msme_number:
            frappe.throw(
                _("Udyam Number is required when 'MSME Registered' is checked."),
                frappe.MandatoryError,
                title=_("Missing Udyam Number"),
            )
        num = self.msme_number.strip().upper()
        if not UDYAM_RE.match(num):
            frappe.throw(
                _("Udyam Number '{0}' is invalid. Expected format: UDYAM-MH-00-0000000.").format(num),
                frappe.ValidationError,
                title=_("Invalid Udyam Number"),
            )
        self.msme_number = num

    def validate_gstin(self):
        """Entity-level GSTIN (different from Organisation's primary GSTIN)."""
        if not self.gstin:
            return
        gstin = self.gstin.strip().upper()
        if not GSTIN_RE.match(gstin):
            frappe.throw(
                _("GSTIN '{0}' is invalid. Expected format: 22AAAAA0000A1Z5.").format(gstin),
                frappe.ValidationError,
                title=_("Invalid GSTIN"),
            )
        self.gstin = gstin

    def validate_parent_entity(self):
        """Parent entity must belong to the same Organisation and not be itself."""
        if not self.parent_entity:
            return
        if self.parent_entity == self.name:
            frappe.throw(
                _("An entity cannot be its own parent."),
                frappe.ValidationError,
            )
        parent_org = frappe.db.get_value("Business Entity", self.parent_entity, "organisation")
        if parent_org != self.organisation:
            frappe.throw(
                _(
                    "Parent Entity '{0}' belongs to Organisation '{1}', "
                    "but this entity belongs to '{2}'. They must match."
                ).format(self.parent_entity, parent_org, self.organisation),
                frappe.ValidationError,
                title=_("Parent Entity Organisation Mismatch"),
            )

    def validate_latitude_longitude(self):
        """Latitude in [-90, 90] and Longitude in [-180, 180]."""
        if self.geo_lat is not None:
            if not (-90.0 <= float(self.geo_lat) <= 90.0):
                frappe.throw(_("Latitude must be between -90 and 90."), frappe.ValidationError)
        if self.geo_lng is not None:
            if not (-180.0 <= float(self.geo_lng) <= 180.0):
                frappe.throw(_("Longitude must be between -180 and 180."), frappe.ValidationError)

    # ─── Business logic ─────────────────────────────────────────────────────

    def check_hazardous_notification(self):
        """
        When an entity is flagged as handling hazardous substances,
        notify all Compliance Officers in the organisation.
        They must then seed MSIHC and PESO obligations.
        """
        if not self.is_hazardous:
            return

        officers = frappe.get_all(
            "User Profile",
            filters={
                "organisation": self.organisation,
                "primary_persona": "Compliance Officer",
            },
            pluck="user",
        )

        if not officers:
            frappe.log_error(
                title="No Compliance Officer found",
                message=(
                    f"Entity '{self.entity_name}' (org: {self.organisation}) was marked hazardous "
                    f"but no Compliance Officer found to notify."
                ),
            )
            return

        for officer in officers:
            frappe.sendmail(
                recipients=[officer],
                subject=_("Action Required: Hazardous Substance Entity — {0}").format(self.entity_name),
                message=_(
                    "Business Entity <b>{0}</b> ({1}) has been marked as handling hazardous substances.<br><br>"
                    "Please seed the following compliance obligations:<br>"
                    "• MSIHC Rules, 1989<br>"
                    "• PESO (Petroleum and Explosives Safety Organisation) — if applicable<br>"
                    "• State PCB (Pollution Control Board) consents<br><br>"
                    "Take action in ComplyAI Compliance Calendar."
                ).format(self.entity_name, self.name),
                now=True,
            )

    def block_hard_delete_if_linked(self):
        """
        Business Entities must never be hard-deleted if compliance records exist.
        Use soft-delete (is_active = 0) instead.
        """
        for dt in LINKED_DOCTYPES:
            linked = frappe.db.exists(dt, {"business_entity": self.name})
            if linked:
                frappe.throw(
                    _(
                        "Cannot delete Business Entity '{0}' — it has linked {1} records. "
                        "Use soft-delete (set 'Active' = unchecked) instead to preserve audit trail."
                    ).format(self.name, dt),
                    frappe.ValidationError,
                    title=_("Delete Blocked"),
                )

    # ─── API helpers ────────────────────────────────────────────────────────

    def get_applicable_regulations(self):
        """
        Returns Regulation names that could apply to this entity
        based on jurisdiction (Central always, State if state matches).
        """
        central = frappe.get_all(
            "Regulation",
            filters={"jurisdiction": "Central", "is_active": 1, "extraction_status": "Published"},
            pluck="name",
        )
        # State-specific (Regulation State child table links to state)
        state_regs = frappe.db.sql(
            """
            SELECT DISTINCT parent
            FROM `tabRegulation State`
            WHERE state = %s
            AND parent IN (
                SELECT name FROM `tabRegulation`
                WHERE jurisdiction = 'State' AND is_active = 1 AND extraction_status = 'Published'
            )
            """,
            self.state,
            as_list=True,
        )
        state_names = [r[0] for r in state_regs]
        return list(set(central + state_names))