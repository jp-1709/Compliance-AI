"""
user_profile.py — Frappe DocType controller for User Profile (F3)
ComplyAI · Foundation Module

Validations:
  - primary_persona is mandatory
  - entity_access rows must belong to the same Organisation
  - phone and whatsapp must be +91XXXXXXXXXX
  - reports_to cannot be the user themselves
  - all_entities_access=1 overrides entity_access table (get_user_entities utility)
  - Frappe Role is auto-synced on save
  - One Frappe User → one Organisation (consultant caveat documented)
"""

import re
import frappe
from frappe import _
from frappe.model.document import Document

# ─── Constants ──────────────────────────────────────────────────────────────
PHONE_RE = re.compile(r"^\+91[0-9]{10}$")

PERSONA_TO_ROLE = {
    "Group CXO":         "Group CXO",
    "Compliance Officer":"Compliance Officer",
    "QMS Administrator": "QMS Administrator",
    "Internal Auditor":  "Internal Auditor",
    "Department Manager":"Department Manager",
    "Legal Counsel":     "Legal Counsel",
    "Read Only":         "Read Only",
}

# Roles that should be exclusive (warn if user has conflicting roles)
HIGH_PRIVILEGE_ROLES = {"Group CXO", "System Manager"}


class UserProfile(Document):
    # ─── Lifecycle hooks ────────────────────────────────────────────────────

    def validate(self):
        self.validate_persona_required()
        self.validate_organisation_not_changed()
        self.validate_entity_access_belongs_to_org()
        self.validate_phone()
        self.validate_whatsapp()
        self.validate_reports_to()
        self.validate_no_duplicate_entity_rows()

    def after_insert(self):
        self.sync_frappe_role()

    def on_update(self):
        self.sync_frappe_role()

    # ─── Field validators ───────────────────────────────────────────────────

    def validate_persona_required(self):
        """primary_persona drives role assignment — it is always mandatory."""
        if not self.primary_persona:
            frappe.throw(
                _("Primary Persona is mandatory. Please select a role for this user."),
                frappe.MandatoryError,
                title=_("Missing Persona"),
            )

    def validate_organisation_not_changed(self):
        """
        A user must not be moved between organisations post-creation.
        Compliance records are scoped to org; cross-org migration is a data-integrity risk.
        """
        if self.is_new():
            return
        old_org = frappe.db.get_value("User Profile", self.name, "organisation")
        if old_org and old_org != self.organisation:
            frappe.throw(
                _(
                    "Organisation cannot be changed after User Profile creation. "
                    "User '{0}' is permanently scoped to '{1}'. "
                    "If a consultant needs access to multiple orgs, create separate Frappe Users."
                ).format(self.user, old_org),
                frappe.PermissionError,
                title=_("Organisation Change Blocked"),
            )

    def validate_entity_access_belongs_to_org(self):
        """
        Each entity listed in the entity_access table must belong to the same
        Organisation as the User Profile — cross-org entity access is a data-leak vector.
        """
        if self.all_entities_access:
            return  # entity_access table is ignored when all_entities_access is set

        seen = set()
        for row in (self.entity_access or []):
            if not row.business_entity:
                continue

            # Dedup check
            if row.business_entity in seen:
                frappe.throw(
                    _("Business Entity '{0}' appears more than once in the Entity Access table (row {1}).").format(
                        row.business_entity, row.idx
                    ),
                    frappe.ValidationError,
                )
            seen.add(row.business_entity)

            entity_org = frappe.db.get_value("Business Entity", row.business_entity, "organisation")
            if entity_org != self.organisation:
                frappe.throw(
                    _(
                        "Business Entity '{0}' (row {1}) belongs to Organisation '{2}', "
                        "not '{3}'. Entity access must be within the same Organisation."
                    ).format(row.business_entity, row.idx, entity_org, self.organisation),
                    frappe.ValidationError,
                    title=_("Entity-Organisation Mismatch"),
                )

    def validate_phone(self):
        """Indian phone numbers: +91 followed by 10 digits."""
        if self.phone and not PHONE_RE.match(self.phone.strip()):
            frappe.throw(
                _("Phone '{0}' must be in format +91XXXXXXXXXX (e.g. +919876543210). "
                  "WhatsApp Business API silently drops malformed numbers.").format(self.phone),
                frappe.ValidationError,
                title=_("Invalid Phone"),
            )

    def validate_whatsapp(self):
        """WhatsApp number must be valid — silent failures cause missed deadline reminders."""
        if self.whatsapp_number and not PHONE_RE.match(self.whatsapp_number.strip()):
            frappe.throw(
                _("WhatsApp Number '{0}' must be in format +91XXXXXXXXXX (e.g. +919876543210).").format(
                    self.whatsapp_number
                ),
                frappe.ValidationError,
                title=_("Invalid WhatsApp Number"),
            )

    def validate_reports_to(self):
        if self.reports_to and self.reports_to == self.user:
            frappe.throw(
                _("A user cannot report to themselves."),
                frappe.ValidationError,
            )

    def validate_no_duplicate_entity_rows(self):
        """Handled inside validate_entity_access_belongs_to_org — kept as a named method for clarity."""
        pass  # logic is inside validate_entity_access_belongs_to_org

    # ─── Business logic ─────────────────────────────────────────────────────

    def sync_frappe_role(self):
        """
        Auto-assign the Frappe Role that corresponds to primary_persona.
        Called after_insert and on_update.
        Does NOT remove old roles (to allow multi-role edge cases handled by System Manager).
        """
        role = PERSONA_TO_ROLE.get(self.primary_persona)
        if not role:
            return

        try:
            user_doc = frappe.get_doc("User", self.user)
        except frappe.DoesNotExistError:
            frappe.log_error(
                title="sync_frappe_role: User not found",
                message=f"User '{self.user}' does not exist in Frappe User table.",
            )
            return

        existing_roles = {r.role for r in user_doc.roles}
        if role not in existing_roles:
            user_doc.append("roles", {"role": role})
            user_doc.save(ignore_permissions=True)
            frappe.logger().info(
                f"UserProfile {self.name}: assigned Frappe Role '{role}' to user '{self.user}'"
            )

    # ─── API helpers ────────────────────────────────────────────────────────

    def get_accessible_entities(self):
        """
        Returns list of Business Entity names this user can access.
        Mirrors get_user_entities() utility for direct document-level access.
        """
        if self.all_entities_access:
            return frappe.get_all(
                "Business Entity",
                filters={"organisation": self.organisation, "is_active": 1},
                pluck="name",
            )
        return [row.business_entity for row in (self.entity_access or [])]

    def can_access_entity(self, entity_name: str) -> bool:
        """Returns True if the user has any access level to the given entity."""
        return entity_name in self.get_accessible_entities()

    def get_notification_channels(self) -> list[str]:
        """Returns list of active notification channels: 'email', 'whatsapp', 'inapp'."""
        channels = []
        if self.notification_pref_email:
            channels.append("email")
        if self.notification_pref_whatsapp and self.whatsapp_number:
            channels.append("whatsapp")
        if self.notification_pref_inapp:
            channels.append("inapp")
        return channels