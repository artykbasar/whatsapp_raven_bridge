"""Whitelisted APIs for historical WhatsApp->Raven backfill."""

from __future__ import annotations

from typing import Any

import frappe

from whatsapp_raven_bridge.bridge.backfill import (
	BACKFILL_LOCK_KEY,
	acquire_backfill_lock,
	backfill_whatsapp_messages,
	enqueue_scheduled_backfill,
	preview_missed_whatsapp_messages,
	release_backfill_lock,
	run_scheduled_backfill_now as run_scheduled_backfill_now_internal,
)


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
	"""Execute historical backfill synchronously."""
	_require_backfill_permission()
	lock = acquire_backfill_lock(BACKFILL_LOCK_KEY)
	if not lock.get("acquired"):
		return {
			"status": "skipped_locked",
			"lock": dict(lock),
		}
	try:
		return dict(
			backfill_whatsapp_messages(
				whatsapp_account=whatsapp_account,
				phone_number=phone_number,
				from_datetime=from_datetime,
				to_datetime=to_datetime,
				direction=direction,
				limit=limit,
				dry_run=0,
				preserve_raven_timestamps=1,
				scheduled=0,
				lock_key=None,
			)
		)
	finally:
		release_backfill_lock(BACKFILL_LOCK_KEY)


@frappe.whitelist()
def enqueue_backfill(
	whatsapp_account: str | None = None,
	phone_number: str | None = None,
	from_datetime: str | None = None,
	to_datetime: str | None = None,
	direction: str | None = None,
	limit: int = 500,
) -> dict[str, Any]:
	"""Queue asynchronous historical backfill."""
	_require_backfill_permission()
	return dict(
		enqueue_scheduled_backfill(
			whatsapp_account=whatsapp_account,
			phone_number=phone_number,
			from_datetime=from_datetime,
			to_datetime=to_datetime,
			direction=direction,
			limit=limit,
			preserve_raven_timestamps=1,
			scheduled=0,
		)
	)


@frappe.whitelist()
def run_scheduled_backfill_now() -> dict[str, Any]:
	"""Manual trigger for scheduled reconciliation configuration."""
	_require_backfill_permission()
	return dict(run_scheduled_backfill_now_internal())


@frappe.whitelist()
def preview_missed_messages(
	lookback_hours: int = 24,
	limit: int = 200,
	direction: str = "Both",
	whatsapp_account: str | None = None,
) -> dict[str, Any]:
	"""Preview missed recent messages based on lookback window."""
	_require_backfill_permission()
	return dict(
		preview_missed_whatsapp_messages(
			lookback_hours=lookback_hours,
			limit=limit,
			direction=direction,
			whatsapp_account=whatsapp_account,
		)
	)
