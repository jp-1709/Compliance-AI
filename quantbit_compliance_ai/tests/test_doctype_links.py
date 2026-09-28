import json
import unittest
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
BENCH_APPS_ROOT = APP_ROOT.parents[1]
REFERENCE_FIELDTYPES = {"Link", "Table", "Table MultiSelect"}
EXPECTED_QMS_DOCTYPES = {
	"QMS CAPA": {
		"CAPA Case", "CAPA Action", "CAPA Quality Event Link", "RCA Record",
		"RCA Five Whys", "RCA Fishbone Cause", "Effectiveness Check",
		"CAPA AI Suggestion Log",
	},
	"QMS Document Control": {
		"QMS Document", "Document Version", "Document Approval Step", "Document Distribution List",
		"Document Distribution List Member", "Document Access Log", "Document Acknowledgement",
		"Document Review Schedule", "External Document Register",
	},
	"QMS Internal Audit": {
		"Audit Programme", "Audit", "Audit Scope Item", "Audit Team Member",
		"Audit Checklist Template", "Audit Checklist Item Template", "Audit Checklist",
		"Audit Checklist Item", "Audit Finding", "Audit Finding Evidence", "Audit Report",
	},
	"QMS Management Review": {
		"MR Policy", "MR Policy Standard", "MR Cycle", "MR Cycle Standard", "MR Cycle Meeting Reference",
		"MR Meeting", "MR Meeting Attendee", "MR Agenda Item", "MR Input Record", "MR Input Standard Tag",
		"MR Input Metric", "MR Input Evidence", "MR Input Process Area", "MR Output",
		"MR Output Input Reference", "MR Output Standard Tag", "MR Briefing Pack", "MR Briefing Pack Input",
		"MR Briefing Pack Attachment", "MR Briefing Pack Recipient", "MR Minutes", "MR Minutes Input Snapshot",
		"MR Minutes Output Snapshot", "MR Minutes Attendance Snapshot", "MR Minutes Recipient",
		"MR Action Tracker", "MR Input Checklist Template", "MR Checklist Category",
	},
	"QMS Risk Register": {
		"Risk Policy", "Risk Policy Category Method", "Risk Category", "Risk Category Typical Owner",
		"Risk Category Clause", "Risk Method Profile", "Risk Method Anchor", "Risk Method Band", "Risk Appetite",
		"Risk Item", "Risk Item Sub Category", "Risk Item Standard Tag", "Risk Item Process Area",
		"Risk Item Asset Link", "Risk Assessment", "Risk Assessment Evidence", "Risk Assessment Control Credit",
		"Risk Treatment", "Risk Treatment Effectiveness Evidence", "Control", "Control Test History",
		"Risk Acceptance", "Risk Cycle", "Risk Cycle Risk", "Risk Snapshot", "Risk Action Tracker",
		"Heat Map Configuration",
	},
}

VALIDATED_MASTER_DOCTYPES = {
	"CAPA Case", "RCA Record", "Effectiveness Check",
	"QMS Document", "Document Version", "Document Distribution List", "Document Access Log",
	"Document Acknowledgement", "Document Review Schedule", "External Document Register",
	"Audit Programme", "Audit", "Audit Checklist Template", "Audit Checklist", "Audit Finding", "Audit Report",
	"MR Policy", "MR Cycle", "MR Meeting", "MR Input Record", "MR Output", "MR Briefing Pack", "MR Minutes",
	"Risk Policy", "Risk Method Profile", "Risk Appetite", "Risk Item", "Risk Assessment", "Risk Treatment",
	"Control", "Risk Acceptance", "Risk Cycle", "Risk Snapshot",
}

SPEC_SUBMITTABLE_DOCTYPES = {
	"Document Version", "Document Acknowledgement",
	"MR Cycle", "MR Meeting", "MR Input Record", "MR Output", "MR Briefing Pack", "MR Minutes",
	"Risk Appetite", "Risk Item", "Risk Assessment", "Risk Treatment", "Control", "Risk Acceptance",
	"Risk Cycle", "Risk Snapshot",
}

EXPECTED_QMS_SPLIT = {
	"QMS CAPA": (4, 4),
	"QMS Document Control": (7, 2),
	"QMS Internal Audit": (6, 5),
	"QMS Management Review": (9, 19),
	"QMS Risk Register": (13, 14),
}


def _doctype_definitions(root):
	for path in root.glob("*/**/doctype/*/*.json"):
		try:
			definition = json.loads(path.read_text())
		except (json.JSONDecodeError, OSError):
			continue
		if isinstance(definition, dict) and definition.get("doctype") == "DocType":
			yield path, definition


