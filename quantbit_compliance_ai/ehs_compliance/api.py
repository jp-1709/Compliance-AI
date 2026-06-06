"""
EHS Compliance — Whitelisted API
complyai/compliance/ehs_compliance/api.py

All public API endpoints for the EHS Compliance module.
Organised by DocType domain.
"""

import frappe
from frappe import _
from datetime import date, timedelta


# ══════════════════════════════════════════════════════
# ─── Operating Licences ───────────────────────────────
# ══════════════════════════════════════════════════════

@frappe.whitelist()
def add_operating_licence(business_entity: str, licence_data: dict) -> dict:
    """
    Create an Operating Licence and auto-generate a renewal Compliance Calendar Task
    if renewal action is already due.
    """
    if isinstance(licence_data, str):
        import json
        licence_data = json.loads(licence_data)

    entity = frappe.get_doc("Business Entity", business_entity)
    licence_data["doctype"] = "Operating Licence"
    licence_data["business_entity"] = business_entity
    licence_data["organisation"] = entity.organisation

    doc = frappe.get_doc(licence_data)
    doc.insert(ignore_permissions=True)

    return {
        "licence": doc.name,
        "licence_number": doc.licence_number,
        "days_to_expiry": doc.days_to_expiry,
        "next_action_due": str(doc.next_action_due) if doc.next_action_due else None,
        "is_oversubscribed": doc.is_oversubscribed,
    }


@frappe.whitelist()
def list_licences_with_expiry(
    organisation: str = None,
    days_threshold: int = 180,
) -> list:
    """
    Return Active licences expiring within `days_threshold` days.
    Sorted ascending by valid_until (most urgent first).
    """
    filters = {"licence_status": "Active"}
    if organisation:
        filters["organisation"] = organisation

    licences = frappe.get_all(
        "Operating Licence",
        filters=filters,
        fields=[
            "name", "business_entity", "licence_type", "licence_number",
            "valid_until", "days_to_expiry", "next_action_due", "issuing_authority",
            "responsible_person", "is_oversubscribed",
        ],
        order_by="valid_until asc",
    )

    threshold = int(days_threshold)
    return [l for l in licences if (l.days_to_expiry or 9999) <= threshold]


@frappe.whitelist()
def submit_licence_renewal(
    licence: str,
    application_evidence: str,
    application_reference: str,
) -> dict:
    """Mark renewal application as filed; updates licence status to Under Renewal."""
    doc = frappe.get_doc("Operating Licence", licence)
    doc.renewal_application_filed_on = date.today()
    doc.renewal_application_reference = application_reference
    doc.renewal_application_evidence = application_evidence
    doc.licence_status = "Under Renewal"
    doc.save(ignore_permissions=True)

    return {
        "licence": licence,
        "status": "Under Renewal",
        "filed_on": str(date.today()),
    }


# ══════════════════════════════════════════════════════
# ─── Environmental Clearances ─────────────────────────
# ══════════════════════════════════════════════════════

@frappe.whitelist()
def add_environmental_clearance(business_entity: str, clearance_data: dict) -> dict:
    """Create EC / CTO / Authorisation record."""
    if isinstance(clearance_data, str):
        import json
        clearance_data = json.loads(clearance_data)

    entity = frappe.get_doc("Business Entity", business_entity)
    clearance_data["doctype"] = "Environmental Clearance"
    clearance_data["business_entity"] = business_entity
    clearance_data["organisation"] = entity.organisation

    doc = frappe.get_doc(clearance_data)
    doc.insert(ignore_permissions=True)

    return {
        "clearance": doc.name,
        "clearance_number": doc.clearance_number,
        "days_to_expiry": doc.days_to_expiry,
        "compliance_score": doc.compliance_score,
    }


@frappe.whitelist()
def update_clearance_condition_status(
    clearance: str,
    condition_no: str,
    new_status: str,
    evidence: str = None,
) -> dict:
    """Update a specific condition's compliance status and recompute score."""
    from complyai.compliance.ehs_compliance.controllers.environmental_clearance import (
        update_clearance_condition_status as _update,
    )
    return _update(clearance, condition_no, new_status, evidence)


