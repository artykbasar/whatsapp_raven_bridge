"""Retrofit older bridge-created WhatsApp-origin Raven messages to compact UI format."""

from __future__ import annotations

from whatsapp_raven_bridge.bridge.backfill import reformat_existing_whatsapp_origin_raven_messages


def execute():
	reformat_existing_whatsapp_origin_raven_messages()
