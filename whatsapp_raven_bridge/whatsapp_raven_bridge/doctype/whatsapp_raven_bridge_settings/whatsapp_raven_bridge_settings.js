frappe.ui.form.on("WhatsApp Raven Bridge Settings", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}

		frm.add_custom_button(__("Check Setup Status"), async () => {
			await show_setup_status();
		});

		frm.add_custom_button(__("Run Bootstrap Setup"), async () => {
			await run_bootstrap_setup(frm);
		});

		frm.add_custom_button(__("Preview Backfill"), async () => {
			await preview_backfill_all();
		});

		frm.add_custom_button(__("Sync All Message History Now"), async () => {
			await sync_all_history_now(frm);
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

async function run_bootstrap_setup(frm) {
	frappe.dom.freeze(__("Running bootstrap setup for all WhatsApp accounts..."));
	try {
		const response = await frappe.call({
			method: "whatsapp_raven_bridge.api.setup.bootstrap_all_accounts_from_settings",
		});
		await frm.reload_doc();
		show_bootstrap_summary(response.message || {});
	} finally {
		frappe.dom.unfreeze();
	}
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
			<p><strong>${__("Accounts Processed")}:</strong> ${frappe.utils.escape_html(
				String(summary.account_count || routes.length || 0)
			)}</p>
			<h5>${__("Routes")}</h5>
			${route_rows}
			<h5>${__("Warnings")}</h5>
			${warnings_html}
			<h5>${__("Next Manual Steps")}</h5>
			${next_steps_html}
		`,
	});
}

async function preview_backfill_all() {
	frappe.dom.freeze(__("Previewing all message history..."));
	try {
		const response = await frappe.call({
			method: "whatsapp_raven_bridge.api.backfill.preview_all_message_history",
		});
		show_backfill_preview(response.message || {});
	} finally {
		frappe.dom.unfreeze();
	}
}

function show_backfill_preview(summary) {
	const byDirection = summary.by_direction || {};
	const byAccount = summary.by_account || {};
	const byPhone = summary.by_phone || {};
	const sample = summary.sample || [];
	const byAccountDetail = summary.by_account_detail || {};
	const accountOptions = ["All Accounts"].concat(Object.keys(byAccount).sort());

	const dialog = new frappe.ui.Dialog({
		title: __("Backfill Preview"),
		size: "extra-large",
		fields: [
			{
				fieldname: "account_filter",
				label: __("WhatsApp Account Filter"),
				fieldtype: "Select",
				options: accountOptions,
				default: "All Accounts",
				onchange: () => render_preview(),
			},
			{
				fieldname: "preview_html",
				fieldtype: "HTML",
			},
		],
		primary_action_label: __("Close"),
		primary_action: () => dialog.hide(),
	});

	const render_preview = () => {
		const selected = dialog.get_value("account_filter") || "All Accounts";
		const accountDetail = selected === "All Accounts" ? null : byAccountDetail[selected] || {};
		const scopedDirection = selected === "All Accounts" ? byDirection : accountDetail.by_direction || {};
		const scopedPhone = selected === "All Accounts" ? byPhone : accountDetail.by_phone || {};
		const scopedSample = selected === "All Accounts" ? sample : accountDetail.sample || [];
		const scopedRows =
			selected === "All Accounts"
				? [
						["Scanned", String(summary.scanned || 0)],
						["Eligible", String(summary.eligible || 0)],
						["Skipped Existing", String(summary.skipped_existing || 0)],
					]
				: [
						["Scanned", String(accountDetail.scanned || 0)],
						["Eligible", String(accountDetail.eligible || 0)],
						["Skipped Existing", String(accountDetail.skipped_existing || 0)],
					];

		const tableHtml = `
			<table class="table table-bordered">
				<tbody>
					${scopedRows
						.map(
							([label, value]) =>
								`<tr><td><strong>${frappe.utils.escape_html(label)}</strong></td><td>${frappe.utils.escape_html(value)}</td></tr>`
						)
						.join("")}
				</tbody>
			</table>
		`;

		const directionHtml = Object.keys(scopedDirection).length
			? `<ul>${Object.entries(scopedDirection)
					.map(([k, v]) => `<li>${frappe.utils.escape_html(k)}: ${frappe.utils.escape_html(String(v))}</li>`)
					.join("")}</ul>`
			: "<p>None</p>";
		const accountHtml = Object.keys(byAccount).length
			? `<ul>${Object.entries(byAccount)
					.map(([k, v]) => `<li>${frappe.utils.escape_html(k)}: ${frappe.utils.escape_html(String(v))}</li>`)
					.join("")}</ul>`
			: "<p>None</p>";
		const phoneHtml = Object.keys(scopedPhone).length
			? `<ul>${Object.entries(scopedPhone)
					.slice(0, 20)
					.map(([k, v]) => `<li>${frappe.utils.escape_html(k)}: ${frappe.utils.escape_html(String(v))}</li>`)
					.join("")}</ul>`
			: "<p>None</p>";
		const sampleHtml = scopedSample.length
			? `<ul>${scopedSample
					.slice(0, 30)
					.map(
						(row) =>
							`<li>${frappe.utils.escape_html(row.whatsapp_account || "No Account")} / ${frappe.utils.escape_html(
								row.phone || ""
							)} / ${frappe.utils.escape_html(row.type || "")} / ${frappe.utils.escape_html(
								row.original_datetime || ""
							)}</li>`
					)
					.join("")}</ul>`
			: "<p>None</p>";

		dialog.fields_dict.preview_html.$wrapper.html(`
			${tableHtml}
			<h5>${__("By Direction")}</h5>
			${directionHtml}
			${
				selected === "All Accounts"
					? `<h5>${__("By Account")}</h5>${accountHtml}`
					: `<h5>${__("Account")}</h5><p>${frappe.utils.escape_html(selected)}</p>`
			}
			<h5>${__("By Phone (Top 20)")}</h5>
			${phoneHtml}
			<h5>${__("Sample Rows")}</h5>
			${sampleHtml}
		`);
	};

	dialog.show();
	render_preview();
}

async function sync_all_history_now(frm) {
	frappe.confirm(
		__(
			"This will enqueue a full historical sync of local WhatsApp Message records for all accounts. It will not send WhatsApp messages."
		),
		async () => {
			frappe.dom.freeze(__("Queueing full-history sync..."));
			try {
				const response = await frappe.call({
					method: "whatsapp_raven_bridge.api.backfill.enqueue_sync_all_message_history",
				});
				const result = response.message || {};
				await frm.reload_doc();
				frappe.msgprint({
					title: __("Backfill"),
					message: result.job_id
						? __("Full-history sync queued: {0}", [result.job_id])
						: __("Sync request result: {0}", [result.status || "unknown"]),
				});
			} finally {
				frappe.dom.unfreeze();
			}
		}
	);
}

function bool_to_yes_no(value) {
	return value ? "Yes" : "No";
}
