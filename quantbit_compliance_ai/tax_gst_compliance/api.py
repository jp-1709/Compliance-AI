"""
Tax & GST Compliance — Whitelisted API
quantbit_compliance_ai/tax_gst_compliance/api.py

Whitelisted methods for the Tax & GST Compliance module. Covers the core
create/list/action surface for each DocType plus the two cross-cutting
aggregate endpoints (dashboard, ITC-at-risk rollup). Report/print-format/
AI-hook endpoints from the full spec are intentionally out of scope here.
"""

import json

import frappe
from frappe import _
from frappe.utils import today, getdate

from quantbit_compliance_ai.tax_gst_compliance.doctype.tax_notice.tax_notice import TaxNotice
from quantbit_compliance_ai.foundation.utils import pick_task_reviewer


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _get_organisation(business_entity: str) -> str:
    org = frappe.db.get_value("Business Entity", business_entity, "organisation")
    if not org:
        frappe.throw(_("Business Entity {0} not found.").format(business_entity))
    return org


def _parse(data):
    if isinstance(data, str):
        return json.loads(data)
    return data or {}


# ──────────────────────────────────────────────────────────────────────────────
# Tax Registrations
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def add_tax_registration(business_entity: str, registration_data: dict) -> dict:
    """Create a Tax Registration; format + GSTIN state-code checks run in validate()."""
    registration_data = _parse(registration_data)
    registration_data["doctype"] = "Tax Registration"
    registration_data["business_entity"] = business_entity
    registration_data["organisation"] = _get_organisation(business_entity)

    doc = frappe.get_doc(registration_data)
    doc.insert(ignore_permissions=True)
    return {
        "tax_registration": doc.name,
        "registration_number": doc.registration_number,
        "is_eligible_einvoice": doc.is_eligible_einvoice,
        "is_eligible_qrmp": doc.is_eligible_qrmp,
    }


@frappe.whitelist()
def list_gstins(organisation: str = None, business_entity: str = None) -> list:
    filters = {"registration_type": ("like", "GSTIN%"), "is_active": 1}
    if organisation:
        filters["organisation"] = organisation
    if business_entity:
        filters["business_entity"] = business_entity
    return frappe.get_all(
        "Tax Registration",
        filters=filters,
        fields=[
            "name", "business_entity", "registration_number", "state",
            "gstin_suspended", "consecutive_non_filings",
            "last_gstr1_filed_on", "last_gstr3b_filed_on",
        ],
        order_by="business_entity asc",
    )


@frappe.whitelist()
def check_gstin_health(tax_registration: str) -> dict:
    doc = frappe.get_doc("Tax Registration", tax_registration)
    pending_late_fee = frappe.db.sql(
        """
        SELECT COALESCE(SUM(late_fee_inr), 0)
        FROM `tabGSTR Filing`
        WHERE tax_registration = %s AND docstatus != 2 AND late_fee_inr > 0
        """,
        doc.name,
    )[0][0]
    return {
        "tax_registration": doc.name,
        "registration_number": doc.registration_number,
        "gstin_suspended": doc.gstin_suspended,
        "consecutive_non_filings": doc.consecutive_non_filings,
        "last_gstr1_filed_on": str(doc.last_gstr1_filed_on) if doc.last_gstr1_filed_on else None,
        "last_gstr3b_filed_on": str(doc.last_gstr3b_filed_on) if doc.last_gstr3b_filed_on else None,
        "total_late_fees_pending_inr": pending_late_fee,
    }


# ──────────────────────────────────────────────────────────────────────────────
# GSTR Filings
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def list_gstr_filings(
    organisation: str = None, status: str = None,
    return_type: str = None, period_fy: str = None,
) -> list:
    filters = {}
    for key, val in (
        ("organisation", organisation), ("filing_status", status),
        ("return_type", return_type), ("period_fy", period_fy),
    ):
        if val:
            filters[key] = val
    return frappe.get_all(
        "GSTR Filing",
        filters=filters,
        fields=[
            "name", "business_entity", "tax_registration", "return_type",
            "filing_status", "period_month", "period_fy", "filing_due_date",
            "filed_on", "delay_days", "late_fee_inr",
        ],
        order_by="filing_due_date asc",
    )


