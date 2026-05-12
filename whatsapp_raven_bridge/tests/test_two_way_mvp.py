from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import cstr
from frappe.utils.password import set_encrypted_password

from whatsapp_raven_bridge.bridge.conversation import get_or_create_conversation, normalize_phone_number
from whatsapp_raven_bridge.bridge.inbound import (
	handle_whatsapp_message_after_insert,
	process_incoming_whatsapp_message,
)
from whatsapp_raven_bridge.bridge.outbound import (
	handle_raven_message_after_insert,
	process_outgoing_raven_message,
)
from whatsapp_raven_bridge.bridge.raven_destination import ensure_raven_destination


class TestTwoWayMVPHardening(IntegrationTestCase):
	PREFIX = "WARB4B"
	WORKSPACE_NAME = "WARB4B Test Workspace"
	BOT_NAME = "WARB4B Test Bot"
	ACCOUNT_NAME = "WARB4B Test WA Account"
	HUMAN_USER = "Administrator"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._settings_snapshot = cls._snapshot_settings()
		cls._ensure_human_raven_user()
		cls._ensure_bridge_bot()
		cls._ensure_workspace()
		cls._ensure_whatsapp_account()
		cls._configure_bridge_settings()
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
			doc.message_id = f"wamid.warb4b.fake.{frappe.generate_hash(length=8)}"
			doc.status = "Success"
			return {"messages": [{"id": doc.message_id}]}

		WhatsAppMessage.notify = fake_notify

		self._cleanup_test_records()
		self._configure_bridge_settings()
		frappe.db.commit()

	def tearDown(self):
		try:
			from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message.whatsapp_message import WhatsAppMessage

			WhatsAppMessage.notify = self._original_notify
			self._cleanup_test_records()
			frappe.flags.whatsapp_raven_bridge_syncing = False
			frappe.db.commit()
		finally:
			super().tearDown()

	def test_01_incoming_text_creates_full_mapping(self):
		phone = self._phone("01")
		message_id = self._message_id("01")
		normalized_phone = normalize_phone_number(phone)

		incoming = self._insert_whatsapp_message(
			type_="Incoming",
			content_type="text",
			phone=phone,
			message_id=message_id,
			message=f"{self.PREFIX} incoming 01",
		)

		conversation_name = frappe.db.get_value(
			"WhatsApp Raven Conversation",
			{"phone_number": normalized_phone, "whatsapp_account": self.ACCOUNT_NAME, "enabled": 1},
			"name",
		)
		self.assertTrue(conversation_name)

		conversation = frappe.get_doc("WhatsApp Raven Conversation", conversation_name)
		self.assertTrue(conversation.raven_channel)
		self.assertTrue(conversation.raven_workspace)

		channel = frappe.get_doc("Raven Channel", conversation.raven_channel)
		self.assertEqual(channel.linked_doctype, "WhatsApp Raven Conversation")
		self.assertEqual(channel.linked_document, conversation.name)

		link_name = frappe.db.get_value(
			"WhatsApp Raven Message Link",
			{"whatsapp_message": incoming.name},
			"name",
		)
		self.assertTrue(link_name)

		link = frappe.get_doc("WhatsApp Raven Message Link", link_name)
		self.assertEqual(link.direction, "Incoming")
		self.assertEqual(link.whatsapp_message_id, message_id)
		self.assertEqual(link.conversation, conversation.name)
		self.assertEqual(link.raven_channel, channel.name)
		self.assertTrue(link.raven_message)

		raven_message = frappe.get_doc("Raven Message", link.raven_message)
		self.assertEqual(raven_message.link_doctype, "WhatsApp Message")
		self.assertEqual(raven_message.link_document, incoming.name)
		self.assertEqual(raven_message.channel_id, channel.name)
		self.assertEqual(cstr(raven_message.message_type), "Text")

		self.assertEqual(
			frappe.db.count(
				"WhatsApp Raven Conversation",
				{"phone_number": normalized_phone, "whatsapp_account": self.ACCOUNT_NAME, "enabled": 1},
			),
			1,
		)
		self.assertEqual(
			frappe.db.count("WhatsApp Raven Message Link", {"whatsapp_message_id": message_id}),
			1,
		)
		self.assertEqual(
			frappe.db.count(
				"Raven Message",
				{"link_doctype": "WhatsApp Message", "link_document": incoming.name},
			),
			1,
		)

	def test_02_reprocessing_same_whatsapp_message_is_idempotent(self):
		message_id = self._message_id("02")
		incoming = self._insert_whatsapp_message(
			type_="Incoming",
			content_type="text",
			phone=self._phone("02"),
			message_id=message_id,
			message=f"{self.PREFIX} incoming 02",
		)

		before_raven = frappe.db.count("Raven Message")
		before_links = frappe.db.count("WhatsApp Raven Message Link")

		result = process_incoming_whatsapp_message(incoming)
		self.assertEqual(result, "skipped_existing_whatsapp_message")

		self.assertEqual(frappe.db.count("Raven Message"), before_raven)
		self.assertEqual(frappe.db.count("WhatsApp Raven Message Link"), before_links)

	def test_03_duplicate_whatsapp_message_id_does_not_duplicate_mapping(self):
		phone = self._phone("03")
		message_id = self._message_id("03")
		first = self._insert_whatsapp_message(
			type_="Incoming",
			content_type="text",
			phone=phone,
			message_id=message_id,
			message=f"{self.PREFIX} incoming 03 first",
		)

		self.assertTrue(
			frappe.db.exists(
				"WhatsApp Raven Message Link",
				{"whatsapp_message": first.name, "whatsapp_message_id": message_id},
			)
		)

		before_links = frappe.db.count("WhatsApp Raven Message Link", {"whatsapp_message_id": message_id})
		before_raven = frappe.db.count("Raven Message")

		second = self._insert_whatsapp_message(
			type_="Incoming",
			content_type="text",
			phone=phone,
			message_id=message_id,
			message=f"{self.PREFIX} incoming 03 second",
		)

		self.assertEqual(
			frappe.db.count("WhatsApp Raven Message Link", {"whatsapp_message_id": message_id}),
			before_links,
		)
		self.assertFalse(
			frappe.db.exists("WhatsApp Raven Message Link", {"whatsapp_message": second.name})
		)
		self.assertFalse(
			frappe.db.exists(
				"Raven Message",
				{"link_doctype": "WhatsApp Message", "link_document": second.name},
			)
		)
		self.assertEqual(frappe.db.count("Raven Message"), before_raven)

	def test_04_outgoing_whatsapp_message_is_ignored_by_inbound(self):
		outgoing = self._insert_whatsapp_message(
			type_="Outgoing",
			content_type="text",
			phone=self._phone("04"),
			message_id=self._message_id("04"),
			message=f"{self.PREFIX} outbound source 04",
		)

		self.assertFalse(frappe.db.exists("WhatsApp Raven Message Link", {"whatsapp_message": outgoing.name}))
		self.assertFalse(
			frappe.db.exists(
				"Raven Message",
				{"link_doctype": "WhatsApp Message", "link_document": outgoing.name},
			)
		)
		self.assertEqual(process_incoming_whatsapp_message(outgoing), "skipped_outgoing")

	def test_05_incoming_non_text_is_ignored(self):
		doc = self._insert_whatsapp_message(
			type_="Incoming",
			content_type="image",
			phone=self._phone("05"),
			message_id=self._message_id("05"),
			message=f"{self.PREFIX} image source 05",
		)

		self.assertFalse(frappe.db.exists("WhatsApp Raven Message Link", {"whatsapp_message": doc.name}))
		self.assertFalse(
			frappe.db.exists(
				"Raven Message",
				{"link_doctype": "WhatsApp Message", "link_document": doc.name},
			)
		)
		self.assertEqual(process_incoming_whatsapp_message(doc), "skipped_unsupported_content_type")

	def test_06_human_raven_text_creates_outgoing_whatsapp_and_link(self):
		conversation = self._ensure_conversation("06")

		before_whatsapp = frappe.db.count("WhatsApp Message")
		before_links = frappe.db.count("WhatsApp Raven Message Link")

		raven_message = self._insert_raven_message(
			channel_id=conversation.raven_channel,
			text=f"<p>{self.PREFIX} outbound 06 hello</p>",
			is_bot_message=0,
		)

		link_name = frappe.db.get_value(
			"WhatsApp Raven Message Link",
			{"raven_message": raven_message.name},
			"name",
		)
		self.assertTrue(link_name)

		link = frappe.get_doc("WhatsApp Raven Message Link", link_name)
		self.assertEqual(link.direction, "Outgoing")
		self.assertTrue(link.whatsapp_message)
		self.assertEqual(link.content_type, "text")

		wa = frappe.get_doc("WhatsApp Message", link.whatsapp_message)
		self.assertEqual(cstr(wa.type), "Outgoing")
		self.assertEqual(cstr(wa.message_type), "Manual")
		self.assertEqual(cstr(wa.content_type), "text")
		self.assertEqual(cstr(wa.to), conversation.phone_number)
		self.assertIn(f"{self.PREFIX} outbound 06", cstr(wa.message))
		self.assertTrue(bool(cstr(wa.message_id)))
		self.assertEqual(cstr(wa.status), "Success")

		self.assertEqual(frappe.db.count("WhatsApp Message"), before_whatsapp + 1)
		self.assertEqual(frappe.db.count("WhatsApp Raven Message Link"), before_links + 1)

	def test_07_reprocessing_same_raven_message_is_idempotent(self):
		conversation = self._ensure_conversation("07")
		raven_message = self._insert_raven_message(
			channel_id=conversation.raven_channel,
			text=f"<p>{self.PREFIX} outbound 07 hello</p>",
			is_bot_message=0,
		)

		before_whatsapp = frappe.db.count("WhatsApp Message")
		before_links = frappe.db.count("WhatsApp Raven Message Link")

		result = process_outgoing_raven_message(raven_message)
		self.assertEqual(result, "skipped_existing_raven_message")

		self.assertEqual(frappe.db.count("WhatsApp Message"), before_whatsapp)
		self.assertEqual(frappe.db.count("WhatsApp Raven Message Link"), before_links)
		self.assertEqual(
			frappe.db.count("WhatsApp Raven Message Link", {"raven_message": raven_message.name}),
			1,
		)

	def test_08_raven_bot_messages_are_ignored(self):
		conversation = self._ensure_conversation("08")
		raven_message = self._insert_raven_message(
			channel_id=conversation.raven_channel,
			text=f"<p>{self.PREFIX} outbound 08 bot</p>",
			is_bot_message=1,
			bot=self.bridge_raven_user,
		)

		self.assertFalse(
			frappe.db.exists("WhatsApp Raven Message Link", {"raven_message": raven_message.name})
		)
		self.assertEqual(process_outgoing_raven_message(raven_message), "skipped_bot_message")

	def test_09_mirrored_inbound_raven_messages_are_ignored(self):
		conversation = self._ensure_conversation("09")
		source = self._insert_whatsapp_message(
			type_="Incoming",
			content_type="text",
			phone=self._phone("09"),
			message_id=self._message_id("09-source"),
			message=f"{self.PREFIX} inbound source 09",
		)

		raven_message = self._insert_raven_message(
			channel_id=conversation.raven_channel,
			text=f"<p>{self.PREFIX} outbound 09 mirror</p>",
			is_bot_message=0,
			link_doctype="WhatsApp Message",
			link_document=source.name,
		)

		self.assertFalse(
			frappe.db.exists("WhatsApp Raven Message Link", {"raven_message": raven_message.name})
		)
		self.assertEqual(
			process_outgoing_raven_message(raven_message),
			"skipped_linked_whatsapp_source",
		)

	def test_10_raven_messages_outside_mapped_channels_are_ignored(self):
		channel = self._ensure_non_mapped_channel("10")
		raven_message = self._insert_raven_message(
			channel_id=channel.name,
			text=f"<p>{self.PREFIX} outbound 10 no mapping</p>",
			is_bot_message=0,
		)

		self.assertFalse(
			frappe.db.exists("WhatsApp Raven Message Link", {"raven_message": raven_message.name})
		)
		self.assertEqual(process_outgoing_raven_message(raven_message), "skipped_no_conversation")

	def test_11_outbound_disabled_prevents_sync(self):
		conversation = self._ensure_conversation("11")
		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		original = settings.enable_outbound_replies
		settings.enable_outbound_replies = 0
		settings.save(ignore_permissions=True)
		frappe.db.commit()

		try:
			raven_message = self._insert_raven_message(
				channel_id=conversation.raven_channel,
				text=f"<p>{self.PREFIX} outbound 11 disabled</p>",
				is_bot_message=0,
			)
			self.assertFalse(
				frappe.db.exists("WhatsApp Raven Message Link", {"raven_message": raven_message.name})
			)
			self.assertEqual(process_outgoing_raven_message(raven_message), "skipped_outbound_disabled")
		finally:
			settings = frappe.get_single("WhatsApp Raven Bridge Settings")
			settings.enable_outbound_replies = original
			settings.save(ignore_permissions=True)
			frappe.db.commit()

	def test_12_inbound_hook_returns_none(self):
		incoming = self._insert_whatsapp_message(
			type_="Incoming",
			content_type="text",
			phone=self._phone("12"),
			message_id=self._message_id("12"),
			message=f"{self.PREFIX} incoming 12",
		)
		result = handle_whatsapp_message_after_insert(incoming)
		self.assertIsNone(result)

	def test_13_outbound_hook_returns_none(self):
		conversation = self._ensure_conversation("13")
		raven_message = self._insert_raven_message(
			channel_id=conversation.raven_channel,
			text=f"<p>{self.PREFIX} outbound 13 hello</p>",
			is_bot_message=0,
		)
		result = handle_raven_message_after_insert(raven_message)
		self.assertIsNone(result)

	@classmethod
	def _snapshot_settings(cls):
		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		snapshot = {
			"enabled": settings.enabled,
			"bridge_system_user": settings.bridge_system_user,
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
		return snapshot

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
	def _ensure_human_raven_user(cls):
		user = frappe.get_doc("User", cls.HUMAN_USER)
		if "Raven User" not in [row.role for row in user.roles]:
			user.append("roles", {"role": "Raven User"})
			user.save(ignore_permissions=True)

		raven_user_name = frappe.db.get_value("Raven User", {"user": cls.HUMAN_USER}, "name")
		if not raven_user_name:
			raven_user = frappe.get_doc(
				{
					"doctype": "Raven User",
					"type": "User",
					"user": cls.HUMAN_USER,
					"full_name": cls.HUMAN_USER,
					"first_name": cls.HUMAN_USER,
					"enabled": 1,
				}
			)
			raven_user.insert(ignore_permissions=True)
			raven_user_name = raven_user.name
		cls.human_raven_user = raven_user_name

	@classmethod
	def _ensure_bridge_bot(cls):
		if frappe.db.exists("Raven Bot", cls.BOT_NAME):
			bot = frappe.get_doc("Raven Bot", cls.BOT_NAME)
		else:
			bot = frappe.get_doc(
				{
					"doctype": "Raven Bot",
					"bot_name": cls.BOT_NAME,
					"is_ai_bot": 0,
				}
			).insert(ignore_permissions=True)

		if not bot.raven_user:
			bot.reload()
			bot.save(ignore_permissions=True)
			bot.reload()

		cls.bridge_raven_bot = bot.name
		cls.bridge_raven_user = bot.raven_user

	@classmethod
	def _ensure_workspace(cls):
		if frappe.db.exists("Raven Workspace", cls.WORKSPACE_NAME):
			workspace = frappe.get_doc("Raven Workspace", cls.WORKSPACE_NAME)
		else:
			workspace = frappe.get_doc(
				{
					"doctype": "Raven Workspace",
					"workspace_name": cls.WORKSPACE_NAME,
					"type": "Public",
				}
			).insert(ignore_permissions=True)

		cls.workspace = workspace.name

	@classmethod
	def _ensure_whatsapp_account(cls):
		if frappe.db.exists("WhatsApp Account", cls.ACCOUNT_NAME):
			account = frappe.get_doc("WhatsApp Account", cls.ACCOUNT_NAME)
		else:
			account = frappe.get_doc(
				{
					"doctype": "WhatsApp Account",
					"account_name": cls.ACCOUNT_NAME,
					"status": "Active",
					"url": "https://graph.facebook.com",
					"version": "v17.0",
					"phone_id": "warb4b_phone_id",
					"business_id": "warb4b_business_id",
					"app_id": "warb4b_app_id",
					"webhook_verify_token": "warb4b_verify_token",
					"is_default_incoming": 0,
					"is_default_outgoing": 0,
				}
			).insert(ignore_permissions=True)

		set_encrypted_password("WhatsApp Account", account.name, "warb4b-token", "token")
		cls.whatsapp_account = account.name

	@classmethod
	def _configure_bridge_settings(cls):
		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		settings.enabled = 1
		settings.default_raven_workspace = cls.workspace
		settings.default_channel_type = "Private"
		settings.bridge_raven_bot = cls.bridge_raven_bot
		settings.bridge_raven_user = cls.bridge_raven_user
		settings.bridge_system_user = None
		settings.default_whatsapp_account = cls.whatsapp_account
		settings.conversation_strategy = "Channel Per Contact"
		settings.enable_outbound_replies = 1
		settings.set("default_channel_members", [])
		settings.append(
			"default_channel_members",
			{"raven_user": cls.human_raven_user, "is_admin": 1},
		)
		settings.append(
			"default_channel_members",
			{"raven_user": cls.bridge_raven_user, "is_admin": 1},
		)
		settings.save(ignore_permissions=True)

	def _ensure_conversation(self, suffix):
		phone = self._phone(suffix)
		conversation = get_or_create_conversation(
			phone,
			whatsapp_account=self.ACCOUNT_NAME,
			profile_name=f"{self.PREFIX} User {suffix}",
			raven_workspace=self.workspace,
			conversation_strategy="Channel Per Contact",
		)
		if conversation.whatsapp_account != self.ACCOUNT_NAME:
			conversation.whatsapp_account = self.ACCOUNT_NAME
			conversation.save(ignore_permissions=True)

		channel = ensure_raven_destination(conversation)
		conversation.reload()
		self.assertEqual(conversation.raven_channel, channel.name)
		return conversation

	def _ensure_non_mapped_channel(self, suffix):
		channel_name = f"{self.PREFIX.lower()}-no-map-{suffix}"
		channel_id = frappe.db.get_value(
			"Raven Channel",
			{"workspace": self.workspace, "channel_name": channel_name},
			"name",
		)
		if channel_id:
			return frappe.get_doc("Raven Channel", channel_id)

		channel = frappe.get_doc(
			{
				"doctype": "Raven Channel",
				"type": "Private",
				"workspace": self.workspace,
				"channel_name": channel_name,
			}
		)
		channel.flags.do_not_add_member = True
		channel.insert(ignore_permissions=True)
		return channel

	def _insert_whatsapp_message(self, *, type_, content_type, phone, message_id, message):
		payload = {
			"doctype": "WhatsApp Message",
			"type": type_,
			"content_type": content_type,
			"message_type": "Manual",
			"message": message,
			"message_id": message_id,
			"whatsapp_account": self.ACCOUNT_NAME,
		}
		if type_ == "Incoming":
			payload["from"] = phone
			payload["profile_name"] = f"{self.PREFIX} Profile {self._test_slug()}"
		else:
			payload["to"] = phone

		return frappe.get_doc(payload).insert(ignore_permissions=True)

	def _insert_raven_message(
		self,
		*,
		channel_id,
		text,
		is_bot_message=0,
		bot=None,
		link_doctype=None,
		link_document=None,
		json_data=None,
	):
		payload = {
			"doctype": "Raven Message",
			"channel_id": channel_id,
			"message_type": "Text",
			"text": text,
			"is_bot_message": is_bot_message,
		}
		if bot:
			payload["bot"] = bot
		if link_doctype:
			payload["link_doctype"] = link_doctype
		if link_document:
			payload["link_document"] = link_document
		if json_data is not None:
			payload["json"] = json_data

		return frappe.get_doc(payload).insert(ignore_permissions=True)

	def _cleanup_test_records(self):
		self._delete_message_links()
		self._delete_raven_messages()
		self._delete_whatsapp_messages()
		self._delete_conversations_and_channels()

	def _delete_message_links(self):
		links = frappe.get_all(
			"WhatsApp Raven Message Link",
			filters=[
				["whatsapp_message_id", "like", "wamid.warb4b.%"],
			],
			pluck="name",
		)
		for name in links:
			frappe.delete_doc("WhatsApp Raven Message Link", name, force=True)

	def _delete_whatsapp_messages(self):
		names = frappe.get_all(
			"WhatsApp Message",
			filters=[
				["message_id", "like", "wamid.warb4b.%"],
			],
			pluck="name",
		)
		for name in names:
			frappe.delete_doc("WhatsApp Message", name, force=True)

	def _delete_raven_messages(self):
		names = frappe.get_all(
			"Raven Message",
			filters=[
				["text", "like", f"%{self.PREFIX}%"],
			],
			pluck="name",
		)
		for name in names:
			frappe.delete_doc("Raven Message", name, force=True)

	def _delete_conversations_and_channels(self):
		channels = frappe.get_all(
			"Raven Channel",
			filters=[
				["channel_name", "like", "whatsapp-44779988%"],
			],
			pluck="name",
		)
		for name in channels:
			if frappe.db.exists("Raven Channel", name):
				frappe.delete_doc("Raven Channel", name, force=True)

		no_map_channels = frappe.get_all(
			"Raven Channel",
			filters=[
				["workspace", "=", self.workspace],
				["channel_name", "like", f"{self.PREFIX.lower()}-no-map-%"],
			],
			pluck="name",
		)
		for name in no_map_channels:
			if frappe.db.exists("Raven Channel", name):
				frappe.delete_doc("Raven Channel", name, force=True)

		conversations = frappe.get_all(
			"WhatsApp Raven Conversation",
			filters=[
				["phone_number", "like", "44779988%"],
			],
			pluck="name",
		)
		for name in conversations:
			if frappe.db.exists("WhatsApp Raven Conversation", name):
				frappe.delete_doc("WhatsApp Raven Conversation", name, force=True)

	def _test_slug(self):
		return self._testMethodName.replace("test_", "").replace("_", "-")

	def _message_id(self, suffix):
		return f"wamid.warb4b.{self._test_slug()}.{suffix}"

	def _phone(self, suffix):
		return f"44779988{int(cstr(suffix).replace('-', '')[-2:], 10):02d}25"
