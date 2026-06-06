"""
tasks.py
Scheduled background jobs for the Obligation Register module.

Registered in hooks.py scheduler_events:
  daily:   regenerate_pending_embeddings     (02:00)
           run_applicability_regression_tests (03:00)
  weekly:  audit_version_snapshot_integrity  (Sunday 04:00)
           detect_stale_obligations          (Sunday 06:00)
  monthly: curation_pipeline_report          (1st of month)
"""

import json
import hashlib
from datetime import datetime, timedelta

import frappe
from frappe import _
from frappe.utils import now, today, add_months, getdate


# ─────────────────────────────────────────────────────────────────────────────
# Daily: regenerate embeddings for changed obligations
# ─────────────────────────────────────────────────────────────────────────────

def regenerate_pending_embeddings():
    """
    Recompute pgvector embeddings for obligations whose content has changed
    since the last embedding was generated.
    Enqueues compute_embedding() for each pending obligation.
    """
    pending = frappe.get_all(
        "Compliance Obligation",
        filters={
            "is_published": 1,
            "embedding_vector": ("is", "not set"),
        },
        fields=["name"],
        limit_page_length=0,
    )
    for ob in pending:
        frappe.enqueue(
            "complyai.compliance.obligation_register.tasks.compute_embedding",
            obligation=ob["name"],
            queue="long",
            timeout=300,
        )
    frappe.logger().info(
        f"[ObligationRegister] Queued {len(pending)} embedding regeneration jobs."
    )


