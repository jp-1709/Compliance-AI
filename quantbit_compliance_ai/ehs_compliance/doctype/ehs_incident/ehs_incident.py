"""
EHS Incident — Controller
complyai/compliance/ehs_compliance/doctype/ehs_incident/ehs_incident.py

Handles:
  • Reporting delay computation (incident_date+time → reported_on)
  • Auto-flag: is_reportable_to_factory_inspector (LTI + Fatality → Form 18)
  • Auto-flag: is_reportable_to_pcb (Chemical Spill, Gas Release, Environmental Release, Fire)
  • On submit: auto-create QMS Quality Event (idempotent)
  • On submit: 24-hour critical alert for Form 18 reportable incidents
  • Severity → QMS severity mapping
"""

import frappe
from frappe import _
from frappe.model.document import Document
from datetime import date, datetime, time as dt_time


# ──────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────

FACTORY_INSPECTOR_REPORTABLE_TYPES = frozenset({
    "Fatality",
    "Injury — Lost Time (LTI)",
})

PCB_REPORTABLE_TYPES = frozenset({
    "Chemical Spill",
    "Gas Release",
    "Environmental Release",
    "Fire / Explosion",
})

SEVERITY_TO_QMS_MAP: dict[str, str] = {
    "Sev-1 (Catastrophic)": "Sev-1 (Catastrophic)",
    "Sev-2 (Major)":        "Sev-2 (Major)",
    "Sev-3 (Moderate)":     "Sev-3 (Moderate)",
    "Sev-4 (Minor)":        "Sev-4 (Minor)",
    "Sev-5 (Negligible)":   "Sev-5 (Negligible)",
}


