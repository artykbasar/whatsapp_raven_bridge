"""Historical WhatsApp->Raven backfill helpers."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

import frappe
from frappe import _
from frappe.utils import add_to_date, cstr, escape_html, get_datetime, now_datetime, strip_html_tags

from whatsapp_raven_bridge.bridge.conversation import (
	create_message_link,
	get_existing_message_link_by_whatsapp_message,
	get_existing_message_link_by_whatsapp_message_id,
	get_or_create_conversation,
	normalize_phone_number,
)
from whatsapp_raven_bridge.bridge.raven_destination import ensure_raven_destination
from whatsapp_raven_bridge.utils.settings import get_settings

BACKFILL_LOCK_KEY = "whatsapp_raven_bridge:historical_backfill_lock"
BACKFILL_LOCK_TIMEOUT_SECONDS = 30 * 60
SCHEDULE_INTERVAL_MINUTES = {
	"Every 5 Minutes": 5,
	"Hourly": 60,
	"Every 5 Hours": 5 * 60,
	"Daily": 24 * 60,
}


def backfill_whatsapp_messages(
	whatsapp_account: str | None = None,
	phone_number: str | None = None,
	from_datetime: str | datetime | None = None,
	to_datetime: str | datetime | None = None,
	direction: str | None = None,
	limit: int | None = 500,
	dry_run: int = 1,
	preserve_raven_timestamps: int = 1,
	scheduled: int = 0,
	lock_key: str | None = None,
) -> frappe._dict:
	"""Preview or run backfill of historical WhatsApp Message rows."""
	limit = _normalize_backfill_limit(limit)
	dry_run = 1 if int(dry_run or 0) else 0
	preserve_raven_timestamps = 1 if int(preserve_raven_timestamps or 0) else 0
	scheduled = 1 if int(scheduled or 0) else 0
	settings = get_settings()
	if not settings or not int(settings.get("enabled") or 0):
		frappe.throw(_("WhatsApp Raven Bridge is disabled in settings."))
	if not settings.get("bridge_raven_user"):
		frappe.throw(_("Bridge Raven User is required in settings for backfill."))

	candidates = get_backfill_candidates(
		whatsapp_account=whatsapp_account,
		phone_number=phone_number,
		from_datetime=from_datetime,
		to_datetime=to_datetime,
		direction=direction,
		limit=limit,
	)
	if dry_run:
		return _build_preview_summary(candidates)

	summary = frappe._dict(
		{
			"dry_run": 0,
			"scheduled": scheduled,
			"scanned": len(candidates),
			"imported": 0,
			"skipped_existing": 0,
			"skipped_unsupported": 0,
			"errors": [],
			"conversations_touched": [],
			"earliest_timestamp": None,
			"latest_timestamp": None,
			"warnings": [],
		}
	)
	seen_conversations = set()
	touched_channels = set()
	touched_inbox_channels = set()

	try:
		for item in candidates:
			doc = item.get("doc")
			try:
				result = process_backfill_whatsapp_message(
					doc=doc,
					settings=settings,
					original_info=item.get("original_info"),
					preserve_raven_timestamps=preserve_raven_timestamps,
				)
			except Exception:
				summary.errors.append(
					{
						"whatsapp_message": cstr(getattr(doc, "name", "")).strip(),
						"message_id": cstr(doc.get("message_id") if doc else "").strip(),
						"error": frappe.get_traceback(),
					}
				)
				continue

			status = cstr(result.get("status") if isinstance(result, dict) else result).strip()
			if status == "imported":
				summary.imported += 1
				channel_id = cstr(result.get("channel") or "").strip()
				if channel_id:
					touched_channels.add(channel_id)
				inbox_channel_id = cstr(result.get("inbox_channel") or "").strip()
				if inbox_channel_id:
					touched_inbox_channels.add(inbox_channel_id)
				conversation_name = cstr(result.get("conversation") or "").strip()
				if conversation_name and conversation_name not in seen_conversations:
					seen_conversations.add(conversation_name)
					summary.conversations_touched.append(conversation_name)

				used_dt = result.get("original_datetime")
				if used_dt:
					used_dt = get_datetime(used_dt)
					if not summary.earliest_timestamp or used_dt < get_datetime(summary.earliest_timestamp):
						summary.earliest_timestamp = used_dt
					if not summary.latest_timestamp or used_dt > get_datetime(summary.latest_timestamp):
						summary.latest_timestamp = used_dt
			elif status in (
				"skipped_existing_whatsapp_message",
				"skipped_existing_whatsapp_message_id",
				"skipped_existing_raven_message",
			):
				summary.skipped_existing += 1
			elif status in ("skipped_unsupported_content_type", "skipped_missing_phone", "skipped_missing_body"):
				summary.skipped_unsupported += 1

		for channel_id in sorted(touched_channels):
			refresh_thread_last_message_state(channel_id)
		for channel_id in sorted(touched_inbox_channels):
			refresh_thread_last_message_state(channel_id)

		if summary.earliest_timestamp:
			summary.earliest_timestamp = get_datetime(summary.earliest_timestamp).strftime("%Y-%m-%d %H:%M:%S")
		if summary.latest_timestamp:
			summary.latest_timestamp = get_datetime(summary.latest_timestamp).strftime("%Y-%m-%d %H:%M:%S")
		return summary
	finally:
		if lock_key:
			release_backfill_lock(lock_key)


def get_backfill_candidates(
	whatsapp_account: str | None = None,
	phone_number: str | None = None,
	from_datetime: str | datetime | None = None,
	to_datetime: str | datetime | None = None,
	direction: str | None = None,
	limit: int | None = 500,
) -> list[frappe._dict]:
	"""Return WhatsApp Message docs eligible for historical text backfill."""
	limit = _normalize_backfill_limit(limit)
	normalized_phone = normalize_phone_number(phone_number) if phone_number else ""
	from_dt = get_datetime(from_datetime) if from_datetime else None
	to_dt = get_datetime(to_datetime) if to_datetime else None

	filters: list[list[Any]] = [["content_type", "=", "text"]]
	if whatsapp_account:
		filters.append(["whatsapp_account", "=", whatsapp_account])
	direction_filter = _normalize_direction(direction)
	if direction_filter:
		filters.append(["type", "=", direction_filter])
	if from_dt:
		filters.append(["creation", ">=", from_dt])
	if to_dt:
		filters.append(["creation", "<=", to_dt])

	base_limit = limit if limit is not None else 0
	fetch_limit = (base_limit if not normalized_phone else max(base_limit * 5, 1000)) if base_limit else None
	rows = frappe.get_all(
		"WhatsApp Message",
		filters=filters,
		fields=["name", "creation", "modified"],
		order_by="creation asc, name asc",
		limit=fetch_limit,
	)

	timestamp_cache: dict[str, Any] = {}
	candidates: list[frappe._dict] = []
	for row in rows:
		doc = frappe.get_doc("WhatsApp Message", row.name)
		if normalized_phone:
			phone = normalize_phone_number(doc.get("from") or doc.get("to"))
			if phone != normalized_phone:
				continue
		info = get_whatsapp_original_datetime(doc, timestamp_cache=timestamp_cache)
		if from_dt and info.original_datetime and get_datetime(info.original_datetime) < from_dt:
			continue
		if to_dt and info.original_datetime and get_datetime(info.original_datetime) > to_dt:
			continue
		candidates.append(
			frappe._dict(
				{
					"doc": doc,
					"original_info": info,
				}
			)
		)

	candidates.sort(
		key=lambda x: (
			get_datetime(x.original_info.original_datetime or x.doc.creation),
			cstr(x.doc.name),
		)
	)
	return candidates if limit is None else candidates[:limit]


def process_backfill_whatsapp_message(
	doc,
	settings=None,
	original_info=None,
	preserve_raven_timestamps: int = 1,
) -> frappe._dict:
	"""Backfill one WhatsApp Message into Raven without sending anything to Meta."""
	settings = settings or get_settings()
	if cstr(getattr(doc, "doctype", "")).strip() != "WhatsApp Message":
		return frappe._dict({"status": "skipped_invalid_doctype"})
	if cstr(doc.get("content_type")).lower() != "text":
		return frappe._dict({"status": "skipped_unsupported_content_type"})

	phone_raw = cstr(doc.get("from") if cstr(doc.get("type")) == "Incoming" else doc.get("to")).strip()
	phone_normalized = normalize_phone_number(phone_raw)
	if not phone_normalized:
		return frappe._dict({"status": "skipped_missing_phone"})

	if get_existing_message_link_by_whatsapp_message(doc.name):
		return frappe._dict({"status": "skipped_existing_whatsapp_message"})
	if doc.get("message_id") and get_existing_message_link_by_whatsapp_message_id(doc.get("message_id")):
		return frappe._dict({"status": "skipped_existing_whatsapp_message_id"})

	existing_raven_message_name = frappe.db.exists(
		"Raven Message",
		{"link_doctype": "WhatsApp Message", "link_document": doc.name},
	)
	if existing_raven_message_name:
		conversation = get_or_create_conversation(
			phone_raw,
			whatsapp_account=doc.get("whatsapp_account"),
			profile_name=doc.get("profile_name"),
		)
		create_message_link(
			conversation=conversation.name,
			direction=_map_link_direction(doc.get("type")),
			whatsapp_message=doc.name,
			whatsapp_message_id=doc.get("message_id"),
			raven_message=existing_raven_message_name,
			raven_channel=frappe.db.get_value("Raven Message", existing_raven_message_name, "channel_id"),
			content_type=doc.get("content_type"),
			sync_status="Synced",
			metadata={"source": "whatsapp_backfill", "whatsapp_message": doc.name},
			source_creation=doc.creation,
			source_modified=doc.modified,
			whatsapp_timestamp=cstr((original_info or {}).get("whatsapp_timestamp") or "").strip() or None,
			original_message_datetime=(original_info or {}).get("original_datetime") or doc.creation,
			imported_at=now_datetime(),
			is_backfilled=1,
		)
		return frappe._dict({"status": "skipped_existing_raven_message"})

	original_info = original_info or get_whatsapp_original_datetime(doc)
	original_datetime = get_datetime(original_info.original_datetime or doc.creation or doc.modified or now_datetime())
	body_text = _extract_whatsapp_body_text(doc)
	if not body_text:
		return frappe._dict({"status": "skipped_missing_body"})

	conversation = get_or_create_conversation(
		phone_raw,
		whatsapp_account=doc.get("whatsapp_account"),
		profile_name=doc.get("profile_name"),
	)

	parent_before = cstr(conversation.parent_raven_message or "").strip()
	channel_before = cstr(conversation.raven_channel or "").strip()
	raven_channel = ensure_raven_destination(conversation)
	conversation.reload()

	thread_parent_created = (
		cstr(conversation.conversation_strategy) == "Thread Per Contact"
		and not parent_before
		and cstr(conversation.parent_raven_message or "").strip()
	)
	thread_channel_created = (
		cstr(conversation.conversation_strategy) == "Thread Per Contact"
		and not channel_before
		and cstr(conversation.raven_channel or "").strip()
	)

	metadata = {
		"source": "whatsapp_backfill",
		"direction": _map_metadata_direction(doc.get("type")),
		"whatsapp_message": doc.name,
		"whatsapp_message_id": doc.get("message_id"),
		"phone_number": phone_normalized,
		"profile_name": doc.get("profile_name"),
		"whatsapp_account": doc.get("whatsapp_account"),
		"content_type": doc.get("content_type"),
		"is_backfilled": 1,
		"source_creation": cstr(doc.creation),
		"source_modified": cstr(doc.modified),
		"whatsapp_timestamp": cstr(original_info.whatsapp_timestamp or ""),
	}
	if original_info.warning:
		metadata["timestamp_warning"] = original_info.warning

	previous_syncing = getattr(frappe.flags, "whatsapp_raven_bridge_syncing", False)
	try:
		frappe.flags.whatsapp_raven_bridge_syncing = True
		raven_message = frappe.get_doc(
			{
				"doctype": "Raven Message",
				"channel_id": raven_channel.name,
				"message_type": "Text",
				"text": build_backfill_raven_text(doc, phone_normalized, body_text),
				"json": metadata,
				"is_bot_message": 1,
				"bot": settings.get("bridge_raven_user"),
				"link_doctype": "WhatsApp Message",
				"link_document": doc.name,
			}
		).insert(ignore_permissions=True)
	finally:
		frappe.flags.whatsapp_raven_bridge_syncing = previous_syncing

	if preserve_raven_timestamps:
		set_raven_message_timestamp(raven_message.name, original_datetime)

	if preserve_raven_timestamps:
		if thread_parent_created and conversation.parent_raven_message:
			set_raven_message_timestamp(conversation.parent_raven_message, original_datetime)
		if thread_channel_created and conversation.raven_channel:
			frappe.db.sql(
				"""
				update `tabRaven Channel`
				set creation=%s, modified=%s
				where name=%s
				""",
				(original_datetime, original_datetime, conversation.raven_channel),
			)

	direction = _map_link_direction(doc.get("type"))
	message_link = create_message_link(
		conversation=conversation.name,
		direction=direction,
		whatsapp_message=doc.name,
		whatsapp_message_id=doc.get("message_id"),
		raven_message=raven_message.name,
		raven_channel=raven_channel.name,
		content_type=doc.get("content_type"),
		sync_status="Synced",
		metadata=metadata,
		source_creation=doc.creation,
		source_modified=doc.modified,
		whatsapp_timestamp=cstr(original_info.whatsapp_timestamp or "").strip() or None,
		original_message_datetime=original_datetime,
		imported_at=now_datetime(),
		is_backfilled=1,
	)

	_update_conversation_backfill_state(conversation, doc, raven_message, original_datetime)
	return frappe._dict(
		{
			"status": "imported",
			"conversation": conversation.name,
			"channel": raven_channel.name,
			"raven_message": raven_message.name,
			"link": message_link.name,
			"original_datetime": original_datetime,
			"inbox_channel": _get_route_inbox_channel(conversation.account_route),
		}
	)


def get_whatsapp_original_datetime(whatsapp_message, timestamp_cache=None) -> frappe._dict:
	"""Resolve original source datetime for backfill ordering/timestamps."""
	doc = whatsapp_message
	message_id = cstr(doc.get("message_id") or "").strip()
	meta_timestamp = cstr(doc.get("whatsapp_timestamp") or doc.get("timestamp") or "").strip()

	if not meta_timestamp and message_id:
		meta_timestamp = _get_meta_timestamp_for_message_id(message_id, timestamp_cache=timestamp_cache) or ""

	original_datetime = _parse_meta_timestamp(meta_timestamp) if meta_timestamp else None
	warning = None
	if not original_datetime and doc.creation:
		original_datetime = get_datetime(doc.creation)
	elif not original_datetime and doc.modified:
		original_datetime = get_datetime(doc.modified)
	if not original_datetime:
		original_datetime = now_datetime()
		warning = "fallback_now_datetime"

	return frappe._dict(
		{
			"original_datetime": original_datetime,
			"whatsapp_timestamp": meta_timestamp or None,
			"source_creation": doc.creation,
			"source_modified": doc.modified,
			"warning": warning,
		}
	)


def set_raven_message_timestamp(raven_message_name: str, original_datetime) -> None:
	"""Set Raven Message creation/modified to source datetime for backfilled rows."""
	if not raven_message_name or not frappe.db.exists("Raven Message", raven_message_name):
		frappe.throw(_("Raven Message not found: {0}").format(raven_message_name))
	target_dt = get_datetime(original_datetime)
	frappe.db.sql(
		"""
		update `tabRaven Message`
		set creation=%s, modified=%s
		where name=%s
		""",
		(target_dt, target_dt, raven_message_name),
	)
	frappe.clear_document_cache("Raven Message", raven_message_name)


def refresh_thread_last_message_state(channel_id: str) -> None:
	"""Refresh Raven Channel last_message_* fields based on latest message timestamps."""
	if not channel_id or not frappe.db.exists("Raven Channel", channel_id):
		return

	latest = frappe.get_all(
		"Raven Message",
		filters={"channel_id": channel_id, "message_type": ["!=", "System"]},
		fields=["name", "content", "message_type", "owner", "is_bot_message", "bot", "creation"],
		order_by="creation desc, modified desc, name desc",
		limit=1,
	)
	if not latest:
		latest = frappe.get_all(
			"Raven Message",
			filters={"channel_id": channel_id},
			fields=["name", "content", "message_type", "owner", "is_bot_message", "bot", "creation"],
			order_by="creation desc, modified desc, name desc",
			limit=1,
		)
		if not latest:
			return
	msg = latest[0]
	message_details = json.dumps(
		{
			"message_id": msg.name,
			"content": msg.content,
			"message_type": msg.message_type,
			"owner": msg.owner,
			"is_bot_message": msg.is_bot_message,
			"bot": msg.bot,
		}
	)
	frappe.db.sql(
		"""
		update `tabRaven Channel`
		set last_message_timestamp=%s, last_message_details=%s
		where name=%s
		""",
		(msg.creation, message_details, channel_id),
	)
	frappe.clear_document_cache("Raven Channel", channel_id)


def build_backfill_raven_text(doc, normalized_phone: str, body_text: str) -> str:
	"""Build safe Raven HTML text for imported historical WhatsApp message."""
	is_incoming = cstr(doc.get("type")) == "Incoming"
	label = "WhatsApp from" if is_incoming else "WhatsApp outgoing"
	sender = cstr(doc.get("profile_name") or "").strip() if is_incoming else ""
	sender = sender or ("Customer" if is_incoming else "Agent")
	header = f"<p><strong>{escape_html(label)} {escape_html(sender)}</strong> <code>{escape_html(normalized_phone)}</code></p>"
	body_html = f"<p>{escape_html(body_text).replace(chr(10), '<br>')}</p>"
	return f"{header}{body_html}"


def _extract_whatsapp_body_text(doc) -> str:
	raw = cstr(doc.get("message") or "")
	if not raw:
		return ""
	text_html = re.sub(r"(?i)<br\\s*/?>", "\n", raw)
	text_html = re.sub(r"(?i)</p\\s*>", "\n", text_html)
	text_html = re.sub(r"(?i)<p[^>]*>", "", text_html)
	plain = strip_html_tags(text_html)
	plain = plain.replace("\r\n", "\n").replace("\r", "\n")
	plain = re.sub(r"\n{3,}", "\n\n", plain)
	return plain.strip()


def _get_meta_timestamp_for_message_id(message_id: str, timestamp_cache=None) -> str | None:
	cache = timestamp_cache if isinstance(timestamp_cache, dict) else None
	if cache is not None and message_id in cache:
		return cache[message_id]

	log_rows = frappe.get_all(
		"WhatsApp Notification Log",
		filters=[
			["template", "=", "Webhook"],
			["meta_data", "like", f"%{message_id}%"],
		],
		fields=["meta_data", "creation"],
		order_by="creation desc",
		limit=30,
	)
	for row in log_rows:
		payload = _as_dict(row.get("meta_data"))
		for msg in _extract_webhook_messages(payload):
			if cstr(msg.get("id")) == message_id:
				timestamp_value = cstr(msg.get("timestamp") or "").strip()
				if timestamp_value:
					if cache is not None:
						cache[message_id] = timestamp_value
					return timestamp_value
	if cache is not None:
		cache[message_id] = None
	return None


def _extract_webhook_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
	messages = []
	for entry in payload.get("entry", []) or []:
		for change in entry.get("changes", []) or []:
			value = change.get("value", {}) or {}
			for message in value.get("messages", []) or []:
				if isinstance(message, dict):
					messages.append(message)
	return messages


def _parse_meta_timestamp(value: str):
	value = cstr(value or "").strip()
	if not value:
		return None
	if value.isdigit():
		try:
			return datetime.fromtimestamp(int(value), tz=timezone.utc).replace(tzinfo=None)
		except Exception:
			return None
	try:
		return get_datetime(value)
	except Exception:
		return None


def _map_link_direction(message_type: str) -> str:
	return "Incoming" if cstr(message_type) == "Incoming" else "Outgoing"


def _map_metadata_direction(message_type: str) -> str:
	return "incoming" if cstr(message_type) == "Incoming" else "outgoing_import"


def _normalize_direction(direction: str | None) -> str | None:
	value = cstr(direction or "").strip().lower()
	if not value or value == "both":
		return None
	if value in ("incoming", "inbound"):
		return "Incoming"
	if value in ("outgoing", "outbound"):
		return "Outgoing"
	frappe.throw(_("Invalid direction. Use Incoming or Outgoing."))


def _as_dict(value):
	if isinstance(value, dict):
		return value
	if not value:
		return {}
	try:
		return frappe.parse_json(value) or {}
	except Exception:
		return {}


def _build_preview_summary(candidates: list[frappe._dict]) -> frappe._dict:
	summary = frappe._dict(
		{
			"dry_run": 1,
			"scanned": len(candidates),
			"eligible": 0,
			"skipped_existing": 0,
			"by_account": {},
			"by_direction": {"Incoming": 0, "Outgoing": 0},
			"by_phone": {},
			"sample": [],
		}
	)
	for item in candidates:
		doc = item.doc
		if get_existing_message_link_by_whatsapp_message(doc.name) or (
			doc.get("message_id") and get_existing_message_link_by_whatsapp_message_id(doc.get("message_id"))
		):
			summary.skipped_existing += 1
			continue
		summary.eligible += 1
		account = cstr(doc.get("whatsapp_account") or "No Account")
		direction = cstr(doc.get("type") or "Unknown")
		phone = normalize_phone_number(doc.get("from") or doc.get("to"))
		summary.by_account[account] = int(summary.by_account.get(account, 0)) + 1
		summary.by_direction[direction] = int(summary.by_direction.get(direction, 0)) + 1
		summary.by_phone[phone] = int(summary.by_phone.get(phone, 0)) + 1

		if len(summary.sample) < 25:
			summary.sample.append(
				{
					"whatsapp_message": doc.name,
					"message_id": doc.get("message_id"),
					"type": doc.get("type"),
					"phone": phone,
					"whatsapp_account": doc.get("whatsapp_account"),
					"original_datetime": cstr(item.original_info.original_datetime),
				}
			)
	return summary


def _update_conversation_backfill_state(conversation, source_doc, raven_message, original_datetime):
	is_incoming = cstr(source_doc.get("type")) == "Incoming"
	if is_incoming:
		conversation.last_inbound_at = original_datetime
		conversation.last_inbound_whatsapp_message = source_doc.name
	else:
		conversation.last_outbound_at = original_datetime
		conversation.last_outbound_whatsapp_message = source_doc.name

	if source_doc.get("message_id"):
		conversation.last_whatsapp_message_id = source_doc.get("message_id")
	conversation.last_raven_message = raven_message.name
	if source_doc.get("profile_name") and (not conversation.display_name or conversation.display_name == conversation.phone_number):
		conversation.display_name = source_doc.get("profile_name")
	conversation.save(ignore_permissions=True)


def _get_route_inbox_channel(route_name: str | None) -> str | None:
	if not route_name:
		return None
	return frappe.db.get_value("WhatsApp Raven Account Route", route_name, "inbox_channel")


def preview_missed_whatsapp_messages(
	lookback_hours: int = 24,
	limit: int = 200,
	direction: str = "Both",
	whatsapp_account: str | None = None,
) -> frappe._dict:
	"""Preview recent unlinked WhatsApp messages for reconciliation."""
	lookback_hours = max(1, min(int(lookback_hours or 24), 720))
	limit = max(1, min(int(limit or 200), 1000))
	to_dt = now_datetime()
	from_dt = add_to_date(to_dt, hours=-lookback_hours)
	return backfill_whatsapp_messages(
		whatsapp_account=whatsapp_account,
		from_datetime=from_dt,
		to_datetime=to_dt,
		direction=_direction_for_backfill_query(direction),
		limit=limit,
		dry_run=1,
	)


def enqueue_scheduled_backfill(
	*,
	whatsapp_account: str | None = None,
	phone_number: str | None = None,
	from_datetime=None,
	to_datetime=None,
	direction: str | None = None,
	limit: int | None = 200,
	preserve_raven_timestamps: int = 1,
	scheduled: int = 1,
) -> frappe._dict:
	"""Enqueue backfill job with cross-run lock to avoid overlaps."""
	lock = acquire_backfill_lock()
	if not lock.get("acquired"):
		return frappe._dict(
			{
				"status": "skipped_locked",
				"lock": lock,
			}
		)

	try:
		job = frappe.enqueue(
			method="whatsapp_raven_bridge.bridge.backfill._run_backfill_job",
			queue="long",
			timeout=60 * 30,
			whatsapp_account=whatsapp_account,
			phone_number=phone_number,
			from_datetime=from_datetime,
			to_datetime=to_datetime,
			direction=direction,
			limit=limit,
			preserve_raven_timestamps=preserve_raven_timestamps,
			scheduled=scheduled,
			lock_key=BACKFILL_LOCK_KEY,
		)
	except Exception:
		release_backfill_lock(BACKFILL_LOCK_KEY)
		raise
	job_id = getattr(job, "id", None) or cstr(job)
	return frappe._dict({"status": "queued", "job_id": job_id, "lock": lock})


def run_scheduled_backfill_if_due() -> frappe._dict:
	"""Scheduler entrypoint; no-op unless due and enabled in settings."""
	settings = get_settings()
	if not settings:
		return frappe._dict({"status": "skipped_missing_settings"})
	if not int(settings.get("enable_scheduled_backfill") or 0):
		return frappe._dict({"status": "skipped_disabled"})

	interval = cstr(settings.get("scheduled_backfill_interval") or "Hourly").strip()
	if interval not in SCHEDULE_INTERVAL_MINUTES:
		interval = "Hourly"
	last_run = settings.get("last_scheduled_backfill_at")
	if last_run:
		next_due_at = add_to_date(last_run, minutes=SCHEDULE_INTERVAL_MINUTES[interval])
		if now_datetime() < get_datetime(next_due_at):
			return frappe._dict({"status": "skipped_not_due", "next_due_at": next_due_at})

	lookback_hours = max(1, min(int(settings.get("scheduled_backfill_lookback_hours") or 24), 720))
	limit = max(1, min(int(settings.get("scheduled_backfill_limit") or 200), 1000))
	direction = cstr(settings.get("scheduled_backfill_direction") or "Both")
	from_dt = add_to_date(now_datetime(), hours=-lookback_hours)
	to_dt = now_datetime()

	queued = enqueue_scheduled_backfill(
		whatsapp_account=None,
		from_datetime=from_dt,
		to_datetime=to_dt,
		direction=_direction_for_backfill_query(direction),
		limit=limit,
		preserve_raven_timestamps=1,
		scheduled=1,
	)

	if queued.get("status") == "queued":
		_safe_update_scheduled_backfill_state(
			status="queued",
			job_id=queued.get("job_id"),
			summary={
				"interval": interval,
				"lookback_hours": lookback_hours,
				"limit": limit,
				"direction": direction,
				"from_datetime": cstr(from_dt),
				"to_datetime": cstr(to_dt),
			},
		)
		return queued

	if queued.get("status") == "skipped_locked":
		_safe_update_scheduled_backfill_state(status="skipped_locked", summary=queued)
	return queued


def run_scheduled_backfill_now() -> frappe._dict:
	"""Manual trigger helper for admins/UI to run scheduled reconciliation behavior now."""
	settings = get_settings()
	lookback_hours = max(1, min(int((settings or {}).get("scheduled_backfill_lookback_hours") or 24), 720))
	limit = max(1, min(int((settings or {}).get("scheduled_backfill_limit") or 200), 1000))
	direction = cstr((settings or {}).get("scheduled_backfill_direction") or "Both")
	from_dt = add_to_date(now_datetime(), hours=-lookback_hours)
	to_dt = now_datetime()
	queued = enqueue_scheduled_backfill(
		whatsapp_account=None,
		from_datetime=from_dt,
		to_datetime=to_dt,
		direction=_direction_for_backfill_query(direction),
		limit=limit,
		preserve_raven_timestamps=1,
		scheduled=1,
	)
	if queued.get("status") == "queued":
		_safe_update_scheduled_backfill_state(
			status="queued_manual",
			job_id=queued.get("job_id"),
			summary={
				"lookback_hours": lookback_hours,
				"limit": limit,
				"direction": direction,
				"from_datetime": cstr(from_dt),
				"to_datetime": cstr(to_dt),
			},
		)
	return queued


def acquire_backfill_lock(
	lock_key: str = BACKFILL_LOCK_KEY,
	timeout_seconds: int = BACKFILL_LOCK_TIMEOUT_SECONDS,
) -> frappe._dict:
	"""Acquire lock for backfill runs; stale locks are replaced."""
	cache = frappe.cache()
	now_ts = now_datetime()
	current = cache.get_value(lock_key, shared=True)
	current_data = _as_dict(current)
	created_at = get_datetime(current_data.get("created_at")) if current_data.get("created_at") else None
	if created_at:
		age_seconds = (now_ts - created_at).total_seconds()
		if age_seconds < timeout_seconds and cstr(current_data.get("status") or "").lower() == "running":
			return frappe._dict({"acquired": False, "lock_key": lock_key, "lock_data": current_data})

	lock_data = {
		"status": "running",
		"created_at": now_ts.strftime("%Y-%m-%d %H:%M:%S"),
		"user": frappe.session.user,
	}
	cache.set_value(lock_key, lock_data, shared=True, expires_in_sec=timeout_seconds + 300)
	return frappe._dict({"acquired": True, "lock_key": lock_key, "lock_data": lock_data})


def release_backfill_lock(lock_key: str = BACKFILL_LOCK_KEY) -> None:
	"""Release backfill lock."""
	frappe.cache().delete_value(lock_key, shared=True)


def _run_backfill_job(
	whatsapp_account: str | None = None,
	phone_number: str | None = None,
	from_datetime=None,
	to_datetime=None,
	direction: str | None = None,
	limit: int | None = 200,
	dry_run: int = 0,
	preserve_raven_timestamps: int = 1,
	scheduled: int = 1,
	lock_key: str | None = BACKFILL_LOCK_KEY,
) -> frappe._dict:
	"""Background job wrapper for enqueued backfill runs."""
	try:
		result = backfill_whatsapp_messages(
			whatsapp_account=whatsapp_account,
			phone_number=phone_number,
			from_datetime=from_datetime,
			to_datetime=to_datetime,
			direction=direction,
			limit=limit,
			dry_run=dry_run,
			preserve_raven_timestamps=preserve_raven_timestamps,
			scheduled=scheduled,
			lock_key=lock_key,
		)
	except Exception:
		if int(scheduled or 0):
			_safe_update_scheduled_backfill_state(status="failed", summary={"traceback": frappe.get_traceback()})
		if lock_key:
			release_backfill_lock(lock_key)
		raise

	if int(scheduled or 0):
		_safe_update_scheduled_backfill_state(status="completed", summary=result)
	return result


def _safe_update_scheduled_backfill_state(*, status: str, summary: Any = None, job_id: str | None = None) -> None:
	"""Best-effort wrapper for scheduled backfill status updates."""
	try:
		_update_scheduled_backfill_state(status=status, summary=summary, job_id=job_id)
	except Exception:
		frappe.log_error(
			title="WhatsApp Raven Bridge: failed to update scheduled backfill state",
			message=frappe.get_traceback(),
		)


def _update_scheduled_backfill_state(*, status: str, summary: Any = None, job_id: str | None = None) -> None:
	summary_text = (
		json.dumps(summary, default=str, indent=2) if isinstance(summary, (dict, list, frappe._dict)) else cstr(summary)
	)
	frappe.db.set_single_value(
		"WhatsApp Raven Bridge Settings",
		"last_scheduled_backfill_at",
		now_datetime(),
	)
	frappe.db.set_single_value(
		"WhatsApp Raven Bridge Settings",
		"last_scheduled_backfill_status",
		cstr(status),
	)
	frappe.db.set_single_value(
		"WhatsApp Raven Bridge Settings",
		"last_scheduled_backfill_summary",
		summary_text,
	)
	if job_id:
		frappe.db.set_single_value(
			"WhatsApp Raven Bridge Settings",
			"last_backfill_job_id",
			cstr(job_id),
		)


def _direction_for_backfill_query(direction: str | None) -> str | None:
	value = cstr(direction or "").strip()
	if value in {"", "Both"}:
		return None
	return value


def _normalize_backfill_limit(limit: int | None) -> int | None:
	if limit is None:
		return None
	text = cstr(limit).strip()
	if not text:
		return None
	value = int(text)
	if value <= 0:
		return None
	return min(value, 5000)
