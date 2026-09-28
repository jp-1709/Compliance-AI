"""
quantbit_compliance_ai/qms_management_review/coverage_engine.py

Multi-Standard Coverage Check (Q5 spec §7). For each standard the cycle
covers, checks whether every mandatory input category from that standard's
checklist template has at least one non-withdrawn MR Input Record in the
cycle, and writes the result onto the cycle's own MR Cycle Standard rows
(inputs_required_count / inputs_provided_count / coverage_pct / gaps_list)
plus the cycle-level standard_coverage_status / all_standards_covered
summary that MR Minutes.validate() gates signing on.
"""

import frappe


def compute_coverage(cycle_name: str) -> dict:
	# Deliberately reads child rows via frappe.get_all + writes via frappe.db.set_value
	# rather than loading the parent doc and calling .save() — this is invoked from
	# MR Cycle.on_update(), and a full save() there would re-enter on_update()
	# recursively. Direct DB writes are the correct system-side-effect pattern
	# already used throughout this app's engines (see Q1/Q6 for the same choice).
	cycle = frappe.db.get_value("MR Cycle", cycle_name, ["name"], as_dict=True)
	if not cycle:
		return {}
	standard_rows = frappe.get_all(
		"MR Cycle Standard",
		filters={"parent": cycle_name, "parenttype": "MR Cycle"},
		fields=["name", "standard", "input_checklist_template"],
	)
	provided_categories = set(
		frappe.get_all("MR Input Record", filters={"cycle": cycle_name, "status": ("!=", "Withdrawn")}, pluck="input_category")
	)

	summary: dict = {}
	all_covered = True

	for row in standard_rows:
		if not row.input_checklist_template:
			continue
		required = [
			c.input_category
			for c in frappe.get_all(
				"MR Checklist Category",
				filters={"parent": row.input_checklist_template, "parenttype": "MR Input Checklist Template", "mandatory": 1},
				fields=["input_category"],
			)
		]
		covered = [c for c in required if c in provided_categories]
		gaps = [c for c in required if c not in provided_categories]
		pct = round(100.0 * len(covered) / len(required), 1) if required else 100.0

		frappe.db.set_value(
			"MR Cycle Standard",
			row.name,
			{"inputs_required_count": len(required), "inputs_provided_count": len(covered), "coverage_pct": pct, "gaps_list": frappe.as_json(gaps)},
			update_modified=False,
		)

		summary[row.standard] = {"required": len(required), "covered": len(covered), "pct": pct, "gaps": gaps}
		if gaps:
			all_covered = False

	frappe.db.set_value(
		"MR Cycle",
		cycle_name,
		{"standard_coverage_status": frappe.as_json(summary), "all_standards_covered": 1 if all_covered else 0},
		update_modified=False,
	)
	return summary