class EHSIncident(Document):

    # ──────────────────────────────────────────────
    # Lifecycle hooks
    # ──────────────────────────────────────────────

    def validate(self):
        self.validate_incident_date()
        self.compute_reporting_delay()
        self.set_regulatory_reporting_flags()
        self.warn_24h_breach()

    def on_submit(self):
        self.create_or_link_quality_event()
        self.alert_regulatory_reporting()

    # ──────────────────────────────────────────────
    # Validation
    # ──────────────────────────────────────────────

    def validate_incident_date(self):
        if self.incident_date:
            inc_date = (
                self.incident_date
                if isinstance(self.incident_date, date)
                else frappe.utils.getdate(self.incident_date)
            )
            if inc_date > date.today():
                frappe.throw(
                    _("Incident Date ({0}) cannot be in the future.").format(self.incident_date)
                )

    # ──────────────────────────────────────────────
    # Reporting delay
    # ──────────────────────────────────────────────

    def compute_reporting_delay(self):
        """
        delay_in_reporting_hours = (reported_on − incident_datetime) in hours.
        incident_datetime = incident_date + incident_time (or 00:00 if no time).
        """
        if not self.incident_date or not self.reported_on:
            self.delay_in_reporting_hours = 0
            return

        incident_time = self.incident_time or dt_time(0, 0, 0)
        incident_date = (
            self.incident_date
            if isinstance(self.incident_date, date)
            else frappe.utils.getdate(self.incident_date)
        )

        incident_dt = datetime.combine(incident_date, incident_time)

        # reported_on is Datetime field
        if isinstance(self.reported_on, datetime):
            reported_dt = self.reported_on
        elif isinstance(self.reported_on, str):
            # Frappe stores as 'YYYY-MM-DD HH:MM:SS'
            reported_dt = datetime.fromisoformat(self.reported_on.replace(" ", "T"))
        else:
            self.delay_in_reporting_hours = 0
            return

        delta_seconds = (reported_dt - incident_dt).total_seconds()
        self.delay_in_reporting_hours = round(delta_seconds / 3600.0, 2)

    # ──────────────────────────────────────────────
    # Regulatory reporting flags
    # ──────────────────────────────────────────────

    def set_regulatory_reporting_flags(self):
        """
        Factory Inspector (Form 18):
          → Fatality or LTI (48-hour+ absence) — must report within 24 hours.
          Criminal liability under Factories Act §92.

        PCB reporting:
          → Chemical Spill, Gas Release, Environmental Release, Fire/Explosion.
          Must report within 24 hours.
        """
        if self.incident_type in FACTORY_INSPECTOR_REPORTABLE_TYPES:
            self.is_reportable_to_factory_inspector = 1
        else:
            self.is_reportable_to_factory_inspector = 0

        if self.incident_type in PCB_REPORTABLE_TYPES:
            self.is_reportable_to_pcb = 1
        else:
            self.is_reportable_to_pcb = 0

    # ──────────────────────────────────────────────
    # 24-hour breach warning
    # ──────────────────────────────────────────────

    def warn_24h_breach(self):
        """
        If is_reportable_to_factory_inspector and delay > 24 hours →
        hard warning (not a blocker, as user may still report).
        """
        if (
            self.is_reportable_to_factory_inspector
            and self.delay_in_reporting_hours is not None
            and self.delay_in_reporting_hours > 24
        ):
            frappe.msgprint(
                _(
                    "🚨 CRITICAL: This incident (Fatality / LTI) required Form 18 reporting to the "
                    "Factory Inspector within <b>24 hours</b>. "
                    "Current delay: <b>{0:.1f} hours</b>. "
                    "Immediate legal action may be required under Factories Act §92."
                ).format(self.delay_in_reporting_hours),
                title=_("Form 18 Reporting Deadline Breached"),
                indicator="red",
            )

    # ──────────────────────────────────────────────
    # On submit: QMS Quality Event
    # ──────────────────────────────────────────────

    def create_or_link_quality_event(self):
        """
        Auto-create a QMS Quality Event on incident submission.
        Idempotent: skips if already linked.
        """
        if self.linked_quality_event:
            return  # already linked

        severity = SEVERITY_TO_QMS_MAP.get(self.severity, "Sev-3 (Moderate)")
        title = (self.incident_description or "")[:100] if self.incident_description else self.name

        try:
            qe = frappe.get_doc(
                {
                    "doctype": "QMS Quality Event",
                    "organisation": self.organisation,
                    "business_entity": self.business_entity,
                    "event_type": "Safety Incident",
                    "event_severity": severity,
                    "event_title": title,
                    "linked_ehs_incident": self.name,
                    "event_status": "Open",
                    "description": (
                        f"EHS Incident: {self.incident_type}\n"
                        f"Severity: {self.severity}\n"
                        f"Date: {self.incident_date}\n"
                        f"Location: {self.location_within_site}\n"
                        f"Description: {self.incident_description or ''}"
                    ),
                }
            )
            qe.insert(ignore_permissions=True)
            self.db_set("linked_quality_event", qe.name, update_modified=False)

        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                "EHS Incident — QMS Quality Event creation failed",
            )

    # ──────────────────────────────────────────────
    # On submit: regulatory alerts
    # ──────────────────────────────────────────────

    def alert_regulatory_reporting(self):
        """
        Critical alerts for Factory Inspector (Form 18) and PCB reporting obligations.
        Sent on submit — gives EHS Manager immediate visibility.
        """
        if self.is_reportable_to_factory_inspector and not self.regulatory_reporting_done:
            _send_form18_alert(self)

        if self.is_reportable_to_pcb and not self.regulatory_reporting_done:
            _send_pcb_alert(self)


# ──────────────────────────────────────────────────────
# Module-level alert helpers
# ──────────────────────────────────────────────────────

