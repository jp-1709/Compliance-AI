"""
evidence_file.py — Frappe DocType controller for Evidence File (F4)
Quantbit Compliance AI · Foundation Module

Validations & Business Logic:
  - On upload: compute SHA-256 hash, file size, MIME type, page count (PDF)
  - Deduplication within same Organisation via SHA-256 (NOT cross-tenant)
  - destroy_after = issue_date + retention_period_years (auto-set, read-only)
  - expiry_date auto-flips is_expired via daily scheduled job
  - MIME allowlist: PDF, JPEG, PNG, DOCX, XLSX, ZIP
  - Max file size: 25 MB (configurable)
  - uploaded_by auto-set to session user
  - Polymorphic Evidence Link integrity
"""

import os
import re
import hashlib
import mimetypes
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, add_years, today

# ─── Configuration ──────────────────────────────────────────────────────────
MAX_FILE_SIZE_MB = 25
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

ALLOWED_MIME_TYPES: set[str] = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",        # .xlsx
    "application/zip",
    "application/x-zip-compressed",
}

ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".docx", ".xlsx", ".zip"}

MIME_DISPLAY_NAMES = {
    "application/pdf": "PDF",
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "DOCX",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "XLSX",
    "application/zip": "ZIP",
    "application/x-zip-compressed": "ZIP",
}


class EvidenceFile(Document):
    # ─── Lifecycle hooks ────────────────────────────────────────────────────

    def before_insert(self):
        self.set_uploaded_by()
        self.compute_file_metadata()

    def validate(self):
        self.validate_mime_type()
        self.validate_file_size()
        self.validate_expiry_after_issue()
        self.compute_destroy_after()
        self.update_expiry_status()

    def after_insert(self):
        self.check_deduplication()

    # ─── File metadata ───────────────────────────────────────────────────────

    def set_uploaded_by(self):
        """Auto-set to the current session user (non-repudiation)."""
        if not self.uploaded_by:
            self.uploaded_by = frappe.session.user

    def compute_file_metadata(self):
        """
        Compute SHA-256 hash, file size (bytes), MIME type, page count (PDF).
        Called before_insert so deduplication check has the hash available.
        """
        if not self.file:
            return

        file_path = self._resolve_file_path()
        if not file_path or not os.path.exists(file_path):
            frappe.logger().warning(
                f"EvidenceFile: file path not resolvable for {self.file}"
            )
            return

        # SHA-256 (chunked to avoid OOM on large files)
        self.sha256_hash = self._compute_sha256(file_path)

        # File size
        self.file_size_bytes = os.path.getsize(file_path)

        # MIME type (from file extension first, then magic bytes)
        self.mime_type = self._detect_mime(file_path)

        # Page count for PDF
        if self.mime_type == "application/pdf":
            self.page_count = self._count_pdf_pages(file_path)

    def _resolve_file_path(self) -> str | None:
        """Translate the stored file URL to an absolute path on disk."""
        try:
            file_doc = frappe.get_doc("File", {"file_url": self.file})
            rel_path = file_doc.file_url.lstrip("/")
            return frappe.get_site_path(rel_path)
        except Exception:
            return None

    @staticmethod
    def _compute_sha256(file_path: str) -> str:
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()

    @staticmethod
    def _detect_mime(file_path: str) -> str:
        mime, _ = mimetypes.guess_type(file_path)
        if mime:
            return mime
        # Fallback: read magic bytes
        try:
            with open(file_path, "rb") as fh:
                header = fh.read(8)
            if header[:4] == b"%PDF":
                return "application/pdf"
            if header[:2] in (b"\xff\xd8", b"\xff\xe0", b"\xff\xe1"):
                return "image/jpeg"
            if header[:8] == b"\x89PNG\r\n\x1a\n":
                return "image/png"
            if header[:4] == b"PK\x03\x04":
                return "application/zip"
        except Exception:
            pass
        return "application/octet-stream"

    @staticmethod
    def _count_pdf_pages(file_path: str) -> int:
        try:
            import pypdf
            reader = pypdf.PdfReader(file_path)
            return len(reader.pages)
        except Exception:
            return 0

    # ─── Field validators ───────────────────────────────────────────────────

    def validate_mime_type(self):
        """
        Reject files whose MIME type is not in the allowlist.
        MIME type is set by compute_file_metadata; this guard runs during validate().
        """
        if not self.mime_type:
            return  # no file yet — will be caught by reqd on field

        if self.mime_type not in ALLOWED_MIME_TYPES:
            allowed_names = ", ".join(sorted(MIME_DISPLAY_NAMES.values()))
            frappe.throw(
                _(
                    "MIME type '{0}' is not permitted. "
                    "Allowed file types: {1}. "
                    "Please upload a supported file format."
                ).format(self.mime_type, allowed_names),
                frappe.ValidationError,
                title=_("Unsupported File Type"),
            )

    def validate_file_size(self):
        """File must not exceed MAX_FILE_SIZE_MB (configurable per-org in future)."""
        if not self.file_size_bytes:
            return
        size_mb = self.file_size_bytes / (1024 * 1024)
        if self.file_size_bytes > MAX_FILE_SIZE_BYTES:
            frappe.throw(
                _(
                    "File size {0:.2f} MB exceeds the maximum allowed size of {1} MB. "
                    "Please compress or split the file."
                ).format(size_mb, MAX_FILE_SIZE_MB),
                frappe.ValidationError,
                title=_("File Too Large"),
            )

    def validate_expiry_after_issue(self):
        """Expiry date must be after Issue date."""
        if self.issue_date and self.expiry_date:
            if getdate(self.expiry_date) <= getdate(self.issue_date):
                frappe.throw(
                    _("Expiry Date ({0}) must be after Issue Date ({1}).").format(
                        self.expiry_date, self.issue_date
                    ),
                    frappe.ValidationError,
                )

    # ─── Business logic ─────────────────────────────────────────────────────

    def check_deduplication(self):
        """
        If another Evidence File in the SAME Organisation has the same SHA-256 hash,
        alert the user and suggest linking to the existing record.
        Deduplication is per-Organisation intentionally (two orgs can have the same gazette).
        """
        if not self.sha256_hash or not self.organisation:
            return

        duplicate = frappe.db.get_value(
            "Evidence File",
            {
                "sha256_hash": self.sha256_hash,
                "organisation": self.organisation,
                "name": ("!=", self.name),
            },
            "name",
        )

        if duplicate:
            link = f"<a href='/app/evidence-file/{duplicate}'>{duplicate}</a>"
            frappe.msgprint(
                _(
                    "⚠ Duplicate file detected. An Evidence File with identical content already exists: {0}.<br>"
                    "Consider deleting this record and linking {0} to your task instead. "
                    "Duplicate retention wastes storage and creates audit confusion."
                ).format(link),
                title=_("Duplicate File Detected"),
                indicator="orange",
            )

    def compute_destroy_after(self):
        """
        destroy_after = issue_date + retention_period_years.
        Legal minimum per Companies Act 2013 is 8 years; default is 7.
        """
        if self.issue_date and self.retention_period_years:
            self.destroy_after = add_years(
                getdate(self.issue_date), int(self.retention_period_years)
            )

    def update_expiry_status(self):
        """Flip is_expired based on today's date. Also called by daily scheduled job."""
        if not self.expiry_date:
            self.is_expired = 0
            return
        self.is_expired = 1 if getdate(self.expiry_date) < getdate(today()) else 0

    # ─── API helpers ────────────────────────────────────────────────────────

    def link_to_record(self, linked_doctype: str, linked_name: str, purpose: str = "Primary Evidence"):
        """
        Add a polymorphic link from this Evidence File to any business record.
        Checks that the linked record exists before adding.
        """
        if not frappe.db.exists(linked_doctype, linked_name):
            frappe.throw(
                _("Cannot link: {0} '{1}' does not exist.").format(linked_doctype, linked_name),
                frappe.DoesNotExistError,
            )

        # Avoid duplicate links
        for row in (self.linked_records or []):
            if row.linked_doctype == linked_doctype and row.linked_name == linked_name:
                frappe.msgprint(
                    _("Evidence File is already linked to {0} '{1}'.").format(linked_doctype, linked_name),
                    indicator="blue",
                )
                return

        self.append("linked_records", {
            "linked_doctype": linked_doctype,
            "linked_name": linked_name,
            "link_purpose": purpose,
        })
        self.save(ignore_permissions=False)

    def get_file_size_human(self) -> str:
        """Human-readable file size string."""
        if not self.file_size_bytes:
            return "Unknown"
        size = int(self.file_size_bytes)
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"


