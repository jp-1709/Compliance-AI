"""
complyai/compliance/labour_compliance/tasks.py

Scheduled background jobs for the Labour Compliance module.

Scheduler configuration (hooks.py):

scheduler_events = {
    "daily": [
        "complyai.compliance.labour_compliance.tasks.flag_stale_registers",               # 02:00 IST
        "complyai.compliance.labour_compliance.tasks.alert_expiring_contractor_licences", # 09:00 IST
        "complyai.compliance.labour_compliance.tasks.compute_block_status",               # 03:00 IST
        "complyai.compliance.labour_compliance.tasks.posh_inquiry_sla_check",             # 09:30 IST
    ],
    "monthly": [
        "complyai.compliance.labour_compliance.tasks.generate_monthly_register_tasks",    # 1st
        "complyai.compliance.labour_compliance.tasks.alert_pending_wage_rolls",           # 8th
    ],
    "yearly": [
        "complyai.compliance.labour_compliance.tasks.posh_annual_report_kickoff",         # Dec 1
        "complyai.compliance.labour_compliance.tasks.posh_committee_tenure_renewal_check",# Jan 1
    ],
    "cron": {
        "0 9 1 */6 *": [
            "complyai.compliance.labour_compliance.tasks.alert_form_xxiv_due"  # Half-yearly
        ]
    }
}
"""

import frappe
from frappe.utils import today, getdate, now
from datetime import date, timedelta


# ──────────────────────────────────────────────────────────────────────────────
# DAILY JOBS
# ──────────────────────────────────────────────────────────────────────────────

def flag_stale_registers():
    """
    Daily 02:00 IST.
    Active registers with no entry in 90+ days → create warning task if not already open.
    """
    cutoff = str(date.today() - timedelta(days=90))
    stale = frappe.get_all(
        "Statutory Register",
        filters={
            "is_active_register": 1,
            "last_entry_date": ("<", cutoff),
        },
        fields=["name", "register_code", "register_name", "business_entity",
                "organisation", "last_entry_date", "responsible_person"],
    )

    created = 0
    for reg in stale:
        task_title = f"Stale register: {reg.register_code} — {reg.register_name} (no entry > 90 days)"
        if frappe.db.exists(
            "Compliance Calendar Task",
            {
                "business_entity": reg.business_entity,
                "task_title": task_title,
                "status": ("not in", ("Completed", "Not Applicable")),
            },
        ):
            continue

        try:
            frappe.get_doc(
                {
                    "doctype": "Compliance Calendar Task",
                    "organisation": reg.organisation,
                    "business_entity": reg.business_entity,
                    "task_title": task_title,
                    "period_start": str(date.today()),
                    "period_end": str(date.today()),
                    "due_date": str(date.today()),
                    "assigned_to": reg.responsible_person or "Administrator",
                    "status": "Open",
                    "risk_level": "Medium",
                    "category": "Labour",
                }
            ).insert(ignore_permissions=True)
            created += 1
        except Exception as e:
            frappe.log_error(f"Stale register task creation failed for {reg.name}: {e}", "Tasks")

    frappe.logger().info(f"[flag_stale_registers] Created {created} warning tasks from {len(stale)} stale registers")


def alert_expiring_contractor_licences():
    """
    Daily 09:00 IST.
    Alert on contractor licences expiring in 30 / 14 / 7 / 0 days.
    At 7 days: WhatsApp + Email. At 30/14: Email only.
    """
    _today = date.today()
    alert_days = [30, 14, 7, 0]

    for days in alert_days:
        target_date = str(_today + timedelta(days=days))
        engagements = frappe.get_all(
            "Contract Labour Engagement",
            filters={
                "licence_expiry_date": target_date,
                "engagement_status": "Active",
            },
            fields=["name", "contractor_name", "business_entity",
                    "licence_expiry_date", "responsible_person"],
        )
        for eng in engagements:
            _send_licence_expiry_alert(eng, days)
            frappe.logger().info(
                f"[alert_expiring_licences] {eng.contractor_name} — {days} days to expiry"
            )


def _send_licence_expiry_alert(engagement: dict, days_remaining: int):
    """Send channel-appropriate alert for contractor licence expiry."""
    channels = ["email", "in_app"]
    if days_remaining <= 7:
        channels.append("whatsapp")

    from complyai.compliance.compliance_calendar.utils import notify_user
    notify_user(
        user=engagement.responsible_person or "Administrator",
        template="contractor_licence_expiry",
        context={
            "contractor_name": engagement.contractor_name,
            "days_remaining": days_remaining,
            "expiry_date": engagement.licence_expiry_date,
            "engagement": engagement.name,
        },
        channels=channels,
    )


