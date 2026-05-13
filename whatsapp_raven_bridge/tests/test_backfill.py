from __future__ import annotations

import json
from datetime import datetime, timedelta

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import cstr, get_datetime, now_datetime
from frappe.utils.password import set_encrypted_password

import whatsapp_raven_bridge.api.backfill as backfill_api
from whatsapp_raven_bridge.api.backfill import enqueue_backfill, preview_backfill, run_backfill
from whatsapp_raven_bridge.bridge.backfill import (
	BACKFILL_LOCK_KEY,
	backfill_whatsapp_messages,
	release_backfill_lock,
	run_scheduled_backfill_if_due,
	run_scheduled_backfill_now,
)


class TestHistoricalBackfill(IntegrationTestCase):
	PREFIX = "WARB4H"
	WORKSPACE = "WARB4H Workspace"
	BOT_NAME = "WARB4H Bot"
	ACCOUNT_NAME = "WARB4H WhatsApp Account"
	NON_MANAGER_USER = "warb4h_non_manager@example.com"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._settings_snapshot = cls._snapshot_settings()
		cls._ensure_raven_user("Administrator")
		cls._ensure_workspace()
		cls._ensure_bot()
		cls._ensure_whatsapp_account()
		cls._ensure_non_system_manager_user(cls.NON_MANAGER_USER, "WARB4H Non Manager")
		cls._configure_settings()
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		try:
			cls._restore_settings(cls._settings_snapshot)
			frappe.db.commit()
		finally:
			super().tearDownClass()

	def setUp(self):
		super().setUp()
		from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message.whatsapp_message import WhatsAppMessage

		self._original_notify = WhatsAppMessage.notify
		self.notify_calls = []

		def fake_notify(doc, data):
			self.notify_calls.append({"name": doc.name, "data": data})
			doc.message_id = doc.message_id or f"wamid.warb4h.fake.{frappe.generate_hash(length=8)}"
			doc.status = "Success"
			return {"messages": [{"id": doc.message_id}]}

		WhatsAppMessage.notify = fake_notify
		self._cleanup()
		self._configure_settings()
		self._create_route("thread")
		frappe.db.commit()

	def tearDown(self):
		try:
			from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message.whatsapp_message import WhatsAppMessage

			WhatsAppMessage.notify = self._original_notify
			self._cleanup()
			frappe.flags.whatsapp_raven_bridge_syncing = False
			frappe.db.commit()
		finally:
			super().tearDown()

	def test_a_dry_run_counts_without_creating_records(self):
		msg = self._insert_whatsapp_incoming("a01", "dry run one")
		before_links = frappe.db.count("WhatsApp Raven Message Link")
		before_raven = frappe.db.count("Raven Message")

		result = backfill_whatsapp_messages(whatsapp_account=self.ACCOUNT_NAME, dry_run=1, limit=100)

		self.assertEqual(result.dry_run, 1)
		self.assertGreaterEqual(int(result.scanned), 1)
		self.assertGreaterEqual(int(result.eligible), 1)
		self.assertEqual(frappe.db.count("WhatsApp Raven Message Link"), before_links)
		self.assertEqual(frappe.db.count("Raven Message"), before_raven)
		self.assertFalse(frappe.db.exists("WhatsApp Raven Message Link", {"whatsapp_message": msg.name}))

	def test_b_incoming_backfill_preserves_source_creation_and_link_fields(self):
		source_dt = datetime(2026, 1, 10, 9, 15, 0)
		msg = self._insert_whatsapp_incoming("b01", "incoming timestamp source")
		self._set_whatsapp_message_timestamps(msg.name, source_dt)

		result = backfill_whatsapp_messages(
			whatsapp_account=self.ACCOUNT_NAME,
			phone_number="+447744100001",
			dry_run=0,
			limit=20,
		)
		self.assertEqual(int(result.imported), 1)

		link_name = frappe.db.get_value("WhatsApp Raven Message Link", {"whatsapp_message": msg.name}, "name")
		self.assertTrue(link_name)
		link = frappe.get_doc("WhatsApp Raven Message Link", link_name)
		self.assertEqual(int(link.is_backfilled), 1)
		self.assertEqual(get_datetime(link.source_creation), source_dt)
		self.assertEqual(get_datetime(link.source_modified), source_dt)
		self.assertEqual(get_datetime(link.original_message_datetime), source_dt)
		self.assertTrue(link.imported_at)

		raven_message = frappe.get_doc("Raven Message", link.raven_message)
		self.assertEqual(get_datetime(raven_message.creation), source_dt)
		self.assertEqual(get_datetime(raven_message.modified), source_dt)

	def test_c_outgoing_history_import_does_not_notify_or_create_second_outgoing_message(self):
		source_dt = datetime(2026, 1, 11, 14, 0, 0)
		msg = self._insert_whatsapp_outgoing("c01", "historic outgoing from whatsapp")
		self._set_whatsapp_message_timestamps(msg.name, source_dt)
		notify_calls_before_backfill = len(self.notify_calls)

		before_outgoing_count = frappe.db.count("WhatsApp Message", {"type": "Outgoing"})
		result = backfill_whatsapp_messages(whatsapp_account=self.ACCOUNT_NAME, dry_run=0, limit=20)
		self.assertEqual(int(result.imported), 1)
		self.assertEqual(len(self.notify_calls), notify_calls_before_backfill)
		self.assertEqual(frappe.db.count("WhatsApp Message", {"type": "Outgoing"}), before_outgoing_count)

		link_name = frappe.db.get_value("WhatsApp Raven Message Link", {"whatsapp_message": msg.name}, "name")
		self.assertTrue(link_name)
		link = frappe.get_doc("WhatsApp Raven Message Link", link_name)
		self.assertEqual(link.direction, "Outgoing")
		raven_message = frappe.get_doc("Raven Message", link.raven_message)
		self.assertEqual(get_datetime(raven_message.creation), source_dt)
		self.assertEqual(int(raven_message.is_bot_message), 1)
		self.assertFalse(
			frappe.db.exists(
				"WhatsApp Message",
				{"reference_doctype": "Raven Message", "reference_name": raven_message.name},
			)
		)

	def test_d_idempotency_skips_existing_links_and_message_ids(self):
		msg = self._insert_whatsapp_incoming("d01", "idempotency source")
		backfill_whatsapp_messages(whatsapp_account=self.ACCOUNT_NAME, dry_run=0, limit=20)

		before_links = frappe.db.count("WhatsApp Raven Message Link")
		before_raven = frappe.db.count("Raven Message")
		second = self._insert_whatsapp_incoming(
			"d01b",
			"idempotency duplicate message id",
			message_id=msg.message_id,
			phone="+447744100001",
		)

		result = backfill_whatsapp_messages(whatsapp_account=self.ACCOUNT_NAME, dry_run=0, limit=50)
		self.assertGreaterEqual(int(result.skipped_existing), 1)
		self.assertEqual(frappe.db.count("WhatsApp Raven Message Link"), before_links)
		self.assertEqual(frappe.db.count("Raven Message"), before_raven)
		self.assertFalse(frappe.db.exists("WhatsApp Raven Message Link", {"whatsapp_message": second.name}))

	def test_e_thread_route_import_order_and_timestamps(self):
		msg1 = self._insert_whatsapp_incoming("e01", "first historical")
		msg2 = self._insert_whatsapp_incoming("e02", "second historical")
		dt1 = datetime(2026, 1, 12, 10, 0, 0)
		dt2 = datetime(2026, 1, 12, 10, 5, 0)
		self._set_whatsapp_message_timestamps(msg1.name, dt1)
		self._set_whatsapp_message_timestamps(msg2.name, dt2)

		result = backfill_whatsapp_messages(
			whatsapp_account=self.ACCOUNT_NAME,
			phone_number="+447744100001",
			dry_run=0,
			limit=20,
		)
		self.assertEqual(int(result.imported), 2)

		conversation_name = frappe.db.get_value(
			"WhatsApp Raven Conversation",
			{"whatsapp_account": self.ACCOUNT_NAME, "phone_number": "447744100001"},
			"name",
		)
		conversation = frappe.get_doc("WhatsApp Raven Conversation", conversation_name)
		self.assertEqual(conversation.conversation_strategy, "Thread Per Contact")
		self.assertTrue(conversation.parent_raven_message)
		self.assertTrue(conversation.raven_channel)

		links = frappe.get_all(
			"WhatsApp Raven Message Link",
			filters={"conversation": conversation.name, "is_backfilled": 1},
			fields=["whatsapp_message", "raven_message", "original_message_datetime"],
			order_by="original_message_datetime asc, whatsapp_message asc",
		)
		self.assertEqual(len(links), 2)
		self.assertEqual(links[0].whatsapp_message, msg1.name)
		self.assertEqual(links[1].whatsapp_message, msg2.name)

		rm1 = frappe.get_doc("Raven Message", links[0].raven_message)
		rm2 = frappe.get_doc("Raven Message", links[1].raven_message)
		self.assertEqual(get_datetime(rm1.creation), dt1)
		self.assertEqual(get_datetime(rm2.creation), dt2)
		self.assertEqual(rm1.channel_id, conversation.raven_channel)
		self.assertEqual(rm2.channel_id, conversation.raven_channel)

	def test_f_channel_last_message_timestamp_refreshed(self):
		msg1 = self._insert_whatsapp_incoming("f01", "older")
		msg2 = self._insert_whatsapp_incoming("f02", "newer")
		base = datetime(2026, 1, 13, 8, 0, 0)
		self._set_whatsapp_message_timestamps(msg1.name, base)
		self._set_whatsapp_message_timestamps(msg2.name, base + timedelta(minutes=15))

		backfill_whatsapp_messages(whatsapp_account=self.ACCOUNT_NAME, dry_run=0, limit=50)
		conversation_name = frappe.db.get_value(
			"WhatsApp Raven Conversation",
			{"whatsapp_account": self.ACCOUNT_NAME, "phone_number": "447744100001"},
			"name",
		)
		conversation = frappe.get_doc("WhatsApp Raven Conversation", conversation_name)
		channel = frappe.get_doc("Raven Channel", conversation.raven_channel)
		self.assertEqual(get_datetime(channel.last_message_timestamp), base + timedelta(minutes=15))

	def test_g_meta_timestamp_from_notification_log_has_priority(self):
		msg = self._insert_whatsapp_incoming("g01", "meta timestamp priority")
		fallback_creation = datetime(2026, 1, 14, 12, 0, 0)
		self._set_whatsapp_message_timestamps(msg.name, fallback_creation)
		meta_dt = datetime(2026, 1, 14, 10, 30, 0)
		self._insert_webhook_notification_log(msg.message_id, meta_dt)

		backfill_whatsapp_messages(whatsapp_account=self.ACCOUNT_NAME, dry_run=0, limit=20)
		link_name = frappe.db.get_value("WhatsApp Raven Message Link", {"whatsapp_message": msg.name}, "name")
		link = frappe.get_doc("WhatsApp Raven Message Link", link_name)
		raven_message = frappe.get_doc("Raven Message", link.raven_message)
		self.assertEqual(get_datetime(link.original_message_datetime), meta_dt)
		self.assertEqual(get_datetime(raven_message.creation), meta_dt)

	def test_h_backfill_restores_existing_syncing_flag(self):
		msg = self._insert_whatsapp_incoming("h01", "syncing flag restore")
		self._set_whatsapp_message_timestamps(msg.name, datetime(2026, 1, 15, 9, 0, 0))
		frappe.flags.whatsapp_raven_bridge_syncing = True
		try:
			result = backfill_whatsapp_messages(whatsapp_account=self.ACCOUNT_NAME, dry_run=0, limit=20)
			self.assertEqual(int(result.imported), 1)
			self.assertTrue(bool(getattr(frappe.flags, "whatsapp_raven_bridge_syncing", False)))
		finally:
			frappe.flags.whatsapp_raven_bridge_syncing = False

	def test_i_preview_backfill_api_creates_no_records(self):
		msg = self._insert_whatsapp_incoming("i01", "api preview")
		before_links = frappe.db.count("WhatsApp Raven Message Link")
		before_raven = frappe.db.count("Raven Message")
		result = preview_backfill(whatsapp_account=self.ACCOUNT_NAME, limit=20)
		self.assertGreaterEqual(int(result.get("scanned", 0)), 1)
		self.assertEqual(frappe.db.count("WhatsApp Raven Message Link"), before_links)
		self.assertEqual(frappe.db.count("Raven Message"), before_raven)
		self.assertFalse(frappe.db.exists("WhatsApp Raven Message Link", {"whatsapp_message": msg.name}))

	def test_j_run_backfill_permission_denied_for_non_system_manager(self):
		self._insert_whatsapp_incoming("j01", "permission denied")
		current_user = frappe.session.user
		try:
			frappe.set_user(self.NON_MANAGER_USER)
			with self.assertRaises(frappe.PermissionError):
				run_backfill(whatsapp_account=self.ACCOUNT_NAME, limit=20)
		finally:
			frappe.set_user(current_user)

	def test_j2_enqueue_backfill_permission_denied_for_non_system_manager(self):
		current_user = frappe.session.user
		try:
			frappe.set_user(self.NON_MANAGER_USER)
			with self.assertRaises(frappe.PermissionError):
				enqueue_backfill(whatsapp_account=self.ACCOUNT_NAME, limit=20)
		finally:
			frappe.set_user(current_user)

	def test_k_enqueue_backfill_returns_job_id(self):
		self._insert_whatsapp_incoming("k01", "enqueue")
		original_enqueue = frappe.enqueue

		class _Job:
			id = "job-warb4h-enqueue"

		def fake_enqueue(*args, **kwargs):
			return _Job()

		try:
			release_backfill_lock(BACKFILL_LOCK_KEY)
			frappe.enqueue = fake_enqueue
			result = enqueue_backfill(whatsapp_account=self.ACCOUNT_NAME, limit=50)
		finally:
			frappe.enqueue = original_enqueue
			release_backfill_lock(BACKFILL_LOCK_KEY)

		self.assertEqual(result.get("status"), "queued")
		self.assertEqual(result.get("job_id"), "job-warb4h-enqueue")

	def test_l_scheduler_skips_when_disabled(self):
		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		settings.enable_scheduled_backfill = 0
		settings.save(ignore_permissions=True)
		result = run_scheduled_backfill_if_due()
		self.assertEqual(result.get("status"), "skipped_disabled")

	def test_m_scheduler_queues_when_due_and_updates_status(self):
		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		settings.enable_scheduled_backfill = 1
		settings.scheduled_backfill_interval = "Every 5 Minutes"
		settings.scheduled_backfill_lookback_hours = 2
		settings.scheduled_backfill_limit = 75
		settings.scheduled_backfill_direction = "Incoming"
		settings.last_scheduled_backfill_at = datetime(2026, 1, 1, 0, 0, 0)
		settings.save(ignore_permissions=True)

		original_enqueue = frappe.enqueue

		class _Job:
			id = "job-warb4h-scheduled"

		def fake_enqueue(*args, **kwargs):
			return _Job()

		try:
			release_backfill_lock(BACKFILL_LOCK_KEY)
			frappe.enqueue = fake_enqueue
			result = run_scheduled_backfill_if_due()
		finally:
			frappe.enqueue = original_enqueue
			release_backfill_lock(BACKFILL_LOCK_KEY)

		self.assertEqual(result.get("status"), "queued")
		self.assertEqual(result.get("job_id"), "job-warb4h-scheduled")
		settings.reload()
		self.assertEqual(cstr(settings.last_scheduled_backfill_status), "queued")
		self.assertEqual(cstr(settings.last_backfill_job_id), "job-warb4h-scheduled")
		summary = json.loads(cstr(settings.last_scheduled_backfill_summary or "{}"))
		self.assertEqual(int(summary.get("lookback_hours")), 2)
		self.assertEqual(int(summary.get("limit")), 75)
		self.assertEqual(cstr(summary.get("direction")), "Incoming")

	def test_n_scheduler_skips_when_not_due(self):
		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		settings.enable_scheduled_backfill = 1
		settings.scheduled_backfill_interval = "Hourly"
		settings.last_scheduled_backfill_at = now_datetime()
		settings.save(ignore_permissions=True)
		result = run_scheduled_backfill_if_due()
		self.assertEqual(result.get("status"), "skipped_not_due")

	def test_o_scheduler_skips_on_overlap_lock(self):
		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		settings.enable_scheduled_backfill = 1
		settings.scheduled_backfill_interval = "Every 5 Minutes"
		settings.last_scheduled_backfill_at = datetime(2026, 1, 1, 0, 0, 0)
		settings.save(ignore_permissions=True)

		frappe.cache().set_value(
			BACKFILL_LOCK_KEY,
			{"status": "running", "created_at": now_datetime().strftime("%Y-%m-%d %H:%M:%S"), "user": "tester"},
			shared=True,
			expires_in_sec=1800,
		)
		try:
			result = run_scheduled_backfill_if_due()
		finally:
			release_backfill_lock(BACKFILL_LOCK_KEY)
		self.assertEqual(result.get("status"), "skipped_locked")

	def test_p_run_scheduled_backfill_now_manual(self):
		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		settings.enable_scheduled_backfill = 1
		settings.scheduled_backfill_direction = "Outgoing"
		settings.save(ignore_permissions=True)

		original_enqueue = frappe.enqueue

		class _Job:
			id = "job-warb4h-manual-scheduled"

		def fake_enqueue(*args, **kwargs):
			return _Job()

		try:
			release_backfill_lock(BACKFILL_LOCK_KEY)
			frappe.enqueue = fake_enqueue
			result = run_scheduled_backfill_now()
		finally:
			frappe.enqueue = original_enqueue
			release_backfill_lock(BACKFILL_LOCK_KEY)

		self.assertEqual(result.get("status"), "queued")
		self.assertEqual(result.get("job_id"), "job-warb4h-manual-scheduled")

	def test_q_run_backfill_releases_lock_when_backfill_raises(self):
		original_impl = backfill_api.backfill_whatsapp_messages

		def fail_backfill(*args, **kwargs):
			raise RuntimeError("forced backfill failure")

		try:
			release_backfill_lock(BACKFILL_LOCK_KEY)
			backfill_api.backfill_whatsapp_messages = fail_backfill
			with self.assertRaises(RuntimeError):
				run_backfill(whatsapp_account=self.ACCOUNT_NAME, limit=10)
		finally:
			backfill_api.backfill_whatsapp_messages = original_impl
		self.assertIsNone(frappe.cache().get_value(BACKFILL_LOCK_KEY, shared=True))

	def test_r_enqueue_backfill_releases_lock_when_enqueue_fails(self):
		original_enqueue = frappe.enqueue

		def fail_enqueue(*args, **kwargs):
			raise RuntimeError("forced enqueue failure")

		try:
			release_backfill_lock(BACKFILL_LOCK_KEY)
			frappe.enqueue = fail_enqueue
			with self.assertRaises(RuntimeError):
				enqueue_backfill(whatsapp_account=self.ACCOUNT_NAME, limit=10)
		finally:
			frappe.enqueue = original_enqueue
		self.assertIsNone(frappe.cache().get_value(BACKFILL_LOCK_KEY, shared=True))

	@classmethod
	def _snapshot_settings(cls):
		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		return {
			"enabled": settings.enabled,
			"bridge_system_user": settings.bridge_system_user,
			"default_raven_workspace": settings.default_raven_workspace,
			"default_channel_type": settings.default_channel_type,
			"bridge_raven_bot": settings.bridge_raven_bot,
			"bridge_raven_user": settings.bridge_raven_user,
			"default_whatsapp_account": settings.default_whatsapp_account,
			"conversation_strategy": settings.conversation_strategy,
			"enable_outbound_replies": settings.enable_outbound_replies,
			"enable_scheduled_backfill": settings.enable_scheduled_backfill,
			"scheduled_backfill_interval": settings.scheduled_backfill_interval,
			"scheduled_backfill_lookback_hours": settings.scheduled_backfill_lookback_hours,
			"scheduled_backfill_limit": settings.scheduled_backfill_limit,
			"scheduled_backfill_direction": settings.scheduled_backfill_direction,
			"last_scheduled_backfill_at": settings.last_scheduled_backfill_at,
			"last_scheduled_backfill_status": settings.last_scheduled_backfill_status,
			"last_scheduled_backfill_summary": settings.last_scheduled_backfill_summary,
			"last_backfill_job_id": settings.last_backfill_job_id,
			"default_channel_members": [
				{"raven_user": row.raven_user, "is_admin": row.is_admin}
				for row in (settings.default_channel_members or [])
			],
		}

	@classmethod
	def _restore_settings(cls, snapshot):
		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		for key, value in snapshot.items():
			if key == "default_channel_members":
				continue
			settings.set(key, value)
		settings.set("default_channel_members", [])
		for row in snapshot.get("default_channel_members", []):
			settings.append("default_channel_members", {"raven_user": row.get("raven_user"), "is_admin": row.get("is_admin", 0)})
		settings.save(ignore_permissions=True)

	@classmethod
	def _configure_settings(cls):
		bot = frappe.get_doc("Raven Bot", cls.BOT_NAME)
		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		settings.enabled = 1
		settings.default_raven_workspace = cls.WORKSPACE
		settings.default_channel_type = "Private"
		settings.bridge_raven_bot = cls.BOT_NAME
		settings.bridge_raven_user = bot.raven_user or cls.BOT_NAME
		settings.default_whatsapp_account = cls.ACCOUNT_NAME
		settings.conversation_strategy = "Thread Per Contact"
		settings.enable_outbound_replies = 1
		settings.enable_scheduled_backfill = 0
		settings.scheduled_backfill_interval = "Hourly"
		settings.scheduled_backfill_lookback_hours = 24
		settings.scheduled_backfill_limit = 200
		settings.scheduled_backfill_direction = "Both"
		settings.last_scheduled_backfill_at = None
		settings.last_scheduled_backfill_status = None
		settings.last_scheduled_backfill_summary = None
		settings.last_backfill_job_id = None
		settings.set("default_channel_members", [])
		settings.append("default_channel_members", {"raven_user": "Administrator", "is_admin": 1})
		settings.save(ignore_permissions=True)

	@classmethod
	def _ensure_workspace(cls):
		if not frappe.db.exists("Raven Workspace", cls.WORKSPACE):
			frappe.get_doc(
				{
					"doctype": "Raven Workspace",
					"workspace_name": cls.WORKSPACE,
					"type": "Public",
				}
			).insert(ignore_permissions=True)

	@classmethod
	def _ensure_bot(cls):
		if not frappe.db.exists("Raven Bot", cls.BOT_NAME):
			frappe.get_doc(
				{
					"doctype": "Raven Bot",
					"bot_name": cls.BOT_NAME,
					"is_ai_bot": 0,
				}
			).insert(ignore_permissions=True)
		bot = frappe.get_doc("Raven Bot", cls.BOT_NAME)
		if not bot.raven_user:
			bot.save(ignore_permissions=True)

	@classmethod
	def _ensure_raven_user(cls, user):
		user_doc = frappe.get_doc("User", user)
		if "Raven User" not in [r.role for r in user_doc.roles]:
			user_doc.append("roles", {"role": "Raven User"})
			user_doc.save(ignore_permissions=True)
		if not frappe.db.exists("Raven User", {"user": user}):
			frappe.get_doc(
				{
					"doctype": "Raven User",
					"type": "User",
					"user": user,
					"full_name": user,
					"first_name": user,
					"enabled": 1,
				}
			).insert(ignore_permissions=True)

	@classmethod
	def _ensure_non_system_manager_user(cls, user_id, full_name):
		if frappe.db.exists("User", user_id):
			user_doc = frappe.get_doc("User", user_id)
			user_doc.enabled = 1
		else:
			user_doc = frappe.get_doc(
				{
					"doctype": "User",
					"email": user_id,
					"first_name": full_name,
					"send_welcome_email": 0,
					"enabled": 1,
					"user_type": "System User",
				}
			)
		for idx in range(len(user_doc.roles or []) - 1, -1, -1):
			if user_doc.roles[idx].role == "System Manager":
				user_doc.roles.pop(idx)
		user_doc.save(ignore_permissions=True)

	@classmethod
	def _ensure_whatsapp_account(cls):
		if not frappe.db.exists("WhatsApp Account", cls.ACCOUNT_NAME):
			frappe.get_doc(
				{
					"doctype": "WhatsApp Account",
					"account_name": cls.ACCOUNT_NAME,
					"status": "Active",
					"url": "https://graph.facebook.com",
					"version": "v17.0",
					"phone_id": "warb4h_phone_id",
					"business_id": "warb4h_business_id",
					"app_id": "warb4h_app_id",
					"webhook_verify_token": "warb4h_verify_token",
				}
			).insert(ignore_permissions=True)
		set_encrypted_password("WhatsApp Account", cls.ACCOUNT_NAME, "warb4h-token", "token")

	def _create_route(self, suffix):
		existing = frappe.db.get_value(
			"WhatsApp Raven Account Route", {"whatsapp_account": self.ACCOUNT_NAME}, "name"
		)
		if existing:
			return frappe.get_doc("WhatsApp Raven Account Route", existing)
		return frappe.get_doc(
			{
				"doctype": "WhatsApp Raven Account Route",
				"enabled": 1,
				"whatsapp_account": self.ACCOUNT_NAME,
				"raven_workspace": self.WORKSPACE,
				"inbox_channel_name": f"{self.PREFIX.lower()}-inbox-{suffix}",
				"channel_type": "Private",
				"conversation_strategy": "Thread Per Contact",
				"members": [{"raven_user": "Administrator", "is_admin": 1, "can_reply": 1}],
			}
		).insert(ignore_permissions=True)

	def _insert_whatsapp_incoming(self, suffix, body, message_id=None, phone="+447744100001"):
		message_id = message_id or f"wamid.warb4h.in.{suffix}"
		previous_flag = getattr(frappe.flags, "whatsapp_raven_bridge_syncing", False)
		try:
			frappe.flags.whatsapp_raven_bridge_syncing = True
			return frappe.get_doc(
				{
					"doctype": "WhatsApp Message",
					"type": "Incoming",
					"content_type": "text",
					"message_type": "Manual",
					"from": phone,
					"profile_name": f"{self.PREFIX} Customer",
					"message": body,
					"message_id": message_id,
					"whatsapp_account": self.ACCOUNT_NAME,
				}
			).insert(ignore_permissions=True)
		finally:
			frappe.flags.whatsapp_raven_bridge_syncing = previous_flag

	def _insert_whatsapp_outgoing(self, suffix, body, message_id=None, phone="+447744100001"):
		message_id = message_id or f"wamid.warb4h.out.{suffix}"
		return frappe.get_doc(
			{
				"doctype": "WhatsApp Message",
				"type": "Outgoing",
				"content_type": "text",
				"message_type": "Manual",
				"to": phone,
				"message": body,
				"message_id": message_id,
				"status": "Success",
				"whatsapp_account": self.ACCOUNT_NAME,
			}
		).insert(ignore_permissions=True)

	def _set_whatsapp_message_timestamps(self, message_name, timestamp):
		frappe.db.sql(
			"""
			update `tabWhatsApp Message`
			set creation=%s, modified=%s
			where name=%s
			""",
			(timestamp, timestamp, message_name),
		)
		frappe.clear_document_cache("WhatsApp Message", message_name)

	def _insert_webhook_notification_log(self, message_id, timestamp_dt):
		epoch = int(timestamp_dt.timestamp())
		payload = {
			"entry": [
				{
					"changes": [
						{
							"value": {
								"messages": [
									{
										"id": message_id,
										"type": "text",
										"timestamp": str(epoch),
										"text": {"body": "historical"},
									}
								]
							}
						}
					]
				}
			]
		}
		return frappe.get_doc(
			{
				"doctype": "WhatsApp Notification Log",
				"template": "Webhook",
				"meta_data": json.dumps(payload),
			}
		).insert(ignore_permissions=True)

	def _cleanup(self):
		for name in frappe.get_all(
			"WhatsApp Raven Message Link",
			filters=[["whatsapp_message_id", "like", "wamid.warb4h.%"]],
			pluck="name",
		):
			frappe.delete_doc("WhatsApp Raven Message Link", name, force=True)

		for name in frappe.get_all(
			"WhatsApp Message",
			filters=[["message_id", "like", "wamid.warb4h.%"]],
			pluck="name",
		):
			frappe.delete_doc("WhatsApp Message", name, force=True)

		for name in frappe.get_all(
			"WhatsApp Notification Log",
			filters=[["meta_data", "like", "%wamid.warb4h.%"]],
			pluck="name",
		):
			frappe.delete_doc("WhatsApp Notification Log", name, force=True)

		for name in frappe.get_all(
			"Raven Message",
			filters=[["text", "like", "%warb4h%"]],
			pluck="name",
		):
			frappe.delete_doc("Raven Message", name, force=True)

		for name in frappe.get_all(
			"WhatsApp Raven Conversation",
			filters=[["phone_number", "like", "4477441000%"]],
			pluck="name",
		):
			frappe.delete_doc("WhatsApp Raven Conversation", name, force=True)

		for name in frappe.get_all(
			"Raven Channel",
			filters=[["workspace", "=", self.WORKSPACE], ["channel_name", "like", "warb4h-%"]],
			pluck="name",
		):
			if frappe.db.exists("Raven Channel", name):
				frappe.delete_doc("Raven Channel", name, force=True)

		for name in frappe.get_all(
			"Raven Channel",
			filters=[["workspace", "=", self.WORKSPACE], ["is_thread", "=", 1]],
			pluck="name",
		):
			if frappe.db.exists("Raven Channel", name):
				frappe.delete_doc("Raven Channel", name, force=True)

		for name in frappe.get_all(
			"WhatsApp Raven Account Route",
			filters={"whatsapp_account": self.ACCOUNT_NAME},
			pluck="name",
		):
			if frappe.db.exists("WhatsApp Raven Account Route", name):
				frappe.delete_doc("WhatsApp Raven Account Route", name, force=True)