# ─── Scheduled jobs (called from tasks.py) ──────────────────────────────────

def flag_expired_evidence():
    """
    Daily job: set is_expired = 1 for all Evidence Files whose expiry_date < today.
    Runs from scheduler_events['daily'] in hooks.py.
    """
    today_date = getdate(today())
    expired = frappe.db.sql(
        """
        UPDATE `tabEvidence File`
        SET is_expired = 1, modified = NOW()
        WHERE expiry_date < %s AND is_expired = 0
        """,
        today_date,
    )
    frappe.logger().info(f"flag_expired_evidence: updated {frappe.db.affected_rows()} records")


def send_evidence_expiry_reminders():
    """
    Daily job: send 30-day, 14-day, and 7-day reminders for expiring evidence.
    """
    from frappe.utils import add_days

    reminder_days = [30, 14, 7]
    today_date = getdate(today())

    for days in reminder_days:
        target_date = add_days(today_date, days)
        expiring = frappe.get_all(
            "Evidence File",
            filters={"expiry_date": target_date, "is_expired": 0},
            fields=["name", "evidence_title", "organisation", "business_entity",
                    "expiry_date", "uploaded_by"],
        )

        for ev in expiring:
            _send_expiry_notification(ev, days)

    frappe.logger().info(f"send_evidence_expiry_reminders: processed for days {reminder_days}")


def _send_expiry_notification(ev: dict, days_remaining: int):
    """Internal helper: dispatch expiry notification through user preference channels."""
    org = ev["organisation"]
    entity = ev["business_entity"]

    # Recipients: uploader + all Compliance Officers of the org
    recipients = set()
    if ev["uploaded_by"]:
        recipients.add(ev["uploaded_by"])

    officers = frappe.get_all(
        "User Profile",
        filters={"organisation": org, "primary_persona": "Compliance Officer"},
        pluck="user",
    )
    recipients.update(officers)

    subject = _("Evidence Expiring in {0} days: {1}").format(days_remaining, ev["evidence_title"])
    message = _(
        "Evidence File <b>{0}</b> ({1}) for entity <b>{2}</b> "
        "will expire on <b>{3}</b> ({4} days remaining).<br><br>"
        "Please renew or take required action."
    ).format(ev["name"], ev["evidence_title"], entity, ev["expiry_date"], days_remaining)

    for user in recipients:
        # Check user notification preferences
        prefs = frappe.db.get_value(
            "User Profile",
            {"user": user},
            ["notification_pref_email", "notification_pref_inapp"],
            as_dict=True,
        )
        if prefs and prefs.notification_pref_email:
            frappe.sendmail(recipients=[user], subject=subject, message=message, now=False)
        if prefs and prefs.notification_pref_inapp:
            frappe.publish_realtime(
                "notify",
                {"message": message, "title": subject, "indicator": "orange"},
                user=user,
            )