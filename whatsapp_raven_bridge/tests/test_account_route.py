from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.password import set_encrypted_password

from whatsapp_raven_bridge.bridge.account_route import (
	ensure_route_memberships,
	get_or_create_inbox_channel,
	get_route_for_whatsapp_account,
)
from whatsapp_raven_bridge.bridge.conversation import get_or_create_conversation, normalize_phone_number
from whatsapp_raven_bridge.bridge.outbound import process_outgoing_raven_message
from whatsapp_raven_bridge.bridge.raven_destination import ensure_raven_destination


class TestAccountRouteDesign(IntegrationTestCase):
	PREFIX = "WARC4C"
	WORKSPACE = "WARC4C Workspace"
	BOT_NAME = "WARC4C Bot"
	ACCOUNT_NAME = "WARC4C WhatsApp Account"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._settings_snapshot = cls._snapshot_settings()
		cls._ensure_raven_user("Administrator")
		cls._ensure_workspace()
		cls._ensure_bot()
		cls._ensure_whatsapp_account()
		cls._configure_base_settings()
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

		def fake_notify(doc, data):
			doc.message_id = f"wamid.warc4c.fake.{frappe.generate_hash(length=8)}"
			doc.status = "Success"
			return {"messages": [{"id": doc.message_id}]}

		WhatsAppMessage.notify = fake_notify
		self._cleanup()
		self._configure_base_settings()
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

	def test_route_doc_creation_and_lookup(self):
		route = self._create_route("creation", conversation_strategy="Channel Per Contact")
		found = get_route_for_whatsapp_account(self.ACCOUNT_NAME)
		self.assertEqual(found.name, route.name)

	def test_duplicate_enabled_route_is_blocked(self):
		self._create_route("dup-a", conversation_strategy="Channel Per Contact")
		with self.assertRaises(frappe.ValidationError):
			self._create_route("dup-b", conversation_strategy="Channel Per Contact")

	def test_route_inbox_channel_create_and_reuse(self):
		route = self._create_route("inbox", conversation_strategy="Channel Per Contact")
		first = get_or_create_inbox_channel(route)
		second = get_or_create_inbox_channel(route)

		self.assertEqual(first.name, second.name)
		self.assertEqual(first.workspace, route.raven_workspace)
		self.assertFalse(first.is_direct_message)
		route.reload()
		self.assertEqual(route.inbox_channel, first.name)

	def test_route_memberships_added_to_workspace_and_inbox_channel(self):
		route = self._create_route("members", conversation_strategy="Channel Per Contact")
		channel = ensure_route_memberships(route)
		route.reload()

		self.assertTrue(
			frappe.db.exists(
				"Raven Workspace Member",
				{"workspace": route.raven_workspace, "user": "Administrator"},
			)
		)
		self.assertTrue(
			frappe.db.exists(
				"Raven Channel Member",
				{"channel_id": channel.name, "user_id": "Administrator"},
			)
		)

	def test_inbound_uses_account_route_when_whatsapp_account_present(self):
		route = self._create_route("inbound", conversation_strategy="Channel Per Contact")
		phone = "+447733110001"
		message_id = "wamid.warc4c.inbound.001"
		incoming = frappe.get_doc(
			{
				"doctype": "WhatsApp Message",
				"type": "Incoming",
				"content_type": "text",
				"message_type": "Manual",
				"from": phone,
				"profile_name": f"{self.PREFIX} Inbound",
				"message": "inbound route test",
				"message_id": message_id,
				"whatsapp_account": self.ACCOUNT_NAME,
			}
		).insert(ignore_permissions=True)

		phone_norm = normalize_phone_number(phone)
		conversation_name = frappe.db.get_value(
			"WhatsApp Raven Conversation",
			{"phone_number": phone_norm, "whatsapp_account": self.ACCOUNT_NAME},
			"name",
		)
		self.assertTrue(conversation_name)
		conversation = frappe.get_doc("WhatsApp Raven Conversation", conversation_name)
		self.assertEqual(conversation.account_route, route.name)
		self.assertEqual(conversation.raven_workspace, route.raven_workspace)
		self.assertTrue(conversation.raven_channel)
		self.assertTrue(
			frappe.db.exists("WhatsApp Raven Message Link", {"whatsapp_message": incoming.name})
		)

	def test_thread_route_inbound_creates_parent_and_thread_destination(self):
		route = self._create_route("thread-inbound", conversation_strategy="Thread Per Contact")
		phone = "+447733110101"
		phone_norm = normalize_phone_number(phone)

		incoming = frappe.get_doc(
			{
				"doctype": "WhatsApp Message",
				"type": "Incoming",
				"content_type": "text",
				"message_type": "Manual",
				"from": phone,
				"profile_name": f"{self.PREFIX} Thread Inbound",
				"message": "thread route inbound",
				"message_id": "wamid.warc4c.thread.inbound.001",
				"whatsapp_account": self.ACCOUNT_NAME,
			}
		).insert(ignore_permissions=True)

		conversation_name = frappe.db.get_value(
			"WhatsApp Raven Conversation",
			{"phone_number": phone_norm, "whatsapp_account": self.ACCOUNT_NAME},
			"name",
		)
		self.assertTrue(conversation_name)

		conversation = frappe.get_doc("WhatsApp Raven Conversation", conversation_name)
		self.assertEqual(conversation.account_route, route.name)
		self.assertEqual(conversation.conversation_strategy, "Thread Per Contact")
		self.assertTrue(conversation.parent_raven_message)
		self.assertTrue(conversation.raven_channel)
		self.assertEqual(conversation.raven_channel, conversation.parent_raven_message)
		route.reload()

		parent_message = frappe.get_doc("Raven Message", conversation.parent_raven_message)
		self.assertEqual(parent_message.channel_id, route.inbox_channel)
		self.assertEqual(parent_message.link_doctype, "WhatsApp Raven Conversation")
		self.assertEqual(parent_message.link_document, conversation.name)
		self.assertTrue(parent_message.is_thread)
		self.assertTrue(parent_message.is_bot_message)

		thread_channel = frappe.get_doc("Raven Channel", conversation.raven_channel)
		self.assertTrue(thread_channel.is_thread)
		self.assertEqual(thread_channel.workspace, route.raven_workspace)

		inbound_link_name = frappe.db.get_value(
			"WhatsApp Raven Message Link",
			{"whatsapp_message": incoming.name},
			"name",
		)
		self.assertTrue(inbound_link_name)
		inbound_link = frappe.get_doc("WhatsApp Raven Message Link", inbound_link_name)
		self.assertEqual(inbound_link.raven_channel, thread_channel.name)
		raven_message = frappe.get_doc("Raven Message", inbound_link.raven_message)
		self.assertEqual(raven_message.channel_id, thread_channel.name)

	def test_thread_route_repeat_inbound_reuses_same_parent_and_thread(self):
		route = self._create_route("thread-reuse", conversation_strategy="Thread Per Contact")
		phone = "+447733110102"

		self._insert_incoming_thread_message(
			phone=phone,
			message_id="wamid.warc4c.thread.reuse.001",
			body="thread route reuse 1",
		)
		conversation = frappe.get_doc(
			"WhatsApp Raven Conversation",
			frappe.db.get_value(
				"WhatsApp Raven Conversation",
				{"phone_number": normalize_phone_number(phone), "whatsapp_account": self.ACCOUNT_NAME},
				"name",
			),
		)
		first_parent = conversation.parent_raven_message
		first_thread = conversation.raven_channel

		self._insert_incoming_thread_message(
			phone=phone,
			message_id="wamid.warc4c.thread.reuse.002",
			body="thread route reuse 2",
		)
		conversation.reload()

		self.assertEqual(conversation.parent_raven_message, first_parent)
		self.assertEqual(conversation.raven_channel, first_thread)
		self.assertEqual(
			frappe.db.count(
				"Raven Message",
				{"link_doctype": "WhatsApp Raven Conversation", "link_document": conversation.name},
			),
			1,
		)
		self.assertEqual(
			frappe.db.count("Raven Channel", {"name": conversation.raven_channel, "is_thread": 1}),
			1,
		)

	def test_thread_route_outbound_allows_assigned_user(self):
		route = self._create_route("thread-allow", conversation_strategy="Thread Per Contact")
		conversation = self._make_conversation(route, "+447733110103")

		raven_message = frappe.get_doc(
			{
				"doctype": "Raven Message",
				"channel_id": conversation.raven_channel,
				"message_type": "Text",
				"text": "<p>thread assigned user outbound</p>",
				"is_bot_message": 0,
			}
		).insert(ignore_permissions=True)

		self.assertTrue(
			frappe.db.exists("WhatsApp Raven Message Link", {"raven_message": raven_message.name})
		)
		self.assertTrue(
			frappe.db.exists(
				"WhatsApp Message",
				{
					"reference_doctype": "Raven Message",
					"reference_name": raven_message.name,
				},
			)
		)

	def test_thread_route_outbound_skips_unassigned_user(self):
		route = self._create_route(
			"thread-deny",
			conversation_strategy="Thread Per Contact",
			include_admin_member=False,
			allow_unassigned_reply=0,
		)
		conversation = self._make_conversation(route, "+447733110104")

		raven_message = frappe.get_doc(
			{
				"doctype": "Raven Message",
				"channel_id": conversation.raven_channel,
				"message_type": "Text",
				"text": "<p>thread unassigned outbound</p>",
				"is_bot_message": 0,
			}
		).insert(ignore_permissions=True)

		result = process_outgoing_raven_message(raven_message)
		self.assertEqual(result, "skipped_user_not_allowed")
		self.assertFalse(
			frappe.db.exists("WhatsApp Raven Message Link", {"raven_message": raven_message.name})
		)

	def test_thread_parent_message_is_not_sent_to_whatsapp(self):
		route = self._create_route("thread-parent", conversation_strategy="Thread Per Contact")
		conversation = self._make_conversation(route, "+447733110105")
		parent_message_name = conversation.parent_raven_message
		self.assertTrue(parent_message_name)
		self.assertTrue(
			frappe.db.exists(
				"Raven Message",
				{"name": parent_message_name, "is_bot_message": 1, "is_thread": 1},
			)
		)
		self.assertFalse(
			frappe.db.exists(
				"WhatsApp Message",
				{"reference_doctype": "Raven Message", "reference_name": parent_message_name},
			)
		)
		self.assertFalse(
			frappe.db.exists("WhatsApp Raven Message Link", {"raven_message": parent_message_name})
		)

	def test_outbound_allows_assigned_route_member(self):
		route = self._create_route("allow", conversation_strategy="Channel Per Contact")
		conversation = self._make_conversation(route, "+447733110010")

		raven_message = frappe.get_doc(
			{
				"doctype": "Raven Message",
				"channel_id": conversation.raven_channel,
				"message_type": "Text",
				"text": "<p>assigned user outbound</p>",
				"is_bot_message": 0,
			}
		).insert(ignore_permissions=True)

		self.assertTrue(
			frappe.db.exists("WhatsApp Raven Message Link", {"raven_message": raven_message.name})
		)
		self.assertTrue(
			frappe.db.exists(
				"WhatsApp Message",
				{
					"reference_doctype": "Raven Message",
					"reference_name": raven_message.name,
				},
			)
		)

	def test_outbound_skips_unassigned_user(self):
		route = self._create_route(
			"deny",
			conversation_strategy="Channel Per Contact",
			include_admin_member=False,
			allow_unassigned_reply=0,
		)
		conversation = self._make_conversation(route, "+447733110020")

		raven_message = frappe.get_doc(
			{
				"doctype": "Raven Message",
				"channel_id": conversation.raven_channel,
				"message_type": "Text",
				"text": "<p>unassigned user outbound</p>",
				"is_bot_message": 0,
			}
		).insert(ignore_permissions=True)

		result = process_outgoing_raven_message(raven_message)
		self.assertEqual(result, "skipped_user_not_allowed")
		self.assertFalse(
			frappe.db.exists("WhatsApp Raven Message Link", {"raven_message": raven_message.name})
		)

	@classmethod
	def _snapshot_settings(cls):
		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		return {
			"enabled": settings.enabled,
			"default_raven_workspace": settings.default_raven_workspace,
			"default_channel_type": settings.default_channel_type,
			"bridge_raven_bot": settings.bridge_raven_bot,
			"bridge_raven_user": settings.bridge_raven_user,
			"default_whatsapp_account": settings.default_whatsapp_account,
			"conversation_strategy": settings.conversation_strategy,
			"enable_outbound_replies": settings.enable_outbound_replies,
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
			settings.append(
				"default_channel_members",
				{"raven_user": row.get("raven_user"), "is_admin": row.get("is_admin", 0)},
			)
		settings.save(ignore_permissions=True)

	@classmethod
	def _configure_base_settings(cls):
		bot = frappe.get_doc("Raven Bot", cls.BOT_NAME)
		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		settings.enabled = 1
		settings.default_raven_workspace = cls.WORKSPACE
		settings.default_channel_type = "Private"
		settings.bridge_raven_bot = cls.BOT_NAME
		settings.bridge_raven_user = bot.raven_user or cls.BOT_NAME
		settings.default_whatsapp_account = cls.ACCOUNT_NAME
		settings.conversation_strategy = "Channel Per Contact"
		settings.enable_outbound_replies = 1
		settings.set("default_channel_members", [])
		settings.append(
			"default_channel_members",
			{"raven_user": "Administrator", "is_admin": 1},
		)
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
	def _ensure_whatsapp_account(cls):
		if not frappe.db.exists("WhatsApp Account", cls.ACCOUNT_NAME):
			frappe.get_doc(
				{
					"doctype": "WhatsApp Account",
					"account_name": cls.ACCOUNT_NAME,
					"status": "Active",
					"url": "https://graph.facebook.com",
					"version": "v17.0",
					"phone_id": "warc4c_phone_id",
					"business_id": "warc4c_business_id",
					"app_id": "warc4c_app_id",
					"webhook_verify_token": "warc4c_verify_token",
				}
			).insert(ignore_permissions=True)
		set_encrypted_password("WhatsApp Account", cls.ACCOUNT_NAME, "warc4c-token", "token")

	def _create_route(
		self,
		suffix,
		*,
		conversation_strategy,
		include_admin_member=True,
		allow_unassigned_reply=0,
	):
		route = frappe.get_doc(
			{
				"doctype": "WhatsApp Raven Account Route",
				"enabled": 1,
				"whatsapp_account": self.ACCOUNT_NAME,
				"raven_workspace": self.WORKSPACE,
				"inbox_channel_name": f"{self.PREFIX.lower()}-inbox-{suffix}",
				"channel_type": "Private",
				"conversation_strategy": conversation_strategy,
				"allow_unassigned_reply": allow_unassigned_reply,
				"members": (
					[{"raven_user": "Administrator", "is_admin": 1, "can_reply": 1}]
					if include_admin_member
					else []
				),
			}
		).insert(ignore_permissions=True)
		return route

	def _make_conversation(self, route, phone):
		conversation = get_or_create_conversation(
			phone,
			whatsapp_account=self.ACCOUNT_NAME,
			profile_name=f"{self.PREFIX} {phone[-4:]}",
		)
		if conversation.account_route != route.name:
			conversation.account_route = route.name
			conversation.save(ignore_permissions=True)
		ensure_raven_destination(conversation)
		conversation.reload()
		return conversation

	def _insert_incoming_thread_message(self, *, phone, message_id, body):
		return frappe.get_doc(
			{
				"doctype": "WhatsApp Message",
				"type": "Incoming",
				"content_type": "text",
				"message_type": "Manual",
				"from": phone,
				"profile_name": f"{self.PREFIX} Thread Reuse",
				"message": body,
				"message_id": message_id,
				"whatsapp_account": self.ACCOUNT_NAME,
			}
		).insert(ignore_permissions=True)

	def _cleanup(self):
		conversation_names = set(
			frappe.get_all(
				"WhatsApp Raven Conversation",
				filters=[["phone_number", "like", "44773311%"]],
				pluck="name",
			)
		)
		parent_messages = set()
		if conversation_names:
			parent_messages = set(
				frappe.get_all(
					"Raven Message",
					filters={
						"link_doctype": "WhatsApp Raven Conversation",
						"link_document": ["in", list(conversation_names)],
					},
					pluck="name",
				)
			)

		for name in frappe.get_all(
			"WhatsApp Raven Message Link",
			filters=[["whatsapp_message_id", "like", "wamid.warc4c.%"]],
			pluck="name",
		):
			frappe.delete_doc("WhatsApp Raven Message Link", name, force=True)

		for name in frappe.get_all(
			"WhatsApp Message",
			filters=[["message_id", "like", "wamid.warc4c.%"]],
			pluck="name",
		):
			frappe.delete_doc("WhatsApp Message", name, force=True)

		for name in frappe.get_all(
			"Raven Message",
			filters=[
				["text", "like", "%warc4c%"],
			],
			pluck="name",
		):
			frappe.delete_doc("Raven Message", name, force=True)

		for name in frappe.get_all(
			"WhatsApp Raven Conversation",
			filters=[["phone_number", "like", "44773311%"]],
			pluck="name",
		):
			frappe.delete_doc("WhatsApp Raven Conversation", name, force=True)

		for name in parent_messages:
			if frappe.db.exists("Raven Message", name):
				frappe.delete_doc("Raven Message", name, force=True)

		for name in frappe.get_all(
			"Raven Channel",
			filters=[["workspace", "=", self.WORKSPACE], ["channel_name", "like", "warc4c-%"]],
			pluck="name",
		):
			if frappe.db.exists("Raven Channel", name):
				frappe.delete_doc("Raven Channel", name, force=True)

		for name in parent_messages:
			if frappe.db.exists("Raven Channel", name):
				frappe.delete_doc("Raven Channel", name, force=True)

		for name in frappe.get_all(
			"WhatsApp Raven Account Route",
			filters={"whatsapp_account": self.ACCOUNT_NAME},
			pluck="name",
		):
			if frappe.db.exists("WhatsApp Raven Account Route", name):
				frappe.delete_doc("WhatsApp Raven Account Route", name, force=True)
