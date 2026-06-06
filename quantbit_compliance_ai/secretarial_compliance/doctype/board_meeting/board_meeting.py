"""
complyai/compliance/secretarial_compliance/doctype/board_meeting/board_meeting.py

Board Meeting controller:
- §173: max 120-day gap between meetings + min 4 per FY
- §173(1): at-least-one-per-quarter check
- §174: quorum = ceil(directors/3) ≥ 2
- §173(3): 7-day notice rule
- Compliance violation event on FY close with < 4 meetings
"""

from datetime import date, timedelta

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate

QUARTERS = {
    1: ("Apr", "Jun"),
    2: ("Jul", "Sep"),
    3: ("Oct", "Dec"),
    4: ("Jan", "Mar"),
}


def _fy_quarter(meeting_date: date) -> int:
    """Return Indian FY quarter (1-4) for a given date."""
    m = meeting_date.month
    if 4 <= m <= 6:
        return 1
    if 7 <= m <= 9:
        return 2
    if 10 <= m <= 12:
        return 3
    return 4  # Jan-Mar


def _fy_dates(fy: str):
    """Return (start_date, end_date) for a FY string like '2024-25'."""
    parts = fy.split("-")
    start_year = int(parts[0])
    end_year = start_year + 1
    return date(start_year, 4, 1), date(end_year, 3, 31)


def _is_fy_ended(fy: str) -> bool:
    _, end = _fy_dates(fy)
    return date.today() > end


class BoardMeeting(Document):
    # ───────────────────────── lifecycle hooks ──────────────────────────

    def validate(self):
        self.compute_next_meeting_due()
        self.check_quorum()
        self.check_notice_period()

    def on_submit(self):
        self.check_fy_meeting_count()
        self.check_per_quarter_meetings()

    # ───────────────────────── §173 gap ────────────────────────────────

    def compute_next_meeting_due(self):
        """§173: Max 120 days between two consecutive board meetings."""
        if self.meeting_date:
            self.next_meeting_due = getdate(self.meeting_date) + timedelta(days=120)

    # ───────────────────────── §174 quorum ──────────────────────────────

    def check_quorum(self):
        """§174: Quorum = max(2, ceil(total_directors / 3))."""
        if not self.directors_invited_count:
            return
        total = self.directors_invited_count
        # ceil(total / 3) using integer arithmetic
        quorum_required = max(2, (total + 2) // 3)
        present = self.directors_present_count or 0
        self.quorum_met = 1 if present >= quorum_required else 0
        if self.meeting_status == "Held" and not self.quorum_met:
            frappe.msgprint(
                _(f"⚠ Quorum not met. Required: {quorum_required}, Present: {present}."),
                indicator="red",
                alert=True,
            )

    # ───────────────────────── §173(3) notice ───────────────────────────

    def check_notice_period(self):
        """§173(3): Notice ≥ 7 days before meeting unless shorter notice consent obtained."""
        if self.notice_dispatched_on and self.meeting_date:
            days = (getdate(self.meeting_date) - getdate(self.notice_dispatched_on)).days
            if days < 7 and not self.shorter_notice_consent_obtained:
                frappe.msgprint(
                    _(f"Notice period is only {days} day(s). "
                      "Shorter notice consent (from all directors) must be obtained."),
                    indicator="orange",
                    alert=True,
                )

    # ───────────────────────── §173 annual count ────────────────────────

    def check_fy_meeting_count(self):
        """§173: Minimum 4 Regular Board Meetings per FY. Raise violation if FY is over and < 4."""
        count = frappe.db.count(
            "Board Meeting",
            {
                "business_entity": self.business_entity,
                "fy": self.fy,
                "meeting_status": "Held",
                "meeting_type": "Regular Board Meeting",
                "docstatus": 1,
            },
        )
        if count < 4 and _is_fy_ended(self.fy):
            _create_compliance_violation(
                self.business_entity,
                self.organisation,
                f"Only {count} board meeting(s) held in FY {self.fy}. "
                "Minimum 4 required under §173 of Companies Act 2013.",
            )

    # ───────────────────────── §173(1) per-quarter ──────────────────────

    def check_per_quarter_meetings(self):
        """§173(1): At least one meeting in each quarter of the FY."""
        if not self.meeting_date or not self.fy:
            return
        fy_start, fy_end = _fy_dates(self.fy)
        if date.today() <= fy_end:
            return  # FY not ended; check at end
        # Fetch all held meeting dates for this entity+FY
        meeting_dates = frappe.db.get_all(
            "Board Meeting",
            filters={
                "business_entity": self.business_entity,
                "fy": self.fy,
                "meeting_status": "Held",
                "meeting_type": "Regular Board Meeting",
                "docstatus": 1,
            },
            pluck="meeting_date",
        )
        quarters_covered = {_fy_quarter(getdate(d)) for d in meeting_dates}
        missing = {1, 2, 3, 4} - quarters_covered
        if missing:
            q_labels = ", ".join(f"Q{q}" for q in sorted(missing))
            _create_compliance_violation(
                self.business_entity,
                self.organisation,
                f"No board meeting held in quarter(s) {q_labels} of FY {self.fy}. "
                "Companies Act §173(1) requires at least one meeting per quarter.",
            )


# ───────────────────────── helpers ──────────────────────────────────────


def _create_compliance_violation(entity: str, organisation: str, message: str):
    """Create a Compliance Calendar Task flagged as a violation / breach."""
    try:
        frappe.get_doc({
            "doctype": "Compliance Calendar Task",
            "task_type": "Statutory Violation",
            "reference_doctype": "Business Entity",
            "reference_name": entity,
            "description": message,
            "due_date": date.today(),
            "status": "Open",
            "organisation": organisation,
            "priority": "High",
        }).insert(ignore_permissions=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Compliance Violation Task Creation Failed")