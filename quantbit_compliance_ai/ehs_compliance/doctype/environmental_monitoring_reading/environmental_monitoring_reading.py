"""
Environmental Monitoring Reading — Controller
complyai/compliance/ehs_compliance/doctype/environmental_monitoring_reading/environmental_monitoring_reading.py

Handles:
  • Per-parameter norm comparison (measured_value vs prescribed_norm)
  • is_within_norm and deviation_pct computation for each parameter row
  • violation_count + is_violation rollup
  • Auto-create QMS Quality Event on violation (idempotent)
  • Severity mapping: 1 violation→Sev-3, 2→Sev-2, 3+→Sev-1
"""

import frappe
from frappe import _
from frappe.model.document import Document
from datetime import date


class EnvironmentalMonitoringReading(Document):

    # ──────────────────────────────────────────────
    # Lifecycle hooks
    # ──────────────────────────────────────────────

    def validate(self):
        self.validate_reading_date()
        self.compute_violation_status()

    def on_update(self):
        if self.is_violation and not self.linked_quality_event:
            self.create_quality_event_for_violation()

    def before_save(self):
        # Auto-set status when violation is flagged
        if self.is_violation and self.reading_status == "Recorded":
            self.reading_status = "Violation Flagged"

    # ──────────────────────────────────────────────
    # Validation
    # ──────────────────────────────────────────────

    def validate_reading_date(self):
        if self.reading_date:
            reading_date = (
                self.reading_date
                if isinstance(self.reading_date, date)
                else frappe.utils.getdate(self.reading_date)
            )
            if reading_date > date.today():
                frappe.throw(
                    _("Reading Date ({0}) cannot be in the future.").format(self.reading_date)
                )

    # ──────────────────────────────────────────────
    # Core: per-parameter norm comparison
    # ──────────────────────────────────────────────

    def compute_violation_status(self):
        """
        For each parameter row:
          - deviation_pct = ((measured_value - prescribed_norm) / prescribed_norm) × 100
          - is_within_norm = 1 if measured_value ≤ prescribed_norm else 0

        Rollup:
          - violation_count = number of parameters outside norm
          - is_violation = 1 if any violation exists
        """
        if not self.parameters:
            self.violation_count = 0
            self.is_violation = 0
            return

        violations = 0

        for p in self.parameters:
            if p.prescribed_norm is None or p.prescribed_norm == 0:
                # Cannot evaluate without a norm; mark as within norm by default
                p.is_within_norm = 1
                p.deviation_pct = 0.0
                continue

            measured = p.measured_value or 0.0
            norm = float(p.prescribed_norm)

            # Deviation: positive = exceeds norm, negative = below norm (good)
            p.deviation_pct = round(((measured - norm) / norm) * 100, 2)
            p.is_within_norm = 1 if measured <= norm else 0

            if not p.is_within_norm:
                violations += 1

        self.violation_count = violations
        self.is_violation = 1 if violations > 0 else 0

    # ──────────────────────────────────────────────
    # Auto-create QMS Quality Event on violation
    # ──────────────────────────────────────────────

    def create_quality_event_for_violation(self):
        """
        Auto-create a QMS Quality Event for any monitoring violation.
        Severity mapped by number of violations:
          1 violation  → Sev-3 (Moderate)
          2 violations → Sev-2 (Major)
          3+ violations→ Sev-1 (Catastrophic)
        """
        severity = _map_violation_count_to_severity(self.violation_count)

        # Build a readable title
        violated_params = [
            f"{p.parameter} ({p.measured_value} {p.unit}, norm {p.prescribed_norm})"
            for p in self.parameters
            if not p.is_within_norm
        ]
        title = (
            f"{self.monitoring_type} violation at {self.monitoring_point} "
            f"on {self.reading_date}: {', '.join(violated_params[:3])}"
        )
        if len(violated_params) > 3:
            title += f" (+{len(violated_params) - 3} more)"

        try:
            qe = frappe.get_doc(
                {
                    "doctype": "QMS Quality Event",
                    "organisation": self.organisation,
                    "business_entity": self.business_entity,
                    "event_type": "Environmental Non-Conformance",
                    "event_severity": severity,
                    "event_title": title[:140],
                    "linked_environmental_reading": self.name,
                    "event_status": "Open",
                    "description": (
                        f"Monitoring violation detected.\n"
                        f"Type: {self.monitoring_type}\n"
                        f"Point: {self.monitoring_point}\n"
                        f"Date: {self.reading_date}\n"
                        f"Parameters in violation: {self.violation_count}\n"
                        f"Violated: {', '.join(violated_params)}"
                    ),
                }
            )
            qe.insert(ignore_permissions=True)
            # db_set avoids re-triggering on_update
            self.db_set("linked_quality_event", qe.name, update_modified=False)

            frappe.msgprint(
                _(
                    "⚠️ Monitoring violation detected — QMS Quality Event {0} auto-created."
                ).format(qe.name),
                title=_("Violation Flagged"),
                indicator="red",
            )

        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                "Environmental Monitoring Reading — QMS Quality Event creation failed",
            )