def compute_block_status():
    """
    Daily 03:00 IST.
    Re-run block-PO logic for all active Contract Labour Engagements.
    Uses direct DB queries + document save to trigger controller logic.
    """
    engagements = frappe.get_all(
        "Contract Labour Engagement",
        filters={"engagement_status": "Active"},
        pluck="name",
    )
    updated = 0
    for name in engagements:
        try:
            eng = frappe.get_doc("Contract Labour Engagement", name)
            old_block = eng.block_new_pos
            eng.compute_compliance_score()
            eng.compute_block_status()
            if eng.block_new_pos != old_block:
                eng.save(ignore_permissions=True)
                updated += 1
        except Exception as e:
            frappe.log_error(f"Block status recompute failed for {name}: {e}", "Tasks")

    frappe.logger().info(f"[compute_block_status] Updated {updated} of {len(engagements)} engagements")


def posh_inquiry_sla_check():
    """
    Daily 09:30 IST.
    POSH complaints in 'Under Inquiry' with > 75 days elapsed since receipt → alert IC.
    At 90+ days (SLA breach): escalate.
    """
    open_statuses = ("Received", "Under Inquiry", "Conciliation", "Report Submitted")
    complaints = frappe.get_all(
        "POSH Complaint",
        filters={"complaint_status": ("in", open_statuses)},
        fields=["name", "posh_committee", "complaint_received_on",
                "inquiry_target_completion", "business_entity"],
    )

    _today = date.today()
    for complaint in complaints:
        if not complaint.complaint_received_on:
            continue
        received = getdate(complaint.complaint_received_on)
        days_elapsed = (_today - received).days

        if days_elapsed >= 75:
            _send_posh_sla_alert(complaint, days_elapsed)


def _send_posh_sla_alert(complaint: dict, days_elapsed: int):
    """
    POSH SLA alert sent ONLY to IC Presiding Officer.
    Subject uses complaint ID ONLY — never party names.
    """
    ic = frappe.get_cached_doc("POSH Committee", complaint.posh_committee)
    po_email = None
    for m in (ic.members or []):
        if m.role_in_ic == "Presiding Officer":
            po_email = m.email
            break
    if not po_email:
        return

    from complyai.compliance.compliance_calendar.utils import notify_user
    notify_user(
        user=po_email,
        template="posh_inquiry_sla",
        context={
            "complaint_id": complaint.name,  # ONLY ID — no party names
            "days_elapsed": days_elapsed,
            "target_completion": complaint.inquiry_target_completion,
        },
        channels=["in_app"],  # In-app only for POSH — never email for confidential alerts
    )


# ──────────────────────────────────────────────────────────────────────────────
# MONTHLY JOBS
# ──────────────────────────────────────────────────────────────────────────────

def generate_monthly_register_tasks():
    """
    1st of each month.
    Create 'Update Form A entries' tasks for all active entities with active registers.
    Idempotent.
    """
    _today = date.today()
    month_label = _today.strftime("%B %Y")
    task_title = f"Update Form A (Register of Wages) — {month_label}"

    entities = frappe.get_all(
        "Statutory Register",
        filters={"register_code": "FORM-A-MW", "is_active_register": 1},
        fields=["business_entity", "organisation", "responsible_person"],
        distinct=True,
    )

    created = 0
    for reg in entities:
        if frappe.db.exists(
            "Compliance Calendar Task",
            {
                "business_entity": reg.business_entity,
                "task_title": task_title,
                "status": ("not in", ("Completed", "Not Applicable")),
            },
        ):
            continue
        due = date(_today.year, _today.month, 7)  # By 7th of current month for previous month
        frappe.get_doc(
            {
                "doctype": "Compliance Calendar Task",
                "organisation": reg.organisation,
                "business_entity": reg.business_entity,
                "task_title": task_title,
                "period_start": str(_today.replace(day=1)),
                "period_end": str(due),
                "due_date": str(due),
                "assigned_to": reg.responsible_person or "Administrator",
                "status": "Open",
                "risk_level": "Medium",
                "category": "Labour",
            }
        ).insert(ignore_permissions=True)
        created += 1

    frappe.logger().info(f"[generate_monthly_register_tasks] Created {created} tasks")


def alert_pending_wage_rolls():
    """
    8th of each month.
    Wage Rolls for last month not yet submitted → alert Compliance Officer.
    """
    _today = date.today()
    if _today.month == 1:
        prev_year, prev_month = _today.year - 1, 12
    else:
        prev_year, prev_month = _today.year, _today.month - 1

    month_name = date(prev_year, prev_month, 1).strftime("%B")

    # Find entities without a submitted Wage Roll for last month
    submitted = frappe.get_all(
        "Wage Roll",
        filters={
            "wage_period_year": prev_year,
            "wage_period_month": month_name,
            "docstatus": 1,
        },
        pluck="business_entity",
    )

    all_entities = frappe.get_all(
        "Labour Establishment Profile",
        filters={"operational_status": "Operational"},
        fields=["business_entity", "organisation"],
    )

    missing = [e for e in all_entities if e.business_entity not in submitted]
    frappe.logger().info(
        f"[alert_pending_wage_rolls] {len(missing)} entities missing Wage Roll for {month_name} {prev_year}"
    )
    # In production: send email notifications to Compliance Officers for each org


