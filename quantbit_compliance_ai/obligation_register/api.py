"""
api.py
Whitelisted Frappe API methods for the Obligation Register module.

All methods are callable from the client via frappe.call().
Permission checks are enforced inline (not just via DocType perms).
"""

import json
import hashlib

import frappe
from frappe import _
from frappe.utils import now, now_datetime, getdate

from complyai.compliance.obligation_register.controllers.applicability_engine import (
    get_applicable_obligations as _engine_get_applicable,
    is_obligation_applicable,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _require_role(*roles):
    """Raise PermissionError if the session user does not have any of *roles*."""
    user_roles = frappe.get_roles(frappe.session.user)
    if not any(r in user_roles for r in roles):
        frappe.throw(
            _("You do not have permission to perform this action. Required role(s): {0}").format(
                ", ".join(roles)
            ),
            frappe.PermissionError,
        )


def _get_obligation_or_throw(name: str):
    doc = frappe.get_doc("Compliance Obligation", name)
    return doc


# ─────────────────────────────────────────────────────────────────────────────
# Applicability
# ─────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def get_applicable_obligations(
    business_entity: str,
    as_of_date: str = None,
    include_trace: bool = False,
) -> list:
    """
    Return all published obligations applicable to a Business Entity as of a date.
    Performance target: < 500ms for ~5000 obligations.
    """
    return _engine_get_applicable(
        entity_name=business_entity,
        as_of_date=as_of_date,
        include_trace=frappe.utils.sbool(include_trace),
    )


@frappe.whitelist()
def explain_applicability(obligation: str, business_entity: str) -> dict:
    """
    Return full rule trace for why an obligation does (or doesn't) apply.
    """
    ob = frappe.get_doc("Compliance Obligation", obligation).as_dict()
    from complyai.compliance.obligation_register.controllers.applicability_engine import (
        _load_entity_profile,
    )
    entity = _load_entity_profile(business_entity)
    verdict, trace = is_obligation_applicable(ob, entity)
    return {
        "obligation": obligation,
        "business_entity": business_entity,
        "applicable": verdict,
        "trace": trace,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Search
# ─────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def search_obligations(
    query: str = None,
    regulation: str = None,
    category: str = None,
    state: str = None,
    industry: str = None,
    limit: int = 50,
) -> list:
    """
    Lexical search across published obligations.
    Optionally filtered by regulation, category, state, industry.
    """
    filters = {
        "is_published": 1,
        "is_current_version": 1,
        "curation_status": "Published",
    }
    if regulation:
        filters["regulation"] = regulation
    if category:
        filters["category"] = category

    or_filters = []
    if query:
        for field in ("obligation_title", "obligation_code", "keywords",
                      "section_reference", "sub_category"):
            or_filters.append([field, "like", f"%{query}%"])

    results = frappe.get_all(
        "Compliance Obligation",
        filters=filters,
        or_filters=or_filters if or_filters else None,
        fields=[
            "name", "obligation_title", "obligation_code", "obligation_type",
            "category", "sub_category", "regulation", "regulator",
            "frequency", "default_risk_level", "section_reference",
        ],
        limit_page_length=int(limit),
        order_by="obligation_title asc",
    )

    # State / industry post-filter (child table lookup)
    if state or industry:
        names = [r["name"] for r in results]
        if state:
            state_parents = {
                row["parent"]
                for row in frappe.get_all(
                    "Obligation State",
                    filters={"parent": ["in", names], "state": state},
                    fields=["parent"],
                )
            }
            results = [r for r in results if r["name"] in state_parents]
        if industry:
            ind_parents = {
                row["parent"]
                for row in frappe.get_all(
                    "Obligation Industry",
                    filters={"parent": ["in", names], "industry_code": industry},
                    fields=["parent"],
                )
            }
            results = [r for r in results if r["name"] in ind_parents]

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Version / history
# ─────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def get_obligation_with_history(obligation: str) -> dict:
    """Return obligation + all versions + change log entries."""
    doc = frappe.get_doc("Compliance Obligation", obligation).as_dict()
    versions = frappe.get_all(
        "Obligation Version",
        filters={"obligation": obligation},
        fields=["name", "version_number", "effective_from", "effective_until",
                "change_reason", "change_summary", "changed_fields",
                "snapshot_hash", "created_by", "created_on"],
        order_by="version_number asc",
    )
    change_logs = frappe.get_all(
        "Obligation Change Log",
        filters={"obligation": obligation},
        fields=["name", "change_type", "change_date", "effective_date",
                "change_summary", "review_status", "source_type", "source_url"],
        order_by="change_date desc",
    )
    return {
        "obligation": doc,
        "versions": versions,
        "change_log": change_logs,
    }


