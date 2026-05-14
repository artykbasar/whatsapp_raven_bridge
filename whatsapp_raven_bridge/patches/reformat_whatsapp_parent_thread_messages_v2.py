"""Re-run parent thread starter retrofit to enforce plain non-link rendering."""

from __future__ import annotations

from whatsapp_raven_bridge.bridge.backfill import reformat_existing_parent_thread_messages


def execute():
	reformat_existing_parent_thread_messages()