# ──────────────────────────────────────────────────────────────────────────────
# YEARLY JOBS
# ──────────────────────────────────────────────────────────────────────────────

def posh_annual_report_kickoff():
    """
    Dec 1 each year.
    Alert Compliance Officers and IC Presiding Officers to prepare the
    POSH Annual Report due by Jan 31 (calendar year, NOT financial year).
    """
    committees = frappe.get_all(
        "POSH Committee",
        filters={"committee_status": "Constituted"},
        fields=["name", "business_entity", "organisation"],
    )
    cal_year = date.today().year
    for committee in committees:
        task_title = f"File POSH Annual Report to District Officer — Calendar Year {cal_year}"
        if frappe.db.exists(
            "Compliance Calendar Task",
            {"business_entity": committee.business_entity, "task_title": task_title},
        ):
            continue
        frappe.get_doc(
            {
                "doctype": "Compliance Calendar Task",
                "organisation": committee.organisation,
                "business_entity": committee.business_entity,
                "task_title": task_title,
                "period_start": f"{cal_year}-01-01",
                "period_end": f"{cal_year+1}-01-31",
                "due_date": f"{cal_year+1}-01-31",
                "assigned_to": "Administrator",
                "status": "Open",
                "risk_level": "High",
                "category": "Labour",
            }
        ).insert(ignore_permissions=True)
    frappe.logger().info(f"[posh_annual_report_kickoff] Created tasks for {len(committees)} committees")


def posh_committee_tenure_renewal_check():
    """
    Jan 1 each year.
    IC tenures expiring within 60 days → create renewal tasks.
    """
    cutoff = str(date.today() + timedelta(days=60))
    expiring = frappe.get_all(
        "POSH Committee",
        filters={
            "committee_status": "Constituted",
            "tenure_end": ("<=", cutoff),
        },
        fields=["name", "business_entity", "organisation", "tenure_end"],
    )
    for committee in expiring:
        _create_tenure_renewal_task(committee)
    frappe.logger().info(
        f"[posh_committee_tenure_renewal_check] {len(expiring)} committees expiring within 60 days"
    )


def _create_tenure_renewal_task(committee: dict):
    task_title = f"POSH IC Tenure Renewal — expires {committee.tenure_end}"
    if frappe.db.exists(
        "Compliance Calendar Task",
        {
            "business_entity": committee.business_entity,
            "task_title": task_title,
            "status": ("not in", ("Completed", "Not Applicable")),
        },
    ):
        return
    frappe.get_doc(
        {
            "doctype": "Compliance Calendar Task",
            "organisation": committee.organisation,
            "business_entity": committee.business_entity,
            "task_title": task_title,
            "period_start": str(date.today()),
            "period_end": str(committee.tenure_end),
            "due_date": str(date(getdate(committee.tenure_end).year,
                                  getdate(committee.tenure_end).month,
                                  max(1, getdate(committee.tenure_end).day - 30))),
            "assigned_to": "Administrator",
            "status": "Open",
            "risk_level": "High",
            "category": "Labour",
        }
    ).insert(ignore_permissions=True)


# ──────────────────────────────────────────────────────────────────────────────
# HALF-YEARLY JOBS
# ──────────────────────────────────────────────────────────────────────────────

def alert_form_xxiv_due():
    """
    Every 6 months (cron: 0 9 1 */6 *).
    CLRA half-yearly return (Form XXIV) due from Principal Employers.
    """
    engagements = frappe.get_all(
        "Contract Labour Engagement",
        filters={"engagement_status": "Active"},
        fields=["name", "business_entity", "organisation", "responsible_person",
                "form_xxiv_half_yearly_filed"],
    )
    for eng in engagements:
        task_title = f"File CLRA Form XXIV Half-Yearly Return — {date.today().strftime('%b %Y')}"
        if frappe.db.exists(
            "Compliance Calendar Task",
            {
                "business_entity": eng.business_entity,
                "task_title": task_title,
            },
        ):
            continue
        due_date = date.today() + timedelta(days=30)
        frappe.get_doc(
            {
                "doctype": "Compliance Calendar Task",
                "organisation": eng.organisation,
                "business_entity": eng.business_entity,
                "task_title": task_title,
                "period_start": str(date.today()),
                "period_end": str(due_date),
                "due_date": str(due_date),
                "assigned_to": eng.responsible_person or "Administrator",
                "status": "Open",
                "risk_level": "Medium",
                "category": "Labour",
            }
        ).insert(ignore_permissions=True)
    frappe.logger().info(f"[alert_form_xxiv_due] Checked {len(engagements)} active engagements")