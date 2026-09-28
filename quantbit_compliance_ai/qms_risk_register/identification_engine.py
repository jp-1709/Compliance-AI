"""
quantbit_compliance_ai/qms_risk_register/identification_engine.py

Risk Identification Engine (Q6 spec §5).

  - find_potential_duplicates() : text-similarity dedupe against the existing
                                    register (a practical stand-in for the
                                    pgvector embedding search the full spec
                                    describes — no embedding infra is wired
                                    into this app yet, so difflib gives the
                                    same reviewer-facing behaviour: "here are
                                    close matches, confirm/merge/supersede").
  - create_risk_from_event()    : one-click Draft Risk Item from a Quality
                                    Event / CAPA Case / Audit Finding / MR
                                    Output, always landing in Draft for human
                                    review (never auto-submitted, §10.2).
"""

import difflib

import frappe
from frappe import _
from frappe.utils import nowdate

DUPLICATE_THRESHOLD = 0.82

# source_doctype -> (identification_source enum value, title field, description field)
_SOURCE_MAP = {
	"Quality Event": ("Quality Event (Q1) Trigger", "title", "description"),
	"CAPA Case": ("CAPA (Q2) Trigger", "title", "problem_statement"),
	"Audit Finding": ("Audit Finding (Q4) Trigger", "title", "finding_statement"),
	"MR Output": ("MR Output (Q5) Trigger", "title", "description"),
}


def find_potential_duplicates(entity: str, title: str, description: str = "", threshold: float = DUPLICATE_THRESHOLD, limit: int = 5) -> list:
	candidate = f"{title or ''} {description or ''}".strip().lower()
	if not candidate:
		return []
	rows = frappe.get_all(
		"Risk Item",
		filters={"entity": entity, "status": ("!=", "Withdrawn")},
		fields=["name", "title", "risk_description", "status"],
	)
	scored = []
	for row in rows:
		existing = f"{row.title or ''} {row.risk_description or ''}".strip().lower()
		if not existing:
			continue
		score = difflib.SequenceMatcher(None, candidate, existing).ratio()
		if score >= threshold:
			scored.append({"name": row.name, "title": row.title, "status": row.status, "similarity": round(score, 3)})
	scored.sort(key=lambda r: -r["similarity"])
	return scored[:limit]


def _check_capa_loop(capa_name: str):
	"""Single-hop loop guard (§8.3): if this CAPA was itself auto-created by a
	Risk Treatment (source='Risk Assessment', source_reference=<treatment>),
	creating another Risk Item from it and treating *that* risk with a new
	CAPA would re-enter the same risk's lineage. We can't force a hard block
	(a human clicks "create risk from CAPA" deliberately) but we surface the
	originating risk so the reviewer sees the loop before confirming."""
	capa = frappe.db.get_value("CAPA Case", capa_name, ["source", "source_reference"], as_dict=True)
	if not capa or capa.source != "Risk Assessment" or not capa.source_reference:
		return None
	return frappe.db.get_value("Risk Treatment", capa.source_reference, "risk_item")


@frappe.whitelist()
def create_risk_from_event(
	source_doctype: str,
	source_name: str,
	entity: str,
	risk_category: str,
	method_profile: str,
	risk_owner: str,
	overrides: dict | None = None,
) -> dict:
	if source_doctype not in _SOURCE_MAP:
		frappe.throw(_("Unsupported source doctype for risk identification: {0}").format(source_doctype))

	identification_source, title_field, desc_field = _SOURCE_MAP[source_doctype]
	source_doc = frappe.get_doc(source_doctype, source_name)
	title = (source_doc.get(title_field) or source_name)[:120]
	description = source_doc.get(desc_field) or ""

	loop_origin = _check_capa_loop(source_name) if source_doctype == "CAPA Case" else None

	duplicates = find_potential_duplicates(entity, title, description)

	overrides = overrides or {}
	payload = {
		"doctype": "Risk Item",
		"entity": entity,
		"title": overrides.get("title", f"Risk from {source_doctype}: {title}")[:140],
		"risk_category": risk_category,
		"risk_description": overrides.get("risk_description", description),
		"identified_by": frappe.session.user,
		"identified_on": nowdate(),
		"identification_source": identification_source,
		"identification_source_record": source_name,
		"risk_owner": risk_owner,
		"method_profile": method_profile,
		"status": "Draft",
	}
	payload.update({k: v for k, v in overrides.items() if k not in ("title", "risk_description")})

	risk = frappe.get_doc(payload)
	risk.flags.ignore_mandatory = True  # inherent scoring fields are filled at Identified, not Draft
	risk.insert()

	if loop_origin:
		risk.add_comment(
			"Info",
			_("This risk originates from CAPA {0}, which was itself auto-created from a treatment on Risk {1}. Verify this is not a repeat of the same underlying issue before proceeding.").format(
				source_name, loop_origin
			),
		)

	return {
		"risk_item": risk.name,
		"potential_duplicates": duplicates,
		"loop_warning": loop_origin,
	}
