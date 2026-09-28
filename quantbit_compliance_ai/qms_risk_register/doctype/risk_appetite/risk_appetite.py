import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, getdate, today

from quantbit_compliance_ai.qms_validation import require_user_role


class RiskAppetite(Document):
	def validate(self):
		if self.scope == "Category-Specific" and not self.risk_category:
			frappe.throw(_("Risk Category is required for a Category-Specific appetite."))
		if self.scope == "Entity-Wide" and self.risk_category:
			frappe.throw(_("Risk Category must be empty for an Entity-Wide appetite."))
		if self.effective_from and getdate(self.effective_from) < add_days(getdate(today()), -30):
			frappe.throw(_("Effective From cannot be backdated by more than 30 days."))
		if self.effective_to and self.effective_from and getdate(self.effective_to) < getdate(self.effective_from):
			frappe.throw(_("Effective To cannot be before Effective From."))
		if self.status == "Approved" and not self.effective_to:
			filters = {"entity": self.entity, "scope": self.scope, "status": "Approved", "effective_to": ["is", "not set"], "name": ("!=", self.name or "")}
			filters["risk_category"] = self.risk_category or ["is", "not set"]
			if frappe.db.exists("Risk Appetite", filters):
				frappe.throw(_("Only one active Approved appetite is allowed for this scope."))
		if self.status == "Approved":
			if not self.approved_by or not self.approved_on:
				frappe.throw(_("Approved By and Approved On are required."))
			require_user_role(self.approved_by, "Risk Acceptor", _("Approved By"))
			policy = frappe.get_all("Risk Policy", filters={"entity": self.entity}, fields=["appetite_signing_required"], order_by="modified desc", limit=1)
			if policy and policy[0].appetite_signing_required and not self.approval_signature:
				frappe.throw(_("Approval Signature is required by the Risk Policy."))

	def on_submit(self):
		filters = {"entity": self.entity, "scope": self.scope, "status": "Approved", "name": ("!=", self.name)}
		filters["risk_category"] = self.risk_category or ["is", "not set"]
		for prior in frappe.get_all("Risk Appetite", filters=filters, pluck="name"):
			frappe.db.set_value("Risk Appetite", prior, {"status": "Superseded", "effective_to": add_days(getdate(self.effective_from), -1)})
