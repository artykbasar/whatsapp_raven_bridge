"""Setup/bootstrap APIs for WhatsApp Raven Bridge production hardening."""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, cstr

from whatsapp_raven_bridge.bridge.account_route import (
	ensure_route_memberships,
	get_or_create_inbox_channel,
)
from whatsapp_raven_bridge.utils.settings import bridge_user_context, get_settings

DEFAULT_BRIDGE_SYSTEM_USER_EMAIL = "whatsapp.bridge@example.com"


def _ensure_default_bridge_system_user_state(
	default_email: str | None = None, update_settings: bool = True
) -> frappe._dict:
	"""Internal helper to ensure a valid bridge system user and settings link."""
	default_email = cstr(default_email or DEFAULT_BRIDGE_SYSTEM_USER_EMAIL).strip().lower()
	result = frappe._dict({"user": None, "created": False, "settings_updated": False})
	if not default_email:
		return result

	try:
		if not frappe.db.exists("DocType", "WhatsApp Raven Bridge Settings"):
			return result
		if not frappe.db.table_exists("User"):
			return result
	except Exception:
		return result

	settings = get_settings()
	if not settings:
		return result

	configured = cstr(settings.get("bridge_system_user") or "").strip()
	candidates = []
	if configured:
		candidates.append(configured)
	if default_email and default_email not in candidates:
		candidates.append(default_email)

	user_info = None
	for candidate in candidates:
		user_info = _ensure_bridge_system_user_with_state(candidate)
		if user_info and user_info.user:
			break

	if not user_info or not user_info.user:
		return result

	result.user = user_info.user
	result.created = bool(user_info.created)

	if update_settings:
		if settings.bridge_system_user != result.user:
			settings.bridge_system_user = result.user
			settings.save(ignore_permissions=True)
			result.settings_updated = True

	return result


@frappe.whitelist()
def ensure_default_bridge_system_user(
	default_email: str | None = None, update_settings: bool = True
) -> dict[str, Any]:
	"""Create/reuse default Bridge System User and persist a valid Link in settings."""
	_require_setup_permission()
	return dict(
		_ensure_default_bridge_system_user_state(
			default_email=default_email,
			update_settings=bool(cint(update_settings)),
		)
	)


def _repair_bridge_system_user_single_value(default_email: str | None = None) -> frappe._dict:
	"""Repair stale/missing bridge_system_user single value without Settings.save()."""
	default_email = cstr(default_email or DEFAULT_BRIDGE_SYSTEM_USER_EMAIL).strip().lower()
	result = frappe._dict(
		{
			"user": None,
			"created": False,
			"settings_value_before": "",
			"settings_value_after": "",
		}
	)

	if not default_email:
		return result

	try:
		if not frappe.db.exists("DocType", "WhatsApp Raven Bridge Settings"):
			return result
		if not frappe.db.table_exists("User"):
			return result
	except Exception:
		return result

	current_value = cstr(
		frappe.db.get_single_value("WhatsApp Raven Bridge Settings", "bridge_system_user")
	).strip()
	result.settings_value_before = current_value

	if not current_value:
		target_identifier = default_email
	elif frappe.db.exists("User", current_value):
		target_identifier = current_value
	elif "@" in current_value:
		target_identifier = current_value
	else:
		target_identifier = default_email

	user_info = _ensure_bridge_system_user_with_state(target_identifier)
	user_name = cstr((user_info or {}).get("user") or "").strip()

	if not user_name and target_identifier != default_email:
		user_info = _ensure_bridge_system_user_with_state(default_email)
		user_name = cstr((user_info or {}).get("user") or "").strip()

	if not user_name:
		return result

	result.user = user_name
	result.created = bool((user_info or {}).get("created"))

	if current_value != user_name:
		frappe.db.set_single_value("WhatsApp Raven Bridge Settings", "bridge_system_user", user_name)

	result.settings_value_after = cstr(
		frappe.db.get_single_value("WhatsApp Raven Bridge Settings", "bridge_system_user")
	).strip()
	return result


@frappe.whitelist()
def repair_bridge_system_user(default_email: str | None = None) -> dict[str, Any]:
	"""Admin utility to repair stale/missing bridge_system_user in Singles."""
	_require_setup_permission()
	return dict(_repair_bridge_system_user_single_value(default_email=default_email))


def _require_setup_permission() -> None:
	"""Allow bootstrap operations only for privileged setup users."""
	if frappe.session.user == "Administrator":
		return
	frappe.only_for("System Manager")


