"""
compliance_obligation.py
Controller for Compliance Obligation DocType.
Enforces curation state machine, field validations, immutability of published
obligations, version snapshot creation, and embedding queuing.
"""

import re
import json
import hashlib

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now, now_datetime


# ─────────────────────────────────────────────────────────────────────────────
# Allowed curation-status transitions
# ─────────────────────────────────────────────────────────────────────────────
VALID_TRANSITIONS = {
    "Draft":                    ["AI Extracted", "Under Legal Review", "Deprecated"],
    "AI Extracted":             ["Under Legal Review", "Needs Revision", "Deprecated"],
    "Under Legal Review":       ["Needs Revision", "Approved (Pending Publish)", "Deprecated"],
    "Needs Revision":           ["Under Legal Review", "Deprecated"],
    "Approved (Pending Publish)": ["Published", "Needs Revision", "Deprecated"],
    "Published":                ["Deprecated", "Retracted"],
    "Deprecated":               [],
    "Retracted":                [],
}

# Fields whose change is forbidden once an obligation is Published
IMMUTABLE_FIELDS = [
    "obligation_title",
    "section_reference",
    "frequency",
    "due_timing_machine",
    "penalty_inr_min",
    "penalty_inr_max",
    "applicability_rules",
    "applicability_logic",
]

VALID_TIMING_TYPES = {
    "day_of_month",
    "days_after_period_end",
    "days_after_event",
    "fixed_date_in_year",
    "month_day_after_fy_end",
    "continuous",
    "custom",
}


