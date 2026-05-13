# Copyright (c) 2026, Local and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr

from whatsapp_raven_bridge.api.setup import (
	_ensure_bridge_system_user_with_state,
	_ensure_default_bridge_system_user_state,
)

MIN_BACKFILL_LIMIT = 1
MAX_BACKFILL_LIMIT = 1000
MIN_LOOKBACK_HOURS = 1
MAX_LOOKBACK_HOURS = 720


class WhatsAppRavenBridgeSettings(Document):
	def _validate_links(self):
		self._ensure_bridge_system_user_link()
		return super()._validate_links()

	def validate(self):
		self._ensure_bridge_system_user_link()
		self._validate_scheduled_backfill_fields()

	def _ensure_bridge_system_user_link(self):
		user_ref = cstr(self.bridge_system_user).strip()

		if not user_ref:
			ensured = _ensure_default_bridge_system_user_state(update_settings=False)
			user_name = cstr((ensured or {}).get("user") or "").strip()
			if not user_name:
				frappe.throw(_("Could not create or resolve the default Bridge System User."))
			self.bridge_system_user = user_name
			return

		if frappe.db.exists("User", user_ref):
			ensured = _ensure_bridge_system_user_with_state(user_ref)
			self.bridge_system_user = cstr((ensured or {}).get("user") or user_ref).strip()
			return

		if "@" in user_ref:
			ensured = _ensure_bridge_system_user_with_state(user_ref)
			user_name = cstr((ensured or {}).get("user") or "").strip()
			if not user_name:
				frappe.throw(_("Could not create or resolve Bridge System User: {0}").format(user_ref))
			self.bridge_system_user = user_name
			return

		if "@" not in user_ref:
			frappe.throw(
				_(
					"Bridge System User must be an existing User or a valid email address to auto-create."
				)
			)

	def _validate_scheduled_backfill_fields(self):
		self.scheduled_backfill_limit = self._bounded_int(
			self.scheduled_backfill_limit,
			default=200,
			min_value=MIN_BACKFILL_LIMIT,
			max_value=MAX_BACKFILL_LIMIT,
			label=_("Scheduled Backfill Limit"),
		)
		self.scheduled_backfill_lookback_hours = self._bounded_int(
			self.scheduled_backfill_lookback_hours,
			default=24,
			min_value=MIN_LOOKBACK_HOURS,
			max_value=MAX_LOOKBACK_HOURS,
			label=_("Scheduled Backfill Lookback Hours"),
		)
		if cstr(self.scheduled_backfill_interval).strip() not in {
			"Every 5 Minutes",
			"Hourly",
			"Every 5 Hours",
			"Daily",
		}:
			self.scheduled_backfill_interval = "Hourly"
		if cstr(self.scheduled_backfill_direction).strip() not in {"Both", "Incoming", "Outgoing"}:
			self.scheduled_backfill_direction = "Both"

	def _bounded_int(self, value, *, default, min_value, max_value, label):
		try:
			number = int(value) if value is not None and cstr(value).strip() != "" else int(default)
		except Exception:
			number = int(default)
		if number < min_value or number > max_value:
			frappe.throw(_("{0} must be between {1} and {2}.").format(label, min_value, max_value))
		return number