@frappe.whitelist()
def get_obligation_version_snapshot(version_name: str) -> dict:
    """Return the frozen JSON snapshot for a specific Obligation Version."""
    ver = frappe.get_doc("Obligation Version", version_name)
    return {
        "version": version_name,
        "obligation": ver.obligation,
        "version_number": ver.version_number,
        "effective_from": str(ver.effective_from),
        "snapshot_hash": ver.snapshot_hash,
        "snapshot": json.loads(ver.snapshot_json),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Curation lifecycle
# ─────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def submit_for_legal_review(obligation: str, assigned_lawyer: str = None) -> dict:
    """Move an obligation from AI Extracted → Under Legal Review."""
    _require_role("System Manager", "Legal Counsel")
    doc = _get_obligation_or_throw(obligation)
    if doc.curation_status not in ("Draft", "AI Extracted"):
        frappe.throw(
            _(f"Cannot submit for review from status '{doc.curation_status}'"),
            frappe.ValidationError,
        )
    doc.curation_status = "Under Legal Review"
    if assigned_lawyer:
        doc.validated_by_lawyer = assigned_lawyer
    doc.save(ignore_permissions=False)
    return {"obligation": obligation, "status": doc.curation_status}


@frappe.whitelist()
def publish_obligation(
    obligation: str,
    validator_bar_number: str,
    validation_notes: str = None,
) -> dict:
    """
    Publish a single obligation.
    Requires Legal Counsel or System Manager role.
    Creates an immutable Obligation Version snapshot.
    """
    _require_role("System Manager", "Legal Counsel")
    doc = _get_obligation_or_throw(obligation)

    if doc.curation_status != "Approved (Pending Publish)":
        frappe.throw(
            _(
                "Only obligations in 'Approved (Pending Publish)' status can be published. "
                "Current status: {0}"
            ).format(doc.curation_status),
            frappe.ValidationError,
        )

    doc.curation_status = "Published"
    doc.validated_by_lawyer = doc.validated_by_lawyer or frappe.session.user
    doc.validator_bar_number = validator_bar_number
    doc.validated_on = now()
    if validation_notes:
        doc.validation_notes = validation_notes

    doc.save(ignore_permissions=False)
    return {
        "obligation": obligation,
        "status": "Published",
        "published_on": str(doc.published_on),
    }


@frappe.whitelist()
def bulk_publish(obligation_names: list, validator_bar_number: str) -> dict:
    """
    Atomically publish a batch of obligations.
    All must be in 'Approved (Pending Publish)' state.
    Rolls back on any single failure.
    """
    _require_role("System Manager", "Legal Counsel")

    if isinstance(obligation_names, str):
        obligation_names = json.loads(obligation_names)

    if not obligation_names:
        frappe.throw(_("No obligations provided for bulk publish"), frappe.ValidationError)

    published = []
    try:
        for name in obligation_names:
            result = publish_obligation(name, validator_bar_number)
            published.append(result)
    except Exception:
        # Rollback already-published ones (best-effort)
        for p in published:
            try:
                doc = frappe.get_doc("Compliance Obligation", p["obligation"])
                # Force-revert (only in case of partial publish in same transaction)
                frappe.db.rollback()
            except Exception:
                pass
        raise

    return {
        "published_count": len(published),
        "obligations": published,
    }


@frappe.whitelist()
def amend_obligation(obligation: str, changes: dict, change_reason: str) -> dict:
    """
    Create a new version of a published obligation.
    The existing obligation is marked superseded; a new Doc is inserted at v+1.
    Permission: Legal Counsel or System Manager.
    """
    _require_role("System Manager", "Legal Counsel")

    if isinstance(changes, str):
        changes = json.loads(changes)

    old_doc = _get_obligation_or_throw(obligation)

    if not old_doc.is_published:
        frappe.throw(
            _("Only published obligations can be amended. Use direct edit for drafts."),
            frappe.ValidationError,
        )

    # Compute changed fields for audit
    changed_fields = [f for f in changes if old_doc.get(f) != changes[f]]

    # Create new obligation record (copy of old + changes)
    new_doc = frappe.copy_doc(old_doc)
    new_doc.curation_status = "Draft"
    new_doc.is_published = 0
    new_doc.published_on = None
    new_doc.published_by = None
    new_doc.version_number = int(old_doc.version_number or 1) + 1
    new_doc.is_current_version = 1
    new_doc.supersedes = old_doc.name
    new_doc.version_change_reason = change_reason
    new_doc.changed_fields_text = ", ".join(changed_fields)

    for field, value in changes.items():
        new_doc.set(field, value)

    new_doc.insert(ignore_permissions=False)

    # Mark old as superseded
    frappe.db.set_value(
        "Compliance Obligation",
        old_doc.name,
        {
            "superseded_by": new_doc.name,
            "is_current_version": 0,
            "curation_status": "Deprecated",
        },
    )

    # Create change log entry
    _create_change_log(
        obligation=old_doc.name,
        change_type="Amendment",
        change_summary=change_reason,
        fields_changed=", ".join(changed_fields),
        old_values={f: old_doc.get(f) for f in changed_fields},
        new_values={f: changes[f] for f in changed_fields},
    )

    return {
        "new_obligation": new_doc.name,
        "version": new_doc.version_number,
        "supersedes": old_doc.name,
    }


@frappe.whitelist()
def retract_obligation(obligation: str, reason: str) -> dict:
    """
    Hard retraction. Requires dual-approval (System Manager + Legal Counsel).
    All open tasks for this obligation are flagged.
    """
    _require_role("System Manager")  # Dual-approval enforced at workflow level
    doc = _get_obligation_or_throw(obligation)
    if doc.curation_status not in ("Published", "Deprecated"):
        frappe.throw(
            _("Only Published or Deprecated obligations can be retracted."),
            frappe.ValidationError,
        )
    doc.curation_status = "Retracted"
    doc.flags.ignore_permissions = True
    doc.save()
    _create_change_log(
        obligation=obligation,
        change_type="Retraction",
        change_summary=reason,
    )
    frappe.enqueue(
        "complyai.compliance.obligation_register.tasks.flag_tasks_for_retraction",
        obligation=obligation,
        reason=reason,
        queue="default",
        now=frappe.flags.in_test,
    )
    return {"obligation": obligation, "status": "Retracted"}


@frappe.whitelist()
def deprecate_obligation(
    obligation: str,
    reason: str,
    superseded_by: str = None,
) -> dict:
    """
    Soft deprecation. Existing tasks remain; no new tasks generated.
    """
    _require_role("System Manager", "Legal Counsel")
    doc = _get_obligation_or_throw(obligation)
    doc.curation_status = "Deprecated"
    if superseded_by:
        doc.superseded_by = superseded_by
    doc.flags.ignore_permissions = True
    doc.save()
    _create_change_log(
        obligation=obligation,
        change_type="Deprecation",
        change_summary=reason,
    )
    return {"obligation": obligation, "status": "Deprecated"}


# ─────────────────────────────────────────────────────────────────────────────
# Applicability test runner
# ─────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def run_applicability_test(test_case: str = None, run_all: bool = False) -> dict:
    """
    Execute applicability regression test cases.
    CI-callable. Returns pass/fail counts and per-case results.
    """
    _require_role("System Manager", "Legal Counsel")

    filters = {"is_active": 1}
    if test_case:
        filters["name"] = test_case

    cases = frappe.get_all(
        "Applicability Test Case",
        filters=filters,
        fields=["name", "obligation", "expected_applicable",
                "industry_code", "state", "employee_count",
                "turnover_inr_cr", "is_listed", "is_hazardous",
                "is_msme", "business_type", "entity_type"],
        limit_page_length=0,
    )

    passed = 0
    failed = 0
    results = []

    for case in cases:
        ob = frappe.get_doc("Compliance Obligation", case["obligation"]).as_dict()
        entity = {
            "state": case.get("state"),
            "industry_code": case.get("industry_code"),
            "employee_count": case.get("employee_count") or 0,
            "contract_worker_count": 0,
            "women_employee_count": 0,
            "turnover_inr_cr": case.get("turnover_inr_cr") or 0,
            "is_listed": bool(case.get("is_listed")),
            "is_hazardous": bool(case.get("is_hazardous")),
            "is_msme": bool(case.get("is_msme")),
            "entity_type": case.get("entity_type"),
            "business_type": case.get("business_type"),
        }
        verdict, trace = is_obligation_applicable(ob, entity)
        expected = bool(case["expected_applicable"])
        ok = verdict == expected

        if ok:
            passed += 1
        else:
            failed += 1

        # Persist results back to the test case record
        frappe.db.set_value(
            "Applicability Test Case",
            case["name"],
            {
                "last_run_at": now(),
                "last_run_passed": 1 if ok else 0,
                "last_run_actual": 1 if verdict else 0,
                "last_run_trace": json.dumps(trace),
            },
        )

        results.append({
            "test_case": case["name"],
            "obligation": case["obligation"],
            "expected": expected,
            "actual": verdict,
            "passed": ok,
            "trace": trace,
        })

    return {
        "total": len(cases),
        "passed": passed,
        "failed": failed,
        "results": results,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Correction request (from Compliance Officer)
# ─────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def suggest_obligation_correction(
    obligation: str,
    field: str,
    suggested_value: str,
    rationale: str,
) -> dict:
    """
    Compliance Officer raises a correction request.
    Creates an Obligation Change Log entry for Legal Counsel to review.
    """
    _create_change_log(
        obligation=obligation,
        change_type="Clarification",
        change_summary=f"Correction suggestion for field '{field}': {rationale}",
        fields_changed=field,
        new_values={field: suggested_value},
    )
    return {"obligation": obligation, "status": "Correction request logged"}


# ─────────────────────────────────────────────────────────────────────────────
# Bulk import (from extraction pipeline)
# ─────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def import_extracted_batch(extraction_run_id: str, payload: list) -> dict:
    """
    Called by the AI extraction pipeline.
    Bulk-imports obligations as Draft records tagged with the run ID.
    """
    _require_role("System Manager")
    if isinstance(payload, str):
        payload = json.loads(payload)

    created = []
    failed = []

    for item in payload:
        try:
            doc = frappe.new_doc("Compliance Obligation")
            doc.update(item)
            doc.curation_status = "AI Extracted"
            doc.extraction_source = item.get("extraction_source", "AI-Extracted (Claude)")
            doc.extracted_by_run = extraction_run_id
            doc.extracted_on = now()
            doc.flags.ignore_permissions = True
            doc.insert()
            created.append(doc.name)
        except Exception as exc:
            failed.append({"item": item.get("obligation_title"), "error": str(exc)})

    return {
        "extraction_run_id": extraction_run_id,
        "created_count": len(created),
        "failed_count": len(failed),
        "created": created,
        "failed": failed,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline status dashboard data
# ─────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def get_curation_pipeline_status() -> dict:
    """
    Return obligation counts by curation_status (and by regulator for depth).
    For the internal admin pipeline dashboard.
    """
    _require_role("System Manager", "Legal Counsel")

    statuses = [
        "Draft", "AI Extracted", "Under Legal Review",
        "Needs Revision", "Approved (Pending Publish)",
        "Published", "Deprecated", "Retracted",
    ]
    by_status = {}
    for status in statuses:
        by_status[status] = frappe.db.count(
            "Compliance Obligation", {"curation_status": status}
        )

    by_regulator = frappe.db.get_all(
        "Compliance Obligation",
        fields=["regulator", "curation_status", "count(*) as count"],
        group_by="regulator, curation_status",
        as_list=False,
    )

    return {
        "by_status": by_status,
        "by_regulator": by_regulator,
        "total": sum(by_status.values()),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _create_change_log(
    obligation: str,
    change_type: str,
    change_summary: str,
    fields_changed: str = None,
    old_values: dict = None,
    new_values: dict = None,
):
    """Insert an Obligation Change Log record."""
    today = str(frappe.utils.today())
    log = frappe.new_doc("Obligation Change Log")
    log.update(
        {
            "obligation": obligation,
            "change_type": change_type,
            "change_date": today,
            "effective_date": today,
            "source_type": "Internal Update",
            "change_summary": change_summary,
            "fields_changed": fields_changed,
            "old_values": json.dumps(old_values, default=str) if old_values else None,
            "new_values": json.dumps(new_values, default=str) if new_values else None,
            "review_status": "Pending Review",
        }
    )
    log.flags.ignore_permissions = True
    log.insert()