"""
complyai/compliance/labour_compliance/doctype/posh_committee/posh_committee.py

Controller for POSH Committee.

Statutory requirements (POSH Act §4(2)):
  a. Presiding Officer: woman, employed at a senior level (senior to other members).
  b. At least 2 other members from employees — preferably women committed to women's causes.
  c. At least 1 external member from an NGO / expert familiar with SH issues.
  d. Minimum 50% of total IC members must be women.
  e. Tenure: max 3 years (§4(3)).

Common misconception: POSH IC is mandatory at 10+ TOTAL employees (not 10+ women).
"""

import frappe
from frappe.model.document import Document
from frappe.utils import getdate, today
from dateutil.relativedelta import relativedelta
from datetime import date, timedelta


class POSHCommittee(Document):

    # ──────────────────────────────────────────────────────────────────
    # LIFECYCLE HOOKS
    # ──────────────────────────────────────────────────────────────────

    def validate(self):
        self.compute_tenure_end()
        self.check_ic_compliance()
        self.compute_training_due()

    def on_update(self):
        self.update_profile_link()
        self.schedule_renewal_task()

    # ──────────────────────────────────────────────────────────────────
    # TENURE
    # ──────────────────────────────────────────────────────────────────

    def compute_tenure_end(self):
        """tenure_end = constituted_on + tenure_years. Statutory max is 3 years."""
        if self.constituted_on and self.tenure_years:
            if self.tenure_years > 3:
                frappe.throw(
                    "Maximum IC tenure is 3 years under POSH Act §4(3). "
                    "Set tenure_years ≤ 3.",
                    frappe.ValidationError,
                )
            self.tenure_end = getdate(self.constituted_on) + relativedelta(years=self.tenure_years)

    # ──────────────────────────────────────────────────────────────────
    # IC COMPLIANCE CHECK
    # ──────────────────────────────────────────────────────────────────

    def check_ic_compliance(self):
        """
        Validates all three mandatory structural requirements of POSH Act §4(2):
          1. Presiding Officer exists AND is a woman.
          2. At least one External Member (NGO/expert).
          3. ≥50% of all members are women.

        Sets ic_compliant, non_compliance_reasons, and the three indicator fields.
        """
        members = self.members or []
        reasons = []

        if not members:
            self.ic_compliant = 0
            self.presiding_officer_is_woman = 0
            self.has_external_member = 0
            self.min_50pct_women_members = 0
            self.non_compliance_reasons = "No members defined — IC cannot be constituted without members."
            return

        # ── Requirement 1: Presiding Officer must be a woman ──────────
        presiding = [m for m in members if m.role_in_ic == "Presiding Officer"]
        if not presiding:
            reasons.append("No Presiding Officer defined.")
            self.presiding_officer_is_woman = 0
        elif len(presiding) > 1:
            reasons.append(f"Multiple Presiding Officers defined ({len(presiding)}). Only one allowed.")
            self.presiding_officer_is_woman = 0
        elif presiding[0].gender != "Female":
            reasons.append(
                f"Presiding Officer '{presiding[0].member_name}' is not a woman. "
                "POSH Act §4(2)(a) requires a woman Presiding Officer."
            )
            self.presiding_officer_is_woman = 0
        else:
            self.presiding_officer_is_woman = 1

        # ── Requirement 2: External Member (NGO/expert) ───────────────
        external = [m for m in members if m.role_in_ic in ("External Member", "NGO/Expert")]
        self.has_external_member = 1 if external else 0
        if not external:
            reasons.append(
                "No external member from an NGO or expert. "
                "POSH Act §4(2)(d) mandates at least one external member."
            )

        # ── Requirement 3: ≥50% women members ─────────────────────────
        total_members = len(members)
        women_count = sum(1 for m in members if m.gender == "Female")
        # ≥50% means women_count * 2 >= total_members (integer arithmetic, no floats)
        self.min_50pct_women_members = 1 if (women_count * 2 >= total_members) else 0
        if not self.min_50pct_women_members:
            reasons.append(
                f"Only {women_count} of {total_members} members are women "
                f"({int(women_count/total_members*100)}%). "
                "POSH Act §4(2) requires ≥50% women members."
            )

        # ── Overall compliance ─────────────────────────────────────────
        self.ic_compliant = 1 if not reasons else 0
        self.non_compliance_reasons = "; ".join(reasons) if reasons else None

    # ──────────────────────────────────────────────────────────────────
    # TRAINING DUE DATE
    # ──────────────────────────────────────────────────────────────────

    def compute_training_due(self):
        """IC member training should be refreshed annually."""
        if self.last_training_for_members:
            self.next_training_due = getdate(self.last_training_for_members) + relativedelta(years=1)

    # ──────────────────────────────────────────────────────────────────
    # PROFILE LINK
    # ──────────────────────────────────────────────────────────────────

    def update_profile_link(self):
        """Keep Labour Establishment Profile's posh_committee field in sync."""
        if self.business_entity and self.committee_status == "Constituted":
            profile_exists = frappe.db.exists(
                "Labour Establishment Profile", {"business_entity": self.business_entity}
            )
            if profile_exists:
                frappe.db.set_value(
                    "Labour Establishment Profile",
                    self.business_entity,
                    {
                        "posh_ic_constituted": 1,
                        "posh_committee": self.name,
                    },
                    update_modified=False,
                )

    # ──────────────────────────────────────────────────────────────────
    # RENEWAL TASK
    # ──────────────────────────────────────────────────────────────────

    def schedule_renewal_task(self):
        """
        Create a Compliance Calendar Task 60 days before tenure_end
        if one doesn't already exist. Idempotent.
        """
        if not self.tenure_end:
            return

        tenure_end_date = getdate(self.tenure_end)
        task_due = tenure_end_date - timedelta(days=60)

        # Skip if tenure_end is far in the future (> 90 days)
        if (tenure_end_date - date.today()).days > 90:
            return

        task_title = f"POSH IC Tenure Renewal — expires {tenure_end_date.strftime('%d-%b-%Y')}"
        if frappe.db.exists(
            "Compliance Calendar Task",
            {"business_entity": self.business_entity, "task_title": task_title},
        ):
            return

        org = frappe.db.get_value("Business Entity", self.business_entity, "organisation")
        if not org:
            return

        try:
            frappe.get_doc(
                {
                    "doctype": "Compliance Calendar Task",
                    "organisation": org,
                    "business_entity": self.business_entity,
                    "task_title": task_title,
                    "period_start": str(task_due),
                    "period_end": str(tenure_end_date),
                    "due_date": str(task_due),
                    "assigned_to": frappe.session.user,
                    "status": "Open",
                    "risk_level": "High",
                    "category": "Labour",
                }
            ).insert(ignore_permissions=True)
        except Exception as e:
            frappe.log_error(
                f"POSH renewal task creation failed for {self.business_entity}: {e}",
                "POSHCommittee",
            )