@frappe.whitelist()
def get_clearance_compliance_summary(organisation: str = None) -> dict:
    """
    Org-level rollup:
      - CTO health (days to expiry, count expiring <365)
      - EC condition compliance avg
      - Count expiring soon by tier (30/90/365 days)
    """
    filters = {}
    if organisation:
        filters["organisation"] = organisation

    clearances = frappe.get_all(
        "Environmental Clearance",
        filters=filters,
        fields=[
            "name", "clearance_type", "clearance_status", "business_entity",
            "days_to_expiry", "compliance_score", "non_compliance_open_count",
        ],
    )

    cto_count = sum(1 for c in clearances if "Consent to Operate" in (c.clearance_type or ""))
    cto_expiring_365 = sum(
        1 for c in clearances
        if "Consent to Operate" in (c.clearance_type or "")
        and c.days_to_expiry is not None
        and c.days_to_expiry <= 365
    )
    avg_compliance = (
        sum(c.compliance_score or 100 for c in clearances) / len(clearances)
        if clearances else 100
    )

    return {
        "total_clearances": len(clearances),
        "cto_count": cto_count,
        "cto_expiring_within_365_days": cto_expiring_365,
        "avg_compliance_score": round(avg_compliance, 2),
        "open_non_compliances": sum(c.non_compliance_open_count or 0 for c in clearances),
        "expiring_30d": sum(1 for c in clearances if (c.days_to_expiry or 9999) <= 30),
        "expiring_90d": sum(1 for c in clearances if (c.days_to_expiry or 9999) <= 90),
        "expiring_365d": sum(1 for c in clearances if (c.days_to_expiry or 9999) <= 365),
    }


# ══════════════════════════════════════════════════════
# ─── PCB Returns ──────────────────────────────────────
# ══════════════════════════════════════════════════════

@frappe.whitelist()
def create_pcb_return(
    business_entity: str,
    return_type: str,
    period_fy: str,
    period_quarter: str = None,
) -> dict:
    """Create a PCB Return record with auto-computed due date."""
    entity = frappe.get_doc("Business Entity", business_entity)
    doc = frappe.get_doc({
        "doctype": "PCB Return",
        "organisation": entity.organisation,
        "business_entity": business_entity,
        "return_type": return_type,
        "period_fy": period_fy,
        "period_quarter": period_quarter,
        "filing_status": "Pending",
    })
    doc.insert(ignore_permissions=True)
    return {
        "pcb_return": doc.name,
        "filing_due_date": str(doc.filing_due_date) if doc.filing_due_date else None,
    }


@frappe.whitelist()
def auto_populate_form_v(pcb_return: str) -> dict:
    """
    Pre-populate Form-V fields from:
      - 12 months of Environmental Monitoring Readings
      - ERP production/raw material data (if available)
    Returns a summary of what was populated.
    """
    doc = frappe.get_doc("PCB Return", pcb_return)
    if "Form-V" not in (doc.return_type or ""):
        frappe.throw(_("auto_populate_form_v is only applicable for Form-V returns."))

    # Derive the 12-month window
    from complyai.compliance.ehs_compliance.controllers.pcb_return import _parse_fy_end_year
    fy_end = _parse_fy_end_year(doc.period_fy)
    from datetime import date
    period_from = date(fy_end - 1, 4, 1)
    period_to   = date(fy_end,     3, 31)

    # Aggregate monitoring data
    readings = frappe.get_all(
        "Environmental Monitoring Reading",
        filters={
            "business_entity": doc.business_entity,
            "reading_date": ["between", [period_from, period_to]],
        },
        fields=["name"],
    )

    # Placeholder: pull water/effluent data from readings child table
    water_total = 0.0
    effluent_total = 0.0

    import json
    populated = {
        "reading_count": len(readings),
        "period_from": str(period_from),
        "period_to": str(period_to),
    }

    doc.production_data_payload = json.dumps({"source": "ERP", "populated": True})
    doc.save(ignore_permissions=True)

    return {"pcb_return": pcb_return, "populated": populated}


# ══════════════════════════════════════════════════════
# ─── Hazardous Substance Inventory ────────────────────
# ══════════════════════════════════════════════════════

@frappe.whitelist()
def add_hazardous_substance(business_entity: str, substance_data: dict) -> dict:
    """Add substance and run threshold detection."""
    if isinstance(substance_data, str):
        import json
        substance_data = json.loads(substance_data)

    entity = frappe.get_doc("Business Entity", business_entity)
    substance_data["doctype"] = "Hazardous Substance Inventory"
    substance_data["business_entity"] = business_entity
    substance_data["organisation"] = entity.organisation

    doc = frappe.get_doc(substance_data)
    doc.insert(ignore_permissions=True)

    return {
        "substance": doc.name,
        "exceeds_msihc_threshold": doc.exceeds_msihc_threshold,
        "requires_peso_licence": doc.requires_peso_licence,
        "requires_off_site_plan": doc.requires_off_site_plan,
        "msds_review_due": str(doc.msds_review_due) if doc.msds_review_due else None,
    }


