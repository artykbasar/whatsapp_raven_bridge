from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import cstr

from whatsapp_raven_bridge.dev_seed import (
	DEMO_EXPERIMENT_LABELS,
	DEMO_INBOX_CHANNEL_FALLBACK,
	DEMO_TOPICS,
	_delete_demo_seed_data,
	create_demo_whatsapp_raven_data,
)


class TestDemoSeedCleanup(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		_delete_demo_seed_data()
		frappe.db.commit()

	def tearDown(self):
		try:
			_delete_demo_seed_data()
			frappe.db.commit()
		finally:
			super().tearDown()

	def test_seed_cleanup_twice_keeps_exactly_one_demo_generation(self):
		first = create_demo_whatsapp_raven_data(
			conversations=10,
			messages_per_conversation=10,
			cleanup_existing=1,
			force=1,
		)
		second = create_demo_whatsapp_raven_data(
			conversations=10,
			messages_per_conversation=10,
			cleanup_existing=1,
			force=1,
		)
		self.assertEqual(int(first.get("notify_calls") or 0), 0)
		self.assertEqual(int(second.get("notify_calls") or 0), 0)
		self.assertEqual(self._count_demo_conversations(), 10)
		self.assertEqual(self._count_demo_conversations_with_parent(), 10)
		self.assertEqual(self._count_demo_conversations_with_thread(), 10)
		self.assertEqual(self._count_demo_parent_messages(), 10)
		self.assertEqual(self._count_stale_demo_hrefs_or_experiments(), 0)
		self.assertEqual(self._count_polished_demo_parents(), 10)
		for row in self._get_demo_threads():
			self.assertEqual(int(row.get("thread_messages") or 0), 10)

	def test_seed_cleanup_removes_legacy_parent_variants(self):
		create_demo_whatsapp_raven_data(conversations=10, messages_per_conversation=10, cleanup_existing=1, force=1)
		self._insert_legacy_demo_parent("<p><a href=\"/raven/x/y/thread/z\"><strong>Demo Sales inquiry</strong></a></p>")
		self._insert_legacy_demo_parent("<p><strong>Demo Support issue</strong></p><p>447870900901</p>")
		self._insert_legacy_demo_parent("<p>ONCLICK TEST — Demo Payment question</p>")
		frappe.db.commit()

		self.assertGreater(self._count_demo_parent_messages(), 10)
		create_demo_whatsapp_raven_data(conversations=10, messages_per_conversation=10, cleanup_existing=1, force=1)

		self.assertEqual(self._count_demo_parent_messages(), 10)
		self.assertEqual(self._count_stale_demo_hrefs_or_experiments(), 0)
		for row in self._get_inbox_demo_parent_rows():
			text = cstr(row.get("text") or "")
			self.assertIn("<mark><strong>", text)
			self.assertIn("<code>+", text)

	def _insert_legacy_demo_parent(self, text: str):
		frappe.get_doc(
			{
				"doctype": "Raven Message",
				"channel_id": DEMO_INBOX_CHANNEL_FALLBACK,
				"message_type": "Text",
				"is_thread": 1,
				"is_bot_message": 1,
				"hide_link_preview": 1,
				"text": text,
				"content": "legacy demo parent",
			}
		).insert(ignore_permissions=True)

	def _get_inbox_demo_parent_rows(self) -> list[frappe._dict]:
		rows = frappe.get_all(
			"Raven Message",
			filters={"channel_id": DEMO_INBOX_CHANNEL_FALLBACK},
			fields=["name", "text"],
			order_by="creation asc",
		)
		tokens = [f"Demo {topic}" for topic in DEMO_TOPICS]
		return [row for row in rows if any(token in cstr(row.get("text") or "") for token in tokens)]

	def _count_demo_parent_messages(self) -> int:
		return len(self._get_inbox_demo_parent_rows())

	def _count_stale_demo_hrefs_or_experiments(self) -> int:
		count = 0
		for row in self._get_inbox_demo_parent_rows():
			text = cstr(row.get("text") or "")
			if "href=" in text or "/raven/" in text or "/thread/" in text:
				count += 1
				continue
			if any(label in text for label in DEMO_EXPERIMENT_LABELS):
				count += 1
		return count

	def _count_polished_demo_parents(self) -> int:
		count = 0
		for row in self._get_inbox_demo_parent_rows():
			text = cstr(row.get("text") or "")
			if "<mark><strong>" in text and "<code>+" in text:
				count += 1
		return count

	def _count_demo_conversations(self) -> int:
		return int(
			frappe.db.sql(
				"""
				select count(*)
				from `tabWhatsApp Raven Conversation`
				where display_name like 'Demo%'
				""",
			)[0][0]
		)

	def _count_demo_conversations_with_parent(self) -> int:
		return int(
			frappe.db.sql(
				"""
				select sum(parent_raven_message is not null and parent_raven_message != '')
				from `tabWhatsApp Raven Conversation`
				where display_name like 'Demo%'
				""",
			)[0][0]
			or 0
		)

	def _count_demo_conversations_with_thread(self) -> int:
		return int(
			frappe.db.sql(
				"""
				select sum(raven_channel is not null and raven_channel != '')
				from `tabWhatsApp Raven Conversation`
				where display_name like 'Demo%'
				""",
			)[0][0]
			or 0
		)

	def _get_demo_threads(self) -> list[frappe._dict]:
		return frappe.db.sql(
			"""
			select
				c.display_name,
				c.parent_raven_message,
				c.raven_channel,
				count(rm.name) as thread_messages
			from `tabWhatsApp Raven Conversation` c
			left join `tabRaven Message` rm on rm.channel_id = c.raven_channel
			where c.display_name like 'Demo%'
			group by c.display_name, c.parent_raven_message, c.raven_channel
			order by c.display_name
			""",
			as_dict=True,
		)