# ──────────────────────────────────────────────────────
# Module-level helpers
# ──────────────────────────────────────────────────────

def _map_violation_count_to_severity(violation_count: int) -> str:
    """Map number of violations to QMS severity string."""
    if violation_count >= 3:
        return "Sev-1 (Catastrophic)"
    if violation_count == 2:
        return "Sev-2 (Major)"
    return "Sev-3 (Moderate)"


# ──────────────────────────────────────────────────────
# Scheduled: unactioned violations alert
# ──────────────────────────────────────────────────────

def alert_unactioned_violations():
    """
    Daily scheduled job at 10:00.
    Alert for violations open > 30 days without a CAPA linked.
    """
    from datetime import timedelta

    today = date.today()
    threshold_date = today - timedelta(days=30)

    unactioned = frappe.get_all(
        "Environmental Monitoring Reading",
        filters={
            "is_violation": 1,
            "linked_capa": ["is", "not set"],
            "reading_date": ["<=", threshold_date],
            "reading_status": ["not in", ["Action Taken", "Closed"]],
        },
        fields=[
            "name", "business_entity", "organisation",
            "monitoring_type", "monitoring_point",
            "reading_date", "violation_count",
        ],
    )

    for rec in unactioned:
        reading_date = frappe.utils.getdate(rec.reading_date)
        days_open = (today - reading_date).days
        frappe.sendmail(
            subject=(
                f"⚠️ Unactioned Violation ({days_open}d) — "
                f"{rec.monitoring_type} at {rec.monitoring_point}"
            ),
            message=(
                f"<b>Environmental monitoring violation has been open for {days_open} days "
                f"without CAPA.</b><br><br>"
                f"Business Entity: {rec.business_entity}<br>"
                f"Monitoring Type: {rec.monitoring_type}<br>"
                f"Point: {rec.monitoring_point}<br>"
                f"Date: {rec.reading_date}<br>"
                f"Violations: {rec.violation_count}<br>"
                f"Record: {rec.name}"
            ),
            now=True,
        )


# ──────────────────────────────────────────────────────
# Whitelisted: trend data
# ──────────────────────────────────────────────────────

@frappe.whitelist()
def get_monitoring_trend(
    business_entity: str,
    monitoring_point: str,
    parameter: str,
    months: int = 12,
) -> list:
    """
    Return trend data for a specific parameter at a monitoring point.
    Response: [{reading_date, measured_value, prescribed_norm, is_within_norm, deviation_pct}]
    """
    from datetime import timedelta

    since = date.today() - timedelta(days=int(months) * 30)

    readings = frappe.get_all(
        "Environmental Monitoring Reading",
        filters={
            "business_entity": business_entity,
            "monitoring_point": monitoring_point,
            "reading_date": [">=", since],
        },
        fields=["name", "reading_date"],
        order_by="reading_date asc",
    )

    trend = []
    for r in readings:
        params = frappe.get_all(
            "Monitoring Parameter Reading",
            filters={"parent": r.name, "parameter": parameter},
            fields=["measured_value", "prescribed_norm", "is_within_norm", "deviation_pct", "unit"],
        )
        for p in params:
            trend.append(
                {
                    "reading_date": str(r.reading_date),
                    "measured_value": p.measured_value,
                    "prescribed_norm": p.prescribed_norm,
                    "unit": p.unit,
                    "is_within_norm": p.is_within_norm,
                    "deviation_pct": p.deviation_pct,
                }
            )
    return trend