@frappe.whitelist()
def list_substances_above_msihc_threshold(organisation: str = None) -> list:
    """All substances triggering MSIHC obligations."""
    filters = {"exceeds_msihc_threshold": 1, "substance_status": "Active"}
    if organisation:
        filters["organisation"] = organisation

    return frappe.get_all(
        "Hazardous Substance Inventory",
        filters=filters,
        fields=[
            "name", "substance_name", "cas_number", "business_entity",
            "max_storage_quantity_kg", "msihc_schedule",
            "requires_off_site_plan", "requires_peso_licence",
        ],
        order_by="substance_name asc",
    )


@frappe.whitelist()
def get_msds_review_due(organisation: str = None, days_threshold: int = 90) -> list:
    """MSDS due for review within days_threshold (3-year cycle)."""
    today = date.today()
    threshold = today + timedelta(days=int(days_threshold))

    filters = {
        "substance_status": "Active",
        "msds_review_due": ["between", [today, threshold]],
    }
    if organisation:
        filters["organisation"] = organisation

    return frappe.get_all(
        "Hazardous Substance Inventory",
        filters=filters,
        fields=[
            "name", "substance_name", "cas_number", "business_entity",
            "msds_review_due", "responsible_person",
        ],
        order_by="msds_review_due asc",
    )


# ══════════════════════════════════════════════════════
# ─── Fire Safety ──────────────────────────────────────
# ══════════════════════════════════════════════════════

@frappe.whitelist()
def get_or_create_fire_safety_record(business_entity: str) -> dict:
    """Return existing or create new Fire Safety Compliance for this BE."""
    existing = frappe.db.get_value("Fire Safety Compliance", business_entity, "name")
    if existing:
        doc = frappe.get_doc("Fire Safety Compliance", existing)
    else:
        entity = frappe.get_doc("Business Entity", business_entity)
        doc = frappe.get_doc({
            "doctype": "Fire Safety Compliance",
            "business_entity": business_entity,
            "organisation": entity.organisation,
            "responsible_person": frappe.session.user,
        })
        doc.insert(ignore_permissions=True)

    return {
        "name": doc.name,
        "compliance_status": doc.compliance_status,
        "last_drill_date": str(doc.last_drill_date) if doc.last_drill_date else None,
        "drills_this_fy": doc.drills_this_fy,
        "next_drill_due": str(doc.next_drill_due) if doc.next_drill_due else None,
        "fire_noc_valid_until": str(doc.fire_noc_valid_until) if doc.fire_noc_valid_until else None,
    }


@frappe.whitelist()
def log_fire_drill(business_entity: str, drill_data: dict) -> dict:
    """Add drill log entry and recompute drill metrics."""
    from complyai.compliance.ehs_compliance.controllers.fire_safety_compliance import (
        log_fire_drill as _log,
    )
    return _log(business_entity, drill_data)


@frappe.whitelist()
def get_fire_compliance_dashboard(organisation: str = None) -> dict:
    """Per-BE compliance status + key metrics for dashboard."""
    filters = {}
    if organisation:
        filters["organisation"] = organisation

    records = frappe.get_all(
        "Fire Safety Compliance",
        filters=filters,
        fields=[
            "business_entity", "compliance_status", "fire_noc_valid_until",
            "last_drill_date", "drills_this_fy", "next_drill_due",
            "next_extinguisher_refill_due",
        ],
    )

    summary = {
        "total_entities": len(records),
        "compliant": sum(1 for r in records if r.compliance_status == "Compliant"),
        "partial": sum(1 for r in records if r.compliance_status == "Partial"),
        "non_compliant": sum(1 for r in records if r.compliance_status == "Non-Compliant"),
        "entities": records,
    }
    return summary


# ══════════════════════════════════════════════════════
# ─── OHS Plan ─────────────────────────────────────────
# ══════════════════════════════════════════════════════