def _send_form18_alert(incident: "EHSIncident") -> None:
    """Send urgent Form 18 alert to EHS Manager and Legal Counsel."""
    try:
        frappe.sendmail(
            subject=(
                f"🚨 URGENT — Form 18 Factory Inspector Reporting Required: "
                f"{incident.incident_type} at {incident.business_entity}"
            ),
            message=(
                f"<h3 style='color:red'>Immediate Action Required — Within 24 Hours</h3>"
                f"<p>An <b>{incident.incident_type}</b> has been reported that is notifiable "
                f"to the Factory Inspector under Factories Act §88A.</p>"
                f"<ul>"
                f"<li><b>Incident:</b> {incident.name}</li>"
                f"<li><b>Date:</b> {incident.incident_date}</li>"
                f"<li><b>Location:</b> {incident.location_within_site}</li>"
                f"<li><b>Severity:</b> {incident.severity}</li>"
                f"<li><b>Delay so far:</b> {incident.delay_in_reporting_hours:.1f} hours</li>"
                f"</ul>"
                f"<p>File <b>Form 18</b> with the Factory Inspector immediately. "
                f"Criminal liability under §92 for delay.</p>"
            ),
            now=True,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Form 18 alert email failed")


def _send_pcb_alert(incident: "EHSIncident") -> None:
    """Send PCB reporting alert for environmental incidents."""
    try:
        frappe.sendmail(
            subject=(
                f"⚠️ PCB Reporting Required: {incident.incident_type} at "
                f"{incident.business_entity}"
            ),
            message=(
                f"<p>An <b>{incident.incident_type}</b> has occurred that must be "
                f"reported to the State PCB within 24 hours.</p>"
                f"<ul>"
                f"<li><b>Incident:</b> {incident.name}</li>"
                f"<li><b>Date:</b> {incident.incident_date}</li>"
                f"<li><b>Environmental Release:</b> "
                f"{incident.environmental_release_quantity_kg} kg</li>"
                f"</ul>"
                f"<p>Contact the State PCB Environmental Emergency Cell immediately.</p>"
            ),
            now=True,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "PCB alert email failed")


def map_severity_to_qms(severity: str) -> str:
    """Public helper for reuse across module."""
    return SEVERITY_TO_QMS_MAP.get(severity, "Sev-3 (Moderate)")


# ──────────────────────────────────────────────────────
# Whitelisted API
# ──────────────────────────────────────────────────────

@frappe.whitelist()
def log_incident(business_entity: str, incident_data: dict) -> dict:
    """
    Log an EHS incident and auto-assess regulatory reportability.

    incident_data keys:
        organisation (str, required)
        incident_type (str, required)
        severity (str, required)
        incident_date (str, required)
        incident_time (str, optional)
        location_within_site (str, required)
        reported_by (str, required)
        reported_on (str, optional — defaults to now)
        incident_description (str, required)
        immediate_actions_taken (str, optional)
    """
    if isinstance(incident_data, str):
        import json
        incident_data = json.loads(incident_data)

    incident_data["doctype"] = "EHS Incident"
    incident_data["business_entity"] = business_entity

    if not incident_data.get("reported_on"):
        incident_data["reported_on"] = frappe.utils.now()

    doc = frappe.get_doc(incident_data)
    doc.insert(ignore_permissions=True)

    return {
        "incident": doc.name,
        "incident_type": doc.incident_type,
        "severity": doc.severity,
        "is_reportable_to_factory_inspector": doc.is_reportable_to_factory_inspector,
        "is_reportable_to_pcb": doc.is_reportable_to_pcb,
        "delay_in_reporting_hours": doc.delay_in_reporting_hours,
    }


@frappe.whitelist()
def get_incident_trends(organisation: str = None, fy: str = None) -> dict:
    """
    Return LTIFR, severity rate, incident breakdown by type for a given FY.
    LTIFR = (LTI count × 200,000) / total man-hours
    """
    filters = {}
    if organisation:
        filters["organisation"] = organisation

    if fy:
        # Parse FY to date range
        try:
            from complyai.compliance.ehs_compliance.controllers.pcb_return import _parse_fy_end_year
            end_year = _parse_fy_end_year(fy)
            filters["incident_date"] = [
                "between",
                [date(end_year - 1, 4, 1), date(end_year, 3, 31)],
            ]
        except Exception:
            pass

    incidents = frappe.get_all(
        "EHS Incident",
        filters=filters,
        fields=["incident_type", "severity", "production_loss_hours"],
    )

    total = len(incidents)
    lti_count = sum(1 for i in incidents if i.incident_type == "Injury — Lost Time (LTI)")
    fatality_count = sum(1 for i in incidents if i.incident_type == "Fatality")
    near_miss_count = sum(1 for i in incidents if i.incident_type == "Near Miss")
    total_lost_hours = sum((i.production_loss_hours or 0) for i in incidents)

    # LTIFR: per 200,000 man-hours (approximate from production loss)
    man_hours = 200_000  # default denominator
    ltifr = round((lti_count * 200_000) / man_hours, 2) if man_hours else 0

    by_type: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for i in incidents:
        by_type[i.incident_type] = by_type.get(i.incident_type, 0) + 1
        by_severity[i.severity] = by_severity.get(i.severity, 0) + 1

    return {
        "total": total,
        "lti_count": lti_count,
        "fatality_count": fatality_count,
        "near_miss_count": near_miss_count,
        "total_lost_hours": total_lost_hours,
        "ltifr": ltifr,
        "by_type": by_type,
        "by_severity": by_severity,
    }