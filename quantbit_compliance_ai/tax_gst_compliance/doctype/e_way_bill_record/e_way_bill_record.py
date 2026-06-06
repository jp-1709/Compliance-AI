
# ─────────────────────────────────────────────────────────────────────────────
"""
E-Way Bill Record — Controller
complyai/compliance/tax_gst_compliance/doctype/e_way_bill_record/e_way_bill_record.py
 
Handles:
  - Validity computation (1 day per 200 km, min 1 day)
  - Expiry check and is_expired flag
  - Mid-transit expiry notification
  - EWB number format validation (12 digits)
"""
 
from datetime import datetime, timedelta
 
import frappe
from frappe import _
from frappe.model.document import Document
 
 
class EWayBillRecord(Document):
 
    # ──────────────────────────────────────────────
    # Frappe lifecycle hooks
    # ──────────────────────────────────────────────
 
    def validate(self):
        self.validate_ewb_number()
        self.compute_validity()
        self.check_expiry()
 
    # ──────────────────────────────────────────────
    # EWB Number Validation
    # ──────────────────────────────────────────────
 
    def validate_ewb_number(self):
        """E-Way Bill number is a 12-digit numeric string."""
        if self.ewb_number and not self.ewb_number.isdigit():
            frappe.throw(_("E-Way Bill Number must be a 12-digit number. Got: {0}").format(self.ewb_number))
        if self.ewb_number and len(self.ewb_number) != 12:
            frappe.throw(
                _("E-Way Bill Number must be exactly 12 digits. Got {0} digits.").format(len(self.ewb_number))
            )
 
    # ──────────────────────────────────────────────
    # Validity Computation
    # ──────────────────────────────────────────────
 
    def compute_validity(self):
        """
        Compute valid_until based on:
          - Normal cargo  : ceil(distance / 200) days, minimum 1 day
          - ODC cargo     : ceil(distance / 20)  days, minimum 1 day
        Currently we always use the 200 km rule; ODC can be flagged separately.
        """
        if not self.generated_at or not self.approx_distance_km:
            return
 
        dist  = int(self.approx_distance_km)
        days  = max(1, -(-dist // 200))           # ceiling division
 
        gen_dt = _to_datetime(self.generated_at)
        self.valid_until = gen_dt + timedelta(days=days)
 
    # ──────────────────────────────────────────────
    # Expiry Check
    # ──────────────────────────────────────────────
 
    def check_expiry(self):
        """
        Sets is_expired = 1 if current datetime > valid_until.
        Notifies critical users if the EWB is in transit and has expired.
        """
        if not self.valid_until:
            return
 
        exp_dt = _to_datetime(self.valid_until)
        now    = datetime.now()
 
        if now > exp_dt:
            self.is_expired = 1
            if self.ewb_status == "In Transit":
                _notify_ewb_expiry(self)
        else:
            self.is_expired = 0
 
 
# ─── Helpers ──────────────────────────────────────────────────────────────────
 
def _to_datetime(val):
    if isinstance(val, datetime):
        return val
    from datetime import date
    if isinstance(val, date):
        return datetime(val.year, val.month, val.day)
    if isinstance(val, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(val, fmt)
            except ValueError:
                continue
    return datetime.now()
 
 
def _notify_ewb_expiry(doc):
    """Send critical notification when EWB expires mid-transit."""
    msg = _(
        "E-Way Bill {0} (Invoice: {1}) has EXPIRED while In Transit. "
        "Goods movement is now illegal. Extend within 8 hours of expiry or return goods."
    ).format(doc.ewb_number or doc.name, doc.linked_invoice_number or "—")
 
    try:
        frappe.log_error(
            message=msg,
            title=f"EWB Expired In Transit — {doc.business_entity}",
        )
        # Publish real-time alert
        users = frappe.get_all(
            "Has Role",
            filters={"role": ["in", ["Compliance Officer", "Tax Head"]]},
            pluck="parent",
        )
        for user in set(users):
            frappe.publish_realtime(
                event="eval_js",
                message=f"frappe.show_alert({{message: {repr(str(msg))}, indicator: 'red'}}, 120)",
                user=user,
            )
    except Exception:
        pass
 