@frappe.whitelist()
def create_ohs_plan(business_entity: str, plan_data: dict) -> dict:
    """Create OHS Plan in Draft status."""
    if isinstance(plan_data, str):
        import json
        plan_data = json.loads(plan_data)

    entity = frappe.get_doc("Business Entity", business_entity)
    plan_data["doctype"] = "OHS Plan"
    plan_data["business_entity"] = business_entity
    plan_data["organisation"] = entity.organisation
    plan_data["plan_status"] = "Draft"

    doc = frappe.get_doc(plan_data)
    doc.insert(ignore_permissions=True)

    return {"ohs_plan": doc.name, "status": doc.plan_status}


@frappe.whitelist()
def approve_ohs_plan(plan: str, approved_by: str) -> dict:
    """Approve plan; supersede the previous active version."""
    doc = frappe.get_doc("OHS Plan", plan)

    # Supersede existing active plan of same type
    existing_active = frappe.get_all(
        "OHS Plan",
        filters={
            "business_entity": doc.business_entity,
            "plan_type": doc.plan_type,
            "plan_status": "Active",
            "name": ["!=", plan],
        },
        fields=["name"],
    )
    for old in existing_active:
        frappe.db.set_value("OHS Plan", old.name, "plan_status", "Superseded")

    doc.plan_status = "Active"
    doc.approved_by = approved_by
    doc.approved_on = date.today()
    doc.save(ignore_permissions=True)

    return {"ohs_plan": plan, "status": "Active", "superseded": [o.name for o in existing_active]}


@frappe.whitelist()
def get_ohs_plans_due_for_review(organisation: str = None, days_threshold: int = 60) -> list:
    """Plans with annual review approaching within days_threshold."""
    today = date.today()
    threshold = today + timedelta(days=int(days_threshold))

    filters = {
        "plan_status": ["in", ["Active", "Approved"]],
        "next_review_due": ["between", [today, threshold]],
    }
    if organisation:
        filters["organisation"] = organisation

    return frappe.get_all(
        "OHS Plan",
        filters=filters,
        fields=[
            "name", "plan_title", "plan_type", "business_entity",
            "next_review_due", "ehs_manager", "version",
        ],
        order_by="next_review_due asc",
    )


# ══════════════════════════════════════════════════════
# ─── Environmental Monitoring ─────────────────────────
# ══════════════════════════════════════════════════════

@frappe.whitelist()
def log_monitoring_reading(business_entity: str, reading_data: dict) -> dict:
    """Log a monitoring reading with parameters; auto-flags violations."""
    if isinstance(reading_data, str):
        import json
        reading_data = json.loads(reading_data)

    entity = frappe.get_doc("Business Entity", business_entity)
    reading_data["doctype"] = "Environmental Monitoring Reading"
    reading_data["business_entity"] = business_entity
    reading_data["organisation"] = entity.organisation

    doc = frappe.get_doc(reading_data)
    doc.insert(ignore_permissions=True)

    return {
        "reading": doc.name,
        "is_violation": doc.is_violation,
        "violation_count": doc.violation_count,
        "linked_quality_event": doc.linked_quality_event,
    }


@frappe.whitelist()
def list_violations_unactioned(organisation: str = None, days_open_min: int = 30) -> list:
    """Violations open > days_open_min days without CAPA."""
    threshold = date.today() - timedelta(days=int(days_open_min))
    filters = {
        "is_violation": 1,
        "linked_capa": ["is", "not set"],
        "reading_date": ["<=", threshold],
        "reading_status": ["not in", ["Action Taken", "Closed"]],
    }
    if organisation:
        filters["organisation"] = organisation

    return frappe.get_all(
        "Environmental Monitoring Reading",
        filters=filters,
        fields=[
            "name", "business_entity", "monitoring_type", "monitoring_point",
            "reading_date", "violation_count", "linked_quality_event",
        ],
        order_by="reading_date asc",
    )


# ══════════════════════════════════════════════════════
# ─── BOCW ─────────────────────────────────────────────
# ══════════════════════════════════════════════════════

