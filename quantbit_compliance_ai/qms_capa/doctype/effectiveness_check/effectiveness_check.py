import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate

from quantbit_compliance_ai.qms_capa.effectiveness_hooks import sync_capa_from_ec
from quantbit_compliance_ai.qms_validation import text_length


class EffectivenessCheck(Document):
	def validate(self):
		if self.actual_check_date and self.earliest_check_date and getdate(self.actual_check_date) < getdate(self.earliest_check_date):
			frappe.throw(_("Actual Check Date cannot be before Earliest Check Date."))
		if self.parent_capa:
			capa = frappe.db.get_value("CAPA Case", self.parent_capa, ["capa_owner", "qa_approver"], as_dict=True)
			if capa and self.verifier in {capa.capa_owner, capa.qa_approver}:
				frappe.throw(_("Effectiveness Verifier must differ from CAPA Owner and QA Approver."))
			owners = frappe.get_all("CAPA Action", filters={"parent": self.parent_capa}, pluck="owner")
			if self.verifier in owners:
				frappe.throw(_("Effectiveness Verifier cannot own a CAPA Action."))
		if self.status == "Verified":
			if not self.outcome or text_length(self.outcome_rationale) < 100:
				frappe.throw(_("A verified check requires an Outcome and at least 100 characters of rationale."))
			if self.outcome == "Effective" and self.parent_capa:
				open_actions = frappe.db.count("CAPA Action", {"parent": self.parent_capa, "status": ["not in", ["Completed", "Cancelled"]]})
				if open_actions:
					frappe.throw(_("All CAPA Actions must be Completed or Cancelled before an Effective outcome."))

	def on_update(self):
		if self.has_value_changed("status") and self.status == "Verified":
			sync_capa_from_ec(self)