@frappe.whitelist()
def bootstrap_whatsapp_raven_bridge(
	workspace_name: str | None = None,
	bridge_bot_name: str | None = None,
	bridge_system_user: str | None = None,
	whatsapp_accounts: list[str] | str | None = None,
	route_members: list[dict[str, Any] | str] | str | None = None,
	enable_outbound_replies: int = 1,
	enable_start_conversation: int = 0,
	conversation_strategy: str = "Thread Per Contact",
	channel_type: str = "Private",
) -> dict[str, Any]:
	"""Create or reuse minimal bridge configuration for production setup."""
	_require_setup_permission()

	summary = frappe._dict(
		{
			"settings_updated": False,
			"workspace": None,
			"bot": None,
			"bridge_raven_user": None,
			"bridge_system_user": None,
			"setup_actor": frappe.session.user,
			"routes": [],
			"warnings": [],
			"next_manual_steps": [],
		}
	)

	workspace_name = cstr(workspace_name or "WhatsApp Bridge Workspace").strip()
	bridge_bot_name = cstr(bridge_bot_name or "WhatsApp Bridge Bot").strip()
	conversation_strategy = cstr(conversation_strategy or "Thread Per Contact").strip()
	channel_type = cstr(channel_type or "Private").strip()

	if conversation_strategy not in ("Thread Per Contact", "Channel Per Contact"):
		frappe.throw(_("Invalid conversation_strategy: {0}").format(conversation_strategy))

	if channel_type not in ("Private", "Public", "Open"):
		frappe.throw(_("Invalid channel_type: {0}").format(channel_type))

	accounts = _coerce_list(whatsapp_accounts)
	member_inputs = _coerce_list(route_members)
	settings = get_settings()
	if not settings:
		frappe.throw(_("WhatsApp Raven Bridge Settings is missing. Please run migrate first."))

	bridge_system_user_arg = cstr(bridge_system_user or "").strip() or None
	configured_system_user = cstr(settings.get("bridge_system_user") or "").strip()
	if bridge_system_user_arg:
		desired_bridge_system_user = bridge_system_user_arg
	elif configured_system_user and _is_enabled_user(configured_system_user):
		desired_bridge_system_user = configured_system_user
	else:
		desired_bridge_system_user = DEFAULT_BRIDGE_SYSTEM_USER_EMAIL

	system_user_name = _ensure_bridge_system_user(desired_bridge_system_user)
	if not system_user_name and desired_bridge_system_user != DEFAULT_BRIDGE_SYSTEM_USER_EMAIL:
		summary.warnings.append(
			_("Could not resolve configured Bridge System User {0}; using default {1}.").format(
				desired_bridge_system_user, DEFAULT_BRIDGE_SYSTEM_USER_EMAIL
			)
		)
		system_user_name = _ensure_bridge_system_user(DEFAULT_BRIDGE_SYSTEM_USER_EMAIL)

	if not system_user_name:
		frappe.throw(_("Could not create or resolve Bridge System User."))

	workspace, workspace_state = _ensure_workspace(workspace_name)
	bot, bot_state = _ensure_bot(bridge_bot_name)

	summary.workspace = workspace.name
	summary.bot = bot.name
	summary.bridge_raven_user = bot.raven_user
	summary.bridge_system_user = system_user_name

	if not bot.raven_user:
		frappe.throw(_("Raven Bot {0} does not have a linked Raven User.").format(bot.name))

	settings.enabled = 1
	settings.bridge_system_user = system_user_name
	settings.bridge_raven_bot = bot.name
	settings.bridge_raven_user = bot.raven_user
	settings.default_raven_workspace = workspace.name
	settings.default_channel_type = channel_type
	settings.conversation_strategy = conversation_strategy
	settings.enable_outbound_replies = cint(enable_outbound_replies)
	settings.enable_start_conversation = cint(enable_start_conversation)
	settings.save(ignore_permissions=True)
	summary.settings_updated = True
	summary.bridge_system_user = system_user_name

	if not accounts:
		summary.warnings.append(
			_("No WhatsApp Accounts were provided. Configure a WhatsApp Account in frappe_whatsapp first.")
		)
		summary.next_manual_steps.extend(
			[
				_("Create/configure WhatsApp Account in frappe_whatsapp."),
				_("Run bootstrap again with whatsapp_accounts."),
			]
		)
		return summary

	members = _resolve_route_members(member_inputs, summary.warnings)
	if not members:
		default_member = _default_route_member()
		if default_member:
			members = [default_member]
			summary.warnings.append(
				_("route_members was empty. Defaulted to {0}.").format(default_member.get("raven_user"))
			)
		else:
			summary.warnings.append(
				_("No route_members provided and no default Raven User could be resolved.")
			)

	for account_name in accounts:
		account_name = cstr(account_name).strip()
		if not account_name:
			continue
		if not frappe.db.exists("WhatsApp Account", account_name):
			summary.warnings.append(_("WhatsApp Account not found: {0}").format(account_name))
			continue

		route, route_state = _create_or_update_route(
			whatsapp_account=account_name,
			workspace=workspace.name,
			channel_type=channel_type,
			conversation_strategy=conversation_strategy,
			members=members,
			use_bridge_context=False,
		)

		inbox_channel = get_or_create_inbox_channel(route, use_bridge_context=False)
		ensure_route_memberships(route, use_bridge_context=False)

		summary.routes.append(
			{
				"route": route.name,
				"whatsapp_account": account_name,
				"status": route_state,
				"inbox_channel": inbox_channel.name if inbox_channel else None,
				"conversation_strategy": route.conversation_strategy,
				"member_count": len(route.members or []),
			}
		)

	if not summary.routes:
		summary.next_manual_steps.append(_("Create at least one valid WhatsApp Account and re-run bootstrap."))
	else:
		summary.next_manual_steps.extend(
			[
				_("Confirm Meta webhook points to frappe_whatsapp webhook endpoint."),
				_("Send a test inbound WhatsApp text to verify thread creation and sync."),
			]
		)

	# Keep lints quiet while still useful in responses.
	summary.meta = {
		"workspace_status": workspace_state,
		"bot_status": bot_state,
	}

	return summary


