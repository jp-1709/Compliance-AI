"""
quantbit_compliance_ai/qms_internal_audit/findings_engine.py

Q4 spec §4.2-4.5 — the centerpiece business logic of this module:

  - issue_audit_findings()      : the atomic "Issue Findings" transaction.
                                   No Quality Event DocType ships in this app
                                   (the same gap noted in Q5/Q6), so the CAPA
                                   half of the QE->CAPA cascade is what
                                   actually runs; `linked_quality_event` is
                                   left as an honest note rather than a
                                   fabricated link.
  - sync_audit_finding_from_capa(): bidirectional sync (§4.4), two-stage
                                   closure — CAPA Closed alone is NOT enough;
                                   the finding's own closure_verified_by must
                                   also be set (the second independence layer
                                   that distinguishes an audit-driven CAPA).
  - detect_repeat_finding()     : difflib similarity vs Closed findings from
                                   the past 36 months (§4.5), advisory only —
                                   suggests, never forces, a severity upgrade.
"""

import difflib

import frappe
from frappe import _
from frappe.utils import add_days, add_months, getdate, now_datetime, nowdate, today

from quantbit_compliance_ai.qms_internal_audit.state_engine import check_and_close_audit

REPEAT_THRESHOLD = 0.75
REPEAT_LOOKBACK_MONTHS = 36
_CAPA_SEVERITY_FROM_FINDING = {"Major": "Critical", "Minor": "High"}


# ──────────────────────────────────────────────────────────────────────────────
# ISSUE FINDINGS (§4.2)
# ──────────────────────────────────────────────────────────────────────────────

def issue_audit_findings(audit_name: str, no_findings_attestation: bool = False, attestation_text: str | None = None) -> dict:
	audit = frappe.get_doc("Audit", audit_name)
	if audit.status != "In Progress":
		frappe.throw(_("Audit must be In Progress to issue findings."))

	findings = frappe.get_all("Audit Finding", filters={"audit": audit_name, "status": ("in", ["Draft", "Pending Lead Approval"])}, pluck="name")

	if not findings and not no_findings_attestation:
		frappe.throw(
			_(
				"No findings exist for this audit. If genuinely no findings, resubmit with "
				"no_findings_attestation=True (an explicit attestation is required for traceability, §4.7)."
			)
		)

	if findings:
		for name in findings:
			finding = frappe.get_doc("Audit Finding", name)
			if finding.status == "Draft":
				frappe.throw(
					_("Finding {0} is still in Draft. All findings must be submitted for Lead approval before issuing the audit.").format(finding.name)
				)
			finding.status = "Issued"
			finding.lead_auditor_approved = 1
			finding.lead_auditor_approved_at = now_datetime()
			if not finding.linked_quality_event:
				finding.linked_quality_event = "N/A — no Quality Event DocType is shipped in this app; see linked_capa for the tracked corrective action."
			detect_repeat_finding(finding)
			finding.flags.ignore_permissions = True
			finding.save()

			if finding.finding_type == "NC (Non-Conformity)" and finding.corrective_action_required:
				create_capa_for_finding(finding)
	else:
		if not attestation_text or len(attestation_text.strip()) < 200:
			frappe.throw(_("A no-findings attestation requires at least 200 characters explaining why nothing was found."))
		frappe.db.set_value("Audit", audit_name, "no_findings_attestation_text", attestation_text)
		audit.add_comment("Info", _("No-findings attestation recorded by {0}.").format(frappe.session.user))

	audit.reload()
	if findings:
		audit.findings = frappe.as_json(findings)
	audit.status = "Findings Issued"
	audit.flags.ignore_permissions = True
	audit.save()
	_recompute_finding_counts(audit_name)

	return {"audit": audit_name, "status": audit.status, "findings_issued": len(findings)}


def create_capa_for_finding(finding):
	audit = frappe.db.get_value("Audit", finding.audit, ["organisation", "lead_auditor", "auditees_primary_contact"], as_dict=True)
	severity = _CAPA_SEVERITY_FROM_FINDING.get(finding.severity, "High")

	qa_approver = audit.auditees_primary_contact if audit.auditees_primary_contact != audit.lead_auditor else finding.auditor_raised_by
	if qa_approver == audit.lead_auditor:
		frappe.log_error(title=f"CAPA auto-create skipped for finding {finding.name}", message="No QA Approver distinct from the Lead Auditor could be resolved.")
		finding.add_comment("Info", _("CAPA auto-create skipped: could not resolve a QA Approver distinct from the Lead Auditor. Create the CAPA manually."))
		return

	try:
		capa = frappe.get_doc(
			{
				"doctype": "CAPA Case",
				"title": f"CAPA for Audit Finding {finding.name}"[:140],
				"organisation": audit.organisation,
				"business_entity": finding.business_entity,
				"capa_type": "Corrective",
				"severity": severity,
				"priority": {"Critical": "P1", "High": "P2"}.get(severity, "P2"),
				"source": "Internal Audit",
				"source_reference": finding.audit,
				"problem_statement": finding.finding_statement,
				"capa_owner": audit.lead_auditor,
				"qa_approver": qa_approver,
				"target_close_date": finding.target_correction_date or add_days(today(), 90),
				"status": "Draft",
			}
		)
		capa.insert(ignore_permissions=True, ignore_mandatory=True)
		frappe.db.set_value("Audit Finding", finding.name, "linked_capa", capa.name)
	except Exception:
		frappe.log_error(title=f"CAPA auto-create failed for finding {finding.name}", message=frappe.get_traceback())
		finding.add_comment("Info", _("CAPA auto-create failed; please create manually. See the error log for detail."))