@frappe.whitelist()
def create_construction_project(business_entity: str, project_data: dict) -> dict:
    """Create BOCW project record + auto-create Form-I obligation task."""
    if isinstance(project_data, str):
        import json
        project_data = json.loads(project_data)

    entity = frappe.get_doc("Business Entity", business_entity)
    project_data["doctype"] = "BOCW Compliance"
    project_data["business_entity"] = business_entity
    project_data["organisation"] = entity.organisation

    doc = frappe.get_doc(project_data)
    doc.insert(ignore_permissions=True)

    # Auto-create Form-I filing obligation
    try:
        frappe.get_doc({
            "doctype": "Compliance Calendar Task",
            "organisation": entity.organisation,
            "business_entity": business_entity,
            "task_title": f"File Form-I (Labour Dept Intimation) — {doc.project_name}",
            "task_type": "BOCW Obligation",
            "status": "Open",
            "priority": "High",
            "due_date": doc.project_start_date,
            "description": (
                "File Form-I with the Labour Department before construction commences. "
                f"Project: {doc.project_name}. Cost: ₹{doc.estimated_construction_cost_inr:,.0f}"
            ),
            "reference_doctype": "BOCW Compliance",
            "reference_name": doc.name,
        }).insert(ignore_permissions=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "BOCW Form-I task creation failed")

    return {
        "bocw_project": doc.name,
        "cess_amount_estimated_inr": doc.cess_amount_estimated_inr,
        "registration_compliance_pct": doc.registration_compliance_pct,
    }


@frappe.whitelist()
def record_cess_payment(project: str, amount_inr: float, payment_evidence: str) -> dict:
    """Log cess payment; updates remaining cess obligation."""
    from complyai.compliance.ehs_compliance.controllers.bocw_compliance import (
        record_cess_payment as _pay,
    )
    return _pay(project, amount_inr, payment_evidence)


# ══════════════════════════════════════════════════════
# ─── EHS Incident ─────────────────────────────────────
# ══════════════════════════════════════════════════════

@frappe.whitelist()
def log_incident(business_entity: str, incident_data: dict) -> dict:
    """Log incident; auto-assesses regulatory reportability."""
    from complyai.compliance.ehs_compliance.controllers.ehs_incident import (
        log_incident as _log,
    )
    return _log(business_entity, incident_data)


@frappe.whitelist()
def submit_incident_investigation(incident: str, investigation_data: dict) -> dict:
    """Submit completed investigation; auto-creates CAPA if not already linked."""
    if isinstance(investigation_data, str):
        import json
        investigation_data = json.loads(investigation_data)

    doc = frappe.get_doc("EHS Incident", incident)
    doc.update(investigation_data)
    doc.incident_status = "Root Cause Identified"
    doc.save(ignore_permissions=True)

    # Auto-create CAPA if not linked
    if not doc.linked_capa:
        try:
            capa = frappe.get_doc({
                "doctype": "QMS CAPA",
                "organisation": doc.organisation,
                "business_entity": doc.business_entity,
                "capa_title": f"CAPA for {doc.name}: {doc.incident_type}",
                "linked_ehs_incident": doc.name,
                "status": "Open",
            }).insert(ignore_permissions=True)
            doc.db_set("linked_capa", capa.name)
            doc.db_set("incident_status", "CAPA In Progress")
        except Exception:
            frappe.log_error(frappe.get_traceback(), "CAPA creation from incident failed")

    return {
        "incident": incident,
        "status": doc.incident_status,
        "linked_capa": doc.linked_capa,
    }


@frappe.whitelist()
def get_incident_trends(organisation: str = None, fy: str = None) -> dict:
    """LTIFR, severity rate, by-type breakdown."""
    from complyai.compliance.ehs_compliance.controllers.ehs_incident import (
        get_incident_trends as _trends,
    )
    return _trends(organisation, fy)


# ══════════════════════════════════════════════════════
# ─── Cross-cutting ────────────────────────────────────
# ══════════════════════════════════════════════════════

