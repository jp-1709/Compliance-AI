"""
iso_clause.py — Frappe DocType controller for ISO Clause (F5)
ComplyAI · Foundation Module

Validations & Business Logic:
  - clause_full_id must be globally unique (used as document name)
  - clause_full_id format: ISOXXXX-X.X.X (prefix dash clause-number)
  - parent_clause must belong to the same ISO standard
  - parent_clause cannot be itself
  - level auto-derived from clause_number depth
  - version_year must match the standard's year
  - ISO Clause is a GLOBAL library — no organisation field — permission hook must skip it
"""

import re
import frappe
from frappe import _
from frappe.model.document import Document

# ─── Constants ──────────────────────────────────────────────────────────────
CLAUSE_ID_RE = re.compile(r"^[A-Z0-9]+-[\d.]+$")

# Maps ISO Standard label → canonical version year
STANDARD_YEAR: dict[str, int] = {
    "ISO 9001:2015":  2015,
    "ISO 14001:2015": 2015,
    "ISO 45001:2018": 2018,
    "ISO 22000:2018": 2018,
    "ISO 22301:2019": 2019,
    "ISO 27001:2022": 2022,
    "ISO 13485:2016": 2016,
}

# Approximate expected clause counts per standard (for seed validation)
EXPECTED_CLAUSE_COUNTS: dict[str, int] = {
    "ISO 9001:2015":  80,
    "ISO 14001:2015": 70,
    "ISO 45001:2018": 75,
    "ISO 22000:2018": 85,
    "ISO 22301:2019": 60,
    "ISO 27001:2022": 115,
    "ISO 13485:2016": 95,
}


