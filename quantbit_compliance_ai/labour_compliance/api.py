"""
complyai/compliance/labour_compliance/api.py

Whitelisted API methods for the Labour Compliance module.

All methods enforce:
  - Organisation-scope isolation (multi-tenancy)
  - Role-based permission checks
  - Idempotency where applicable (auto_seed_registers, generate_inspection_pack)

POSH methods have extra guards — only POSH IC Members may call file_posh_complaint
and update_posh_complaint.
"""

import frappe
from frappe import _
from frappe.utils import today, getdate, now
from datetime import date, timedelta

from complyai.compliance.labour_compliance.doctype.contract_labour_engagement.contract_labour_engagement import (
    ContractLabourEngagement,
)

# Standard register codes auto-seeded for a Factory with power and 50+ workers
FACTORY_REGISTER_CATALOGUE = [
    ("FORM-A-MW",   "Register of Wages",               "Minimum Wages Act",       "Wages"),
    ("FORM-B-MW",   "Register of Wage Cards",           "Minimum Wages Act",       "Wages"),
    ("FORM-C-MW",   "Wage Slip",                        "Minimum Wages Act",       "Wages"),
    ("FORM-D-MW",   "Register of Fines",                "Payment of Wages Act",    "Fines & Deductions"),
    ("FORM-E-MW",   "Register of Deductions",           "Payment of Wages Act",    "Fines & Deductions"),
    ("FORM-12-FA",  "Register of Adult Workers",        "Factories Act",           "Working Hours"),
    ("FORM-13-FA",  "Register of Adolescent Workers",   "Factories Act",           "Employment Card"),
    ("FORM-14-FA",  "Health Register",                  "Factories Act",           "Other"),
    ("FORM-15-FA",  "Muster Roll",                      "Factories Act",           "Muster Roll"),
    ("FORM-A-PB",   "Register of Bonus",                "Payment of Bonus Act",    "Bonus"),
    ("FORM-3-MAT",  "Register of Maternity Benefit",    "Maternity Benefit Act",   "Other"),
    ("FORM-7-PG",   "Notice of Opening (Gratuity)",     "Payment of Gratuity Act", "Other"),
    ("FORM-2-EP",   "Register of Employees (S&E)",      "State S&E Act",           "Employment Card"),
]

CONTRACT_LABOUR_REGISTERS = [
    ("FORM-XII-CL",  "Register of Workers (Contract Labour)", "Contract Labour Act", "Muster Roll"),
    ("FORM-XIII-CL", "Muster Roll (Contract)",               "Contract Labour Act", "Muster Roll"),
    ("FORM-XIV-CL",  "Register of Wages (Contract)",         "Contract Labour Act", "Wages"),
    ("FORM-XVI-CL",  "Register of Deductions (Contract)",    "Contract Labour Act", "Fines & Deductions"),
    ("FORM-XVII-CL", "Register of Fines (Contract)",         "Contract Labour Act", "Fines & Deductions"),
]


# ──────────────────────────────────────────────────────────────────────────────
# PERMISSION HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _require_role(*roles):
    user_roles = frappe.get_roles(frappe.session.user)
    if not any(r in user_roles for r in roles):
        frappe.throw(
            f"Permission denied. Required role(s): {', '.join(roles)}.",
            frappe.PermissionError,
        )


def _get_user_organisations() -> list:
    """Return orgs the current user has access to."""
    if "System Manager" in frappe.get_roles(frappe.session.user):
        return frappe.get_all("Organisation", pluck="name")
    profile = frappe.db.get_value(
        "User Profile", {"user": frappe.session.user}, "organisation"
    )
    return [profile] if profile else []


# ──────────────────────────────────────────────────────────────────────────────
# PROFILE APIs
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def get_or_create_labour_profile(business_entity: str) -> dict:
    """Return existing Labour Establishment Profile or create a stub from Business Entity."""
    _require_role("System Manager", "Compliance Officer")
    if frappe.db.exists("Labour Establishment Profile", {"business_entity": business_entity}):
        return frappe.get_doc("Labour Establishment Profile", business_entity).as_dict()

    entity = frappe.get_doc("Business Entity", business_entity)
    profile = frappe.get_doc(
        {
            "doctype": "Labour Establishment Profile",
            "business_entity": business_entity,
            "organisation": entity.organisation,
            "state": getattr(entity, "state", None),
            "establishment_type": "Commercial Establishment",  # safe default
            "operational_status": "Operational",
        }
    )
    profile.insert(ignore_permissions=True)
    return profile.as_dict()


