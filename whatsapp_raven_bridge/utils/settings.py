"""Defensive accessors for WhatsApp Raven Bridge settings."""

from contextlib import contextmanager

import frappe
from frappe.utils import cint

SETTINGS_DOCTYPE = "WhatsApp Raven Bridge Settings"

IDENTITY_FIELDS = (
	"bridge_raven_bot",
	"bridge_raven_user",
	"default_raven_workspace",
	"default_channel_type",
	"default_whatsapp_account",
	"conversation_strategy",
	"bridge_system_user",
)


def get_settings():
	"""Return the settings singleton, or None while the DocType is unavailable."""
	try:
		if not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
			return None
		return frappe.get_single(SETTINGS_DOCTYPE)
	except Exception:
		return None


def is_enabled():
	"""Return True only when settings exist and the bridge is enabled."""
	settings = get_settings()
	return bool(settings and cint(settings.get("enabled")))


def get_bridge_identity():
	"""Return the configured bridge identity and defaults."""
	settings = get_settings()
	values = frappe._dict({field: None for field in IDENTITY_FIELDS})

	if not settings:
		return values

	for field in IDENTITY_FIELDS:
		values[field] = settings.get(field)

	return values


def get_bridge_system_user():
	"""Return configured bridge system user when available and enabled."""
	settings = get_settings()
	if not settings:
		return None

	user = settings.get("bridge_system_user")
	if not user:
		return None

	if not frappe.db.exists("User", user):
		return None

	enabled = frappe.db.get_value("User", user, "enabled")
	if enabled is None:
		return None
	return user if cint(enabled) else None


@contextmanager
def bridge_user_context():
	"""Temporarily switch frappe.session.user to configured bridge system user."""
	original_user = frappe.session.user
	bridge_user = get_bridge_system_user()
	changed_user = False

	if bridge_user and bridge_user != original_user:
		try:
			frappe.set_user(bridge_user)
			changed_user = True
		except Exception:
			frappe.log_error(
				title="WhatsApp Raven Bridge: failed to switch bridge system user",
				message=frappe.get_traceback(),
			)

	try:
		yield frappe.session.user
	finally:
		if changed_user and frappe.session.user != original_user:
			frappe.set_user(original_user)


def validate_settings_for_inbound():
	"""Return missing settings required for inbound WhatsApp to Raven sync."""
	return _missing_fields(("default_raven_workspace", "bridge_raven_user", "default_channel_type"))


def validate_settings_for_outbound():
	"""Return missing settings required for outbound Raven to WhatsApp sync."""
	return _missing_fields(("default_whatsapp_account",))


def _missing_fields(required_fields):
	settings = get_settings()
	if not settings:
		return list(required_fields)

	return [field for field in required_fields if not settings.get(field)]
