"""
Fire Safety Compliance — Controller
complyai/compliance/ehs_compliance/doctype/fire_safety_compliance/fire_safety_compliance.py

Handles:
  • Drill metrics: last_drill_date, drills_this_fy, next_drill_due (last + 90 days)
  • Extinguisher refill due date (annual)
  • Fire training next due (annual)
  • Compliance status computation: Compliant / Partial / Non-Compliant
  • High-rise check drives fire_noc_required
"""

import frappe
from frappe import _
from frappe.model.document import Document
from datetime import date, timedelta

try:
    from dateutil.relativedelta import relativedelta
except ImportError:
    relativedelta = None


# ──────────────────────────────────────────────────────
# Helpers: Financial Year boundaries
# ──────────────────────────────────────────────────────

def _current_fy_start() -> date:
    """
    Indian FY: 01-Apr to 31-Mar.
    If today >= Apr 1 of current year → FY started Apr 1 this year.
    Else FY started Apr 1 last year.
    """
    today = date.today()
    if today.month >= 4:
        return date(today.year, 4, 1)
    return date(today.year - 1, 4, 1)


def _add_years(d: date, years: int) -> date:
    """Add years to a date, handling month-end edge cases."""
    if relativedelta:
        return d + relativedelta(years=years)
    # Fallback: approximate
    return d.replace(year=d.year + years)


class FireSafetyCompliance(Document):

    # ──────────────────────────────────────────────
    # Lifecycle hooks
    # ──────────────────────────────────────────────

    def validate(self):
        self.auto_set_noc_required()
        self.compute_drill_metrics()
        self.compute_extinguisher_due()
        self.compute_training_due()
        self.compute_compliance_status()

    def before_save(self):
        # Ensure one record per BE — handled by naming_rule: field:business_entity
        pass

    # ──────────────────────────────────────────────
    # High-rise → NOC required
    # ──────────────────────────────────────────────

    def auto_set_noc_required(self):
        """High-rise buildings (≥15m / ≥4 floors) must have Fire NOC."""
        if self.is_high_rise:
            self.fire_noc_required = 1

        if self.building_height_m and self.building_height_m >= 15:
            self.is_high_rise = 1
            self.fire_noc_required = 1

    # ──────────────────────────────────────────────
    # Drill metrics
    # ──────────────────────────────────────────────

    def compute_drill_metrics(self):
        """
        Quarterly mock drills are mandatory.
        Derive:
          - last_drill_date  : most recent drill
          - drills_this_fy   : count of drills since Apr 1
          - next_drill_due   : last_drill_date + 90 days
        If no drills logged → next_drill_due = today (overdue immediately).
        """
        if not self.drills_log:
            self.last_drill_date = None
            self.drills_this_fy = 0
            self.next_drill_due = date.today()
            return

        # Sort descending by drill_date
        sorted_drills = sorted(
            self.drills_log,
            key=lambda d: frappe.utils.getdate(d.drill_date),
            reverse=True,
        )

        self.last_drill_date = sorted_drills[0].drill_date
        self.next_drill_due = frappe.utils.getdate(self.last_drill_date) + timedelta(days=90)

        fy_start = _current_fy_start()
        self.drills_this_fy = sum(
            1
            for d in self.drills_log
            if frappe.utils.getdate(d.drill_date) >= fy_start
        )

    # ──────────────────────────────────────────────
    # Extinguisher refill due
    # ──────────────────────────────────────────────

    def compute_extinguisher_due(self):
        """
        Annual mandatory refill/inspection.
        Note: ABC powder actual refill = every 3 years; CO2 = 5 years.
        This tracks the annual inspection/servicing date.
        """
        if self.last_extinguisher_refill_date:
            last = frappe.utils.getdate(self.last_extinguisher_refill_date)
            self.next_extinguisher_refill_due = _add_years(last, 1)

    # ──────────────────────────────────────────────
    # Fire training due
    # ──────────────────────────────────────────────

    def compute_training_due(self):
        """Fire training: annual cycle."""
        if self.last_training_date:
            last = frappe.utils.getdate(self.last_training_date)
            self.next_training_due = _add_years(last, 1)

    # ──────────────────────────────────────────────
    # Compliance status
    # ──────────────────────────────────────────────

    def compute_compliance_status(self):
        """
        Count active issues and derive overall status:
          0 issues       → Compliant
          1–2 issues     → Partial
          3+ issues      → Non-Compliant
        """
        issues = []
        today = date.today()

        # Fire NOC checks
        if self.fire_noc_required and not self.fire_noc_number:
            issues.append("Fire NOC number not recorded")

        if self.fire_noc_valid_until:
            noc_until = frappe.utils.getdate(self.fire_noc_valid_until)
            if noc_until < today:
                issues.append(f"Fire NOC expired on {self.fire_noc_valid_until}")
            elif (noc_until - today).days <= 30:
                issues.append(
                    f"Fire NOC expires in {(noc_until - today).days} days"
                )

        # Drill checks
        if self.next_drill_due:
            next_drill = frappe.utils.getdate(self.next_drill_due)
            if next_drill < today:
                days_overdue = (today - next_drill).days
                issues.append(f"Quarterly drill overdue by {days_overdue} days")

        if self.drills_this_fy is not None and self.drills_this_fy < 2:
            issues.append(
                f"Only {self.drills_this_fy} drill(s) this FY — minimum 4 required"
            )

        # Extinguisher checks
        if self.next_extinguisher_refill_due:
            refill_due = frappe.utils.getdate(self.next_extinguisher_refill_due)
            if refill_due < today:
                issues.append("Extinguisher annual refill/inspection overdue")

        if self.fire_extinguishers_count == 0:
            issues.append("No fire extinguishers recorded")

        # Derive status
        if not issues:
            self.compliance_status = "Compliant"
        elif len(issues) <= 2:
            self.compliance_status = "Partial"
        else:
            self.compliance_status = "Non-Compliant"

        # Store issue summary as a note (non-blocking)
        if issues:
            self._log_issues(issues)

    def _log_issues(self, issues: list[str]) -> None:
        frappe.msgprint(
            _("Fire Safety Issues Detected:<br>• {0}").format("<br>• ".join(issues)),
            title=_("Fire Safety Compliance Status"),
            indicator="orange" if self.compliance_status == "Partial" else "red",
        )