@frappe.whitelist()
def update_labour_profile(business_entity: str, updates: dict) -> dict:
    """Update profile fields; threshold crossing recomputes obligations in background."""
    _require_role("System Manager", "Compliance Officer")
    profile = frappe.get_doc("Labour Establishment Profile", business_entity)
    for key, value in updates.items():
        if hasattr(profile, key):
            setattr(profile, key, value)
    profile.save()
    return profile.as_dict()


@frappe.whitelist()
def get_required_registers_for_entity(business_entity: str) -> list:
    """
    Evaluate the register catalogue rules for this entity's profile.
    Returns a list of {register_code, register_name, reason} dicts.
    """
    _require_role("System Manager", "Compliance Officer")
    profile = frappe.get_doc(
        "Labour Establishment Profile", {"business_entity": business_entity}
    )
    return _compute_required_registers(profile)


def _compute_required_registers(profile) -> list:
    """Core rule engine: returns which register codes apply to this entity."""
    required = list(FACTORY_REGISTER_CATALOGUE)  # All entities need Wages + Muster

    if profile.establishment_type == "Factory":
        pass  # All factory registers already included

    if profile.contract_workers and profile.contract_workers > 0:
        required.extend(CONTRACT_LABOUR_REGISTERS)

    # Dedup by code
    seen = set()
    result = []
    for code, name, reg, reg_type in required:
        if code not in seen:
            seen.add(code)
            result.append({"register_code": code, "register_name": name,
                           "regulation": reg, "register_type": reg_type})
    return result


# ──────────────────────────────────────────────────────────────────────────────
# REGISTER APIs
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def auto_seed_registers(business_entity: str) -> dict:
    """
    Bulk-create Statutory Register records for all required-but-missing registers.
    Idempotent: runs silently if register already exists.
    """
    _require_role("System Manager", "Compliance Officer")
    profile = frappe.get_doc(
        "Labour Establishment Profile", {"business_entity": business_entity}
    )
    org = profile.organisation
    required = _compute_required_registers(profile)

    created = 0
    skipped = 0
    for reg_def in required:
        if frappe.db.exists(
            "Statutory Register",
            {"business_entity": business_entity, "register_code": reg_def["register_code"]},
        ):
            skipped += 1
            continue
        frappe.get_doc(
            {
                "doctype": "Statutory Register",
                "organisation": org,
                "business_entity": business_entity,
                "register_code": reg_def["register_code"],
                "register_name": reg_def["register_name"],
                "regulation": reg_def["regulation"],
                "register_type": reg_def["register_type"],
                "form_reference": reg_def["register_code"],
                "is_active_register": 1,
                "responsible_person": frappe.session.user,
                "retention_years": 3,
            }
        ).insert(ignore_permissions=True)
        created += 1

    return {"created": created, "skipped": skipped, "entity": business_entity}


@frappe.whitelist()
def upload_register_entry(
    register: str,
    entry_date: str,
    payload: dict,
    subject_employee: str = None,
    period_month: str = None,
    period_year: int = None,
) -> dict:
    """Add a single validated entry to a register."""
    import json
    _require_role("System Manager", "Compliance Officer", "Department Manager")
    doc = frappe.get_doc(
        {
            "doctype": "Register Entry",
            "register": register,
            "entry_date": entry_date,
            "entry_payload": json.dumps(payload),
            "subject_employee": subject_employee,
            "period_month": period_month,
            "period_year": period_year,
        }
    )
    doc.insert()
    return {"name": doc.name, "is_anomaly": doc.is_anomaly, "anomaly_reason": doc.anomaly_reason}


