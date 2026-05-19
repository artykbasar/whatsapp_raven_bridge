from __future__ import annotations

import frappe


def execute():
	if not frappe.db.table_exists("WhatsApp Raven Conversation"):
		return

	frappe.db.sql(
		"""
		update `tabWhatsApp Raven Conversation`
		set delivery_mode='Route Thread'
		where ifnull(delivery_mode, '')=''
		"""
	)