@frappe.whitelist()
def bootstrap_from_settings_dialog(
	workspace_name: str | None = None,
	bridge_bot_name: str | None = None,
	bridge_system_user: str | None = None,
	whatsapp_account: str | None = None,
	primary_raven_user: str | None = None,
	conversation_strategy: str = "Thread Per Contact",
	channel_type: str = "Private",
	enable_outbound_replies: int = 1,
	enable_start_conversation: int = 0,
	can_reply: int = 1,
	is_admin: int = 1,
) -> dict[str, Any]:
	"""Dialog-friendly wrapper around bootstrap_whatsapp_raven_bridge."""
	_require_setup_permission()

	bridge_system_user_value = cstr(bridge_system_user).strip() or None

	accounts: list[str] = []
	if cstr(whatsapp_account).strip():
		accounts = [cstr(whatsapp_account).strip()]

	route_members: list[dict[str, Any]] = []
	if cstr(primary_raven_user).strip():
		route_members.append(
			{
				"raven_user": cstr(primary_raven_user).strip(),
				"is_admin": cint(is_admin),
				"can_reply": cint(can_reply),
			}
		)

	return bootstrap_whatsapp_raven_bridge(
		workspace_name=workspace_name,
		bridge_bot_name=bridge_bot_name,
		bridge_system_user=bridge_system_user_value,
		whatsapp_accounts=accounts,
		route_members=route_members,
		enable_outbound_replies=enable_outbound_replies,
		enable_start_conversation=enable_start_conversation,
		conversation_strategy=conversation_strategy,
		channel_type=channel_type,
	)


