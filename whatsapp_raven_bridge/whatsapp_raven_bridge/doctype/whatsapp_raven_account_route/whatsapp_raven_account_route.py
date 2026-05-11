# Copyright (c) 2026, Local and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr


class WhatsAppRavenAccountRoute(Document):
	def validate(self):
		self._normalize_fields()
		self._validate_unique_enabled_route()
		self._validate_inbox_channel()

	def _normalize_fields(self):
		self.inbox_channel_name = _normalize_channel_name(self.inbox_channel_name)
		if not self.inbox_channel_name and self.whatsapp_account:
			self.inbox_channel_name = _normalize_channel_name(f"whatsapp-inbox-{self.whatsapp_account}")

	def _validate_unique_enabled_route(self):
		if not self.enabled or not _doctype_ready(self.doctype):
			return

		filters = [["enabled", "=", 1], ["whatsapp_account", "=", self.whatsapp_account]]
		if not self.is_new():
			filters.append(["name", "!=", self.name])

		duplicate = frappe.get_all(self.doctype, filters=filters, pluck="name", limit=1)
		if duplicate:
			frappe.throw(
				_("An enabled route already exists for WhatsApp Account {0}: {1}").format(
					self.whatsapp_account, duplicate[0]
				)
			)

	def _validate_inbox_channel(self):
		if not self.inbox_channel:
			return

		inbox = frappe.get_doc("Raven Channel", self.inbox_channel)
		if self.raven_workspace and inbox.workspace != self.raven_workspace:
			frappe.throw(
				_("Inbox Channel {0} must belong to Raven Workspace {1}.").format(
					self.inbox_channel, self.raven_workspace
				)
			)

		if inbox.is_direct_message:
			frappe.throw(_("Inbox Channel cannot be a direct message channel."))


def _normalize_channel_name(value):
	text = cstr(value or "").strip().lower()
	text = re.sub(r"[^a-z0-9]+", "-", text)
	text = re.sub(r"-{2,}", "-", text).strip("-")
	return text


def _doctype_ready(doctype):
	try:
		return frappe.db.exists("DocType", doctype) and frappe.db.table_exists(doctype)
	except Exception:
		return False