@frappe.whitelist()
def create_gstr_filing(
    business_entity: str, tax_registration: str, return_type: str,
    period_fy: str, period_month: str = None, period_quarter: str = None,
    **kwargs,
) -> dict:
    doc = frappe.get_doc(
        {
            "doctype": "GSTR Filing",
            "organisation": _get_organisation(business_entity),
            "business_entity": business_entity,
            "tax_registration": tax_registration,
            "return_type": return_type,
            "period_fy": period_fy,
            "period_month": period_month,
            "period_quarter": period_quarter,
            "filing_status": "Pending",
            **{k: v for k, v in kwargs.items() if v is not None},
        }
    )
    doc.insert(ignore_permissions=True)
    return {"gstr_filing": doc.name, "filing_due_date": str(doc.filing_due_date)}


@frappe.whitelist()
def submit_gstr_filing(
    filing: str, arn_number: str, filed_on: str, late_fee_inr: float = 0, **kwargs
) -> dict:
    doc = frappe.get_doc("GSTR Filing", filing)
    doc.arn_number = arn_number
    doc.filed_on = getdate(filed_on)
    doc.filing_status = "Filed"
    for key, val in kwargs.items():
        if hasattr(doc, key) and val is not None:
            setattr(doc, key, val)
    doc.save(ignore_permissions=True)
    if (doc.late_fee_inr or 0) > 0:
        doc.filing_status = "Filed with Late Fee"
        doc.save(ignore_permissions=True)
    if doc.docstatus == 0:
        doc.submit()
    return {
        "gstr_filing": doc.name,
        "filing_status": doc.filing_status,
        "late_fee_inr": doc.late_fee_inr,
        "delay_days": doc.delay_days,
    }


# ──────────────────────────────────────────────────────────────────────────────
# ITC Reconciliation
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def create_itc_recon(
    business_entity: str, tax_registration: str, period_month: str, period_fy: str
) -> dict:
    doc = frappe.get_doc(
        {
            "doctype": "ITC Reconciliation",
            "organisation": _get_organisation(business_entity),
            "business_entity": business_entity,
            "tax_registration": tax_registration,
            "period_month": period_month,
            "period_fy": period_fy,
            "recon_status": "Draft",
            "recon_date": today(),
        }
    )
    doc.insert(ignore_permissions=True)
    return {"itc_reconciliation": doc.name}


@frappe.whitelist()
def run_recon_match(itc_recon: str) -> dict:
    """
    Recompute diff/match/Rule-36(4) from whatever books/2B totals + line items
    are currently on the record (upload/parsing of raw books/2B files is an
    external-adapter concern, out of scope here — see module docstring).
    """
    doc = frappe.get_doc("ITC Reconciliation", itc_recon)
    doc.save(ignore_permissions=True)
    return {
        "itc_reconciliation": doc.name,
        "diff_total_itc_inr": doc.diff_total_itc_inr,
        "match_percentage": doc.match_percentage,
        "itc_at_risk_inr": doc.itc_at_risk_inr,
        "rule_36_4_compliant": doc.rule_36_4_compliant,
        "vendor_chase_pending": doc.vendor_chase_pending,
    }


@frappe.whitelist()
def get_itc_at_risk_summary(organisation: str = None, period_fy: str = None) -> dict:
    filters = {}
    if organisation:
        filters["organisation"] = organisation
    if period_fy:
        filters["period_fy"] = period_fy
    recons = frappe.get_all(
        "ITC Reconciliation",
        filters=filters,
        fields=["itc_at_risk_inr", "additional_itc_in_2b_inr", "recon_status"],
    )
    return {
        "total_itc_at_risk_inr": sum(r.itc_at_risk_inr or 0 for r in recons),
        "total_additional_itc_in_2b_inr": sum(r.additional_itc_in_2b_inr or 0 for r in recons),
        "reconciliations_count": len(recons),
        "unresolved_count": sum(
            1 for r in recons if r.recon_status in ("Mismatch Identified", "Vendor Chase")
        ),
    }


# ──────────────────────────────────────────────────────────────────────────────
# E-Invoice
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def create_einvoice(business_entity: str, invoice_data: dict) -> dict:
    invoice_data = _parse(invoice_data)
    invoice_data["doctype"] = "E-Invoice Record"
    invoice_data["business_entity"] = business_entity
    invoice_data["organisation"] = _get_organisation(business_entity)

    doc = frappe.get_doc(invoice_data)
    doc.insert(ignore_permissions=True)
    return {
        "einvoice": doc.name,
        "irn": doc.irn,
        "irn_generated_within_7days": doc.irn_generated_within_7days,
    }


