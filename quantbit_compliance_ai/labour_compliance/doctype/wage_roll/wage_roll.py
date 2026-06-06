"""
complyai/compliance/labour_compliance/doctype/wage_roll/wage_roll.py

Controller for Wage Roll.

Key compliance checks:
1. Payment due date: 7th of next month (≤1000 workers) or 10th (>1000 workers).
2. Minimum wages: per-employee Basic+DA vs state-notified rate for skill category.
3. Compliance score: weighted deductions for each violation.
4. Unique per (organisation, business_entity, period_year, period_month).
"""

import frappe
from frappe.model.document import Document
from frappe.utils import getdate, today
from datetime import date, timedelta


class WageRoll(Document):

    # ──────────────────────────────────────────────────────────────────
    # LIFECYCLE HOOKS
    # ──────────────────────────────────────────────────────────────────

    def validate(self):
        self.validate_period_dates()
        self.compute_payment_due_date()
        self.run_minimum_wages_check()
        self.count_register_entries()
        self.compute_compliance_score()

    def on_submit(self):
        """Submission locks the record for the period."""
        if self.compliance_score < 60:
            frappe.msgprint(
                f"Warning: Wage Roll compliance score is {self.compliance_score}% "
                "(below 60%). Review violations before submitting.",
                alert=True,
                indicator="orange",
            )

    # ──────────────────────────────────────────────────────────────────
    # VALIDATION
    # ──────────────────────────────────────────────────────────────────

    def validate_period_dates(self):
        """Period end must be after period start."""
        if self.wage_period_start and self.wage_period_end:
            if getdate(self.wage_period_end) < getdate(self.wage_period_start):
                frappe.throw(
                    "Wage Period End cannot be before Wage Period Start.",
                    frappe.ValidationError,
                )

    # ──────────────────────────────────────────────────────────────────
    # PAYMENT DUE DATE (Payment of Wages Act §5)
    # ──────────────────────────────────────────────────────────────────

    def compute_payment_due_date(self):
        """
        Payment of Wages Act §5:
        - Wages must be paid before the 7th of the next month (≤1000 workers).
        - Before the 10th of the next month (>1000 workers).
        Uses total_workers from Labour Establishment Profile.
        """
        if not self.wage_period_end:
            return

        period_end = getdate(self.wage_period_end)
        # First day of the month after period_end
        if period_end.month == 12:
            next_month_first = date(period_end.year + 1, 1, 1)
        else:
            next_month_first = date(period_end.year, period_end.month + 1, 1)

        # Get total workers from profile
        total_workers = frappe.db.get_value(
            "Labour Establishment Profile",
            {"business_entity": self.business_entity},
            "total_workers",
        ) or 0

        pay_day = 10 if total_workers > 1000 else 7

        self.payment_due_date = date(next_month_first.year, next_month_first.month, pay_day)

        # Compute delay
        if self.actually_paid_on:
            paid = getdate(self.actually_paid_on)
            due = getdate(self.payment_due_date)
            self.delay_days = max(0, (paid - due).days)
            self.wages_paid_within_due_date = 1 if self.delay_days == 0 else 0
        else:
            self.delay_days = 0
            self.wages_paid_within_due_date = 0

    # ──────────────────────────────────────────────────────────────────
    # MINIMUM WAGES CHECK
    # ──────────────────────────────────────────────────────────────────

    def run_minimum_wages_check(self):
        """
        For each employee in the linked Form A register for this period,
        verify that Basic + DA >= state-notified minimum wage for their skill category.

        Uses rate effective on wage_period_end date — NOT today's rate.
        This is critical for retroactive validation.
        """
        state = frappe.db.get_value("Business Entity", self.business_entity, "state")
        if not state:
            return

        # Fetch rates effective during this wage period
        rates = frappe.get_all(
            "Minimum Wage Rate",
            filters={
                "state": state,
                "effective_from": ("<=", self.wage_period_end),
                "effective_to": (">=", self.wage_period_start),
            },
            fields=["skill_category", "monthly_rate_inr"],
        )

        if not rates:
            frappe.msgprint(
                f"No minimum wages notified for {state} in this period — "
                "verify manually with state Labour Commissioner website.",
                alert=True,
                indicator="orange",
            )
            self.minimum_wages_compliant = 0
            self.min_wage_violations_count = 0
            return

        # Build lookup: skill_category → rate
        rate_lookup = {r.skill_category: r.monthly_rate_inr for r in rates}

        # Fetch Form A register for this entity
        form_a_register = frappe.db.get_value(
            "Statutory Register",
            {
                "business_entity": self.business_entity,
                "register_code": "FORM-A-MW",
            },
            "name",
        )
        if not form_a_register:
            self.minimum_wages_compliant = 0
            self.min_wage_violations_count = 0
            return

        # Get all register entries for this period
        entries = frappe.get_all(
            "Register Entry",
            filters={
                "register": form_a_register,
                "period_month": self.wage_period_month,
                "period_year": self.wage_period_year,
            },
            fields=["entry_payload", "subject_employee", "name"],
        )

        violations = 0
        for entry in entries:
            try:
                payload = frappe.parse_json(entry.entry_payload or "{}")
                basic = float(payload.get("basic") or 0)
                da = float(payload.get("da") or 0)
                basic_da = basic + da
                skill = payload.get("skill_category", "Unskilled")

                applicable_rate = rate_lookup.get(skill) or rate_lookup.get("Unskilled")
                if applicable_rate and basic_da < float(applicable_rate):
                    violations += 1
                    # Flag the entry as anomaly
                    frappe.db.set_value(
                        "Register Entry",
                        entry.name,
                        {
                            "is_anomaly": 1,
                            "anomaly_reason": (
                                f"Basic+DA ₹{basic_da:.2f} < "
                                f"minimum wage ₹{applicable_rate:.2f} for {skill}"
                            ),
                        },
                        update_modified=False,
                    )
            except Exception as e:
                frappe.log_error(
                    f"Min wage check error for entry {entry.name}: {e}",
                    "WageRoll.run_minimum_wages_check",
                )
                continue

        self.min_wage_violations_count = violations
        self.minimum_wages_compliant = 1 if violations == 0 else 0

    # ──────────────────────────────────────────────────────────────────
    # REGISTER ENTRIES COUNT
    # ──────────────────────────────────────────────────────────────────

    def count_register_entries(self):
        """Count Form A entries for this period. Should equal total_employees_in_period."""
        form_a = frappe.db.get_value(
            "Statutory Register",
            {"business_entity": self.business_entity, "register_code": "FORM-A-MW"},
            "name",
        )
        if form_a:
            self.register_entries_count = frappe.db.count(
                "Register Entry",
                {
                    "register": form_a,
                    "period_month": self.wage_period_month,
                    "period_year": self.wage_period_year,
                },
            )

    # ──────────────────────────────────────────────────────────────────
    # COMPLIANCE SCORE
    # ──────────────────────────────────────────────────────────────────

    def compute_compliance_score(self):
        """
        Score starts at 100; deductions:
         -30 : min wages non-compliant
         -20 : wages paid late
         -10 : delay > 7 days (additional)
         -15 : notice board displays don't match paid wages
         -25 : register entries missing (proportional)
        Floor at 0.
        """
        score = 100

        if not self.minimum_wages_compliant:
            score -= 30

        if not self.wages_paid_within_due_date:
            score -= 20
            if (self.delay_days or 0) > 7:
                score -= 10

        if not self.displays_match_paid:
            score -= 15

        required = self.total_employees_in_period or 0
        actual = self.register_entries_count or 0
        if required > 0 and actual < required:
            gap_pct = (required - actual) / required
            score -= int(gap_pct * 25)

        self.compliance_score = max(0, score)