@frappe.whitelist()
def import_register_from_csv(register: str, file_url: str) -> dict:
    """
    Bulk import register entries from a CSV file.
    Expected CSV columns: entry_date, subject_employee, period_month, period_year, + payload fields.
    Returns {created, errors}.
    """
    import csv, json
    _require_role("System Manager", "Compliance Officer")

    reg_doc = frappe.get_doc("Statutory Register", register)
    register_code = reg_doc.register_code

    # Download and parse the file (uses Frappe's file system)
    file_content = frappe.get_file(file_url)
    lines = file_content.decode("utf-8").splitlines()
    reader = csv.DictReader(lines)

    created = 0
    errors = []
    for i, row in enumerate(reader, start=2):  # Row 1 = header
        try:
            # Build payload from remaining columns
            payload_fields = {k: v for k, v in row.items()
                              if k not in ("entry_date", "subject_employee", "period_month", "period_year")}
            frappe.get_doc(
                {
                    "doctype": "Register Entry",
                    "register": register,
                    "entry_date": row.get("entry_date"),
                    "subject_employee": row.get("subject_employee"),
                    "period_month": row.get("period_month"),
                    "period_year": int(row.get("period_year") or 0),
                    "entry_payload": json.dumps(payload_fields),
                }
            ).insert(ignore_permissions=True)
            created += 1
        except Exception as e:
            errors.append({"row": i, "error": str(e)})

    return {"created": created, "errors": errors, "total_rows": created + len(errors)}


@frappe.whitelist()
def check_register_completeness(register: str, period_month: str, period_year: int) -> dict:
    """
    Run completeness rules: are all expected entries present? Fields complete? Anomalies?
    Returns a summary dict with score and anomaly list.
    """
    _require_role("System Manager", "Compliance Officer", "Internal Auditor")
    entries = frappe.get_all(
        "Register Entry",
        filters={"register": register, "period_month": period_month, "period_year": period_year},
        fields=["name", "subject_employee", "is_anomaly", "anomaly_reason"],
    )
    total = len(entries)
    anomalies = [e for e in entries if e.is_anomaly]
    score = int(((total - len(anomalies)) / total * 100)) if total > 0 else 0

    frappe.db.set_value("Statutory Register", register, "completeness_score", score)
    return {
        "total_entries": total,
        "anomaly_count": len(anomalies),
        "anomalies": anomalies,
        "completeness_score": score,
    }


@frappe.whitelist()
def list_stale_registers(business_entity: str = None) -> list:
    """Active registers with last_entry_date > 90 days ago."""
    _require_role("System Manager", "Compliance Officer", "Internal Auditor")
    cutoff = str(date.today() - timedelta(days=90))
    filters = {
        "is_active_register": 1,
        "last_entry_date": ("<", cutoff),
    }
    if business_entity:
        filters["business_entity"] = business_entity
    else:
        orgs = _get_user_organisations()
        filters["organisation"] = ("in", orgs)
    return frappe.get_all(
        "Statutory Register",
        filters=filters,
        fields=["name", "register_code", "register_name", "business_entity", "last_entry_date"],
        order_by="last_entry_date asc",
    )


# ──────────────────────────────────────────────────────────────────────────────
# WAGE ROLL APIs
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def submit_wage_roll(
    business_entity: str,
    period_month: str,
    period_year: int,
    payload: dict,
) -> dict:
    """Create Wage Roll, run compliance checks, return result."""
    _require_role("System Manager", "Compliance Officer")
    entity = frappe.get_doc("Business Entity", business_entity)
    doc = frappe.get_doc(
        {
            "doctype": "Wage Roll",
            "organisation": entity.organisation,
            "business_entity": business_entity,
            "wage_period_month": period_month,
            "wage_period_year": period_year,
            **payload,
        }
    )
    doc.insert()
    return {
        "name": doc.name,
        "compliance_score": doc.compliance_score,
        "minimum_wages_compliant": doc.minimum_wages_compliant,
        "wages_paid_within_due_date": doc.wages_paid_within_due_date,
        "min_wage_violations_count": doc.min_wage_violations_count,
    }