# ──────────────────────────────────────────────────────
# Whitelisted: drill logging helper
# ──────────────────────────────────────────────────────

@frappe.whitelist()
def log_fire_drill(business_entity: str, drill_data: dict) -> dict:
    """
    Add a drill log entry to the Fire Safety Compliance record and
    recompute all drill metrics.

    drill_data keys:
        drill_date (str, required)
        drill_type (str)
        duration_minutes (int)
        participants_count (int)
        observations (str)
        drill_evidence (str)  — Evidence File name
    """
    if isinstance(drill_data, str):
        import json
        drill_data = json.loads(drill_data)

    doc = frappe.get_doc("Fire Safety Compliance", business_entity)

    doc.append(
        "drills_log",
        {
            "drill_date": drill_data.get("drill_date"),
            "drill_type": drill_data.get("drill_type"),
            "duration_minutes": drill_data.get("duration_minutes"),
            "participants_count": drill_data.get("participants_count"),
            "observations": drill_data.get("observations"),
            "drill_evidence": drill_data.get("drill_evidence"),
        },
    )

    doc.compute_drill_metrics()
    doc.compute_compliance_status()
    doc.save(ignore_permissions=True)

    return {
        "business_entity": business_entity,
        "last_drill_date": str(doc.last_drill_date),
        "drills_this_fy": doc.drills_this_fy,
        "next_drill_due": str(doc.next_drill_due),
        "compliance_status": doc.compliance_status,
    }


# ──────────────────────────────────────────────────────
# Scheduled alerts
# ──────────────────────────────────────────────────────

def alert_drill_overdue():
    """
    Daily scheduled job at 09:30.
    Alert when quarterly drill is overdue (next_drill_due < today).
    """
    today = date.today()
    overdue = frappe.get_all(
        "Fire Safety Compliance",
        filters={"next_drill_due": ["<", today]},
        fields=["name", "business_entity", "organisation",
                "next_drill_due", "drills_this_fy", "responsible_person"],
    )
    for rec in overdue:
        days_overdue = (today - frappe.utils.getdate(rec.next_drill_due)).days
        frappe.sendmail(
            subject=f"Fire Mock Drill Overdue — {rec.business_entity} ({days_overdue}d)",
            message=(
                f"<b>Quarterly mock drill is overdue by {days_overdue} days</b><br>"
                f"Business Entity: {rec.business_entity}<br>"
                f"Drills this FY: {rec.drills_this_fy} (minimum 4 required)<br>"
                f"Last due: {rec.next_drill_due}"
            ),
            recipients=[rec.responsible_person] if rec.responsible_person else [],
            now=True,
        )


def alert_extinguisher_refill_due():
    """
    Daily scheduled job.
    Alert when extinguisher refill is due within 30 days or overdue.
    """
    today = date.today()
    threshold = today + timedelta(days=30)
    due_soon = frappe.get_all(
        "Fire Safety Compliance",
        filters={"next_extinguisher_refill_due": ["<=", threshold]},
        fields=["name", "business_entity", "next_extinguisher_refill_due",
                "responsible_person"],
    )
    for rec in due_soon:
        due = frappe.utils.getdate(rec.next_extinguisher_refill_due)
        days_left = (due - today).days
        status = f"OVERDUE by {abs(days_left)} days" if days_left < 0 else f"due in {days_left} days"
        frappe.sendmail(
            subject=f"Extinguisher Refill {status} — {rec.business_entity}",
            message=(
                f"Fire extinguisher annual refill/inspection is {status}.<br>"
                f"Business Entity: {rec.business_entity}<br>"
                f"Due Date: {rec.next_extinguisher_refill_due}"
            ),
            recipients=[rec.responsible_person] if rec.responsible_person else [],
            now=True,
        )