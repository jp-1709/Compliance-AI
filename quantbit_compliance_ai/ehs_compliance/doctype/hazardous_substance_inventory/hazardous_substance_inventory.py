"""
Hazardous Substance Inventory — Controller
complyai/compliance/ehs_compliance/doctype/hazardous_substance_inventory/hazardous_substance_inventory.py

Handles:
  • MSIHC Schedule 3 threshold detection (CAS-keyed)
  • PESO licence requirement detection (petroleum >5KL, LPG >100kg, explosives any)
  • Off-site emergency plan requirement (MAH installation)
  • MSDS review due date (3-year cycle)
"""

import frappe
from frappe import _
from frappe.model.document import Document
from datetime import date
from dateutil.relativedelta import relativedelta


# ──────────────────────────────────────────────────────
# MSIHC Schedule 3 thresholds (kg) — CAS → threshold
# (Manufacture, Storage and Import of Hazardous Chemicals Rules 1989)
# ──────────────────────────────────────────────────────

MSIHC_THRESHOLDS: dict[str, float] = {
    # CAS Number → Max storage threshold (kg) for MSIHC applicability
    "10035-10-6": 25_000,   # Hydrogen Bromide
    "10049-04-4": 10,       # Chlorine Dioxide
    "10102-43-9": 50,       # Nitric Oxide
    "10102-44-0": 50,       # Nitrogen Dioxide
    "10544-72-6": 50,       # Nitrogen Tetroxide
    "1310-73-2":  25,       # Sodium Hydroxide (>50%)
    "7446-09-5":  250,      # Sulphur Dioxide
    "7647-01-0":  250,      # Hydrogen Chloride (Hydrochloric Acid)
    "7664-39-3":  50,       # Hydrogen Fluoride
    "7664-41-7":  150,      # Ammonia
    "7664-93-9":  25_000,   # Sulphuric Acid (>50% conc.)
    "7681-49-4":  250,      # Sodium Fluoride
    "7697-37-2":  250,      # Nitric Acid
    "7726-95-6":  10,       # Bromine
    "7782-41-4":  10,       # Fluorine
    "7782-50-5":  25,       # Chlorine
    "7783-06-4":  50,       # Hydrogen Sulphide
    "7803-51-2":  50,       # Phosphine
    "74-86-2":    50_000,   # Acetylene
    "74-87-3":    200,      # Methyl Chloride
    "74-90-8":    20,       # Hydrogen Cyanide
    "75-01-4":    500,      # Vinyl Chloride
    "75-15-0":    200,      # Carbon Disulphide
    "75-21-8":    50,       # Ethylene Oxide
    "75-31-0":    500,      # Isopropylamine
    "79-04-9":    300,      # Chloroacetyl Chloride
    "107-02-8":   20,       # Acrolein
    "107-30-2":   500,      # Chloromethyl Methyl Ether
    "108-91-8":   500,      # Cyclohexylamine
    "109-86-4":   500,      # Methyl Cellosolve
    "10595-95-6": 500,      # N-Methyl Diethanolamine
    "115-10-6":   200,      # Dimethyl Ether
    "115-29-7":   500,      # Endosulfan
    "117-84-0":   500,      # Di(2-ethylhexyl) Phthalate
    "1563-66-2":  500,      # Carbofuran
    "7803-52-3":  100,      # Stibine
    "2699-79-8":  50,       # Sulphuryl Fluoride
    "353-50-4":   100,      # Carbonyl Fluoride
    "7784-34-1":  500,      # Arsenic Trichloride
    "7784-42-1":  20,       # Arsine
    "7789-24-4":  500,      # Lithium Fluoride
}

# PESO thresholds (quantities that require PESO licence)
PESO_THRESHOLDS: dict[str, float] = {
    "petroleum_class_a": 5_000,   # litres — Class A (flash point <23°C)
    "petroleum_class_b": 5_000,   # litres — Class B (flash point 23-65°C)
    "petroleum_class_c": 25_000,  # litres — Class C (flash point >65°C)
    "lpg_kg": 100,                # kg
}