def compute_embedding(obligation: str):
    """
    Background job: compute pgvector embedding for one obligation and store the ID.
    Stubbed here; actual vector computation injected by AI Copilot module.
    """
    try:
        # Compose text corpus for embedding
        doc = frappe.get_doc("Compliance Obligation", obligation)
        corpus_parts = [
            doc.obligation_title or "",
            doc.section_reference or "",
            doc.applicability_summary or "",
            doc.purpose_text or "",
            doc.compliance_steps or "",
            doc.keywords or "",
        ]
        corpus = " ".join(p for p in corpus_parts if p)

        # Delegate to AI Copilot module (import lazily to avoid circular deps)
        try:
            from complyai.ai_copilot.vector_store import upsert_embedding
            vector_id = upsert_embedding(doctype="Compliance Obligation",
                                         docname=obligation,
                                         text=corpus)
            frappe.db.set_value("Compliance Obligation", obligation,
                                "embedding_vector", vector_id)
        except ImportError:
            # AI Copilot not installed yet; log and continue
            frappe.logger().warning(
                f"[ObligationRegister] AI Copilot not available; "
                f"skipping embedding for {obligation}"
            )
    except Exception as exc:
        frappe.log_error(
            message=str(exc),
            title=f"Embedding error: {obligation}",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Daily: run applicability regression tests
# ─────────────────────────────────────────────────────────────────────────────

def run_applicability_regression_tests():
    """
    Run all active Applicability Test Cases.
    Alert engineering via notification if any fail.
    """
    from complyai.compliance.obligation_register.api.api import run_applicability_test

    result = run_applicability_test(run_all=True)
    if result["failed"] > 0:
        _alert_regression_failure(result)
    frappe.logger().info(
        f"[ObligationRegister] Regression tests: "
        f"{result['passed']}/{result['total']} passed, "
        f"{result['failed']} failed."
    )


def _alert_regression_failure(result: dict):
    """Send a system notification when regression tests fail."""
    failed_cases = [r for r in result["results"] if not r["passed"]]
    message = (
        f"{result['failed']} Applicability Test Case(s) FAILED on {today()}.\n\n"
        + "\n".join(
            f"  • {c['test_case']} (obligation: {c['obligation']})"
            for c in failed_cases[:20]
        )
    )
    frappe.sendmail(
        recipients=_get_system_manager_emails(),
        subject=f"[ComplyAI] Applicability Regression FAILURE — {result['failed']} case(s)",
        message=message,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Weekly: snapshot integrity audit
# ─────────────────────────────────────────────────────────────────────────────

def audit_version_snapshot_integrity():
    """
    Verify SHA-256 hashes of all Obligation Version snapshots.
    Report any tampering / hash mismatches.
    """
    versions = frappe.get_all(
        "Obligation Version",
        fields=["name", "snapshot_json", "snapshot_hash"],
        limit_page_length=0,
    )

    violations = []
    for ver in versions:
        stored_hash = ver.get("snapshot_hash") or ""
        snapshot_str = ver.get("snapshot_json") or ""
        computed = hashlib.sha256(snapshot_str.encode()).hexdigest()
        if stored_hash and computed != stored_hash:
            violations.append({
                "version": ver["name"],
                "stored_hash": stored_hash,
                "computed_hash": computed,
            })

    if violations:
        _alert_integrity_violation(violations)
    frappe.logger().info(
        f"[ObligationRegister] Snapshot integrity: "
        f"{len(versions)} checked, {len(violations)} violations."
    )


def _alert_integrity_violation(violations: list):
    message = (
        f"ALERT: {len(violations)} Obligation Version snapshot(s) failed hash check.\n\n"
        + "\n".join(
            f"  • {v['version']} — stored: {v['stored_hash'][:16]}…  "
            f"computed: {v['computed_hash'][:16]}…"
            for v in violations
        )
        + "\n\nImmediate investigation required."
    )
    frappe.sendmail(
        recipients=_get_system_manager_emails() + _get_legal_counsel_emails(),
        subject="[ComplyAI] CRITICAL: Obligation Version Snapshot Integrity Violation",
        message=message,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Weekly: detect stale obligations
# ─────────────────────────────────────────────────────────────────────────────

def detect_stale_obligations():
    """
    Flag obligations not validated in the last 12 months.
    Pushes them into lawyers' revalidation queue via Change Log entry.
    """
    cutoff = add_months(today(), -12)
    stale = frappe.get_all(
        "Compliance Obligation",
        filters={
            "curation_status": "Published",
            "is_published": 1,
            "validated_on": ("<=", cutoff),
        },
        fields=["name", "obligation_title", "validated_on", "validated_by_lawyer"],
        limit_page_length=0,
    )

    for ob in stale:
        frappe.db.set_value(
            "Compliance Obligation",
            ob["name"],
            "needs_review_reason",
            f"Not validated since {ob.get('validated_on') or 'unknown'}. Auto-flagged for revalidation.",
        )

    if stale:
        _send_stale_digest(stale)

    frappe.logger().info(
        f"[ObligationRegister] Stale obligations detected: {len(stale)}"
    )


def _send_stale_digest(stale: list):
    message = (
        f"{len(stale)} obligation(s) have not been revalidated in 12+ months.\n\n"
        + "\n".join(
            f"  • {ob['obligation_title']} (last validated: {ob.get('validated_on') or 'never'})"
            for ob in stale[:50]
        )
    )
    frappe.sendmail(
        recipients=_get_legal_counsel_emails(),
        subject=f"[ComplyAI] Stale Obligations Digest — {len(stale)} need revalidation",
        message=message,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Monthly: curation pipeline report
# ─────────────────────────────────────────────────────────────────────────────

def curation_pipeline_report():
    """
    Email internal staff a summary of extraction → validation → publication metrics.
    """
    from complyai.compliance.obligation_register.api.api import get_curation_pipeline_status

    pipeline = get_curation_pipeline_status()
    lines = [f"  {status}: {count}" for status, count in pipeline["by_status"].items()]
    message = (
        f"Monthly Curation Pipeline Report — {today()}\n\n"
        + "Obligation counts by status:\n"
        + "\n".join(lines)
        + f"\n\nTotal: {pipeline['total']}"
    )
    frappe.sendmail(
        recipients=_get_system_manager_emails(),
        subject=f"[ComplyAI] Monthly Curation Pipeline Report — {today()}",
        message=message,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Task flagging helpers (called by api.py enqueue)
# ─────────────────────────────────────────────────────────────────────────────

def flag_tasks_for_obligation_change(obligation: str):
    """
    Mark open Compliance Tasks that reference this obligation so
    Compliance Officers know the underlying obligation has been updated.
    """
    try:
        tasks = frappe.get_all(
            "Compliance Task",
            filters={"obligation": obligation, "status": ["not in", ["Completed", "Cancelled"]]},
            fields=["name"],
            limit_page_length=0,
        )
        for task in tasks:
            frappe.db.set_value(
                "Compliance Task",
                task["name"],
                "obligation_changed_flag",
                1,
            )
        frappe.logger().info(
            f"[ObligationRegister] Flagged {len(tasks)} task(s) for obligation change: {obligation}"
        )
    except Exception as exc:
        frappe.log_error(str(exc), "flag_tasks_for_obligation_change")


def flag_tasks_for_retraction(obligation: str, reason: str):
    """
    Freeze all open tasks referencing a retracted obligation.
    """
    try:
        tasks = frappe.get_all(
            "Compliance Task",
            filters={"obligation": obligation, "status": ["not in", ["Completed", "Cancelled"]]},
            fields=["name"],
            limit_page_length=0,
        )
        for task in tasks:
            frappe.db.set_value(
                "Compliance Task",
                task["name"],
                {
                    "obligation_retracted": 1,
                    "retraction_reason": reason,
                },
            )
    except Exception as exc:
        frappe.log_error(str(exc), "flag_tasks_for_retraction")


# ─────────────────────────────────────────────────────────────────────────────
# Email recipient helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_system_manager_emails() -> list:
    return [
        u.email
        for u in frappe.get_all(
            "Has Role",
            filters={"role": "System Manager", "parenttype": "User"},
            fields=["parent as email"],
        )
        if u.email and "@" in u.email
    ]


def _get_legal_counsel_emails() -> list:
    return [
        u.email
        for u in frappe.get_all(
            "Has Role",
            filters={"role": "Legal Counsel", "parenttype": "User"},
            fields=["parent as email"],
        )
        if u.email and "@" in u.email
    ]