def _recompute_finding_counts(audit_name: str):
	findings = frappe.get_all("Audit Finding", filters={"audit": audit_name, "is_voided": 0}, fields=["finding_type", "severity"])
	counts = {"nc_major_count": 0, "nc_minor_count": 0, "ofi_count": 0, "obs_count": 0}
	for row in findings:
		if row.finding_type == "NC (Non-Conformity)":
			if row.severity == "Major":
				counts["nc_major_count"] += 1
			elif row.severity == "Minor":
				counts["nc_minor_count"] += 1
		elif row.finding_type == "OFI (Opportunity for Improvement)":
			counts["ofi_count"] += 1
		elif row.finding_type == "Observation":
			counts["obs_count"] += 1
	counts["total_findings_count"] = len(findings)
	frappe.db.set_value("Audit", audit_name, counts)


# ──────────────────────────────────────────────────────────────────────────────
# BIDIRECTIONAL SYNC (§4.4) — two-stage closure
# ──────────────────────────────────────────────────────────────────────────────

def sync_audit_finding_from_capa(capa_doc, method=None):
	"""Registered in hooks.py against CAPA Case's on_update event."""
	if capa_doc.source != "Internal Audit":
		return
	findings = frappe.get_all("Audit Finding", filters={"linked_capa": capa_doc.name}, fields=["name", "status", "closure_verified_by"])
	for finding in findings:
		if capa_doc.status == "In Progress" and finding.status == "Acknowledged":
			frappe.db.set_value("Audit Finding", finding.name, "status", "Action In Progress")
		elif capa_doc.status == "Pending Effectiveness" and finding.status == "Action In Progress":
			frappe.db.set_value("Audit Finding", finding.name, "status", "Action Complete")
		elif capa_doc.status == "Closed" and finding.status == "Action Complete":
			# Two-stage closure: the CAPA being Closed is one signal; the
			# finding's OWN independent closure_verified_by must already be
			# set (via verify_finding_closure) before the finding itself
			# can move to Closed.
			if finding.closure_verified_by:
				frappe.db.set_value(
					"Audit Finding", finding.name, {"status": "Closed", "actual_close_date": today(), "closure_verified_at": now_datetime()}
				)
				check_and_close_audit(frappe.db.get_value("Audit Finding", finding.name, "audit"))


# ──────────────────────────────────────────────────────────────────────────────
# REPEAT-FINDING DETECTION (§4.5)
# ──────────────────────────────────────────────────────────────────────────────

def detect_repeat_finding(finding, threshold: float = REPEAT_THRESHOLD, months: int = REPEAT_LOOKBACK_MONTHS) -> list:
	candidate = f"{finding.finding_statement or ''} {finding.requirement_breached or ''} {finding.objective_evidence or ''}".strip().lower()
	if not candidate:
		return []
	cutoff = add_months(getdate(nowdate()), -months)
	rows = frappe.get_all(
		"Audit Finding",
		filters={"business_entity": finding.business_entity, "status": "Closed", "name": ("!=", finding.name or ""), "actual_close_date": (">=", cutoff)},
		fields=["name", "finding_statement", "requirement_breached", "objective_evidence", "severity"],
	)
	matches = []
	for row in rows:
		existing = f"{row.finding_statement or ''} {row.requirement_breached or ''} {row.objective_evidence or ''}".strip().lower()
		if not existing:
			continue
		score = difflib.SequenceMatcher(None, candidate, existing).ratio()
		if score >= threshold:
			matches.append({"name": row.name, "similarity": round(score, 3), "severity": row.severity})
	if not matches:
		return []

	matches.sort(key=lambda m: -m["similarity"])
	finding.is_repeat = 1
	finding.repeat_of_finding = matches[0]["name"]
	finding.prior_similar_findings = frappe.as_json([m["name"] for m in matches[:5]])
	if finding.severity == "Minor":
		finding.add_comment(
			"Info",
			_(
				"Repeat finding detected (similar to {0}, {1}% match). Repeat findings often indicate a systemic "
				"failure — consider upgrading severity to Major."
			).format(matches[0]["name"], round(matches[0]["similarity"] * 100)),
		)
	return matches