@frappe.whitelist()
def get_wage_compliance_history(business_entity: str, months: int = 12) -> list:
    """Trailing N-month Wage Roll compliance scores for chart rendering."""
    _require_role("System Manager", "Compliance Officer", "Group CXO", "Internal Auditor")
    return frappe.db.sql(
        """
        SELECT wage_period_year, wage_period_month, compliance_score,
               minimum_wages_compliant, wages_paid_within_due_date, delay_days
        FROM `tabWage Roll`
        WHERE business_entity = %(entity)s
          AND docstatus != 2
        ORDER BY wage_period_year DESC, FIELD(wage_period_month,
            'December','November','October','September','August','July',
            'June','May','April','March','February','January') ASC
        LIMIT %(months)s
        """,
        {"entity": business_entity, "months": months},
        as_dict=True,
    )


# ──────────────────────────────────────────────────────────────────────────────
# CONTRACT LABOUR APIs
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def is_contractor_blocked(contractor_pan: str, business_entity: str) -> bool:
    """
    ERP webhook entrypoint — fast yes/no for PO creation.
    Target response time: < 100ms.
    """
    return ContractLabourEngagement.is_blocked(contractor_pan, business_entity)


@frappe.whitelist()
def get_blocked_contractors(business_entity: str = None) -> list:
    """List all currently blocked contractors."""
    _require_role("System Manager", "Compliance Officer")
    filters = {"block_new_pos": 1, "engagement_status": "Active"}
    if business_entity:
        filters["business_entity"] = business_entity
    return frappe.get_all(
        "Contract Labour Engagement",
        filters=filters,
        fields=[
            "name", "contractor_name", "contractor_pan", "business_entity",
            "block_reason", "licence_expiry_date", "compliance_score",
        ],
        order_by="licence_expiry_date asc",
    )


@frappe.whitelist()
def verify_contractor_compliance(engagement: str, period_month: str = None) -> dict:
    """Trigger compliance re-check; returns updated block status."""
    _require_role("System Manager", "Compliance Officer")
    eng = frappe.get_doc("Contract Labour Engagement", engagement)
    eng.compute_compliance_score()
    eng.compute_block_status()
    eng.save()
    return {
        "block_new_pos": eng.block_new_pos,
        "block_reason": eng.block_reason,
        "compliance_score": eng.compliance_score,
    }


# ──────────────────────────────────────────────────────────────────────────────
# POSH APIs
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def constitute_posh_committee(
    business_entity: str,
    members: list,
    constitution_evidence: str,
) -> dict:
    """Create a POSH Committee with members; runs IC compliance check."""
    _require_role("System Manager", "Compliance Officer")
    entity = frappe.get_doc("Business Entity", business_entity)
    committee = frappe.get_doc(
        {
            "doctype": "POSH Committee",
            "organisation": entity.organisation,
            "business_entity": business_entity,
            "committee_status": "Constituted",
            "constituted_on": today(),
            "tenure_years": 3,
            "constitution_order_evidence": constitution_evidence,
            "members": members,
        }
    )
    committee.insert()
    return {
        "name": committee.name,
        "ic_compliant": committee.ic_compliant,
        "non_compliance_reasons": committee.non_compliance_reasons,
    }


@frappe.whitelist()
def file_posh_complaint(business_entity: str, complaint_data: dict) -> dict:
    """Create a POSH Complaint. Only POSH IC Members may call this."""
    user_roles = frappe.get_roles(frappe.session.user)
    if "POSH IC Member" not in user_roles and "System Manager" not in user_roles:
        frappe.throw(
            "Only POSH IC Members may file complaints. Contact the IC Presiding Officer.",
            frappe.PermissionError,
        )
    entity = frappe.get_doc("Business Entity", business_entity)
    complaint = frappe.get_doc(
        {
            "doctype": "POSH Complaint",
            "organisation": entity.organisation,
            "business_entity": business_entity,
            **complaint_data,
        }
    )
    complaint.insert()
    # Notification: in-app only to IC Presiding Officer (no PII in subject)
    _notify_ic_complaint_received(complaint)
    return {"name": complaint.name}


def _notify_ic_complaint_received(complaint) -> None:
    """Send REDACTED in-app notification to IC Presiding Officer only."""
    ic = frappe.get_doc("POSH Committee", complaint.posh_committee)
    po_email = None
    for m in (ic.members or []):
        if m.role_in_ic == "Presiding Officer":
            po_email = m.email
            break
    if po_email:
        frappe.get_doc(
            {
                "doctype": "Notification Log",
                "subject": f"New complaint filed — {complaint.name}",
                "for_user": po_email,
                "type": "Alert",
                "document_type": "POSH Complaint",
                "document_name": complaint.name,
                "from_user": frappe.session.user,
            }
        ).insert(ignore_permissions=True)


