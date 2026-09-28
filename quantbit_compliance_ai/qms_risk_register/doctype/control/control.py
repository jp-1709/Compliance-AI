import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, getdate


class Control(Document):
	def validate(self):
		policy = frappe.get_all("Risk Policy", filters={"entity": self.entity}, fields=["effectiveness_tester_must_differ_from_control_owner"], order_by="modified desc", limit=1)
		if policy and policy[0].effectiveness_tester_must_differ_from_control_owner:
			if self.effectiveness_tester in {self.control_owner, self.control_operator}:
				frappe.throw(_("Effectiveness Tester must differ from Control Owner and Operator."))
		if self.last_test_date and self.next_test_due_date and self.control_frequency not in {"Continuous", "On-Event", "Ad Hoc"}:
			days = {"Daily": 1, "Weekly": 7, "Monthly": 31, "Quarterly": 93, "Half-Yearly": 186, "Annual": 366}
			maximum = add_days(getdate(self.last_test_date), days[self.control_frequency])
			if getdate(self.next_test_due_date) > maximum:
				frappe.throw(_("Next Test Due Date exceeds the selected Control Frequency."))
		if self.status == "Tested-Effective" and (self.last_test_result != "Effective" or not self.last_test_evidence):
			frappe.throw(_("Tested-Effective controls require an Effective result and test evidence."))
