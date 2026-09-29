# Copyright (c) 2026, Quantbit Technologies Pvt Ltd and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate


class MinimumWageRate(Document):

    def validate(self):
        self.validate_date_range()

    def validate_date_range(self):
        if self.effective_from and self.effective_to:
            if getdate(self.effective_to) < getdate(self.effective_from):
                frappe.throw(
                    _("Effective To ({0}) cannot be before Effective From ({1}).").format(
                        self.effective_to, self.effective_from
                    )
                )
