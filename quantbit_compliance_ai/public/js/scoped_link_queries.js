const organisationScopedDoctypes = [
	"Advance Tax Payment",
	"Board Meeting",
	"Board Resolution",
	"BOCW Compliance",
	"Charge Filing",
	"Compliance Calendar Task",
	"Contract Labour Engagement",
	"E-Invoice Record",
	"E-Way Bill Record",
	"EHS Incident",
	"Environmental Clearance",
	"Environmental Monitoring Reading",
	"Evidence File",
	"Fire Safety Compliance",
	"GSTR Filing",
	"Hazardous Substance Inventory",
	"Income Tax Return",
	"ITC Reconciliation",
	"Labour Establishment Profile",
	"Labour Inspection",
	"Listed Entity Disclosure",
	"MCA E-Form Filing",
	"OHS Plan",
	"Operating Licence",
	"PCB Return",
	"POSH Committee",
	"POSH Complaint",
	"CAPA Case",
	"Quality Event",
	"Register Entry",
	"Related Party Transaction",
	"Risk Item",
	"Shareholder Meeting",
	"Statutory Register",
	"Statutory Register Companies",
	"Tax Notice",
	"Tax Registration",
	"TCS Return",
	"TDS Return",
	"Wage Roll",
];

organisationScopedDoctypes.forEach((doctype) => {
	frappe.ui.form.on(doctype, {
		setup(frm) {
			if (!frm.fields_dict.business_entity) return;

			frm.set_query("business_entity", () => ({
				filters: {
					organisation: frm.doc.organisation || "",
					is_active: 1,
				},
			}));
		},

		organisation(frm) {
			if (!frm.doc.business_entity) return;

			frappe.db.get_value(
				"Business Entity",
				frm.doc.business_entity,
				"organisation",
				({ organisation }) => {
					if (organisation && organisation !== frm.doc.organisation) {
						frm.set_value("business_entity", null);
					}
				}
			);
		},
	});
});