@frappe.whitelist()
def cancel_einvoice(einvoice: str, reason_code: str) -> dict:
    doc = frappe.get_doc("E-Invoice Record", einvoice)
    doc.einvoice_status = "Cancelled"
    doc.cancellation_reason = reason_code
    doc.cancellation_date = frappe.utils.now_datetime()
    doc.save(ignore_permissions=True)  # 24h window enforced in validate()
    return {"einvoice": doc.name, "status": doc.einvoice_status}


@frappe.whitelist()
def get_einvoice_compliance_summary(business_entity: str, period_month: str, period_fy: str) -> dict:
    from datetime import date
    import calendar

    month_map = {
        "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
        "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
    }
    month_num = month_map.get(period_month)
    fy_start = int((period_fy or "0-0").split("-")[0]) if period_fy else None
    filters = {"business_entity": business_entity}
    if month_num and fy_start:
        year = fy_start if month_num >= 4 else fy_start + 1
        last_day = calendar.monthrange(year, month_num)[1]
        filters["invoice_date"] = ["between", [date(year, month_num, 1), date(year, month_num, last_day)]]

    invoices = frappe.get_all(
        "E-Invoice Record", filters=filters,
        fields=["einvoice_status", "irn_generated_within_7days"],
    )
    generated = [i for i in invoices if i.einvoice_status == "Generated"]
    within_7 = sum(1 for i in generated if i.irn_generated_within_7days)
    return {
        "total": len(invoices),
        "generated": len(generated),
        "within_7_days": within_7,
        "compliance_pct": round((within_7 / len(generated)) * 100, 2) if generated else 100.0,
        "cancelled": sum(1 for i in invoices if i.einvoice_status == "Cancelled"),
    }


# ──────────────────────────────────────────────────────────────────────────────
# E-Way Bill
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def create_ewb(business_entity: str, ewb_data: dict) -> dict:
    ewb_data = _parse(ewb_data)
    ewb_data["doctype"] = "E-Way Bill Record"
    ewb_data["business_entity"] = business_entity
    ewb_data["organisation"] = _get_organisation(business_entity)

    doc = frappe.get_doc(ewb_data)
    doc.insert(ignore_permissions=True)
    return {
        "ewb": doc.name,
        "valid_until": str(doc.valid_until) if doc.valid_until else None,
    }


@frappe.whitelist()
def extend_ewb(ewb: str, extension_reason: str) -> dict:
    """
    Extend validity by re-running compute_validity() from now, as the closest
    approximation to the portal's "extend from current location" flow without
    a live GPS/tracking integration.
    """
    doc = frappe.get_doc("E-Way Bill Record", ewb)
    doc.is_extended = 1
    doc.extension_reason = extension_reason
    doc.generated_at = frappe.utils.now_datetime()
    doc.save(ignore_permissions=True)
    return {"ewb": doc.name, "valid_until": str(doc.valid_until), "is_expired": doc.is_expired}


@frappe.whitelist()
def cancel_ewb(ewb: str, reason: str) -> dict:
    doc = frappe.get_doc("E-Way Bill Record", ewb)
    doc.ewb_status = "Cancelled"
    doc.cancellation_reason = reason
    doc.cancellation_date = frappe.utils.now_datetime()
    doc.save(ignore_permissions=True)
    return {"ewb": doc.name, "status": doc.ewb_status}


@frappe.whitelist()
def get_active_ewbs(business_entity: str = None) -> list:
    filters = {"ewb_status": "In Transit"}
    if business_entity:
        filters["business_entity"] = business_entity
    return frappe.get_all(
        "E-Way Bill Record",
        filters=filters,
        fields=["name", "business_entity", "ewb_number", "valid_until", "is_expired", "linked_invoice_number"],
        order_by="valid_until asc",
    )


# ──────────────────────────────────────────────────────────────────────────────
# TDS / TCS
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def create_tds_return(
    business_entity: str, tax_registration: str, form_type: str,
    period_quarter: str, period_fy: str,
) -> dict:
    doc = frappe.get_doc(
        {
            "doctype": "TDS Return",
            "organisation": _get_organisation(business_entity),
            "business_entity": business_entity,
            "tax_registration": tax_registration,
            "form_type": form_type,
            "period_quarter": period_quarter,
            "period_fy": period_fy,
            "filing_status": "Pending",
        }
    )
    doc.insert(ignore_permissions=True)
    return {"tds_return": doc.name, "filing_due_date": str(doc.filing_due_date)}


