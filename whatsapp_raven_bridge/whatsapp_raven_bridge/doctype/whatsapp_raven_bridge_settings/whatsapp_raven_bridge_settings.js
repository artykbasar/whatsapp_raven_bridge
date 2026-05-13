frappe.ui.form.on("WhatsApp Raven Bridge Settings", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}

		frm.add_custom_button(__("Check Setup Status"), async () => {
			await show_setup_status();
		});

		frm.add_custom_button(__("Run Bootstrap Setup"), async () => {
			await open_bootstrap_dialog(frm);
		});

		frm.add_custom_button(__("Preview Backfill"), async () => {
			await open_backfill_preview_dialog();
		});

		frm.add_custom_button(__("Run Backfill Now"), async () => {
			await open_backfill_run_dialog(frm);
		});

		frm.add_custom_button(__("Run Scheduled Backfill Now"), async () => {
			await run_scheduled_backfill_now(frm);
		});
	},
});

async function show_setup_status() {
	const response = await frappe.call({
		method: "whatsapp_raven_bridge.api.setup.get_setup_status",
	});
	const status = response.message || {};
	const warnings = status.warnings || [];

	const rows = [
		["WhatsApp Accounts Found", bool_to_yes_no(status.has_whatsapp_account)],
		["Raven Workspace Found", bool_to_yes_no(status.has_raven_workspace)],
		["Raven Bot Found", bool_to_yes_no(status.has_raven_bot)],
		["Bridge System User Configured", bool_to_yes_no(status.has_bridge_system_user)],
		["Bridge Enabled", bool_to_yes_no(status.settings_enabled)],
		["Outbound Replies Enabled", bool_to_yes_no(status.enable_outbound_replies)],
		["Account Routes", String(status.number_of_routes || 0)],
		["Routes Using Thread Per Contact", String(status.routes_using_thread_per_contact || 0)],
	];

	const warning_html = warnings.length
		? `<ul>${warnings.map((w) => `<li>${frappe.utils.escape_html(w)}</li>`).join("")}</ul>`
		: "<p>None</p>";

	const table_html = `
		<table class="table table-bordered">
			<tbody>
				${rows
					.map(
						([label, value]) =>
							`<tr><td style="width: 50%;"><strong>${frappe.utils.escape_html(label)}</strong></td><td>${frappe.utils.escape_html(
								value
							)}</td></tr>`
					)
					.join("")}
			</tbody>
		</table>
	`;

	frappe.msgprint({
		title: __("WhatsApp Raven Bridge Setup Status"),
		message: `${table_html}<h5>${__("Warnings")}</h5>${warning_html}`,
		wide: true,
	});
}

async function open_bootstrap_dialog(frm) {
	const default_raven_user = await get_current_session_raven_user();
	const status = await frappe.call({
		method: "whatsapp_raven_bridge.api.setup.get_setup_status",
	});
	const has_whatsapp_account = !!status?.message?.has_whatsapp_account;

	const dialog = new frappe.ui.Dialog({
		title: __("Run Bootstrap Setup"),
		fields: [
			{
				fieldname: "workspace_name",
				label: __("Workspace Name"),
				fieldtype: "Data",
				default: "WhatsApp Inbox",
				reqd: 1,
			},
			{
				fieldname: "bridge_bot_name",
				label: __("Bridge Bot Name"),
				fieldtype: "Data",
				default: "WhatsApp Bridge Bot",
				reqd: 1,
			},
			{
				fieldname: "whatsapp_account",
				label: __("WhatsApp Account"),
				fieldtype: "Link",
				options: "WhatsApp Account",
				reqd: has_whatsapp_account ? 1 : 0,
				description: has_whatsapp_account
					? ""
					: __("No WhatsApp Account found yet. Create one in frappe_whatsapp first."),
			},
			{
				fieldname: "primary_raven_user",
				label: __("Primary Raven User"),
				fieldtype: "Link",
				options: "Raven User",
				default: default_raven_user || "",
				description: __("For multiple members, use CLI bootstrap with route_members list."),
			},
			{
				fieldname: "conversation_strategy",
				label: __("Conversation Strategy"),
				fieldtype: "Select",
				options: ["Thread Per Contact", "Channel Per Contact"],
				default: "Thread Per Contact",
				reqd: 1,
			},
			{
				fieldname: "channel_type",
				label: __("Channel Type"),
				fieldtype: "Select",
				options: ["Private", "Public", "Open"],
				default: "Private",
				reqd: 1,
			},
			{
				fieldname: "enable_outbound_replies",
				label: __("Enable Outbound Replies"),
				fieldtype: "Check",
				default: 1,
			},
		],
		primary_action_label: __("Run Bootstrap"),
		primary_action: async () => {
			const values = dialog.get_values();
			if (!values) return;

			dialog.hide();
			frappe.dom.freeze(__("Running bridge bootstrap..."));

			try {
				const response = await frappe.call({
					method: "whatsapp_raven_bridge.api.setup.bootstrap_from_settings_dialog",
					args: values,
				});
				await frm.reload_doc();
				show_bootstrap_summary(response.message || {});
			} finally {
				frappe.dom.unfreeze();
			}
		},
	});

	dialog.show();
}