class ISOClause(Document):
    # ─── Lifecycle hooks ────────────────────────────────────────────────────

    def validate(self):
        self.normalise_clause_id()
        self.validate_clause_full_id_format()
        self.validate_clause_number_matches_id()
        self.validate_parent_clause()
        self.auto_set_level()
        self.validate_version_year()

    def before_insert(self):
        self.validate_unique_clause_full_id()

    # ─── Field validators ───────────────────────────────────────────────────

    def normalise_clause_id(self):
        """Uppercase the clause ID for consistent storage (ISO9001 not iso9001)."""
        if self.clause_full_id:
            self.clause_full_id = self.clause_full_id.strip().upper()
        if self.clause_number:
            self.clause_number = self.clause_number.strip()

    def validate_clause_full_id_format(self):
        """
        Format: PREFIX-CLAUSE_NUMBER where PREFIX is alphanumeric and
        CLAUSE_NUMBER uses dot-separated integers (e.g. ISO9001-7.5.3).
        """
        if not self.clause_full_id:
            frappe.throw(_("Clause ID is mandatory."), frappe.MandatoryError)

        if not CLAUSE_ID_RE.match(self.clause_full_id):
            frappe.throw(
                _(
                    "Clause ID '{0}' format is invalid. "
                    "Expected: PREFIX-CLAUSE_NUMBER (e.g. ISO9001-7.5.3)."
                ).format(self.clause_full_id),
                frappe.ValidationError,
                title=_("Invalid Clause ID"),
            )

    def validate_clause_number_matches_id(self):
        """
        The numeric part of clause_full_id must match clause_number.
        e.g. ISO9001-7.5.3  → clause_number must be 7.5.3
        """
        if not self.clause_full_id or not self.clause_number:
            return
        parts = self.clause_full_id.split("-", 1)
        if len(parts) == 2 and parts[1] != self.clause_number.strip():
            frappe.throw(
                _(
                    "Clause Number '{0}' does not match the suffix of Clause ID '{1}'. "
                    "They must be identical."
                ).format(self.clause_number, self.clause_full_id),
                frappe.ValidationError,
                title=_("Clause ID / Number Mismatch"),
            )

    def validate_unique_clause_full_id(self):
        """Uniqueness is enforced at DB level (unique=1), but we give a cleaner error."""
        if frappe.db.exists("ISO Clause", self.clause_full_id):
            frappe.throw(
                _(
                    "A clause with ID '{0}' already exists. "
                    "Duplicate clause IDs are not permitted."
                ).format(self.clause_full_id),
                frappe.UniqueValidationError,
                title=_("Duplicate Clause ID"),
            )

    def validate_parent_clause(self):
        """
        Parent clause must:
          1. Exist
          2. Belong to the same ISO Standard
          3. Not be the same clause (self-reference)
          4. Be one level above (optional but recommended)
        """
        if not self.parent_clause:
            return

        if self.parent_clause == self.clause_full_id:
            frappe.throw(
                _("A clause cannot reference itself as its parent."),
                frappe.ValidationError,
            )

        parent_data = frappe.db.get_value(
            "ISO Clause",
            self.parent_clause,
            ["iso_standard", "level"],
            as_dict=True,
        )

        if not parent_data:
            frappe.throw(
                _("Parent Clause '{0}' does not exist.").format(self.parent_clause),
                frappe.DoesNotExistError,
            )

        if parent_data.iso_standard != self.iso_standard:
            frappe.throw(
                _(
                    "Parent Clause '{0}' belongs to standard '{1}', "
                    "but this clause belongs to '{2}'. Parent must be from the same standard."
                ).format(self.parent_clause, parent_data.iso_standard, self.iso_standard),
                frappe.ValidationError,
                title=_("Standard Mismatch in Parent"),
            )

    def auto_set_level(self):
        """
        Derive hierarchy level from the depth of clause_number.
        '7'        → level 1  (section)
        '7.5'      → level 2  (clause)
        '7.5.3'    → level 3  (sub-clause)
        '7.5.3.1'  → level 4  (sub-sub-clause)
        """
        if self.clause_number:
            self.level = len(self.clause_number.strip().split("."))

    def validate_version_year(self):
        """version_year must match the year encoded in iso_standard (warn, don't block)."""
        if not self.iso_standard or not self.version_year:
            return
        expected = STANDARD_YEAR.get(self.iso_standard)
        if expected and int(self.version_year) != expected:
            frappe.msgprint(
                _(
                    "Version Year {0} does not match the expected year ({1}) for '{2}'. "
                    "Please verify — mixed-year clause sets cause confusion during audits."
                ).format(self.version_year, expected, self.iso_standard),
                title=_("Version Year Warning"),
                indicator="orange",
            )

    # ─── API helpers ────────────────────────────────────────────────────────

    def get_children(self) -> list[str]:
        """Returns direct child clause IDs."""
        return frappe.get_all(
            "ISO Clause",
            filters={"parent_clause": self.name, "is_active": 1},
            pluck="name",
        )

    def get_clause_tree(self) -> list[dict]:
        """
        Returns the full subtree of this clause as a flat list of dicts
        with name, clause_number, clause_title, level.
        Useful for rendering tree views and evidence mapping.
        """
        result = []
        queue = [self.name]
        visited = set()

        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)

            children = frappe.get_all(
                "ISO Clause",
                filters={"parent_clause": current, "is_active": 1},
                fields=["name", "clause_number", "clause_title", "level"],
                order_by="clause_number asc",
            )
            result.extend(children)
            queue.extend([c["name"] for c in children])

        return result

    def get_linked_evidence_count(self) -> int:
        """
        Count Evidence Files linked to this clause.
        Cross-module query — Evidence Link table stores DocType + record name.
        """
        return frappe.db.count(
            "Evidence Link",
            {"linked_doctype": "ISO Clause", "linked_name": self.name},
        )

    @staticmethod
    def search_clauses(query: str, iso_standard: str = None) -> list[dict]:
        """
        Full-text search over ISO Clause library.
        Searches clause_full_id, clause_title, and clause_text.
        """
        filters = {"is_active": 1}
        if iso_standard:
            filters["iso_standard"] = iso_standard

        clauses = frappe.get_all(
            "ISO Clause",
            filters=filters,
            fields=["name", "clause_number", "clause_title", "iso_standard", "level"],
            or_filters={
                "clause_full_id": ("like", f"%{query}%"),
                "clause_title": ("like", f"%{query}%"),
                "clause_number": ("like", f"%{query}%"),
            },
            order_by="iso_standard asc, clause_number asc",
            limit=50,
        )
        return clauses