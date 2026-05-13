import frappe


def execute():
	frappe.db.delete(
		"Singles",
		{
			"doctype": "WhatsApp Raven Bridge Settings",
			"field": "default_whatsapp_account",
		},
	)