function show_bootstrap_summary(summary) {
	const routes = summary.routes || [];
	const warnings = summary.warnings || [];
	const next_steps = summary.next_manual_steps || [];

	const route_rows = routes.length
		? `<ul>${routes
				.map(
					(r) =>
						`<li>${frappe.utils.escape_html(r.whatsapp_account || "")}: ${frappe.utils.escape_html(
							r.route || ""
						)} (${frappe.utils.escape_html(r.status || "")})</li>`
				)
				.join("")}</ul>`
		: "<p>None</p>";

	const warnings_html = warnings.length
		? `<ul>${warnings.map((w) => `<li>${frappe.utils.escape_html(w)}</li>`).join("")}</ul>`
		: "<p>None</p>";

	const next_steps_html = next_steps.length
		? `<ul>${next_steps.map((s) => `<li>${frappe.utils.escape_html(s)}</li>`).join("")}</ul>`
		: "<p>None</p>";

	frappe.msgprint({
		title: __("Bootstrap Result"),
		wide: true,
		message: `
			<p><strong>${__("Settings Updated")}:</strong> ${bool_to_yes_no(summary.settings_updated)}</p>
			<p><strong>${__("Workspace")}:</strong> ${frappe.utils.escape_html(summary.workspace || "")}</p>
			<p><strong>${__("Bot")}:</strong> ${frappe.utils.escape_html(summary.bot || "")}</p>
			<p><strong>${__("Bridge Raven User")}:</strong> ${frappe.utils.escape_html(summary.bridge_raven_user || "")}</p>
			<p><strong>${__("Bridge System User")}:</strong> ${frappe.utils.escape_html(summary.bridge_system_user || "")}</p>
			<h5>${__("Routes")}</h5>
			${route_rows}
			<h5>${__("Warnings")}</h5>
			${warnings_html}
			<h5>${__("Next Manual Steps")}</h5>
			${next_steps_html}
		`,
	});
}

async function get_current_session_raven_user() {
	try {
		const me = await frappe.call({
			method: "frappe.client.get_value",
			args: {
				doctype: "Raven User",
				filters: { user: frappe.session.user, enabled: 1 },
				fieldname: "name",
			},
		});
		return me?.message?.name || "";
	} catch (error) {
		return "";
	}
}

function bool_to_yes_no(value) {
	return value ? "Yes" : "No";
}

function get_backfill_filter_fields() {
	return [
		{
			fieldname: "whatsapp_account",
			label: __("WhatsApp Account"),
			fieldtype: "Link",
			options: "WhatsApp Account",
		},
		{
			fieldname: "phone_number",
			label: __("Phone Number"),
			fieldtype: "Data",
		},
		{
			fieldname: "from_datetime",
			label: __("From Datetime"),
			fieldtype: "Datetime",
		},
		{
			fieldname: "to_datetime",
			label: __("To Datetime"),
			fieldtype: "Datetime",
		},
		{
			fieldname: "direction",
			label: __("Direction"),
			fieldtype: "Select",
			options: ["Both", "Incoming", "Outgoing"],
			default: "Both",
		},
		{
			fieldname: "limit",
			label: __("Limit"),
			fieldtype: "Int",
			default: 100,
			reqd: 1,
		},
	];
}

