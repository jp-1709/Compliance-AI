import frappe
from frappe import _
from frappe.model.document import Document


class RiskSnapshot(Document):
	def validate(self):
		if self.included_risks_count is not None and self.included_risks_count < 0:
			frappe.throw(_("Included Risks Count cannot be negative."))
		if bool(self.countersigned_by) != bool(self.countersignature):
			frappe.throw(_("Countersigned By and Countersignature must be provided together."))

	def before_submit(self):
		if not all((self.generated_pdf, self.generated_pdf_hash, self.tamper_evident_block, self.signed_by, self.signature, self.signed_on)):
			frappe.throw(_("Signed snapshots require PDF, hash, tamper-evident block, signer, signature, and signing date."))
