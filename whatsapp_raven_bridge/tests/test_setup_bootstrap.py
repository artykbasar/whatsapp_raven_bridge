from __future__ import annotations

import frappe
from frappe.exceptions import ValidationError
from frappe.tests import IntegrationTestCase
from frappe.utils import cstr
from frappe.utils.password import set_encrypted_password

from whatsapp_raven_bridge.api.setup import (
	DEFAULT_BRIDGE_SYSTEM_USER_EMAIL,
	bootstrap_from_settings_dialog,
	bootstrap_whatsapp_raven_bridge,
	ensure_default_bridge_system_user,
	get_setup_status,
	repair_bridge_system_user,
)
from whatsapp_raven_bridge.patches.fix_stale_bridge_system_user import execute as patch_fix_stale_bridge_system_user
from whatsapp_raven_bridge.patches.fix_stale_bridge_system_user_v2 import execute as patch_fix_stale_bridge_system_user_v2
from whatsapp_raven_bridge.bridge.conversation import normalize_phone_number
from whatsapp_raven_bridge.bridge.raven_destination import ensure_raven_destination
from whatsapp_raven_bridge.utils.settings import bridge_user_context


class TestSetupBootstrapAndServiceUser(IntegrationTestCase):
	PREFIX = "WARBH"
	WORKSPACE_NAME = "WARBH Workspace"
	BOT_NAME = "WARBH Bot"
	ACCOUNT_NAME = "WARBH WhatsApp Account"
	SYSTEM_USER_EMAIL = "warbh-service@example.com"
	AGENT_USER = "warbh-agent@example.com"
	NO_SETUP_USER = "warbh-no-setup@example.com"
	SYSTEM_MANAGER_USER = "warbh-system-manager@example.com"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._settings_snapshot = cls._snapshot_settings()
		cls._ensure_raven_user("Administrator")
		cls._ensure_user_with_raven_user(cls.AGENT_USER, "WARBH Agent")
		cls._ensure_non_system_manager_user(cls.NO_SETUP_USER, "WARBH No Setup")
		cls._ensure_system_manager_user(cls.SYSTEM_MANAGER_USER, "WARBH System Manager")
		cls._ensure_whatsapp_account()
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
		frappe.set_user("Administrator")
		from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message.whatsapp_message import WhatsAppMessage

		self._original_notify = WhatsAppMessage.notify

		def fake_notify(doc, data):
			doc.message_id = f"wamid.warbh.fake.{frappe.generate_hash(length=8)}"
			doc.status = "Success"
			return {"messages": [{"id": doc.message_id}]}

		WhatsAppMessage.notify = fake_notify
		self._cleanup()
		self._restore_settings(self._settings_snapshot)
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

	def test_bootstrap_creates_and_reuses_core_records(self):
		first = self._bootstrap_with_admin_member()
		self.assertTrue(first.get("settings_updated"))
		self.assertEqual(first.get("workspace"), self.WORKSPACE_NAME)
		self.assertEqual(first.get("bot"), self.BOT_NAME)
		self.assertTrue(first.get("bridge_raven_user"))
		self.assertTrue(first.get("bridge_system_user"))
		self.assertEqual(len(first.get("routes") or []), 1)

		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		self.assertEqual(cstr(settings.bridge_system_user), self.SYSTEM_USER_EMAIL)
		self.assertEqual(cstr(settings.bridge_raven_bot), self.BOT_NAME)
		self.assertEqual(cstr(settings.default_raven_workspace), self.WORKSPACE_NAME)
		self.assertTrue(int(settings.enabled))

		route_name = first["routes"][0]["route"]
		route = frappe.get_doc("WhatsApp Raven Account Route", route_name)
		self.assertEqual(route.whatsapp_account, self.ACCOUNT_NAME)
		self.assertEqual(route.conversation_strategy, "Thread Per Contact")
		self.assertTrue(route.inbox_channel)
		self.assertGreater(len(route.members or []), 0)

		second = self._bootstrap_with_admin_member()
		self.assertEqual(second.get("workspace"), first.get("workspace"))
		self.assertEqual(second.get("bot"), first.get("bot"))
		self.assertEqual(second.get("bridge_system_user"), first.get("bridge_system_user"))
		self.assertEqual(
			frappe.db.count(
				"WhatsApp Raven Account Route",
				{"whatsapp_account": self.ACCOUNT_NAME, "enabled": 1},
			),
			1,
		)

		status = get_setup_status()
		self.assertTrue(status.get("has_whatsapp_account"))
		self.assertTrue(status.get("has_raven_workspace"))
		self.assertTrue(status.get("has_raven_bot"))
		self.assertTrue(status.get("has_bridge_system_user"))
		self.assertGreaterEqual(status.get("number_of_routes", 0), 1)
		self.assertGreaterEqual(status.get("routes_using_thread_per_contact", 0), 1)

	def test_default_bridge_system_user_seed_does_not_enable_bridge(self):
		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		settings.enabled = 0
		settings.save(ignore_permissions=True)
		self._force_set_bridge_system_user(None)
		frappe.db.commit()

		ensured = ensure_default_bridge_system_user()
		user_name = ensured.get("user")
		self.assertTrue(user_name)
		self.assertTrue(frappe.db.exists("User", user_name))
		self.assertIn("created", ensured)
		self.assertIn("settings_updated", ensured)

		settings.reload()
		self.assertEqual(settings.bridge_system_user, user_name)
		self.assertEqual(int(settings.enabled), 0)

		user_name_again = ensure_default_bridge_system_user().get("user")
		self.assertEqual(user_name_again, user_name)

	def test_bootstrap_without_bridge_system_user_uses_default_service_user(self):
		self._force_set_bridge_system_user(None)
		frappe.db.commit()

		result = bootstrap_whatsapp_raven_bridge(
			workspace_name=self.WORKSPACE_NAME,
			bridge_bot_name=self.BOT_NAME,
			whatsapp_accounts=[self.ACCOUNT_NAME],
			route_members=[{"raven_user": "Administrator", "is_admin": 1, "can_reply": 1}],
		)

		self.assertEqual(result.get("bridge_system_user"), DEFAULT_BRIDGE_SYSTEM_USER_EMAIL)
		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		settings.reload()
		self.assertEqual(settings.bridge_system_user, DEFAULT_BRIDGE_SYSTEM_USER_EMAIL)
		self.assertTrue(frappe.db.exists("User", DEFAULT_BRIDGE_SYSTEM_USER_EMAIL))

	def test_bootstrap_dialog_default_system_user_resolves_before_settings_save(self):
		self._force_set_bridge_system_user(None)
		frappe.db.commit()

		result = bootstrap_from_settings_dialog(
			workspace_name=self.WORKSPACE_NAME,
			bridge_bot_name=self.BOT_NAME,
			bridge_system_user=DEFAULT_BRIDGE_SYSTEM_USER_EMAIL,
			whatsapp_account=self.ACCOUNT_NAME,
			primary_raven_user="Administrator",
			conversation_strategy="Thread Per Contact",
			channel_type="Private",
			enable_outbound_replies=1,
		)

		self.assertTrue(result.get("settings_updated"))
		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		settings.reload()
		self.assertEqual(settings.bridge_system_user, DEFAULT_BRIDGE_SYSTEM_USER_EMAIL)
		self.assertTrue(frappe.db.exists("User", settings.bridge_system_user))

	def test_bootstrap_repairs_stale_missing_bridge_system_user(self):
		stale_value = "warbh-stale-missing@example.com"
		self._force_set_bridge_system_user(stale_value)
		frappe.db.commit()

		result = bootstrap_whatsapp_raven_bridge(
			workspace_name=self.WORKSPACE_NAME,
			bridge_bot_name=self.BOT_NAME,
			whatsapp_accounts=[self.ACCOUNT_NAME],
			route_members=[{"raven_user": "Administrator", "is_admin": 1, "can_reply": 1}],
		)

		self.assertNotEqual(result.get("bridge_system_user"), stale_value)
		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		settings.reload()
		self.assertNotEqual(settings.bridge_system_user, stale_value)
		self.assertTrue(frappe.db.exists("User", settings.bridge_system_user))

	def test_ensure_default_bridge_system_user_repairs_stale_missing_setting(self):
		stale_value = "warbh-repair-missing@example.com"
		self._force_set_bridge_system_user(stale_value)
		frappe.db.commit()

		user_name = ensure_default_bridge_system_user().get("user")
		self.assertEqual(user_name, stale_value)
		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		settings.reload()
		self.assertEqual(settings.bridge_system_user, stale_value)
		self.assertTrue(frappe.db.exists("User", stale_value))

	def test_ensure_default_bridge_system_user_reenables_disabled_user(self):
		disabled_email = "warbh-disabled-service@example.com"
		if frappe.db.exists("User", disabled_email):
			user_doc = frappe.get_doc("User", disabled_email)
		else:
			user_doc = frappe.get_doc(
				{
					"doctype": "User",
					"email": disabled_email,
					"first_name": "WARBH",
					"last_name": "Disabled",
					"enabled": 1,
					"user_type": "System User",
					"send_welcome_email": 0,
				}
			).insert(ignore_permissions=True)

		user_doc.enabled = 0
		user_doc.save(ignore_permissions=True)

		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		settings.bridge_system_user = disabled_email
		settings.save(ignore_permissions=True)
		frappe.db.commit()

		user_name = ensure_default_bridge_system_user().get("user")
		self.assertEqual(user_name, disabled_email)
		self.assertTrue(frappe.db.exists("User", {"name": disabled_email, "enabled": 1}))

	def test_bridge_system_user_field_is_optional_link_user(self):
		meta = frappe.get_meta("WhatsApp Raven Bridge Settings")
		field = meta.get_field("bridge_system_user")
		self.assertEqual(field.fieldtype, "Link")
		self.assertEqual(field.options, "User")
		self.assertFalse(bool(int(field.reqd or 0)))

	def test_settings_save_with_empty_bridge_system_user_auto_creates_default(self):
		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		settings.bridge_system_user = ""
		settings.save(ignore_permissions=True)
		settings.reload()

		self.assertEqual(settings.bridge_system_user, DEFAULT_BRIDGE_SYSTEM_USER_EMAIL)
		self.assertTrue(frappe.db.exists("User", DEFAULT_BRIDGE_SYSTEM_USER_EMAIL))

	def test_settings_save_with_existing_bridge_system_user_keeps_value(self):
		ensure_default_bridge_system_user(default_email=self.SYSTEM_USER_EMAIL)
		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		settings.bridge_system_user = self.SYSTEM_USER_EMAIL
		settings.save(ignore_permissions=True)
		settings.reload()

		self.assertEqual(settings.bridge_system_user, self.SYSTEM_USER_EMAIL)

	def test_validate_links_repairs_empty_before_link_validation(self):
		self._force_set_bridge_system_user(None)
		if frappe.db.exists("User", DEFAULT_BRIDGE_SYSTEM_USER_EMAIL):
			frappe.delete_doc("User", DEFAULT_BRIDGE_SYSTEM_USER_EMAIL, force=True)
		frappe.db.commit()

		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		settings.bridge_system_user = ""
		settings._action = "save"
		settings._validate_links()

		self.assertEqual(settings.bridge_system_user, DEFAULT_BRIDGE_SYSTEM_USER_EMAIL)
		self.assertTrue(frappe.db.exists("User", DEFAULT_BRIDGE_SYSTEM_USER_EMAIL))

	def test_validate_links_repairs_stale_missing_email_before_link_validation(self):
		stale_email = "warbh-validate-links-stale@example.com"
		if frappe.db.exists("User", stale_email):
			frappe.delete_doc("User", stale_email, force=True)
		frappe.db.commit()

		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		settings.bridge_system_user = stale_email
		settings._action = "save"
		settings._validate_links()

		self.assertEqual(settings.bridge_system_user, stale_email)
		self.assertTrue(frappe.db.exists("User", stale_email))

	def test_settings_save_with_missing_email_bridge_system_user_creates_user(self):
		missing_email = "warbh-created-from-settings@example.com"
		if frappe.db.exists("User", missing_email):
			frappe.delete_doc("User", missing_email, force=True)

		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		settings.bridge_system_user = missing_email
		settings.save(ignore_permissions=True)
		settings.reload()

		self.assertEqual(settings.bridge_system_user, missing_email)
		self.assertTrue(frappe.db.exists("User", missing_email))

	def test_settings_save_with_missing_non_email_bridge_system_user_throws(self):
		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		settings.bridge_system_user = "warbh-missing-service-user"

		with self.assertRaises(ValidationError):
			settings.save(ignore_permissions=True)

	def test_settings_save_reenables_disabled_bridge_system_user(self):
		disabled_email = "warbh-disabled-on-save@example.com"
		if frappe.db.exists("User", disabled_email):
			user_doc = frappe.get_doc("User", disabled_email)
		else:
			user_doc = frappe.get_doc(
				{
					"doctype": "User",
					"email": disabled_email,
					"first_name": "WARBH",
					"last_name": "Disabled Save",
					"enabled": 1,
					"user_type": "System User",
					"send_welcome_email": 0,
				}
			).insert(ignore_permissions=True)
		user_doc.enabled = 0
		user_doc.save(ignore_permissions=True)

		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		settings.bridge_system_user = disabled_email
		settings.save(ignore_permissions=True)
		settings.reload()

		self.assertEqual(settings.bridge_system_user, disabled_email)
		self.assertTrue(frappe.db.exists("User", {"name": disabled_email, "enabled": 1}))

	def test_ensure_default_bridge_system_user_update_settings_false_does_not_save_settings(self):
		self._force_set_bridge_system_user(None)
		frappe.db.commit()

		result = ensure_default_bridge_system_user(update_settings=False)
		self.assertEqual(result.get("user"), DEFAULT_BRIDGE_SYSTEM_USER_EMAIL)
		self.assertFalse(bool(result.get("settings_updated")))

		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		settings.reload()
		self.assertFalse(cstr(settings.bridge_system_user).strip())

	def test_settings_js_has_no_create_bridge_system_user_button(self):
		js_path = frappe.get_app_path(
			"whatsapp_raven_bridge",
			"whatsapp_raven_bridge",
			"doctype",
			"whatsapp_raven_bridge_settings",
			"whatsapp_raven_bridge_settings.js",
		)
		with open(js_path, encoding="utf-8") as handle:
			js = handle.read()

		self.assertNotIn("Create / Use Default Bridge System User", js)
		self.assertIn("Check Setup Status", js)
		self.assertIn("Run Bootstrap Setup", js)
		self.assertNotIn("fieldname: \"bridge_system_user\"", js)

	def test_bootstrap_from_settings_dialog_without_bridge_system_user_uses_default_path(self):
		self._force_set_bridge_system_user(None)
		frappe.db.commit()

		result = bootstrap_from_settings_dialog(
			workspace_name=self.WORKSPACE_NAME,
			bridge_bot_name=self.BOT_NAME,
			bridge_system_user="",
			whatsapp_account=self.ACCOUNT_NAME,
			primary_raven_user="Administrator",
			conversation_strategy="Thread Per Contact",
			channel_type="Private",
			enable_outbound_replies=1,
		)
		self.assertTrue(result.get("settings_updated"))
		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		self.assertTrue(cstr(settings.bridge_system_user).strip())
		self.assertTrue(frappe.db.exists("User", settings.bridge_system_user))

	def test_bootstrap_from_settings_dialog_without_bridge_system_user_argument(self):
		self._force_set_bridge_system_user(None)
		frappe.db.commit()

		result = bootstrap_from_settings_dialog(
			workspace_name=self.WORKSPACE_NAME,
			bridge_bot_name=self.BOT_NAME,
			whatsapp_account=self.ACCOUNT_NAME,
			primary_raven_user="Administrator",
			conversation_strategy="Thread Per Contact",
			channel_type="Private",
			enable_outbound_replies=1,
		)
		self.assertTrue(result.get("settings_updated"))
		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		self.assertTrue(cstr(settings.bridge_system_user).strip())
		self.assertTrue(frappe.db.exists("User", settings.bridge_system_user))

	def test_bootstrap_dialog_with_missing_default_service_user_and_empty_settings(self):
		self._force_set_bridge_system_user(None)
		if frappe.db.exists("User", DEFAULT_BRIDGE_SYSTEM_USER_EMAIL):
			frappe.delete_doc("User", DEFAULT_BRIDGE_SYSTEM_USER_EMAIL, force=True)
		frappe.db.commit()

		result = bootstrap_from_settings_dialog(
			workspace_name=self.WORKSPACE_NAME,
			bridge_bot_name=self.BOT_NAME,
			whatsapp_account=self.ACCOUNT_NAME,
			primary_raven_user="Administrator",
			conversation_strategy="Thread Per Contact",
			channel_type="Private",
			enable_outbound_replies=1,
		)

		self.assertTrue(result.get("settings_updated"))
		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		self.assertEqual(settings.bridge_system_user, DEFAULT_BRIDGE_SYSTEM_USER_EMAIL)
		self.assertTrue(frappe.db.exists("User", DEFAULT_BRIDGE_SYSTEM_USER_EMAIL))

	def test_bootstrap_runs_as_setup_actor_without_switching_session_user(self):
		self._force_set_bridge_system_user(None)
		if frappe.db.exists("User", DEFAULT_BRIDGE_SYSTEM_USER_EMAIL):
			frappe.delete_doc("User", DEFAULT_BRIDGE_SYSTEM_USER_EMAIL, force=True)
		frappe.db.commit()

		current_user = frappe.session.user
		suffix = frappe.generate_hash(length=6)
		workspace_name = f"{self.WORKSPACE_NAME} Actor {suffix}"
		bot_name = f"{self.BOT_NAME} Actor {suffix}"

		result = bootstrap_from_settings_dialog(
			workspace_name=workspace_name,
			bridge_bot_name=bot_name,
			whatsapp_account=self.ACCOUNT_NAME,
			primary_raven_user="Administrator",
			conversation_strategy="Thread Per Contact",
			channel_type="Private",
			enable_outbound_replies=1,
		)

		self.assertEqual(frappe.session.user, current_user)
		self.assertEqual(result.get("setup_actor"), current_user)

		workspace = frappe.get_doc("Raven Workspace", result.get("workspace"))
		route = frappe.get_doc("WhatsApp Raven Account Route", result["routes"][0]["route"])
		self.assertEqual(workspace.owner, current_user)
		self.assertEqual(route.owner, current_user)
		if route.inbox_channel:
			channel = frappe.get_doc("Raven Channel", route.inbox_channel)
			self.assertEqual(channel.owner, current_user)

	def test_patch_repairs_empty_bridge_system_user_single(self):
		self._force_set_bridge_system_user("")
		if frappe.db.exists("User", DEFAULT_BRIDGE_SYSTEM_USER_EMAIL):
			frappe.delete_doc("User", DEFAULT_BRIDGE_SYSTEM_USER_EMAIL, force=True)
		frappe.db.commit()

		patch_fix_stale_bridge_system_user()
		self.assertTrue(frappe.db.exists("User", DEFAULT_BRIDGE_SYSTEM_USER_EMAIL))
		self.assertEqual(
			frappe.db.get_single_value("WhatsApp Raven Bridge Settings", "bridge_system_user"),
			DEFAULT_BRIDGE_SYSTEM_USER_EMAIL,
		)

	def test_patch_repairs_missing_email_bridge_system_user_single(self):
		missing_email = "warbh-patch-missing-email@example.com"
		self._force_set_bridge_system_user(missing_email)
		if frappe.db.exists("User", missing_email):
			frappe.delete_doc("User", missing_email, force=True)
		frappe.db.commit()

		patch_fix_stale_bridge_system_user()
		self.assertTrue(frappe.db.exists("User", missing_email))
		self.assertEqual(
			frappe.db.get_single_value("WhatsApp Raven Bridge Settings", "bridge_system_user"),
			missing_email,
		)

	def test_patch_reenables_disabled_bridge_system_user(self):
		disabled_email = "warbh-patch-disabled@example.com"
		if frappe.db.exists("User", disabled_email):
			user_doc = frappe.get_doc("User", disabled_email)
		else:
			user_doc = frappe.get_doc(
				{
					"doctype": "User",
					"email": disabled_email,
					"first_name": "WARBH",
					"last_name": "Patch Disabled",
					"enabled": 1,
					"user_type": "System User",
					"send_welcome_email": 0,
				}
			).insert(ignore_permissions=True)
		user_doc.enabled = 0
		user_doc.save(ignore_permissions=True)

		self._force_set_bridge_system_user(disabled_email)
		frappe.db.commit()

		patch_fix_stale_bridge_system_user()
		self.assertTrue(frappe.db.exists("User", {"name": disabled_email, "enabled": 1}))
		self.assertEqual(
			frappe.db.get_single_value("WhatsApp Raven Bridge Settings", "bridge_system_user"),
			disabled_email,
		)

	def test_patch_falls_back_for_non_email_stale_bridge_system_user(self):
		stale_value = "warbh_non_email_stale_value"
		self._force_set_bridge_system_user(stale_value)
		if frappe.db.exists("User", DEFAULT_BRIDGE_SYSTEM_USER_EMAIL):
			frappe.delete_doc("User", DEFAULT_BRIDGE_SYSTEM_USER_EMAIL, force=True)
		frappe.db.commit()

		patch_fix_stale_bridge_system_user()
		self.assertTrue(frappe.db.exists("User", DEFAULT_BRIDGE_SYSTEM_USER_EMAIL))
		self.assertEqual(
			frappe.db.get_single_value("WhatsApp Raven Bridge Settings", "bridge_system_user"),
			DEFAULT_BRIDGE_SYSTEM_USER_EMAIL,
		)

	def test_patch_v2_repairs_stale_single_even_after_old_patch(self):
		# Simulate old patch having already run.
		patch_fix_stale_bridge_system_user()

		stale_email = "warbh-v2-stale@example.com"
		self._force_set_bridge_system_user(stale_email)
		if frappe.db.exists("User", stale_email):
			frappe.delete_doc("User", stale_email, force=True)
		frappe.db.commit()

		patch_fix_stale_bridge_system_user_v2()
		self.assertTrue(frappe.db.exists("User", stale_email))
		self.assertEqual(
			frappe.db.get_single_value("WhatsApp Raven Bridge Settings", "bridge_system_user"),
			stale_email,
		)

	def test_repair_bridge_system_user_fixes_stale_single(self):
		stale_email = "warbh-repair-api-stale@example.com"
		self._force_set_bridge_system_user(stale_email)
		if frappe.db.exists("User", stale_email):
			frappe.delete_doc("User", stale_email, force=True)
		frappe.db.commit()

		result = repair_bridge_system_user()
		self.assertEqual(result.get("settings_value_before"), stale_email)
		self.assertEqual(result.get("settings_value_after"), stale_email)
		self.assertEqual(result.get("user"), stale_email)
		self.assertTrue(frappe.db.exists("User", stale_email))

	def test_bootstrap_from_settings_dialog_with_existing_bridge_system_user(self):
		ensured = ensure_default_bridge_system_user()
		service_user = ensured.get("user")
		self.assertTrue(service_user)

		result = bootstrap_from_settings_dialog(
			workspace_name=self.WORKSPACE_NAME,
			bridge_bot_name=self.BOT_NAME,
			bridge_system_user=service_user,
			whatsapp_account=self.ACCOUNT_NAME,
			primary_raven_user="Administrator",
			conversation_strategy="Thread Per Contact",
			channel_type="Private",
			enable_outbound_replies=1,
		)
		self.assertTrue(result.get("settings_updated"))
		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		self.assertEqual(settings.bridge_system_user, service_user)

	def test_setup_status_warns_for_missing_configured_bridge_system_user(self):
		self._force_set_bridge_system_user("warbh-status-missing@example.com")
		frappe.db.commit()

		status = get_setup_status()
		self.assertFalse(status.get("has_bridge_system_user"))
		self.assertTrue(
			any(
				"Configured Bridge System User is missing. Run migrate or repair_bridge_system_user."
				in cstr(warning)
				for warning in status.get("warnings", [])
			)
		)

	def test_settings_controller_uses_internal_helpers_not_whitelisted_wrapper(self):
		controller_path = frappe.get_app_path(
			"whatsapp_raven_bridge",
			"whatsapp_raven_bridge",
			"doctype",
			"whatsapp_raven_bridge_settings",
			"whatsapp_raven_bridge_settings.py",
		)
		with open(controller_path, encoding="utf-8") as handle:
			content = handle.read()

		self.assertIn("_ensure_default_bridge_system_user_state", content)
		self.assertIn("_ensure_bridge_system_user_with_state", content)
		self.assertNotIn("ensure_default_bridge_system_user(", content)

	def test_bridge_user_context_restores_original_user(self):
		self._bootstrap_with_admin_member()
		original_user = frappe.session.user

		with bridge_user_context() as active_user:
			self.assertEqual(active_user, self.SYSTEM_USER_EMAIL)
			self.assertEqual(frappe.session.user, self.SYSTEM_USER_EMAIL)

		self.assertEqual(frappe.session.user, original_user)

	def test_guest_inbound_memberships_not_owned_by_guest(self):
		result = self._bootstrap_with_admin_member()
		route_name = result["routes"][0]["route"]
		route = frappe.get_doc("WhatsApp Raven Account Route", route_name)
		bridge_user = frappe.get_single("WhatsApp Raven Bridge Settings").bridge_raven_user
		member_users = {"Administrator", bridge_user}

		current_user = frappe.session.user
		try:
			frappe.set_user("Guest")
			incoming = self._insert_incoming_text(
				phone="+447755330001",
				message_id="wamid.warbh.guest.001",
				message="guest membership recreation",
			)
		finally:
			frappe.set_user(current_user)

		conversation_name = frappe.db.get_value(
			"WhatsApp Raven Conversation",
			{"phone_number": normalize_phone_number("+447755330001"), "whatsapp_account": self.ACCOUNT_NAME},
			"name",
		)
		self.assertTrue(conversation_name)
		self.assertTrue(frappe.db.exists("WhatsApp Raven Message Link", {"whatsapp_message": incoming.name}))

		route.reload()
		self.assertEqual(
			frappe.db.count(
				"Raven Channel Member",
				{
					"channel_id": route.inbox_channel,
					"user_id": ["in", list(member_users)],
					"owner": "Guest",
				},
			),
			0,
		)
		self.assertEqual(
			frappe.db.count(
				"Raven Workspace Member",
				{
					"workspace": route.raven_workspace,
					"user": ["in", list(member_users)],
					"owner": "Guest",
				},
			),
			0,
		)

		for user_id in member_users:
			channel_member = frappe.get_doc(
				"Raven Channel Member",
				frappe.db.get_value(
					"Raven Channel Member",
					{"channel_id": route.inbox_channel, "user_id": user_id},
					"name",
				),
			)
			workspace_member = frappe.get_doc(
				"Raven Workspace Member",
				frappe.db.get_value(
					"Raven Workspace Member",
					{"workspace": route.raven_workspace, "user": user_id},
					"name",
				),
			)
			self.assertTrue(channel_member.name)
			self.assertTrue(workspace_member.name)
			self.assertNotEqual(channel_member.owner, "Guest")
			self.assertNotEqual(workspace_member.owner, "Guest")

	def test_inbound_mirrored_raven_message_uses_bridge_raven_user(self):
		self._bootstrap_with_admin_member()
		incoming = self._insert_incoming_text(
			phone="+447755330002",
			message_id="wamid.warbh.inbound.001",
			message="bridge sender check",
		)
		link_name = frappe.db.get_value(
			"WhatsApp Raven Message Link",
			{"whatsapp_message": incoming.name},
			"name",
		)
		self.assertTrue(link_name)
		link = frappe.get_doc("WhatsApp Raven Message Link", link_name)
		raven_message = frappe.get_doc("Raven Message", link.raven_message)
		settings = frappe.get_single("WhatsApp Raven Bridge Settings")
		self.assertEqual(raven_message.bot, settings.bridge_raven_user)
		self.assertTrue(raven_message.is_bot_message)

	def test_outbound_permission_uses_message_owner_not_bridge_system_user(self):
		result = bootstrap_whatsapp_raven_bridge(
			workspace_name=self.WORKSPACE_NAME,
			bridge_bot_name=self.BOT_NAME,
			bridge_system_user=self.SYSTEM_USER_EMAIL,
			whatsapp_accounts=[self.ACCOUNT_NAME],
			route_members=[{"raven_user": self.AGENT_USER, "is_admin": 0, "can_reply": 1}],
			enable_outbound_replies=1,
			enable_start_conversation=0,
			conversation_strategy="Thread Per Contact",
			channel_type="Private",
		)
		route = frappe.get_doc("WhatsApp Raven Account Route", result["routes"][0]["route"])
		self.assertFalse(
			frappe.db.exists(
				"WhatsApp Raven Account Route Member",
				{"parent": route.name, "raven_user": frappe.get_single("WhatsApp Raven Bridge Settings").bridge_raven_user},
			)
		)

		incoming = self._insert_incoming_text(
			phone="+447755330003",
			message_id="wamid.warbh.outbound.owner.001",
			message="owner permission seed",
		)
		link_name = frappe.db.get_value("WhatsApp Raven Message Link", {"whatsapp_message": incoming.name}, "name")
		self.assertTrue(link_name)

		conversation_name = frappe.db.get_value(
			"WhatsApp Raven Conversation",
			{"phone_number": normalize_phone_number("+447755330003"), "whatsapp_account": self.ACCOUNT_NAME},
			"name",
		)
		conversation = frappe.get_doc("WhatsApp Raven Conversation", conversation_name)
		thread_channel = ensure_raven_destination(conversation)

		current_user = frappe.session.user
		try:
			frappe.set_user(self.AGENT_USER)
			raven_message = frappe.get_doc(
				{
					"doctype": "Raven Message",
					"channel_id": thread_channel.name,
					"message_type": "Text",
					"text": "<p>agent outbound reply</p>",
					"is_bot_message": 0,
				}
			).insert(ignore_permissions=True)
		finally:
			frappe.set_user(current_user)

		self.assertTrue(
			frappe.db.exists("WhatsApp Raven Message Link", {"raven_message": raven_message.name})
		)
		self.assertTrue(
			frappe.db.exists(
				"WhatsApp Message",
				{"reference_doctype": "Raven Message", "reference_name": raven_message.name},
			)
		)

	def test_bootstrap_permission_denied_for_non_system_manager(self):
		current_user = frappe.session.user
		try:
			frappe.set_user(self.NO_SETUP_USER)
			with self.assertRaises(frappe.PermissionError):
				bootstrap_whatsapp_raven_bridge(
					workspace_name=self.WORKSPACE_NAME,
					bridge_bot_name=self.BOT_NAME,
					whatsapp_accounts=[self.ACCOUNT_NAME],
				)
		finally:
			frappe.set_user(current_user)

	def test_bootstrap_permission_allowed_for_administrator(self):
		current_user = frappe.session.user
		try:
			frappe.set_user("Administrator")
			result = bootstrap_whatsapp_raven_bridge(
				workspace_name=self.WORKSPACE_NAME,
				bridge_bot_name=self.BOT_NAME,
				bridge_system_user=self.SYSTEM_USER_EMAIL,
				whatsapp_accounts=[self.ACCOUNT_NAME],
				route_members=[{"raven_user": "Administrator", "is_admin": 1, "can_reply": 1}],
			)
			self.assertTrue(result.get("settings_updated"))
		finally:
			frappe.set_user(current_user)

	def test_bootstrap_permission_allowed_for_system_manager(self):
		current_user = frappe.session.user
		try:
			frappe.set_user(self.SYSTEM_MANAGER_USER)
			result = bootstrap_whatsapp_raven_bridge(
				workspace_name=self.WORKSPACE_NAME,
				bridge_bot_name=self.BOT_NAME,
				bridge_system_user=self.SYSTEM_USER_EMAIL,
				whatsapp_accounts=[self.ACCOUNT_NAME],
				route_members=[{"raven_user": "Administrator", "is_admin": 1, "can_reply": 1}],
			)
			self.assertTrue(result.get("settings_updated"))
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
			"default_whatsapp_account": settings.default_whatsapp_account,
			"conversation_strategy": settings.conversation_strategy,
			"enable_outbound_replies": settings.enable_outbound_replies,
			"enable_start_conversation": settings.enable_start_conversation,
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
	def _ensure_user_with_raven_user(cls, user_id, full_name):
		if frappe.db.exists("User", user_id):
			user_doc = frappe.get_doc("User", user_id)
			if not user_doc.enabled:
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
		if "Raven User" not in [row.role for row in (user_doc.roles or [])]:
			user_doc.append("roles", {"role": "Raven User"})
		user_doc.save(ignore_permissions=True)

		raven_user = frappe.db.get_value("Raven User", {"user": user_doc.name}, "name")
		if not raven_user:
			raven_user = frappe.get_doc(
				{
					"doctype": "Raven User",
					"type": "User",
					"user": user_doc.name,
					"full_name": full_name,
					"first_name": full_name.split()[0],
					"enabled": 1,
				}
			).insert(ignore_permissions=True).name
		return user_doc.name, raven_user

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
			role = user_doc.roles[idx]
			if role.role in {"System Manager", "Raven User"}:
				user_doc.roles.pop(idx)

		user_doc.save(ignore_permissions=True)

	@classmethod
	def _ensure_system_manager_user(cls, user_id, full_name):
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

		if "System Manager" not in [row.role for row in (user_doc.roles or [])]:
			user_doc.append("roles", {"role": "System Manager"})
		user_doc.save(ignore_permissions=True)

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
					"phone_id": "warbh_phone_id",
					"business_id": "warbh_business_id",
					"app_id": "warbh_app_id",
					"webhook_verify_token": "warbh_verify_token",
				}
			).insert(ignore_permissions=True)
		set_encrypted_password("WhatsApp Account", cls.ACCOUNT_NAME, "warbh-token", "token")

	def _bootstrap_with_admin_member(self):
		return bootstrap_whatsapp_raven_bridge(
			workspace_name=self.WORKSPACE_NAME,
			bridge_bot_name=self.BOT_NAME,
			bridge_system_user=self.SYSTEM_USER_EMAIL,
			whatsapp_accounts=[self.ACCOUNT_NAME],
			route_members=[{"raven_user": "Administrator", "is_admin": 1, "can_reply": 1}],
			enable_outbound_replies=1,
			enable_start_conversation=0,
			conversation_strategy="Thread Per Contact",
			channel_type="Private",
		)

	def _insert_incoming_text(self, *, phone, message_id, message):
		return frappe.get_doc(
			{
				"doctype": "WhatsApp Message",
				"type": "Incoming",
				"content_type": "text",
				"message_type": "Manual",
				"from": phone,
				"profile_name": f"{self.PREFIX} Profile",
				"message": message,
				"message_id": message_id,
				"whatsapp_account": self.ACCOUNT_NAME,
			}
		).insert(ignore_permissions=True)

	def _force_set_bridge_system_user(self, value):
		frappe.db.set_value(
			"WhatsApp Raven Bridge Settings",
			"WhatsApp Raven Bridge Settings",
			"bridge_system_user",
			value,
			update_modified=False,
		)

	def _cleanup(self):
		conversation_names = set(
			frappe.get_all(
				"WhatsApp Raven Conversation",
				filters=[["phone_number", "like", "44775533%"]],
				pluck="name",
			)
		)
		parent_message_names = set()
		if conversation_names:
			parent_message_names = set(
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
			filters=[["whatsapp_message_id", "like", "wamid.warbh.%"]],
			pluck="name",
		):
			frappe.delete_doc("WhatsApp Raven Message Link", name, force=True)

		for name in frappe.get_all(
			"WhatsApp Message",
			filters=[["message_id", "like", "wamid.warbh.%"]],
			pluck="name",
		):
			frappe.delete_doc("WhatsApp Message", name, force=True)

		for name in frappe.get_all(
			"Raven Message",
			filters=[["text", "like", "%warbh%"]],
			pluck="name",
		):
			frappe.delete_doc("Raven Message", name, force=True)

		for name in frappe.get_all(
			"WhatsApp Raven Conversation",
			filters=[["phone_number", "like", "44775533%"]],
			pluck="name",
		):
			frappe.delete_doc("WhatsApp Raven Conversation", name, force=True)

		for name in frappe.get_all(
			"WhatsApp Raven Account Route",
			filters={"whatsapp_account": self.ACCOUNT_NAME},
			pluck="name",
		):
			if frappe.db.exists("WhatsApp Raven Account Route", name):
				frappe.delete_doc("WhatsApp Raven Account Route", name, force=True)

		for name in frappe.get_all(
			"Raven Channel",
			filters=[
				["workspace", "=", self.WORKSPACE_NAME],
				["channel_name", "like", "whatsapp-inbox-warbh%"],
			],
			pluck="name",
		):
			if frappe.db.exists("Raven Channel", name):
				frappe.delete_doc("Raven Channel", name, force=True)

		for name in frappe.get_all(
			"Raven Channel",
			filters=[
				["workspace", "=", self.WORKSPACE_NAME],
				["channel_name", "like", "whatsapp-44775533%"],
			],
			pluck="name",
		):
			if frappe.db.exists("Raven Channel", name):
				frappe.delete_doc("Raven Channel", name, force=True)

		# Thread channel names equal parent Raven Message names.
		for name in parent_message_names:
			if frappe.db.exists("Raven Channel", name):
				frappe.delete_doc("Raven Channel", name, force=True)
			if frappe.db.exists("Raven Message", name):
				frappe.delete_doc("Raven Message", name, force=True)

		# Keep shared fixtures (workspace, bot, service user) for class-level reuse.
