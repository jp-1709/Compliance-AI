"""
obligation_version.py
Controller for Obligation Version (append-only snapshot DocType).

No record may ever be written or deleted after creation.
Only inserts are permitted — enforced at DocType permission level
(no write/delete for any role) and here in validate().
"""

import json
import hashlib

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now


class ObligationVersion(Document):

    def validate(self):
        self._enforce_immutability()
        self._compute_hash_if_missing()

    def before_insert(self):
        """Compute snapshot hash before first insert."""
        self._compute_hash_if_missing()

    # ── Immutability guard ─────────────────────────────────────────────────

    def _enforce_immutability(self):
        """
        Reject any save attempt on an existing record.
        Obligation versions are write-once (append-only).
        """
        if not self.is_new():
            frappe.throw(
                _(
                    "Obligation Versions are immutable. "
                    "Tampering with version history carries legal liability."
                ),
                frappe.PermissionError,
            )

    # ── Hash helpers ───────────────────────────────────────────────────────

    def _compute_hash_if_missing(self):
        """Compute SHA-256 of snapshot_json and set snapshot_hash."""
        if self.snapshot_json and not self.snapshot_hash:
            raw = self.snapshot_json
            if not isinstance(raw, str):
                raw = json.dumps(raw, sort_keys=True, default=str)
            self.snapshot_hash = hashlib.sha256(raw.encode()).hexdigest()

    # ── Class method: verify integrity ────────────────────────────────────

    @staticmethod
    def verify_integrity(version_name: str) -> bool:
        """
        Re-compute the hash of a snapshot and compare to stored value.
        Returns True if intact, False if tampered.
        """
        ver = frappe.get_doc("Obligation Version", version_name)
        raw = ver.snapshot_json or ""
        if not isinstance(raw, str):
            raw = json.dumps(raw, sort_keys=True, default=str)
        computed = hashlib.sha256(raw.encode()).hexdigest()
        return computed == (ver.snapshot_hash or "")