async function open_backfill_preview_dialog() {
	const dialog = new frappe.ui.Dialog({
		title: __("Preview Backfill"),
		fields: get_backfill_filter_fields(),
		primary_action_label: __("Preview"),
		primary_action: async () => {
			const values = dialog.get_values();
			if (!values) return;
			dialog.hide();
			frappe.dom.freeze(__("Previewing backfill candidates..."));
			try {
				const args = {
					...values,
					direction: values.direction === "Both" ? null : values.direction,
				};
				const response = await frappe.call({
					method: "whatsapp_raven_bridge.api.backfill.preview_backfill",
					args,
				});
				show_backfill_preview(response.message || {});
			} finally {
				frappe.dom.unfreeze();
			}
		},
	});
	dialog.show();
}

function show_backfill_preview(summary) {
	const rows = [
		["Scanned", String(summary.scanned || 0)],
		["Eligible", String(summary.eligible || 0)],
		["Skipped Existing", String(summary.skipped_existing || 0)],
	];
	const byDirection = summary.by_direction || {};
	const byAccount = summary.by_account || {};
	const byPhone = summary.by_phone || {};

	const tableHtml = `
		<table class="table table-bordered">
			<tbody>
				${rows
					.map(
						([label, value]) =>
							`<tr><td><strong>${frappe.utils.escape_html(label)}</strong></td><td>${frappe.utils.escape_html(value)}</td></tr>`
					)
					.join("")}
			</tbody>
		</table>
	`;

	const directionHtml = Object.keys(byDirection).length
		? `<ul>${Object.entries(byDirection)
				.map(([k, v]) => `<li>${frappe.utils.escape_html(k)}: ${frappe.utils.escape_html(String(v))}</li>`)
				.join("")}</ul>`
		: "<p>None</p>";
	const accountHtml = Object.keys(byAccount).length
		? `<ul>${Object.entries(byAccount)
				.map(([k, v]) => `<li>${frappe.utils.escape_html(k)}: ${frappe.utils.escape_html(String(v))}</li>`)
				.join("")}</ul>`
		: "<p>None</p>";
	const phoneHtml = Object.keys(byPhone).length
		? `<ul>${Object.entries(byPhone)
				.slice(0, 20)
				.map(([k, v]) => `<li>${frappe.utils.escape_html(k)}: ${frappe.utils.escape_html(String(v))}</li>`)
				.join("")}</ul>`
		: "<p>None</p>";

	frappe.msgprint({
		title: __("Backfill Preview"),
		wide: true,
		message: `
			${tableHtml}
			<h5>${__("By Direction")}</h5>
			${directionHtml}
			<h5>${__("By Account")}</h5>
			${accountHtml}
			<h5>${__("By Phone (Top 20)")}</h5>
			${phoneHtml}
		`,
	});
}

async function open_backfill_run_dialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Run Backfill Now"),
		fields: get_backfill_filter_fields(),
		primary_action_label: __("Queue Backfill"),
		primary_action: async () => {
			const values = dialog.get_values();
			if (!values) return;
			dialog.hide();
			frappe.confirm(
				__(
					"This will create Raven messages for existing WhatsApp Message records. It will not send WhatsApp messages."
				),
				async () => {
					frappe.dom.freeze(__("Queueing backfill job..."));
					try {
						const args = {
							...values,
							direction: values.direction === "Both" ? null : values.direction,
						};
						const response = await frappe.call({
							method: "whatsapp_raven_bridge.api.backfill.enqueue_backfill",
							args,
						});
						const result = response.message || {};
						await frm.reload_doc();
						frappe.msgprint({
							title: __("Backfill"),
							message: result.job_id
								? __("Backfill job queued: {0}", [result.job_id])
								: __("Backfill request result: {0}", [result.status || "unknown"]),
						});
					} finally {
						frappe.dom.unfreeze();
					}
				}
			);
		},
	});
	dialog.show();
}

async function run_scheduled_backfill_now(frm) {
	frappe.dom.freeze(__("Queueing scheduled backfill..."));
	try {
		const response = await frappe.call({
			method: "whatsapp_raven_bridge.api.backfill.run_scheduled_backfill_now",
		});
		const result = response.message || {};
		await frm.reload_doc();
		frappe.msgprint({
			title: __("Scheduled Backfill"),
			message: result.job_id
				? __("Scheduled backfill job queued: {0}", [result.job_id])
				: __("Scheduled backfill result: {0}", [result.status || "unknown"]),
		});
	} finally {
		frappe.dom.unfreeze();
	}
}