@frappe.whitelist()
def get_setup_status() -> dict[str, Any]:
	"""Return setup status summary for bridge configuration checks."""
	status = frappe._dict(
		{
			"has_whatsapp_account": False,
			"has_raven_workspace": False,
			"has_raven_bot": False,
			"has_bridge_system_user": False,
			"settings_enabled": False,
			"enable_outbound_replies": False,
			"number_of_routes": 0,
			"routes_missing_members": [],
			"routes_using_thread_per_contact": 0,
			"warnings": [],
		}
	)

	settings = get_settings()
	status.has_whatsapp_account = bool(frappe.db.exists("WhatsApp Account", {}))
	status.has_raven_workspace = bool(frappe.db.exists("Raven Workspace", {}))
	status.has_raven_bot = bool(frappe.db.exists("Raven Bot", {}))

	if settings:
		status.settings_enabled = bool(cint(settings.enabled))
		status.enable_outbound_replies = bool(cint(settings.enable_outbound_replies))
		system_user = cstr(settings.get("bridge_system_user") or "").strip()
		status.has_bridge_system_user = bool(system_user and _is_enabled_user(system_user))
		if system_user and not status.has_bridge_system_user:
			status.warnings.append(
				_("Configured Bridge System User is missing. Run migrate or repair_bridge_system_user.")
			)
	else:
		status.warnings.append(_("WhatsApp Raven Bridge Settings is missing."))

	routes = frappe.get_all(
		"WhatsApp Raven Account Route",
		fields=["name", "whatsapp_account", "conversation_strategy"],
	)
	status.number_of_routes = len(routes)

	for route in routes:
		member_count = frappe.db.count(
			"WhatsApp Raven Account Route Member",
			{"parenttype": "WhatsApp Raven Account Route", "parent": route.name},
		)
		if member_count == 0:
			status.routes_missing_members.append(route.name)
		if cstr(route.conversation_strategy) == "Thread Per Contact":
			status.routes_using_thread_per_contact += 1

	if not status.has_whatsapp_account:
		status.warnings.append(_("No WhatsApp Account found."))
	if not status.has_raven_workspace:
		status.warnings.append(_("No Raven Workspace found."))
	if not status.has_raven_bot:
		status.warnings.append(_("No Raven Bot found."))
	if not cstr(settings.get("bridge_system_user") if settings else "").strip():
		status.warnings.append(
			_(
				"Bridge System User is not configured. Save settings empty to auto-create it, or run bootstrap."
			)
		)
	if status.number_of_routes == 0:
		status.warnings.append(_("No WhatsApp Raven Account Route records found."))
	if status.routes_missing_members:
		status.warnings.append(_("Some routes have no assigned members."))

	return status


def _coerce_list(value: Any) -> list[Any]:
	if value is None:
		return []
	if isinstance(value, list):
		return value
	if isinstance(value, tuple):
		return list(value)
	if isinstance(value, str):
		text = value.strip()
		if not text:
			return []
		try:
			parsed = frappe.parse_json(text)
			if isinstance(parsed, list):
				return parsed
			if isinstance(parsed, dict):
				return [parsed]
		except Exception:
			pass
		return [item.strip() for item in text.split(",") if item.strip()]
	if isinstance(value, dict):
		return [value]
	return [value]


def _ensure_bridge_system_user(user_identifier: str | None) -> str | None:
	user_info = _ensure_bridge_system_user_with_state(user_identifier)
	return cstr(user_info.get("user") or "").strip() or None


def _ensure_bridge_system_user_with_state(user_identifier: str | None) -> frappe._dict:
	user_identifier = cstr(user_identifier or "").strip()
	result = frappe._dict({"user": None, "created": False})
	if not user_identifier:
		return result

	existing_user_name = None
	if frappe.db.exists("User", user_identifier):
		existing_user_name = user_identifier
	elif "@" in user_identifier:
		existing_user_name = frappe.db.get_value("User", {"email": user_identifier}, "name")
	else:
		existing_user_name = frappe.db.get_value("User", {"name": user_identifier}, "name")

	if existing_user_name:
		user_doc = frappe.get_doc("User", existing_user_name)
		if not cint(user_doc.enabled):
			user_doc.enabled = 1
			user_doc.save(ignore_permissions=True)
		result.user = user_doc.name
		return result

	email = user_identifier if "@" in user_identifier else f"{user_identifier}@local.invalid"

	try:
		user_doc = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "WhatsApp Bridge",
				"last_name": "Service",
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
		result.user = user_doc.name
		result.created = True
		return result
	except Exception:
		# Handle race where another process created it in parallel.
		existing_user_name = frappe.db.get_value("User", {"email": email}, "name")
		if existing_user_name:
			user_doc = frappe.get_doc("User", existing_user_name)
			if not cint(user_doc.enabled):
				user_doc.enabled = 1
				user_doc.save(ignore_permissions=True)
			result.user = user_doc.name
			return result
		return result


def _is_enabled_user(user_name: str | None) -> bool:
	user_name = cstr(user_name or "").strip()
	if not user_name:
		return False
	return bool(frappe.db.exists("User", {"name": user_name, "enabled": 1}))


def _ensure_workspace(workspace_name: str):
	if frappe.db.exists("Raven Workspace", workspace_name):
		return frappe.get_doc("Raven Workspace", workspace_name), "reused"

	workspace = frappe.get_doc(
		{
			"doctype": "Raven Workspace",
			"workspace_name": workspace_name,
			"type": "Private",
		}
	).insert(ignore_permissions=True)
	return workspace, "created"


