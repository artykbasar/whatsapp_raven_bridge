"""Whitelisted APIs for historical WhatsApp->Raven backfill."""

from __future__ import annotations

from typing import Any

import frappe

from whatsapp_raven_bridge.bridge.backfill import backfill_whatsapp_messages


def _require_backfill_permission():
	if frappe.session.user == "Administrator":
		return
	frappe.only_for("System Manager")


@frappe.whitelist()
def preview_backfill(
	whatsapp_account: str | None = None,
	phone_number: str | None = None,
	from_datetime: str | None = None,
	to_datetime: str | None = None,
	direction: str | None = None,
	limit: int = 500,
) -> dict[str, Any]:
	"""Dry-run preview of WhatsApp historical backfill candidates."""
	_require_backfill_permission()
	return dict(
		backfill_whatsapp_messages(
			whatsapp_account=whatsapp_account,
			phone_number=phone_number,
			from_datetime=from_datetime,
			to_datetime=to_datetime,
			direction=direction,
			limit=limit,
			dry_run=1,
		)
	)


@frappe.whitelist()
def run_backfill(
	whatsapp_account: str | None = None,
	phone_number: str | None = None,
	from_datetime: str | None = None,
	to_datetime: str | None = None,
	direction: str | None = None,
	limit: int = 500,
) -> dict[str, Any]:
	"""Execute historical backfill of WhatsApp messages into Raven."""
	_require_backfill_permission()
	return dict(
		backfill_whatsapp_messages(
			whatsapp_account=whatsapp_account,
			phone_number=phone_number,
			from_datetime=from_datetime,
			to_datetime=to_datetime,
			direction=direction,
			limit=limit,
			dry_run=0,
		)
	)