class ComplianceObligationRegister(Document):

    # ── Frappe lifecycle hooks ─────────────────────────────────────────────

    def validate(self):
        self.normalise_obligation_code()
        self.validate_threshold_consistency()
        self.validate_due_timing_machine()
        self.validate_applicability_logic()
        self.enforce_curation_state_machine()
        self.enforce_publish_requires_validator()

    def after_insert(self):
        self.create_initial_version()
        self.enqueue_embedding()

    def on_update(self):
        # Guard: published content fields must not change
        if self._published_field_changed():
            frappe.throw(
                _("Published obligations are immutable. Use 'Amend' to create a new version."),
                frappe.ValidationError,
            )
        if self.has_value_changed("curation_status") and self.curation_status == "Published":
            self._on_published()

    # ── Validation helpers ─────────────────────────────────────────────────

    def normalise_obligation_code(self):
        """Codes must be UPPER_CASE_SNAKE (letters, digits, underscores, dashes)."""
        if self.obligation_code:
            if not re.match(r"^[A-Z][A-Z0-9_-]+$", self.obligation_code):
                frappe.throw(
                    _(
                        "Obligation code must be UPPER_CASE alphanumeric/underscore/dash. "
                        "Got: {0}"
                    ).format(self.obligation_code),
                    frappe.ValidationError,
                )

    def validate_threshold_consistency(self):
        if self.min_employees and self.max_employees:
            if int(self.min_employees) > int(self.max_employees):
                frappe.throw(
                    _("Min employees cannot exceed Max employees"),
                    frappe.ValidationError,
                )
        if self.min_turnover_inr_cr and self.max_turnover_inr_cr:
            if float(self.min_turnover_inr_cr) > float(self.max_turnover_inr_cr):
                frappe.throw(
                    _("Min turnover cannot exceed Max turnover"),
                    frappe.ValidationError,
                )
        if self.applies_to_msme_only and self.exempt_for_msme:
            frappe.throw(
                _("Cannot be both 'MSMEs Only' and 'MSMEs Exempt'"),
                frappe.ValidationError,
            )
        if self.penalty_inr_min and self.penalty_inr_max:
            if float(self.penalty_inr_min) > float(self.penalty_inr_max):
                frappe.throw(
                    _("Min penalty cannot exceed max penalty"),
                    frappe.ValidationError,
                )

    def validate_due_timing_machine(self):
        """Parse due_timing_machine JSON and verify the 'type' field is valid."""
        raw = (self.due_timing_machine or "").strip()
        if not raw:
            frappe.throw(_("Due Timing Rule (Machine) is required"), frappe.ValidationError)
        try:
            rule = json.loads(raw)
        except json.JSONDecodeError:
            frappe.throw(
                _("Due timing machine rule must be valid JSON"),
                frappe.ValidationError,
            )
        if "type" not in rule:
            frappe.throw(
                _("Due timing rule missing 'type'"),
                frappe.ValidationError,
            )
        if rule["type"] not in VALID_TIMING_TYPES:
            frappe.throw(
                _("Unknown due timing type: {0}. Valid: {1}").format(
                    rule["type"], ", ".join(sorted(VALID_TIMING_TYPES))
                ),
                frappe.ValidationError,
            )

    def validate_applicability_logic(self):
        logic = self.applicability_logic or "ALL (AND)"
        if logic == "Custom Expression":
            if not (self.applicability_expression or "").strip():
                frappe.throw(
                    _("Custom expression required when logic = 'Custom Expression'"),
                    frappe.ValidationError,
                )
            try:
                compile(self.applicability_expression, "<applicability>", "eval")
            except SyntaxError as exc:
                frappe.throw(
                    _("Applicability expression syntax error: {0}").format(exc),
                    frappe.ValidationError,
                )
        elif logic in ("ALL (AND)", "ANY (OR)"):
            if not self.applicability_rules:
                frappe.throw(
                    _(
                        "At least one applicability rule required "
                        "(or use Custom Expression with no rules)"
                    ),
                    frappe.ValidationError,
                )

    def enforce_curation_state_machine(self):
        """Reject forbidden curation-status transitions."""
        if self.is_new():
            return
        old_doc = self.get_doc_before_save()
        old_status = old_doc.curation_status if old_doc else "Draft"
        new_status = self.curation_status
        if old_status == new_status:
            return
        allowed = VALID_TRANSITIONS.get(old_status, [])
        if new_status not in allowed:
            frappe.throw(
                _(
                    "Invalid curation transition: {0} → {1}. "
                    "Allowed from '{0}': {2}"
                ).format(old_status, new_status, ", ".join(allowed) or "none"),
                frappe.ValidationError,
            )

    def enforce_publish_requires_validator(self):
        if self.curation_status == "Published":
            if not self.validated_by_lawyer:
                frappe.throw(
                    _("Cannot publish without lawyer validation"),
                    frappe.ValidationError,
                )
            if not self.validator_bar_number:
                frappe.throw(
                    _(
                        "Validator's Bar Council number required for publication (legal indemnity)"
                    ),
                    frappe.ValidationError,
                )
            # Auto-set publish metadata if not already set
            if not self.is_published:
                self.is_published = 1
                self.published_on = now()
                self.published_by = frappe.session.user

    # ── Internal helpers ───────────────────────────────────────────────────

    def _published_field_changed(self) -> bool:
        """True if any immutable business-content field changed on an already-published doc."""
        if not self.is_published or self.is_new():
            return False
        old = self.get_doc_before_save()
        if not old:
            return False
        # Skip if the obligation was just now being published (transition)
        if not old.is_published:
            return False
        for field in IMMUTABLE_FIELDS:
            old_val = old.get(field)
            new_val = self.get(field)
            # Normalise child-table comparison
            if isinstance(new_val, list):
                old_val = json.dumps(
                    [r.as_dict() if hasattr(r, "as_dict") else r for r in (old_val or [])],
                    sort_keys=True,
                )
                new_val = json.dumps(
                    [r.as_dict() if hasattr(r, "as_dict") else r for r in (new_val or [])],
                    sort_keys=True,
                )
            if old_val != new_val:
                return True
        return False

    def _on_published(self):
        """Side-effects fired when an obligation transitions to Published."""
        self._create_version_snapshot()
        self._notify_published()
        self._flag_open_tasks()

    def create_initial_version(self):
        """Create version 1 snapshot immediately after insert."""
        self._create_version_snapshot(version_number=1, change_reason="Initial version")

    def _create_version_snapshot(
        self,
        version_number: int = None,
        change_reason: str = None,
        changed_fields: str = None,
    ):
        """Persist an immutable snapshot of the current obligation state."""
        vnum = version_number or (int(self.version_number or 1))
        snapshot = self.as_dict()
        snapshot_str = json.dumps(snapshot, sort_keys=True, default=str)
        sha = hashlib.sha256(snapshot_str.encode()).hexdigest()

        version_doc = frappe.new_doc("Obligation Version")
        version_doc.update(
            {
                "obligation": self.name,
                "version_number": vnum,
                "effective_from": self.effective_from,
                "effective_until": self.effective_until,
                "snapshot_json": snapshot_str,
                "snapshot_hash": sha,
                "change_reason": change_reason or self.version_change_reason,
                "change_summary": self.version_change_log,
                "changed_fields": changed_fields,
                "created_by": frappe.session.user,
                "created_on": now(),
                "validated_by": self.validated_by_lawyer,
                "validator_bar_number": self.validator_bar_number,
            }
        )
        version_doc.flags.ignore_permissions = True
        version_doc.insert()

    def enqueue_embedding(self):
        """Queue background job to compute/update pgvector embedding."""
        try:
            frappe.enqueue(
                "complyai.compliance.obligation_register.tasks.compute_embedding",
                obligation=self.name,
                queue="long",
                timeout=300,
                now=frappe.flags.in_test,
            )
        except Exception:
            # Do not block save on embedding failure
            pass

    def _notify_published(self):
        frappe.publish_realtime(
            "obligation_published",
            {"obligation": self.name, "title": self.obligation_title},
            after_commit=True,
        )

    def _flag_open_tasks(self):
        """Notify open compliance tasks that their source obligation changed."""
        try:
            frappe.enqueue(
                "complyai.compliance.obligation_register.tasks.flag_tasks_for_obligation_change",
                obligation=self.name,
                queue="default",
                now=frappe.flags.in_test,
            )
        except Exception:
            pass