def _ensure_bot(bot_name: str):
	if frappe.db.exists("Raven Bot", bot_name):
		bot = frappe.get_doc("Raven Bot", bot_name)
		if not bot.raven_user:
			bot.save(ignore_permissions=True)
			bot.reload()
		return bot, "reused"

	bot = frappe.get_doc(
		{
			"doctype": "Raven Bot",
			"bot_name": bot_name,
			"is_ai_bot": 0,
		}
	).insert(ignore_permissions=True)
	if not bot.raven_user:
		bot.reload()
		bot.save(ignore_permissions=True)
		bot.reload()
	return bot, "created"


def _resolve_route_members(member_inputs: list[Any], warnings: list[str]) -> list[dict[str, Any]]:
	resolved = []
	seen = set()

	for item in member_inputs:
		row = item if isinstance(item, dict) else {"raven_user": cstr(item).strip()}
		raven_user = _resolve_raven_user(row.get("raven_user"))
		if not raven_user:
			warnings.append(_("Could not resolve route member: {0}").format(row.get("raven_user")))
			continue
		if raven_user in seen:
			continue
		seen.add(raven_user)
		resolved.append(
			{
				"raven_user": raven_user,
				"is_admin": cint(row.get("is_admin")),
				"can_reply": cint(row.get("can_reply", 1)),
			}
		)

	return resolved


def _default_route_member() -> dict[str, Any] | None:
	session_raven_user = frappe.db.get_value("Raven User", {"user": frappe.session.user, "enabled": 1}, "name")
	if session_raven_user:
		return {"raven_user": session_raven_user, "is_admin": 1, "can_reply": 1}

	admin_raven_user = frappe.db.get_value("Raven User", {"user": "Administrator", "enabled": 1}, "name")
	if admin_raven_user:
		return {"raven_user": admin_raven_user, "is_admin": 1, "can_reply": 1}
	return None


def _resolve_raven_user(reference: str | None) -> str | None:
	reference = cstr(reference or "").strip()
	if not reference:
		return None

	if frappe.db.exists("Raven User", reference):
		return reference

	# Map from Frappe User ID/email to Raven User.
	if frappe.db.exists("User", reference):
		raven_user = frappe.db.get_value("Raven User", {"user": reference, "enabled": 1}, "name")
		if raven_user:
			return raven_user

	user_name = frappe.db.get_value("User", {"email": reference}, "name")
	if user_name:
		raven_user = frappe.db.get_value("Raven User", {"user": user_name, "enabled": 1}, "name")
		if raven_user:
			return raven_user

	return None


def _create_or_update_route(
	*,
	whatsapp_account,
	workspace,
	channel_type,
	conversation_strategy,
	members,
	use_bridge_context: bool = True,
):
	route_name = frappe.db.get_value(
		"WhatsApp Raven Account Route",
		{"whatsapp_account": whatsapp_account},
		"name",
		order_by="modified desc",
	)

	if route_name:
		route = frappe.get_doc("WhatsApp Raven Account Route", route_name)
		state = "reused"
	else:
		route = frappe.new_doc("WhatsApp Raven Account Route")
		route.whatsapp_account = whatsapp_account
		state = "created"

	ctx = bridge_user_context() if use_bridge_context else nullcontext()
	with ctx:
		route.enabled = 1
		route.raven_workspace = workspace
		route.channel_type = channel_type
		route.conversation_strategy = conversation_strategy
		if not route.inbox_channel_name:
			route.inbox_channel_name = f"whatsapp-inbox-{_slugify(whatsapp_account)}"

		existing_members = {
			row.raven_user: row for row in (route.members or []) if row.raven_user
		}
		for member in members or []:
			raven_user = member.get("raven_user")
			if not raven_user:
				continue
			existing = existing_members.get(raven_user)
			if existing:
				existing.is_admin = cint(member.get("is_admin"))
				existing.can_reply = cint(member.get("can_reply", 1))
			else:
				route.append(
					"members",
					{
						"raven_user": raven_user,
						"is_admin": cint(member.get("is_admin")),
						"can_reply": cint(member.get("can_reply", 1)),
					},
				)

		route.save(ignore_permissions=True)

	return route, state


def _slugify(value: str | None) -> str:
	text = cstr(value or "").strip().lower()
	return "".join(ch if ch.isalnum() else "-" for ch in text).strip("-") or "account"
