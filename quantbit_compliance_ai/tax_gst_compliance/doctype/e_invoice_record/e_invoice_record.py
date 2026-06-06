"""
E-Invoice Record — Controller
complyai/compliance/tax_gst_compliance/doctype/e_invoice_record/e_invoice_record.py
 
Handles:
  - IRN within-7-days compliance flag
  - Delay days computation
  - 24-hour cancellation window enforcement
  - IRN format validation (64 chars)
"""
 
from datetime import datetime, timedelta, date
 
import frappe
from frappe import _
from frappe.model.document import Document
 
IRN_LENGTH = 64
 
 
class EInvoiceRecord(Document):
 
    # ──────────────────────────────────────────────
    # Frappe lifecycle hooks
    # ──────────────────────────────────────────────
 
    def validate(self):
        self.validate_irn_format()
        self.compute_irn_within_7days()
        self.validate_cancellation_window()
 
    # ──────────────────────────────────────────────
    # IRN Format Validation
    # ──────────────────────────────────────────────
 
    def validate_irn_format(self):
        """IRN must be exactly 64 hex characters when present."""
        if self.irn and len(self.irn) != IRN_LENGTH:
            frappe.throw(
                _("IRN must be exactly {0} characters. Got {1} characters: {2}").format(
                    IRN_LENGTH, len(self.irn), self.irn
                )
            )
 
    # ──────────────────────────────────────────────
    # IRN Within 7 Days
    # ──────────────────────────────────────────────
 
    def compute_irn_within_7days(self):
        """
        From 1-Apr-2024, entities with annual turnover >= ₹100 cr must generate
        IRN within 7 days of invoice date. We track this flag for all records
        regardless of turnover; the dashboard/reports can filter by turnover.
        """
        if not self.ack_date or not self.invoice_date:
            self.delay_days = 0
            self.irn_generated_within_7days = 0
            return
 
        ack_dt = (
            self.ack_date if isinstance(self.ack_date, date)
            else self.ack_date.date() if hasattr(self.ack_date, "date")
            else self.ack_date
        )
        inv_dt = (
            self.invoice_date if isinstance(self.invoice_date, date)
            else self.invoice_date
        )
 
        delta = (ack_dt - inv_dt).days
        self.delay_days = max(0, delta)
        self.irn_generated_within_7days = 1 if delta <= 7 else 0
 
    # ──────────────────────────────────────────────
    # Cancellation Window (24 hours)
    # ──────────────────────────────────────────────
 
    def validate_cancellation_window(self):
        """
        E-invoice can only be cancelled within 24 hours of IRN generation.
        After that, a credit note + new invoice is required.
        """
        if self.einvoice_status != "Cancelled":
            return
        if not self.cancellation_date or not self.ack_date:
            return
 
        ack_dt  = _to_datetime(self.ack_date)
        canc_dt = _to_datetime(self.cancellation_date)
 
        if (canc_dt - ack_dt) > timedelta(hours=24):
            frappe.throw(
                _("E-invoice IRN can only be cancelled within 24 hours of generation "
                  "(IRN generated at {0}). For late cancellations, issue a Credit Note.").format(
                    ack_dt.strftime("%d-%b-%Y %H:%M")
                )
            )
 
 
# ─── Helpers ──────────────────────────────────────────────────────────────────
 
def _to_datetime(val):
    """Convert date/datetime/string to datetime."""
    if isinstance(val, datetime):
        return val
    if isinstance(val, date):
        return datetime(val.year, val.month, val.day)
    if isinstance(val, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(val, fmt)
            except ValueError:
                continue
    return datetime.now()
 
 