class HazardousSubstanceInventory(Document):

    # ──────────────────────────────────────────────
    # Lifecycle hooks
    # ──────────────────────────────────────────────

    def validate(self):
        self.detect_msihc_threshold()
        self.detect_peso_requirement()
        self.detect_off_site_plan_requirement()
        self.compute_msds_review_due()
        self.warn_if_critical()

    def on_update(self):
        if self.exceeds_msihc_threshold:
            self._ensure_msihc_obligations()
        if self.requires_peso_licence:
            self._flag_peso_check()

    # ──────────────────────────────────────────────
    # MSIHC threshold detection
    # ──────────────────────────────────────────────

    def detect_msihc_threshold(self):
        """
        If max_storage_quantity_kg ≥ Schedule 3 threshold for this CAS number →
        MSIHC applies and obligations are triggered.
        """
        if not self.cas_number:
            # No CAS number — can't do precise threshold check
            self.exceeds_msihc_threshold = 0
            return

        cas = self.cas_number.strip()
        if cas not in MSIHC_THRESHOLDS:
            self.exceeds_msihc_threshold = 0
            return

        threshold = MSIHC_THRESHOLDS[cas]
        qty = self.max_storage_quantity_kg or 0

        if qty >= threshold:
            self.exceeds_msihc_threshold = 1
            # Auto-upgrade MSIHC schedule if not already set
            if self.msihc_schedule in ("Not Listed", None, ""):
                self.msihc_schedule = "Schedule 3 — Class C (Highest)"
        else:
            self.exceeds_msihc_threshold = 0

    # ──────────────────────────────────────────────
    # PESO licence requirement
    # ──────────────────────────────────────────────

    def detect_peso_requirement(self):
        """
        PESO licence mandatory for:
          • Any explosive quantity
          • Flammable liquid (petroleum class A/B) storage > 5,000 litres (~4,350 kg)
          • LPG > 100 kg
        """
        if self.is_explosive:
            self.requires_peso_licence = 1
            return

        qty_kg = self.max_storage_quantity_kg or 0

        # LPG heuristic: if flammable + cryogenic/liquefied gas > 100kg
        if self.is_flammable and qty_kg > PESO_THRESHOLDS["lpg_kg"]:
            # Rough heuristic for petroleum class A/B: 5000L ≈ 4000–4350 kg
            if qty_kg >= 4_000:
                self.requires_peso_licence = 1
                return

        # Direct check: any flammable substance > 5000 litres equivalent
        if self.is_flammable and qty_kg > 5_000:
            self.requires_peso_licence = 1
            return

        self.requires_peso_licence = 0

    # ──────────────────────────────────────────────
    # Off-site emergency plan (MAH installation)
    # ──────────────────────────────────────────────

    def detect_off_site_plan_requirement(self):
        """
        Major Accident Hazard (MAH) installation:
        Off-site emergency plan required when MSIHC threshold is exceeded.
        District Collector prepares it; company provides input.
        """
        self.requires_off_site_plan = 1 if self.exceeds_msihc_threshold else 0

    # ──────────────────────────────────────────────
    # MSDS review due date
    # ──────────────────────────────────────────────

    def compute_msds_review_due(self):
        """MSDS reviewed every 3 years or on regulatory/composition change."""
        if self.msds_date and not self.msds_review_due:
            msds_date = (
                self.msds_date
                if isinstance(self.msds_date, date)
                else frappe.utils.getdate(self.msds_date)
            )
            self.msds_review_due = msds_date + relativedelta(years=3)

    # ──────────────────────────────────────────────
    # Warning messages
    # ──────────────────────────────────────────────

    def warn_if_critical(self):
        warnings = []
        if self.exceeds_msihc_threshold:
            warnings.append(
                _("⚠️ MSIHC threshold exceeded — on-site safety report, DSIR, "
                  "and DISHA obligations apply.")
            )
        if self.requires_peso_licence:
            warnings.append(
                _("⚠️ PESO licence required — check with Petroleum & Explosives Safety Organisation.")
            )
        if self.requires_off_site_plan:
            warnings.append(
                _("⚠️ MAH installation — notify District Collector for off-site emergency plan.")
            )
        if warnings:
            frappe.msgprint(
                "<br>".join(warnings),
                title=_("Regulatory Obligations Detected"),
                indicator="red" if self.exceeds_msihc_threshold else "orange",
            )

    # ──────────────────────────────────────────────
    # Post-save hooks
    # ──────────────────────────────────────────────

    def _ensure_msihc_obligations(self):
        """Create MSIHC compliance tasks if not already present."""
        try:
            existing = frappe.db.exists(
                "Compliance Calendar Task",
                {
                    "business_entity": self.business_entity,
                    "task_title": ["like", "%MSIHC Safety Report%"],
                    "status": ["in", ["Open", "In Progress"]],
                },
            )
            if not existing:
                frappe.get_doc(
                    {
                        "doctype": "Compliance Calendar Task",
                        "organisation": self.organisation,
                        "business_entity": self.business_entity,
                        "task_title": "Prepare MSIHC Safety Report (On-Site Emergency Plan)",
                        "task_type": "MSIHC Obligation",
                        "status": "Open",
                        "priority": "High",
                        "assigned_to": self.responsible_person,
                        "reference_doctype": "Hazardous Substance Inventory",
                        "reference_name": self.name,
                        "description": (
                            f"Substance '{self.substance_name}' (CAS: {self.cas_number}) "
                            f"exceeds MSIHC Schedule 3 threshold. "
                            "Required: On-Site Emergency Plan, MSIHC Safety Report, DSIR notification."
                        ),
                    }
                ).insert(ignore_permissions=True)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "MSIHC obligation task creation failed")

    def _flag_peso_check(self):
        """Log a note that PESO licence check is needed."""
        frappe.log_error(
            f"PESO licence check needed for {self.substance_name} in {self.business_entity}",
            "PESO Licence Flag",
        )


# ──────────────────────────────────────────────────────
# Scheduled: MSDS review due alerts
# ──────────────────────────────────────────────────────

def alert_msds_review_due():
    """
    Daily scheduled job.
    Alert when MSDS review is due within 90 or 30 days.
    """
    today = date.today()
    from datetime import timedelta

    due_soon = frappe.get_all(
        "Hazardous Substance Inventory",
        filters={
            "substance_status": "Active",
            "msds_review_due": ["between", [today, today + timedelta(days=90)]],
        },
        fields=["name", "substance_name", "cas_number", "msds_review_due",
                "business_entity", "responsible_person"],
    )

    for subst in due_soon:
        due = frappe.utils.getdate(subst.msds_review_due)
        days_left = (due - today).days
        frappe.sendmail(
            subject=f"MSDS Review Due: {subst.substance_name} in {days_left} days",
            message=(
                f"MSDS for <b>{subst.substance_name}</b> (CAS: {subst.cas_number}) "
                f"is due for 3-year review on {subst.msds_review_due}.<br>"
                f"Business Entity: {subst.business_entity}"
            ),
            recipients=[subst.responsible_person] if subst.responsible_person else [],
            now=True,
        )