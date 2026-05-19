from __future__ import annotations
from urllib.parse import quote

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import cint, cstr
from frappe.utils.password import set_encrypted_password

from whatsapp_raven_bridge.api.conversation import (
	list_active_raven_users,
	move_message_conversation_to_private_channel,
	move_to_private_channel,
)
from whatsapp_raven_bridge.bridge.account_route import (
	ensure_route_memberships,
	get_or_create_inbox_channel,
	get_route_for_whatsapp_account,
)
from whatsapp_raven_bridge.bridge.conversation import get_or_create_conversation, normalize_phone_number
from whatsapp_raven_bridge.bridge.outbound import process_outgoing_raven_message
from whatsapp_raven_bridge.bridge.raven_actions import (
	ACTION_FUNCTION_PATH,
	ACTION_NAME,
	ensure_raven_message_actions,
)
from whatsapp_raven_bridge.bridge.raven_destination import ensure_raven_destination
from whatsapp_raven_bridge.bridge.whatsapp_message_rendering import format_phone_for_display


class TestAccountRouteDesign(IntegrationTestCase):
	PREFIX = "WARC4C"
	WORKSPACE = "WARC4C Workspace"
	BOT_NAME = "WARC4C Bot"
	ACCOUNT_NAME = "WARC4C WhatsApp Account"
	PRIVATE_ALLOWED_USER = "warc4c_private_allowed@example.com"
	PRIVATE_DENIED_USER = "warc4c_private_denied@example.com"
	NON_MANAGER_USER = "warc4c_non_manager@example.com"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._settings_snapshot = cls._snapshot_settings()
		cls._ensure_raven_user("Administrator")
		cls._ensure_workspace()
		cls._ensure_bot()
		cls._ensure_whatsapp_account()
		cls._ensure_named_user(cls.PRIVATE_ALLOWED_USER, "WARC4C Allowed Agent")
		cls._ensure_named_user(cls.PRIVATE_DENIED_USER, "WARC4C Denied Agent")
		cls._ensure_named_user(cls.NON_MANAGER_USER, "WARC4C Non Manager")
		cls._ensure_raven_user(cls.PRIVATE_ALLOWED_USER)
		cls._ensure_raven_user(cls.PRIVATE_DENIED_USER)
		cls._ensure_raven_user(cls.NON_MANAGER_USER)
		cls._remove_system_manager_role(cls.NON_MANAGER_USER)
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
		self.assertFalse(parent_message.link_doctype)
		self.assertFalse(parent_message.link_document)
		self.assertEqual(int(parent_message.hide_link_preview or 0), 1)
		self.assertTrue(parent_message.is_thread)
		self.assertTrue(parent_message.is_bot_message)
		self.assertNotIn("href=", parent_message.text)
		self.assertNotIn("/raven/", parent_message.text)
		self.assertNotIn("/thread/", parent_message.text)
		self.assertNotIn("target=", parent_message.text)
		self.assertNotIn("onclick=", parent_message.text)
		self.assertNotIn("style=", parent_message.text)
		self.assertIn("<mark><strong>", parent_message.text)
		self.assertIn("<code>", parent_message.text)
		self.assertIn(format_phone_for_display(phone_norm), parent_message.text)
		self.assertNotIn("WhatsApp Raven Conversation", parent_message.text)
		self.assertNotIn("WhatsApp conversation", parent_message.text)
		self.assertNotIn(conversation.name, parent_message.text)

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
		self.assertEqual(frappe.db.count("Raven Message", {"name": conversation.parent_raven_message}), 1)
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

	def test_thread_destination_does_not_call_raven_create_thread_api(self):
		route = self._create_route("thread-no-api", conversation_strategy="Thread Per Contact")
		conversation = get_or_create_conversation(
			"+447733110106",
			whatsapp_account=self.ACCOUNT_NAME,
			profile_name=f"{self.PREFIX} No API",
		)

		from raven.api import threads as raven_threads

		original_create_thread = raven_threads.create_thread
		state = {"called": False}

		def fake_create_thread(message_id):
			state["called"] = True
			raise AssertionError("create_thread should not be called by bridge thread resolver")

		raven_threads.create_thread = fake_create_thread
		try:
			channel = ensure_raven_destination(conversation)
		finally:
			raven_threads.create_thread = original_create_thread

		self.assertFalse(state["called"])
		self.assertTrue(channel.is_thread)

	def test_guest_inbound_thread_creation_does_not_log_create_thread_permission_error(self):
		route = self._create_route("thread-guest", conversation_strategy="Thread Per Contact")
		error_title = "WhatsApp Raven Bridge: create_thread fallback"
		before_count = frappe.db.count("Error Log", {"method": error_title})

		current_user = frappe.session.user
		try:
			frappe.set_user("Guest")
			incoming = self._insert_incoming_thread_message(
				phone="+447733110107",
				message_id="wamid.warc4c.thread.guest.001",
				body="thread route guest inbound",
			)
		finally:
			frappe.set_user(current_user)

		phone_norm = normalize_phone_number("+447733110107")
		conversation_name = frappe.db.get_value(
			"WhatsApp Raven Conversation",
			{"phone_number": phone_norm, "whatsapp_account": self.ACCOUNT_NAME},
			"name",
		)
		self.assertTrue(conversation_name)
		conversation = frappe.get_doc("WhatsApp Raven Conversation", conversation_name)
		self.assertEqual(conversation.account_route, route.name)
		self.assertTrue(conversation.raven_channel)
		self.assertTrue(conversation.parent_raven_message)
		self.assertTrue(frappe.db.exists("WhatsApp Raven Message Link", {"whatsapp_message": incoming.name}))

		after_count = frappe.db.count("Error Log", {"method": error_title})
		self.assertEqual(after_count, before_count)

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

	def test_private_channel_move_basic_conversion(self):
		allowed_raven = self._raven_user_for(self.PRIVATE_ALLOWED_USER)
		denied_raven = self._raven_user_for(self.PRIVATE_DENIED_USER)
		route = self._create_route(
			"private-basic",
			conversation_strategy="Thread Per Contact",
			route_members=[
				{"raven_user": "Administrator", "is_admin": 1, "can_reply": 1},
				{"raven_user": allowed_raven, "is_admin": 0, "can_reply": 1},
				{"raven_user": denied_raven, "is_admin": 0, "can_reply": 1},
			],
		)
		conversation = self._make_conversation(route, "+447733119901")
		parent_before = cstr(conversation.parent_raven_message or "").strip()
		channel_before = cstr(conversation.raven_channel or "").strip()
		parent_doc_before = frappe.get_doc("Raven Message", parent_before)
		inbox_before = cstr(parent_doc_before.channel_id or "").strip()
		self.assertTrue(parent_before)
		self.assertEqual(parent_before, channel_before)
		self.assertEqual(int(parent_doc_before.is_thread or 0), 1)

		result = move_to_private_channel(
			conversation=conversation.name,
			raven_users=[allowed_raven],
			channel_name="warc4c-private-escalation",
		)
		conversation.reload()
		channel_doc = frappe.get_doc("Raven Channel", conversation.raven_channel)
		parent_doc = frappe.get_doc("Raven Message", conversation.parent_raven_message)

		self.assertEqual(result.get("conversation"), conversation.name)
		self.assertEqual(cstr(conversation.delivery_mode), "Private Channel")
		self.assertEqual(cstr(conversation.raven_channel), channel_before)
		self.assertEqual(cstr(result.get("workspace")), cstr(route.raven_workspace))
		self.assertEqual(
			cstr(result.get("private_channel_url")),
			f"/raven/{quote(route.raven_workspace, safe='')}/{quote(conversation.raven_channel, safe='')}",
		)
		self.assertIn("private channel", cstr(result.get("message")).lower())
		self.assertEqual(int(channel_doc.is_thread or 0), 0)
		self.assertEqual(cstr(channel_doc.type), "Private")
		self.assertEqual(cstr(channel_doc.channel_name), "warc4c-private-escalation")
		self.assertEqual(cstr(conversation.private_channel_name), "warc4c-private-escalation")
		self.assertEqual(cstr(conversation.previous_parent_raven_message), parent_before)
		self.assertEqual(cstr(conversation.previous_route_thread_channel), channel_before)
		self.assertEqual(cstr(conversation.previous_route_inbox_channel), inbox_before)
		self.assertEqual(int(parent_doc.is_thread or 0), 0)
		self.assertEqual(cstr(parent_doc.channel_id), inbox_before)
		self.assertTrue(frappe.db.exists("Raven Message", parent_before))
		self.assertNotEqual(cstr(parent_doc.text or ""), "")
		self.assertGreaterEqual(
			frappe.db.count("Raven Message", {"channel_id": conversation.raven_channel}),
			0,
		)
		actor_raven = self._raven_user_for("Administrator")
		self.assertEqual(cstr(result.get("actor_raven_user")), actor_raven)
		self.assertTrue(
			frappe.db.exists(
				"Raven Channel Member",
				{"channel_id": conversation.raven_channel, "user_id": actor_raven},
			)
		)

	def test_private_channel_move_accepts_list_json_and_comma_raven_users(self):
		allowed_raven = self._raven_user_for(self.PRIVATE_ALLOWED_USER)
		denied_raven = self._raven_user_for(self.PRIVATE_DENIED_USER)
		route = self._create_route("private-user-parser", conversation_strategy="Thread Per Contact")

		conversation_list = self._make_conversation(route, "+447733119921")
		move_to_private_channel(conversation=conversation_list.name, raven_users=[allowed_raven], channel_name="warc4c-parse-list")
		conversation_list.reload()
		self.assertTrue(
			frappe.db.exists(
				"WhatsApp Raven Conversation Private Member",
				{"parent": conversation_list.name, "raven_user": allowed_raven},
			)
		)

		conversation_json = self._make_conversation(route, "+447733119922")
		move_to_private_channel(
			conversation=conversation_json.name,
			raven_users=f'["{allowed_raven}","{denied_raven}"]',
			channel_name="warc4c-parse-json",
		)
		conversation_json.reload()
		self.assertTrue(
			frappe.db.exists(
				"WhatsApp Raven Conversation Private Member",
				{"parent": conversation_json.name, "raven_user": denied_raven},
			)
		)

		conversation_csv = self._make_conversation(route, "+447733119923")
		move_to_private_channel(
			conversation=conversation_csv.name,
			raven_users=f"{allowed_raven}, {self.PRIVATE_DENIED_USER}",
			channel_name="warc4c-parse-csv",
		)
		conversation_csv.reload()
		self.assertTrue(
			frappe.db.exists(
				"WhatsApp Raven Conversation Private Member",
				{"parent": conversation_csv.name, "raven_user": denied_raven},
			)
		)

	def test_private_channel_move_membership_and_outbound_permissions(self):
		allowed_raven = self._raven_user_for(self.PRIVATE_ALLOWED_USER)
		denied_raven = self._raven_user_for(self.PRIVATE_DENIED_USER)
		route = self._create_route(
			"private-members",
			conversation_strategy="Thread Per Contact",
			route_members=[
				{"raven_user": "Administrator", "is_admin": 1, "can_reply": 1},
				{"raven_user": allowed_raven, "is_admin": 0, "can_reply": 1},
				{"raven_user": denied_raven, "is_admin": 0, "can_reply": 1},
			],
		)
		conversation = self._make_conversation(route, "+447733119902")
		move_to_private_channel(conversation=conversation.name, raven_users=[allowed_raven], channel_name="warc4c-private-members")
		conversation.reload()

		self.assertTrue(
			frappe.db.exists(
				"Raven Channel Member",
				{"channel_id": conversation.raven_channel, "user_id": allowed_raven},
			)
		)
		self.assertFalse(
			frappe.db.exists(
				"Raven Channel Member",
				{"channel_id": conversation.raven_channel, "user_id": denied_raven},
			)
		)

		allowed_message = self._insert_human_raven_message(
			channel_id=conversation.raven_channel,
			text="<p>warc4c private allowed outbound</p>",
			user_id=self.PRIVATE_ALLOWED_USER,
		)
		self.assertTrue(
			frappe.db.exists("WhatsApp Raven Message Link", {"raven_message": allowed_message.name})
		)

		denied_message = self._insert_human_raven_message(
			channel_id=conversation.raven_channel,
			text="<p>warc4c private denied outbound</p>",
			user_id=self.PRIVATE_DENIED_USER,
		)
		result = process_outgoing_raven_message(denied_message)
		self.assertEqual(result, "skipped_user_not_allowed")
		self.assertFalse(
			frappe.db.exists("WhatsApp Raven Message Link", {"raven_message": denied_message.name})
		)

	def test_private_channel_inbound_routes_to_private_channel_without_new_parent(self):
		allowed_raven = self._raven_user_for(self.PRIVATE_ALLOWED_USER)
		route = self._create_route(
			"private-inbound",
			conversation_strategy="Thread Per Contact",
			route_members=[
				{"raven_user": "Administrator", "is_admin": 1, "can_reply": 1},
				{"raven_user": allowed_raven, "is_admin": 0, "can_reply": 1},
			],
		)
		phone = "+447733119903"
		first = self._insert_incoming_thread_message(
			phone=phone,
			message_id="wamid.warc4c.private.inbound.001",
			body="before private move",
		)
		conversation = frappe.get_doc(
			"WhatsApp Raven Conversation",
			frappe.db.get_value(
				"WhatsApp Raven Conversation",
				{"phone_number": normalize_phone_number(phone), "whatsapp_account": self.ACCOUNT_NAME},
				"name",
			),
		)
		parent_before = cstr(conversation.parent_raven_message or "").strip()
		inbox_before = cstr(frappe.db.get_value("Raven Message", parent_before, "channel_id") or "").strip()
		inbox_message_count_before = frappe.db.count("Raven Message", {"channel_id": inbox_before})
		move_to_private_channel(conversation=conversation.name, raven_users=[allowed_raven], channel_name="warc4c-private-inbound")
		conversation.reload()

		second = self._insert_incoming_thread_message(
			phone=phone,
			message_id="wamid.warc4c.private.inbound.002",
			body="after private move",
		)
		link_name = frappe.db.get_value("WhatsApp Raven Message Link", {"whatsapp_message": second.name}, "name")
		self.assertTrue(link_name)
		link = frappe.get_doc("WhatsApp Raven Message Link", link_name)
		self.assertEqual(cstr(link.raven_channel), cstr(conversation.raven_channel))
		self.assertTrue(frappe.db.exists("Raven Message", parent_before))
		self.assertEqual(cint(frappe.db.get_value("Raven Message", parent_before, "is_thread") or 0), 0)
		inbox_message_count_after = frappe.db.count("Raven Message", {"channel_id": inbox_before})
		self.assertEqual(inbox_message_count_after, inbox_message_count_before)
		self.assertTrue(frappe.db.exists("WhatsApp Message", first.name))

	def test_private_channel_move_is_idempotent_and_updates_members(self):
		allowed_raven = self._raven_user_for(self.PRIVATE_ALLOWED_USER)
		denied_raven = self._raven_user_for(self.PRIVATE_DENIED_USER)
		route = self._create_route(
			"private-idempotent",
			conversation_strategy="Thread Per Contact",
			route_members=[
				{"raven_user": "Administrator", "is_admin": 1, "can_reply": 1},
				{"raven_user": allowed_raven, "is_admin": 0, "can_reply": 1},
				{"raven_user": denied_raven, "is_admin": 0, "can_reply": 1},
			],
		)
		conversation = self._make_conversation(route, "+447733119904")
		first = move_to_private_channel(conversation=conversation.name, raven_users=[allowed_raven], channel_name="warc4c-private-1")
		second = move_to_private_channel(
			conversation=conversation.name,
			raven_users=[allowed_raven, denied_raven],
			channel_name="warc4c-private-2",
		)
		conversation.reload()

		self.assertEqual(cstr(first.get("private_channel")), cstr(second.get("private_channel")))
		self.assertEqual(cstr(conversation.private_channel_name), "warc4c-private-2")
		self.assertTrue(
			frappe.db.exists(
				"Raven Channel Member",
				{"channel_id": conversation.raven_channel, "user_id": denied_raven},
			)
		)

	def test_private_channel_move_permission_denied_for_non_system_manager(self):
		route = self._create_route("private-perm", conversation_strategy="Thread Per Contact")
		conversation = self._make_conversation(route, "+447733119905")
		current_user = frappe.session.user
		try:
			frappe.set_user(self.NON_MANAGER_USER)
			with self.assertRaises(frappe.PermissionError):
				move_to_private_channel(conversation=conversation.name, raven_users=["Administrator"])
		finally:
			frappe.set_user(current_user)

	def test_private_channel_move_permission_denied_for_guest(self):
		route = self._create_route("private-guest", conversation_strategy="Thread Per Contact")
		conversation = self._make_conversation(route, "+447733119907")
		current_user = frappe.session.user
		try:
			frappe.set_user("Guest")
			with self.assertRaises(frappe.PermissionError):
				move_to_private_channel(conversation=conversation.name, raven_users=["Administrator"])
		finally:
			frappe.set_user(current_user)

	def test_private_channel_old_route_thread_reply_is_blocked(self):
		allowed_raven = self._raven_user_for(self.PRIVATE_ALLOWED_USER)
		route = self._create_route(
			"private-old-thread",
			conversation_strategy="Thread Per Contact",
			route_members=[
				{"raven_user": "Administrator", "is_admin": 1, "can_reply": 1},
				{"raven_user": allowed_raven, "is_admin": 0, "can_reply": 1},
			],
		)
		conversation = self._make_conversation(route, "+447733119906")
		move_to_private_channel(conversation=conversation.name, raven_users=[allowed_raven], channel_name="warc4c-private-old")
		conversation.reload()

		if conversation.previous_route_inbox_channel:
			raven_message = frappe.get_doc(
				{
					"doctype": "Raven Message",
					"channel_id": conversation.previous_route_inbox_channel,
					"message_type": "Text",
					"text": "<p>warc4c reply on old inbox should not sync</p>",
					"is_bot_message": 0,
				}
			).insert(ignore_permissions=True)
			result = process_outgoing_raven_message(raven_message)
			self.assertIn(result, ("conversation_moved_to_private_channel", "skipped_no_conversation"))
			self.assertFalse(
				frappe.db.exists("WhatsApp Raven Message Link", {"raven_message": raven_message.name})
			)

	def test_raven_message_action_created_idempotently(self):
		result_1 = ensure_raven_message_actions()
		result_2 = ensure_raven_message_actions()

		action_names = frappe.get_all(
			"Raven Message Action",
			filters={"custom_function_path": ACTION_FUNCTION_PATH},
			pluck="name",
		)
		self.assertEqual(len(action_names), 1)
		action = frappe.get_doc("Raven Message Action", action_names[0])
		self.assertEqual(cstr(action.action_name), ACTION_NAME)
		self.assertEqual(cstr(action.action), "Custom Function")
		self.assertEqual(cstr(action.custom_function_path), ACTION_FUNCTION_PATH)
		self.assertTrue(result_1.get("updated"))
		self.assertTrue(result_2.get("updated"))

		fields = {row.fieldname: row for row in (action.fields or [])}
		self.assertIn("raven_message", fields)
		self.assertIn("channel_id", fields)
		self.assertIn("raven_users", fields)
		self.assertIn("channel_name", fields)
		self.assertEqual(cstr(fields["raven_message"].default_value_type), "Message Field")
		self.assertEqual(cstr(fields["raven_message"].default_value), "name")
		self.assertEqual(cstr(fields["channel_id"].default_value_type), "Message Field")
		self.assertEqual(cstr(fields["channel_id"].default_value), "channel_id")
		self.assertEqual(cstr(fields["raven_users"].type), "Small Text")
		self.assertEqual(cint(fields["raven_users"].is_required), 0)
		self.assertIn("optional", cstr(fields["raven_users"].helper_text).lower())

	def test_list_active_raven_users_returns_enabled_rows(self):
		rows = list_active_raven_users()
		self.assertTrue(rows)
		by_value = {cstr(row.get("value")): row for row in rows}
		admin_raven = self._raven_user_for("Administrator")
		self.assertIn(admin_raven, by_value)
		self.assertIn("label", by_value[admin_raven])

	def test_raven_message_action_resolves_parent_starter(self):
		allowed_raven = self._raven_user_for(self.PRIVATE_ALLOWED_USER)
		route = self._create_route("action-parent", conversation_strategy="Thread Per Contact")
		conversation = self._make_conversation(route, "+447733119908")

		result = move_message_conversation_to_private_channel(
			raven_message=conversation.parent_raven_message,
			raven_users=[allowed_raven],
			channel_name="warc4c-action-parent",
		)
		conversation.reload()
		self.assertEqual(cstr(result.get("resolved_conversation")), conversation.name)
		self.assertEqual(cstr(conversation.delivery_mode), "Private Channel")
		self.assertEqual(cint(frappe.db.get_value("Raven Channel", conversation.raven_channel, "is_thread") or 0), 0)

	def test_raven_message_action_resolves_thread_message(self):
		allowed_raven = self._raven_user_for(self.PRIVATE_ALLOWED_USER)
		route = self._create_route("action-thread-msg", conversation_strategy="Thread Per Contact")
		conversation = self._make_conversation(route, "+447733119909")

		thread_message = frappe.get_doc(
			{
				"doctype": "Raven Message",
				"channel_id": conversation.raven_channel,
				"message_type": "Text",
				"text": "<p>warc4c action thread message</p>",
				"is_bot_message": 1,
			}
		).insert(ignore_permissions=True)

		result = move_message_conversation_to_private_channel(
			raven_message=thread_message.name,
			raven_users=[allowed_raven],
			channel_name="warc4c-action-thread-msg",
		)
		conversation.reload()
		self.assertEqual(cstr(result.get("resolved_conversation")), conversation.name)
		self.assertEqual(cstr(conversation.delivery_mode), "Private Channel")

	def test_raven_message_action_resolves_whatsapp_linked_message(self):
		allowed_raven = self._raven_user_for(self.PRIVATE_ALLOWED_USER)
		route = self._create_route("action-link-msg", conversation_strategy="Thread Per Contact")
		phone = "+447733119910"
		incoming = self._insert_incoming_thread_message(
			phone=phone,
			message_id="wamid.warc4c.action.link.001",
			body="warc4c action linked message",
		)
		conversation = frappe.get_doc(
			"WhatsApp Raven Conversation",
			frappe.db.get_value(
				"WhatsApp Raven Conversation",
				{"phone_number": normalize_phone_number(phone), "whatsapp_account": self.ACCOUNT_NAME},
				"name",
			),
		)
		link_name = frappe.db.get_value("WhatsApp Raven Message Link", {"whatsapp_message": incoming.name}, "name")
		self.assertTrue(link_name)
		raven_message = cstr(frappe.db.get_value("WhatsApp Raven Message Link", link_name, "raven_message") or "").strip()
		self.assertTrue(raven_message)

		result = move_message_conversation_to_private_channel(
			raven_message=raven_message,
			raven_users=[allowed_raven],
			channel_name="warc4c-action-link-msg",
		)
		conversation.reload()
		self.assertEqual(cstr(result.get("resolved_conversation")), conversation.name)
		self.assertEqual(cstr(conversation.delivery_mode), "Private Channel")

	def test_raven_message_action_unrelated_message_fails_safely(self):
		ensure_raven_message_actions()
		channel = frappe.get_doc(
			{
				"doctype": "Raven Channel",
				"channel_name": "warc4c-action-unrelated",
				"workspace": self.WORKSPACE,
				"type": "Private",
			}
		).insert(ignore_permissions=True)
		raven_message = frappe.get_doc(
			{
				"doctype": "Raven Message",
				"channel_id": channel.name,
				"message_type": "Text",
				"text": "<p>warc4c unrelated</p>",
				"is_bot_message": 1,
			}
		).insert(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			move_message_conversation_to_private_channel(
				raven_message=raven_message.name,
				raven_users=["Administrator"],
			)

	def test_raven_message_action_permission_denied(self):
		route = self._create_route("action-perm", conversation_strategy="Thread Per Contact")
		conversation = self._make_conversation(route, "+447733119911")
		current_user = frappe.session.user
		try:
			frappe.set_user(self.NON_MANAGER_USER)
			with self.assertRaises(frappe.PermissionError):
				move_message_conversation_to_private_channel(
					raven_message=conversation.parent_raven_message,
					raven_users=["Administrator"],
				)
			frappe.set_user("Guest")
			with self.assertRaises(frappe.PermissionError):
				move_message_conversation_to_private_channel(
					raven_message=conversation.parent_raven_message,
					raven_users=["Administrator"],
				)
		finally:
			frappe.set_user(current_user)

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
		settings.bridge_system_user = None
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
		route_members=None,
	):
		if route_members is None:
			route_members = (
				[{"raven_user": "Administrator", "is_admin": 1, "can_reply": 1}]
				if include_admin_member
				else []
			)
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
				"members": route_members,
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

	def _insert_human_raven_message(self, *, channel_id, text, user_id):
		current_user = frappe.session.user
		try:
			frappe.set_user(user_id)
			return frappe.get_doc(
				{
					"doctype": "Raven Message",
					"channel_id": channel_id,
					"message_type": "Text",
					"text": text,
					"is_bot_message": 0,
				}
			).insert(ignore_permissions=True)
		finally:
			frappe.set_user(current_user)

	@classmethod
	def _raven_user_for(cls, user_id):
		return cstr(frappe.db.get_value("Raven User", {"user": user_id, "enabled": 1}, "name") or "").strip()

	@classmethod
	def _ensure_named_user(cls, user_id, full_name):
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
		user_doc.full_name = full_name
		user_doc.first_name = full_name
		user_doc.save(ignore_permissions=True)

	@classmethod
	def _remove_system_manager_role(cls, user_id):
		if not frappe.db.exists("User", user_id):
			return
		user_doc = frappe.get_doc("User", user_id)
		changed = False
		for idx in range(len(user_doc.roles or []) - 1, -1, -1):
			if user_doc.roles[idx].role == "System Manager":
				user_doc.roles.pop(idx)
				changed = True
		if changed:
			user_doc.save(ignore_permissions=True)

	def _cleanup(self):
		for name in frappe.get_all(
			"Raven Message Action",
			filters={"custom_function_path": ACTION_FUNCTION_PATH},
			pluck="name",
		):
			if frappe.db.exists("Raven Message Action", name):
				frappe.delete_doc("Raven Message Action", name, force=True)

		conversation_rows = frappe.get_all(
			"WhatsApp Raven Conversation",
			filters=[["phone_number", "like", "44773311%"]],
			fields=["name", "parent_raven_message"],
		)
		conversation_names = {row.name for row in conversation_rows}
		parent_messages = {row.parent_raven_message for row in conversation_rows if row.parent_raven_message}

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
			"WhatsApp Raven Conversation",
			filters=[["phone_number", "like", "44773311%"]],
			pluck="name",
		):
			frappe.delete_doc("WhatsApp Raven Conversation", name, force=True)

		for name in parent_messages:
			if frappe.db.exists("Raven Message", name):
				frappe.delete_doc("Raven Message", name, force=True)

		for name in frappe.get_all(
			"Raven Message",
			filters=[
				["text", "like", "%warc4c%"],
			],
			pluck="name",
		):
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
