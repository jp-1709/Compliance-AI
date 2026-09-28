import frappe
from frappe import _
from frappe.model.document import Document

from quantbit_compliance_ai.qms_management_review.output_hooks import dispatch_outputs_to_owners
from quantbit_compliance_ai.qms_validation import as_list, row_value


class MRMinutes(Document):
	def validate(self):
		if self.chair_signed and self.secretariat_signed and self.chair_signature == self.secretariat_signature:
			frappe.throw(_("Chairperson and Secretariat signatures must be different."))
		if not self.all_standards_inputs_covered and not self.coverage_gaps_acknowledged:
			frappe.throw(_("Standards coverage gaps must be acknowledged."))
		policy = frappe.db.get_value("MR Cycle", self.cycle, "policy") if self.cycle else None
		if policy and frappe.db.get_value("MR Policy", policy, "witness_signing") and not self.witness_signed:
			frappe.throw(_("Witness signature is required by the MR Policy."))
		for row in as_list(self.captured_outputs):
			output_record = row_value(row, "output_record")
			status = frappe.db.get_value("MR Output", output_record, "status")
			if status not in {"Captured in Meeting", "Pending Owner Acceptance", "Owner Accepted", "Downstream Created", "In Progress", "Completed", "Verified Effective", "Closed", "Carried Forward"}:
				frappe.throw(_("Captured Output {0} is not in a valid meeting state.").format(output_record))

	def before_submit(self):
		if not self.chair_signed or not self.secretariat_signed:
			frappe.throw(_("Chairperson and Secretariat signatures are required."))
		if not self.generated_pdf or not self.generated_pdf_hash or not self.tamper_evident_block:
			frappe.throw(_("Generated PDF, PDF Hash, and Tamper Evident Block are required."))

	def on_submit(self):
		# §4.7 on_submit: signing the minutes is the trigger that advances the
		# cycle/meeting state machines and releases outputs to their owners.
		frappe.db.set_value("MR Cycle", self.cycle, "status", "Minutes Signed")
		frappe.db.set_value("MR Meeting", self.meeting, "status", "Minutes Signed")
		dispatched = dispatch_outputs_to_owners(self.cycle)
		self.add_comment("Info", _("Minutes signed; {0} output(s) dispatched to their owners for acceptance.").format(dispatched))