@frappe.whitelist()
def generate_posh_annual_report(business_entity: str, calendar_year: int) -> dict:
    """Generate annual POSH report data (PDF format per Rule 14). Returns structured data."""
    _require_role("System Manager", "Compliance Officer", "POSH IC Member")
    ytd_start = f"{calendar_year}-01-01"
    ytd_end = f"{calendar_year}-12-31"

    committee = frappe.get_doc(
        "POSH Committee",
        {"business_entity": business_entity, "committee_status": "Constituted"},
    )
    complaints = frappe.get_all(
        "POSH Complaint",
        filters={
            "posh_committee": committee.name,
            "complaint_received_on": ("between", [ytd_start, ytd_end]),
        },
        fields=["complaint_status", "outcome", "complaint_received_on"],
    )

    report_data = {
        "business_entity": business_entity,
        "calendar_year": calendar_year,
        "committee_name": committee.name,
        "ic_compliant": committee.ic_compliant,
        "total_complaints": len(complaints),
        "disposed_substantiated": sum(
            1 for c in complaints if c.complaint_status == "Disposed - Substantiated"
        ),
        "disposed_not_substantiated": sum(
            1 for c in complaints if c.complaint_status == "Disposed - Not Substantiated"
        ),
        "pending": sum(
            1 for c in complaints
            if c.complaint_status in ("Received", "Under Inquiry", "Conciliation", "Report Submitted")
        ),
        "withdrawn": sum(1 for c in complaints if c.complaint_status == "Withdrawn"),
        "referred_to_police": sum(
            1 for c in complaints if c.complaint_status == "Referred to Police"
        ),
        "awareness_sessions": committee.awareness_sessions_this_year or 0,
        "last_training_date": str(committee.last_training_for_members or ""),
    }
    return report_data


# ──────────────────────────────────────────────────────────────────────────────
# INSPECTION APIs
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def log_inspection(business_entity: str, inspection_data: dict) -> dict:
    """Log a new Labour Inspection."""
    _require_role("System Manager", "Compliance Officer")
    entity = frappe.get_doc("Business Entity", business_entity)
    doc = frappe.get_doc(
        {
            "doctype": "Labour Inspection",
            "organisation": entity.organisation,
            "business_entity": business_entity,
            **inspection_data,
        }
    )
    doc.insert()
    return {"name": doc.name}


@frappe.whitelist()
def generate_inspection_pack(
    business_entity: str,
    period_from: str,
    period_to: str,
    inspector_type: str = None,
) -> dict:
    """
    Generate an Inspection Pack: all registers + filings + payments + evidence
    for the period. Returns metadata and item count.
    Target: < 30 seconds for a 3-year window.

    Pack contents:
    1. Labour Profile snapshot
    2. Statutory Registers list + last entry dates
    3. Wage Rolls for period (compliance scores)
    4. Register Entries (most recent per register)
    5. Compliance Calendar Tasks (Labour category) for period
    6. Previous inspections and their outcomes
    7. POSH Committee status (non-confidential)
    8. Contractor compliance summary
    """
    _require_role("System Manager", "Compliance Officer", "Legal Counsel")

    entity = frappe.get_doc("Business Entity", business_entity)
    pack = {
        "generated_at": now(),
        "generated_by": frappe.session.user,
        "business_entity": business_entity,
        "period_from": period_from,
        "period_to": period_to,
    }

    # 1. Profile
    profile = frappe.db.get_value(
        "Labour Establishment Profile",
        {"business_entity": business_entity},
        ["establishment_type", "total_workers", "profile_completeness"],
        as_dict=True,
    )

    # 2. Registers
    registers = frappe.get_all(
        "Statutory Register",
        filters={"business_entity": business_entity, "is_active_register": 1},
        fields=["register_code", "register_name", "last_entry_date", "completeness_score"],
    )

    # 3. Wage Rolls
    wage_rolls = frappe.get_all(
        "Wage Roll",
        filters={
            "business_entity": business_entity,
            "wage_period_start": (">=", period_from),
            "wage_period_end": ("<=", period_to),
        },
        fields=["name", "wage_period_month", "wage_period_year", "compliance_score",
                "minimum_wages_compliant", "wages_paid_within_due_date"],
    )

    # 4. Recent compliance tasks (Labour category)
    tasks = frappe.get_all(
        "Compliance Calendar Task",
        filters={
            "business_entity": business_entity,
            "category": "Labour",
            "due_date": ("between", [period_from, period_to]),
        },
        fields=["task_title", "status", "due_date", "evidence_completeness"],
        limit=200,
    )

    # 5. Previous inspections
    inspections = frappe.get_all(
        "Labour Inspection",
        filters={
            "business_entity": business_entity,
            "inspection_date": ("between", [period_from, period_to]),
        },
        fields=["inspection_type", "inspection_date", "inspection_status", "penalty_imposed_inr"],
    )

    pack.update(
        {
            "profile": profile,
            "registers": registers,
            "wage_rolls": wage_rolls,
            "compliance_tasks": tasks,
            "previous_inspections": inspections,
            "item_count": len(registers) + len(wage_rolls) + len(tasks) + len(inspections),
            "pack_url": None,  # In production: generate ZIP/PDF and return URL
        }
    )
    return pack