@frappe.whitelist()
def create_tcs_return(business_entity: str, tax_registration: str, period_quarter: str, period_fy: str) -> dict:
    doc = frappe.get_doc(
        {
            "doctype": "TCS Return",
            "organisation": _get_organisation(business_entity),
            "business_entity": business_entity,
            "tax_registration": tax_registration,
            "period_quarter": period_quarter,
            "period_fy": period_fy,
            "filing_status": "Pending",
        }
    )
    doc.insert(ignore_permissions=True)
    return {"tcs_return": doc.name, "filing_due_date": str(doc.filing_due_date)}


# ──────────────────────────────────────────────────────────────────────────────
# Advance Tax
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def get_advance_tax_schedule(business_entity: str, period_fy: str) -> dict:
    rows = frappe.get_all(
        "Advance Tax Payment",
        filters={"business_entity": business_entity, "period_fy": period_fy},
        fields=[
            "instalment", "due_date", "payment_status", "cumulative_required_inr",
            "cumulative_paid_till_now_inr", "shortfall_inr", "interest_234c_inr",
        ],
        order_by="due_date asc",
    )
    return {"business_entity": business_entity, "period_fy": period_fy, "instalments": rows}


@frappe.whitelist()
def record_advance_tax_payment(
    business_entity: str, period_fy: str, instalment: str,
    amount_inr: float, due_date: str, estimated_total_tax_inr: float,
    challan_data: dict = None,
) -> dict:
    challan_data = _parse(challan_data)
    existing = frappe.db.exists(
        "Advance Tax Payment",
        {"business_entity": business_entity, "period_fy": period_fy, "instalment": instalment},
    )
    if existing:
        doc = frappe.get_doc("Advance Tax Payment", existing)
    else:
        doc = frappe.new_doc("Advance Tax Payment")
        doc.organisation = _get_organisation(business_entity)
        doc.business_entity = business_entity
        doc.period_fy = period_fy
        doc.instalment = instalment
        doc.due_date = getdate(due_date)

    doc.estimated_total_tax_inr = estimated_total_tax_inr
    doc.this_instalment_inr = amount_inr
    doc.cumulative_paid_till_now_inr = (doc.cumulative_paid_till_now_inr or 0) + float(amount_inr)
    doc.paid_on = today()
    doc.payment_status = "Paid"
    doc.challan_number = challan_data.get("challan_number")
    doc.challan_evidence = challan_data.get("challan_evidence")
    doc.save(ignore_permissions=True)

    return {
        "advance_tax_payment": doc.name,
        "shortfall_inr": doc.shortfall_inr,
        "interest_234c_inr": doc.interest_234c_inr,
    }


# ──────────────────────────────────────────────────────────────────────────────
# ITR
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def create_itr(business_entity: str, period_fy: str, itr_form: str, **kwargs) -> dict:
    doc = frappe.get_doc(
        {
            "doctype": "Income Tax Return",
            "organisation": _get_organisation(business_entity),
            "business_entity": business_entity,
            "period_fy": period_fy,
            "itr_form": itr_form,
            "filing_status": "Pending",
            **{k: v for k, v in kwargs.items() if v is not None},
        }
    )
    doc.insert(ignore_permissions=True)

    if doc.tax_audit_required:
        _create_form_3cd_task(doc)

    return {"itr": doc.name, "ay": doc.ay, "filing_due_date": str(doc.filing_due_date)}


def _create_form_3cd_task(itr) -> None:
    """Form 3CD (tax audit report) is due 30 days before the ITR itself."""
    if not itr.filing_due_date:
        return
    due = frappe.utils.add_days(itr.filing_due_date, -30)
    task_title = f"File Form 3CD (Tax Audit Report) — {itr.business_entity} FY {itr.period_fy}"[:140]
    if frappe.db.exists("Compliance Calendar Task", {"business_entity": itr.business_entity, "task_title": task_title}):
        return
    try:
        frappe.get_doc(
            {
                "doctype": "Compliance Calendar Task",
                "organisation": itr.organisation,
                "business_entity": itr.business_entity,
                "task_title": task_title,
                # due (filing_due_date - 30) can already be in the past for a
                # record created close to/after its own audit deadline —
                # bracket both dates rather than assume today is earlier.
                "period_start": min(getdate(today()), getdate(due)),
                "period_end": max(getdate(today()), getdate(due)),
                "due_date": due,
                "assigned_to": frappe.session.user,
                "reviewer": pick_task_reviewer(frappe.session.user),
                "status": "Open",
                "risk_level": "High",
                "category": "Tax",
                "section_reference": f"Income Tax Return: {itr.name}",
            }
        ).insert(ignore_permissions=True)
    except Exception as e:
        frappe.log_error(f"Failed to create Form 3CD task for {itr.name}: {e}", "IncomeTaxReturn")