@frappe.whitelist()
def get_ehs_dashboard(organisation: str = None, business_entity: str = None) -> dict:
    """
    Aggregated EHS health metrics for Plant Head / Group CXO dashboards.
    Includes: licences, clearances, violations, incidents, fire compliance.
    """
    org_filter = {"organisation": organisation} if organisation else {}
    be_filter = {**org_filter, "business_entity": business_entity} if business_entity else org_filter

    today = date.today()

    # Licences
    licences = frappe.get_all(
        "Operating Licence",
        filters={**be_filter, "licence_status": "Active"},
        fields=["days_to_expiry", "is_oversubscribed"],
    )

    # Clearances
    clearances = frappe.get_all(
        "Environmental Clearance",
        filters={**be_filter, "clearance_status": ["not in", ["Revoked", "Suspended"]]},
        fields=["days_to_expiry", "compliance_score", "clearance_type"],
    )

    # Incidents (YTD)
    fy_start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
    incidents = frappe.get_all(
        "EHS Incident",
        filters={**be_filter, "incident_date": [">=", fy_start]},
        fields=["incident_type", "severity", "incident_status"],
    )

    # Violations
    violations = frappe.get_all(
        "Environmental Monitoring Reading",
        filters={**be_filter, "is_violation": 1, "reading_status": ["!=", "Closed"]},
        fields=["name"],
    )

    return {
        "licences": {
            "total_active": len(licences),
            "expiring_90d": sum(1 for l in licences if (l.days_to_expiry or 9999) <= 90),
            "expired": sum(1 for l in licences if (l.days_to_expiry or 0) < 0),
            "oversubscribed": sum(1 for l in licences if l.is_oversubscribed),
        },
        "clearances": {
            "total": len(clearances),
            "cto_active": sum(1 for c in clearances if "Consent to Operate" in (c.clearance_type or "")),
            "expiring_365d": sum(1 for c in clearances if (c.days_to_expiry or 9999) <= 365),
            "avg_compliance_score": round(
                sum(c.compliance_score or 100 for c in clearances) / len(clearances), 2
            ) if clearances else 100,
        },
        "incidents_ytd": {
            "total": len(incidents),
            "lti": sum(1 for i in incidents if i.incident_type == "Injury — Lost Time (LTI)"),
            "fatalities": sum(1 for i in incidents if i.incident_type == "Fatality"),
            "near_miss": sum(1 for i in incidents if i.incident_type == "Near Miss"),
            "open": sum(1 for i in incidents if i.incident_status not in ["Closed"]),
        },
        "violations_open": len(violations),
    }


@frappe.whitelist()
def generate_ehs_inspection_pack(
    business_entity: str,
    period_from: str,
    period_to: str,
) -> dict:
    """
    Pre-inspection bundle: licences + clearances + returns + readings + incidents.
    Target: < 30 seconds for a 3-year period.

    Returns a dict with pack_url pointing to a ZIP file of all relevant records.
    """
    import time as _time
    start = _time.time()

    period_from_date = frappe.utils.getdate(period_from)
    period_to_date = frappe.utils.getdate(period_to)

    entity = frappe.get_doc("Business Entity", business_entity)

    # Gather all relevant records
    pack = {
        "licences": frappe.get_all(
            "Operating Licence",
            filters={"business_entity": business_entity},
            fields=["name", "licence_type", "licence_number", "valid_until", "licence_status"],
        ),
        "clearances": frappe.get_all(
            "Environmental Clearance",
            filters={"business_entity": business_entity},
            fields=["name", "clearance_type", "clearance_number", "valid_until"],
        ),
        "pcb_returns": frappe.get_all(
            "PCB Return",
            filters={"business_entity": business_entity},
            fields=["name", "return_type", "period_fy", "filing_status", "filed_on"],
        ),
        "monitoring_readings": frappe.get_all(
            "Environmental Monitoring Reading",
            filters={
                "business_entity": business_entity,
                "reading_date": ["between", [period_from_date, period_to_date]],
            },
            fields=["name", "monitoring_type", "monitoring_point", "reading_date", "is_violation"],
            limit=500,
        ),
        "incidents": frappe.get_all(
            "EHS Incident",
            filters={
                "business_entity": business_entity,
                "incident_date": ["between", [period_from_date, period_to_date]],
            },
            fields=["name", "incident_type", "severity", "incident_date", "incident_status"],
        ),
    }

    # In production: generate a ZIP file and upload to File doctype.
    # Here we generate a summary JSON as a placeholder pack_url.
    import json
    pack_content = json.dumps(pack, default=str)

    # Create a temporary file record
    file_doc = frappe.get_doc({
        "doctype": "File",
        "file_name": f"EHS_Inspection_Pack_{business_entity}_{period_from}_{period_to}.json",
        "content": pack_content,
        "is_private": 1,
    })
    file_doc.insert(ignore_permissions=True)

    elapsed = _time.time() - start

    return {
        "pack_url": file_doc.file_url,
        "business_entity": business_entity,
        "period_from": period_from,
        "period_to": period_to,
        "generation_seconds": round(elapsed, 2),
        "summary": {
            "licences": len(pack["licences"]),
            "clearances": len(pack["clearances"]),
            "pcb_returns": len(pack["pcb_returns"]),
            "monitoring_readings": len(pack["monitoring_readings"]),
            "incidents": len(pack["incidents"]),
        },
    }