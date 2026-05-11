# Copyright (c) 2026, Local and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class WhatsAppRavenMessageLink(Document):
	def validate(self):
		self._validate_unique_links()

	def _validate_unique_links(self):
		if not _doctype_ready(self.doctype):
			return

		for fieldname, label in (
			("whatsapp_message", _("WhatsApp Message")),
			("whatsapp_message_id", _("WhatsApp Message ID")),
			("raven_message", _("Raven Message")),
		):
			value = self.get(fieldname)
			if not value:
				continue

			filters = [[fieldname, "=", value]]
			if not self.is_new():
				filters.append(["name", "!=", self.name])

			duplicate = frappe.get_all(self.doctype, filters=filters, pluck="name", limit=1)
			if duplicate:
				frappe.throw(_("{0} is already linked by WhatsApp Raven Message Link {1}.").format(label, duplicate[0]))


def _doctype_ready(doctype):
	try:
		return frappe.db.exists("DocType", doctype) and frappe.db.table_exists(doctype)
	except Exception:
		return False