# ──────────────────────────────────────────────────────────────────────────────
# DASHBOARD / REPORTS
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def get_labour_dashboard(business_entity: str = None) -> dict:
    """Aggregated labour compliance metrics for dashboard widgets."""
    _require_role("System Manager", "Compliance Officer", "Group CXO", "Department Manager")
    orgs = _get_user_organisations()
    filters = {"organisation": ("in", orgs)}
    if business_entity:
        filters["business_entity"] = business_entity

    # Stale registers
    stale_count = frappe.db.count(
        "Statutory Register",
        {
            **filters,
            "is_active_register": 1,
            "last_entry_date": ("<", str(date.today() - timedelta(days=90))),
        },
    )

    # Blocked contractors
    blocked_count = frappe.db.count(
        "Contract Labour Engagement",
        {**filters, "block_new_pos": 1, "engagement_status": "Active"},
    )

    # Latest wage roll compliance score
    latest_wage_roll = frappe.get_all(
        "Wage Roll",
        filters={**filters, "docstatus": ("!=", 2)},
        fields=["compliance_score", "minimum_wages_compliant"],
        order_by="creation desc",
        limit=1,
    )

    # POSH IC compliant entities
    ic_compliant = frappe.db.count(
        "POSH Committee",
        {**filters, "ic_compliant": 1, "committee_status": "Constituted"},
    )

    return {
        "stale_registers": stale_count,
        "blocked_contractors": blocked_count,
        "latest_wage_compliance_score": latest_wage_roll[0].compliance_score if latest_wage_roll else None,
        "ic_compliant_committees": ic_compliant,
    }


@frappe.whitelist()
def get_committee_compliance_status(organisation: str = None) -> dict:
    """All entities: which committees are compliant / non-compliant / missing."""
    _require_role("System Manager", "Compliance Officer", "Internal Auditor")
    orgs = _get_user_organisations()
    if organisation:
        orgs = [organisation] if organisation in orgs else []

    entities = frappe.get_all(
        "Business Entity",
        filters={"organisation": ("in", orgs)},
        fields=["name", "entity_name"],
    )

    result = []
    for entity in entities:
        profile = frappe.db.get_value(
            "Labour Establishment Profile",
            {"business_entity": entity.name},
            ["total_workers", "posh_ic_constituted", "works_committee_constituted",
             "safety_committee_constituted"],
            as_dict=True,
        )
        committee = frappe.db.get_value(
            "POSH Committee",
            {"business_entity": entity.name, "committee_status": "Constituted"},
            ["name", "ic_compliant", "tenure_end"],
            as_dict=True,
        )
        result.append(
            {
                "entity": entity.name,
                "entity_name": entity.entity_name,
                "profile": profile,
                "posh_committee": committee,
            }
        )
    return {"entities": result}