class TestDocTypeLinks(unittest.TestCase):
	def test_qms_primary_and_child_counts_match_specs(self):
		actual = {module: [0, 0] for module in EXPECTED_QMS_SPLIT}
		for _, definition in _doctype_definitions(APP_ROOT):
			module = definition.get("module")
			if module in actual:
				actual[module][1 if definition.get("istable") else 0] += 1
		self.assertEqual(EXPECTED_QMS_SPLIT, {module: tuple(counts) for module, counts in actual.items()})

	def test_spec_validation_controllers_exist(self):
		definitions = {
			definition["name"]: path for path, definition in _doctype_definitions(APP_ROOT)
		}
		missing = []
		for doctype in VALIDATED_MASTER_DOCTYPES:
			json_path = definitions[doctype]
			controller_path = json_path.with_suffix(".py")
			if not controller_path.exists() or "def validate(self):" not in controller_path.read_text():
				missing.append(doctype)
		self.assertFalse(missing, "Missing validation controllers: " + ", ".join(sorted(missing)))

	def test_spec_submission_boundaries_are_enabled(self):
		definitions = {
			definition["name"]: definition for _, definition in _doctype_definitions(APP_ROOT)
		}
		missing = [name for name in SPEC_SUBMITTABLE_DOCTYPES if not definitions[name].get("is_submittable")]
		self.assertFalse(missing, "Specification-defined submittable DocTypes: " + ", ".join(sorted(missing)))

	def test_qms_modules_contain_only_specified_doctypes(self):
		actual = {module: set() for module in EXPECTED_QMS_DOCTYPES}
		for _, definition in _doctype_definitions(APP_ROOT):
			module = definition.get("module")
			if module in actual:
				actual[module].add(definition["name"])

		self.assertEqual(EXPECTED_QMS_DOCTYPES, actual)

	def test_all_static_link_targets_exist(self):
		"""Every concrete Link/Table target must be shipped by an installed app."""
		available = {
			definition["name"]
			for _, definition in _doctype_definitions(BENCH_APPS_ROOT)
			if definition.get("name")
		}
		broken = []

		for path, definition in _doctype_definitions(APP_ROOT):
			for field in definition.get("fields", []):
				if field.get("fieldtype") not in REFERENCE_FIELDTYPES:
					continue
				target = field.get("options")
				if target and target not in available:
					broken.append(
						f"{definition['name']}.{field.get('fieldname')} -> {target} ({path})"
					)

		self.assertFalse(broken, "Unresolved DocType references:\n" + "\n".join(sorted(broken)))

	def test_app_has_no_erpnext_only_doctype_targets(self):
		providers = {}
		for path, definition in _doctype_definitions(BENCH_APPS_ROOT):
			providers.setdefault(definition["name"], set()).add(path.parts[-6])

		invalid = []
		for path, definition in _doctype_definitions(APP_ROOT):
			for field in definition.get("fields", []):
				target = field.get("options")
				if field.get("fieldtype") in REFERENCE_FIELDTYPES and providers.get(target) == {"erpnext"}:
					invalid.append(f"{definition['name']}.{field.get('fieldname')} -> {target} ({path})")

		self.assertFalse(invalid, "ERPNext-only DocType references:\n" + "\n".join(sorted(invalid)))

	def test_table_multiselect_targets_have_one_link_field(self):
		definitions = {
			definition["name"]: definition for _, definition in _doctype_definitions(APP_ROOT)
		}
		invalid = []

		for _, definition in _doctype_definitions(APP_ROOT):
			for field in definition.get("fields", []):
				if field.get("fieldtype") != "Table MultiSelect":
					continue
				target = definitions.get(field.get("options"), {})
				link_fields = [
					row for row in target.get("fields", []) if row.get("fieldtype") == "Link"
				]
				if not target.get("istable") or len(link_fields) != 1:
					invalid.append(f"{definition['name']}.{field.get('fieldname')}")

		self.assertFalse(
			invalid, "Invalid Table MultiSelect targets: " + ", ".join(sorted(invalid))
		)

	def test_organisation_and_regulation_fields_are_links(self):
		"""Core master references must never silently regress to free text."""
		invalid = []
		for path, definition in _doctype_definitions(APP_ROOT):
			for field in definition.get("fields", []):
				fieldname = field.get("fieldname")
				if fieldname not in {"organisation", "regulation"}:
					continue
				if field.get("fieldtype") != "Link" or field.get("options") != fieldname.title():
					invalid.append(f"{definition['name']}.{fieldname} ({path})")

		self.assertFalse(invalid, "Non-link master references: " + ", ".join(sorted(invalid)))
