import frappe


MODULES = (
	("CALENDAR", "Compliance Calendar"),
	("OBLIGATIONS", "Obligation Register"),
	("TAX_GST", "Tax & GST Compliance"),
	("LABOUR", "Labour Compliance"),
	("SECRETARIAL", "Secretarial Compliance"),
	("EHS", "EHS Compliance"),
	("QMS", "Quality & CAPA"),
	("RISK", "Compliance Risk"),
)

INDUSTRY_SECTORS = (
	("AGRI", "Agriculture, Forestry & Fishing"),
	("MINING", "Mining & Quarrying"),
	("MFG", "Manufacturing"),
	("UTIL", "Electricity, Gas, Water & Utilities"),
	("CONST", "Construction & Infrastructure"),
	("TRADE", "Wholesale & Retail Trade"),
	("TRANS", "Transport & Logistics"),
	("IT", "Information Technology & Communications"),
	("FIN", "Financial & Insurance Services"),
	("PROF", "Professional & Business Services"),
	("HEALTH", "Healthcare & Life Sciences"),
	("EDU", "Education"),
	("HOSP", "Hospitality & Tourism"),
	("GOV", "Government & Public Sector"),
	("OTHER", "Other"),
)

# State codes follow the GST jurisdiction code list. The document name uses the
# stable two-letter code so links survive spelling/display-name changes.
INDIAN_STATES = (
	("AN", "Andaman and Nicobar Islands", "35", 1),
	("AP", "Andhra Pradesh", "37", 0),
	("AR", "Arunachal Pradesh", "12", 0),
	("AS", "Assam", "18", 0),
	("BR", "Bihar", "10", 0),
	("CH", "Chandigarh", "04", 1),
	("CG", "Chhattisgarh", "22", 0),
	("DH", "Dadra and Nagar Haveli and Daman and Diu", "26", 1),
	("DL", "Delhi", "07", 1),
	("GA", "Goa", "30", 0),
	("GJ", "Gujarat", "24", 0),
	("HR", "Haryana", "06", 0),
	("HP", "Himachal Pradesh", "02", 0),
	("JK", "Jammu and Kashmir", "01", 1),
	("JH", "Jharkhand", "20", 0),
	("KA", "Karnataka", "29", 0),
	("KL", "Kerala", "32", 0),
	("LA", "Ladakh", "38", 1),
	("LD", "Lakshadweep", "31", 1),
	("MP", "Madhya Pradesh", "23", 0),
	("MH", "Maharashtra", "27", 0),
	("MN", "Manipur", "14", 0),
	("ML", "Meghalaya", "17", 0),
	("MZ", "Mizoram", "15", 0),
	("NL", "Nagaland", "13", 0),
	("OD", "Odisha", "21", 0),
	("PY", "Puducherry", "34", 1),
	("PB", "Punjab", "03", 0),
	("RJ", "Rajasthan", "08", 0),
	("SK", "Sikkim", "11", 0),
	("TN", "Tamil Nadu", "33", 0),
	("TS", "Telangana", "36", 0),
	("TR", "Tripura", "16", 0),
	("UP", "Uttar Pradesh", "09", 0),
	("UK", "Uttarakhand", "05", 0),
	("WB", "West Bengal", "19", 0),
)

RISK_CATEGORIES = (
	("strategic", "Strategic"),
	("operational", "Operational"),
	("compliance", "Compliance"),
	("infosec", "Information Security"),
	("ohs", "OH&S"),
	("environmental", "Environmental"),
	("food-safety", "Food Safety"),
	("product-safety", "Product Safety"),
	("business-continuity", "Business Continuity"),
	("financial", "Financial"),
	("reputational", "Reputational"),
	("other", "Other"),
)


def ensure_reference_data():
	"""Idempotently seed reference data needed by mandatory Link fields."""
	if not frappe.db.exists("DocType", "Compliance Module"):
		return

	for module_key, module_name in MODULES:
		_insert_if_missing(
			"Compliance Module",
			module_key,
			{"module_key": module_key, "module_name": module_name, "is_active": 1},
		)

	for sector_code, sector_name in INDUSTRY_SECTORS:
		_insert_if_missing(
			"Industry Sector",
			sector_code,
			{"sector_code": sector_code, "sector_name": sector_name, "is_active": 1},
		)

	for state_code, state_name, gst_state_code, is_union_territory in INDIAN_STATES:
		_insert_if_missing(
			"State",
			state_code,
			{
				"state_code": state_code,
				"state_name": state_name,
				"country": "India",
				"gst_state_code": gst_state_code,
				"is_union_territory": is_union_territory,
				"is_active": 1,
			},
		)

	if frappe.db.exists("DocType", "Risk Category"):
		for slug, category_name in RISK_CATEGORIES:
			_insert_if_missing(
				"Risk Category",
				slug,
				{"slug": slug, "category_name": category_name, "is_seed_data": 1},
			)


def _insert_if_missing(doctype, name, values):
	if not frappe.db.exists(doctype, name):
		frappe.get_doc({"doctype": doctype, **values}).insert(ignore_permissions=True)
