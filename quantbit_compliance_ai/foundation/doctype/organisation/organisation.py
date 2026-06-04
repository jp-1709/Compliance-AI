"""
organisation.py — Frappe DocType controller for Organisation (F1)
ComplyAI · Foundation Module

Validations:
  - PAN format: ^[A-Z]{5}[0-9]{4}[A-Z]$
  - CIN format (if present): ^[LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}$
  - Primary GSTIN format (if present): ^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$
  - Subscription End >= Subscription Start
  - PIN Code must be 6 digits
  - Email and phone format validation
  - Subscription expiry auto-deactivates the org (402 grace logic)
"""

import re
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, today, add_days

# ─── Regex constants ────────────────────────────────────────────────────────
PAN_RE    = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
CIN_RE    = re.compile(r"^[LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}$")
GSTIN_RE  = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$")
PINCODE_RE = re.compile(r"^\d{6}$")
PHONE_RE  = re.compile(r"^\+91[0-9]{10}$")
EMAIL_RE  = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")


class Organisation(Document):
    # ─── Lifecycle hooks ────────────────────────────────────────────────────

    def validate(self):
        self.validate_pan()
        self.validate_cin()
        self.validate_gstin()
        self.validate_pincode()
        self.validate_subscription_dates()
        self.validate_email()
        self.validate_phone()
        self.validate_lei()
        self.validate_max_values()

    def before_save(self):
        self.check_subscription_expiry()

    def on_update(self):
        self.sync_entity_count()

    # ─── Field validators ───────────────────────────────────────────────────

    def validate_pan(self):
        """PAN is mandatory and must match the Indian PAN format."""
        if not self.pan:
            frappe.throw(_("PAN is mandatory."), frappe.MandatoryError, title=_("Missing PAN"))

        pan = self.pan.strip().upper()
        if not PAN_RE.match(pan):
            frappe.throw(
                _("PAN '{0}' is invalid. Expected format: AAAAA9999A (e.g. ABCDE1234F)").format(pan),
                frappe.ValidationError,
                title=_("Invalid PAN"),
            )
        self.pan = pan  # normalise to uppercase

    def validate_cin(self):
        """CIN is optional but, if provided, must follow MCA format."""
        if not self.cin:
            return
        cin = self.cin.strip().upper()
        if not CIN_RE.match(cin):
            frappe.throw(
                _("CIN '{0}' is invalid. Expected: L12345AB2020ABC123456 "
                  "(starts with L/U, 21 chars total)").format(cin),
                frappe.ValidationError,
                title=_("Invalid CIN"),
            )
        self.cin = cin

    def validate_gstin(self):
        """Primary GSTIN is optional but must be valid if present."""
        if not self.gstin_primary:
            return
        gstin = self.gstin_primary.strip().upper()
        if not GSTIN_RE.match(gstin):
            frappe.throw(
                _("Primary GSTIN '{0}' is invalid. Expected format: 22AAAAA0000A1Z5").format(gstin),
                frappe.ValidationError,
                title=_("Invalid GSTIN"),
            )
        self.gstin_primary = gstin

    def validate_pincode(self):
        """PIN Code must be exactly 6 digits."""
        if not self.pincode:
            frappe.throw(_("PIN Code is mandatory."), frappe.MandatoryError)
        if not PINCODE_RE.match(str(self.pincode).strip()):
            frappe.throw(
                _("PIN Code must be exactly 6 digits. Got: '{0}'").format(self.pincode),
                frappe.ValidationError,
                title=_("Invalid PIN Code"),
            )

    def validate_subscription_dates(self):
        """Subscription End must be >= Subscription Start."""
        if self.subscription_start and self.subscription_end:
            start = getdate(self.subscription_start)
            end   = getdate(self.subscription_end)
            if end < start:
                frappe.throw(
                    _("Subscription End ({0}) must be on or after Subscription Start ({1}).").format(
                        self.subscription_end, self.subscription_start
                    ),
                    frappe.ValidationError,
                    title=_("Invalid Subscription Dates"),
                )

    def validate_email(self):
        if self.email and not EMAIL_RE.match(self.email.strip()):
            frappe.throw(
                _("Email address '{0}' is not valid.").format(self.email),
                frappe.ValidationError,
                title=_("Invalid Email"),
            )

    def validate_phone(self):
        """Indian phone numbers must be stored as +91XXXXXXXXXX."""
        if self.phone and not PHONE_RE.match(self.phone.strip()):
            frappe.throw(
                _("Phone '{0}' must be in format +91XXXXXXXXXX (e.g. +919876543210).").format(self.phone),
                frappe.ValidationError,
                title=_("Invalid Phone"),
            )

    def validate_lei(self):
        """LEI is 20 alphanumeric characters (ISO 17442)."""
        if self.lei:
            lei = self.lei.strip().upper()
            if not re.match(r"^[A-Z0-9]{20}$", lei):
                frappe.throw(
                    _("LEI '{0}' must be exactly 20 alphanumeric characters.").format(lei),
                    frappe.ValidationError,
                    title=_("Invalid LEI"),
                )
            self.lei = lei

    def validate_max_values(self):
        """max_users and max_entities must be positive integers."""
        if self.max_users is not None and self.max_users < 1:
            frappe.throw(_("Max Users must be at least 1."), frappe.ValidationError)
        if self.max_entities is not None and self.max_entities < 1:
            frappe.throw(_("Max Entities must be at least 1."), frappe.ValidationError)

    # ─── Business logic ─────────────────────────────────────────────────────

    def check_subscription_expiry(self):
        """
        Auto-deactivate organisation when subscription_end < today.
        Keeps data intact for 90-day grace period (per spec §7 point 10).
        All API calls on an inactive org should return 402.
        """
        if not self.subscription_end:
            return
        if getdate(self.subscription_end) < getdate(today()):
            if self.is_active:
                self.is_active = 0
                frappe.log_error(
                    title="Organisation Deactivated — Subscription Expired",
                    message=f"Organisation {self.name} deactivated on {today()}. "
                            f"Subscription expired on {self.subscription_end}. "
                            f"Data retained for 90-day grace period."
                )
                # Notify primary contact
                if self.primary_contact:
                    frappe.sendmail(
                        recipients=[self.primary_contact],
                        subject=_("Your ComplyAI subscription has expired"),
                        template="subscription_expired",
                        args={"org_name": self.organisation_name},
                        now=True,
                    )

    def sync_entity_count(self):
        """Cache the current entity count for fast dashboard queries."""
        count = frappe.db.count("Business Entity", {"organisation": self.name, "is_active": 1})
        frappe.db.set_value(self.doctype, self.name, "_entity_count", count, update_modified=False)

    # ─── API helpers ────────────────────────────────────────────────────────

    def get_active_entities(self):
        """Return list of active Business Entity names for this org."""
        return frappe.get_all(
            "Business Entity",
            filters={"organisation": self.name, "is_active": 1},
            pluck="name",
        )

    def is_subscription_valid(self):
        """Returns True if subscription is active and not expired."""
        if not self.is_active:
            return False
        if self.subscription_end and getdate(self.subscription_end) < getdate(today()):
            return False
        return True

    def days_until_subscription_expires(self):
        """Returns integer days remaining, or None if no end date."""
        if not self.subscription_end:
            return None
        delta = getdate(self.subscription_end) - getdate(today())
        return delta.days