"""Retrofit older WhatsApp thread parent messages to plain contact+phone text."""

from __future__ import annotations

from whatsapp_raven_bridge.bridge.backfill import reformat_existing_parent_thread_messages


def execute():
	reformat_existing_parent_thread_messages()
