import frappe
from frappe import _
from frappe.model.document import Document


class QMSSettings(Document):
	def validate(self):
		if not 1 <= (self.default_review_frequency_months or 0) <= 60:
			frappe.throw(_("Default Review Frequency must be between 1 and 60 months."))
		if (self.default_acknowledgement_window_days or 0) <= 0:
			frappe.throw(_("Default Acknowledgement Window must be greater than zero."))
		if (self.review_confirm_window_days or 0) <= 0:
			frappe.throw(_("Review Confirm Window must be greater than zero."))
		if (self.review_fanout_horizon_days or 0) <= 0:
			frappe.throw(_("Review Schedule Fan-out Horizon must be greater than zero."))