@frappe.whitelist()
def get_itr_pre_filing_checklist(itr: str) -> dict:
    doc = frappe.get_doc("Income Tax Return", itr)
    return {
        "itr": doc.name,
        "form_3cd_ready": (not doc.tax_audit_required) or bool(doc.form_3cd_filed),
        "form_3ceb_ready": (not doc.transfer_pricing_required) or bool(doc.form_3ceb_filed),
        "caro_ready": (not doc.caro_applicable) or bool(doc.caro_evidence),
        "advance_tax_paid_inr": doc.advance_tax_paid_inr,
        "tds_credit_inr": doc.tds_credit_inr,
        "ready_to_file": (
            ((not doc.tax_audit_required) or bool(doc.form_3cd_filed))
            and ((not doc.transfer_pricing_required) or bool(doc.form_3ceb_filed))
            and ((not doc.caro_applicable) or bool(doc.caro_evidence))
        ),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Tax Notices
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def log_tax_notice(business_entity: str, notice_data: dict) -> dict:
    notice_data = _parse(notice_data)
    notice_data["doctype"] = "Tax Notice"
    notice_data["business_entity"] = business_entity
    notice_data["organisation"] = _get_organisation(business_entity)

    doc: TaxNotice = frappe.get_doc(notice_data)
    doc.insert(ignore_permissions=True)
    return {
        "tax_notice": doc.name,
        "total_demand_inr": doc.total_demand_inr,
        "response_due_date": str(doc.response_due_date) if doc.response_due_date else None,
    }


@frappe.whitelist()
def respond_to_notice(notice: str, response_evidence: str, response_filed_on: str) -> dict:
    doc = frappe.get_doc("Tax Notice", notice)
    doc.response_evidence = response_evidence
    doc.response_filed_on = getdate(response_filed_on)
    doc.notice_status = "Response Filed"
    doc.save(ignore_permissions=True)
    return {"tax_notice": doc.name, "status": doc.notice_status}


@frappe.whitelist()
def file_appeal(notice: str, appeal_forum: str, appeal_reference: str, appeal_evidence: str = None) -> dict:
    doc = frappe.get_doc("Tax Notice", notice)
    doc.appeal_filed = 1
    doc.appeal_forum = appeal_forum
    doc.appeal_reference = appeal_reference
    doc.notice_status = "Appeal Filed"
    doc.save(ignore_permissions=True)
    return {"tax_notice": doc.name, "status": doc.notice_status}


# ──────────────────────────────────────────────────────────────────────────────
# Dashboard
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def get_tax_dashboard(organisation: str = None, business_entity: str = None) -> dict:
    filters = {}
    if organisation:
        filters["organisation"] = organisation
    if business_entity:
        filters["business_entity"] = business_entity

    filings = frappe.get_all(
        "GSTR Filing", filters=filters,
        fields=["filing_status", "delay_days", "late_fee_inr"],
    )
    on_time = sum(1 for f in filings if f.filing_status in ("Filed", "Nil Filed"))
    itc_summary = get_itc_at_risk_summary(organisation=organisation)
    notices = frappe.get_all(
        "Tax Notice", filters=filters,
        fields=["notice_status", "total_demand_inr"],
    )
    active_notices = [n for n in notices if n.notice_status not in ("Closed", "Favourable")]

    return {
        "filings_on_time_pct": round((on_time / len(filings)) * 100, 2) if filings else 100.0,
        "total_late_fees_inr": sum(f.late_fee_inr or 0 for f in filings),
        "itc_at_risk_inr": itc_summary["total_itc_at_risk_inr"],
        "active_notices_count": len(active_notices),
        "active_notices_demand_inr": sum(n.total_demand_inr or 0 for n in active_notices),
    }
