frappe.ui.form.on("WhatsApp Raven Conversation", {
	refresh(frm) {
		if (!isConversationAdmin()) {
			return;
		}

		frm.add_custom_button(__("Move to Private Channel"), async () => {
			await openMoveToPrivateDialog(frm);
		});

		frm.add_custom_button(__("Check Private Channel State"), () => {
			frappe.call({
				method: "whatsapp_raven_bridge.api.conversation.check_private_channel_state",
				args: {
					conversation: frm.doc.name,
				},
				callback: (r) => {
					const data = r.message || {};
					frappe.msgprint({
						title: __("Private Channel State"),
						message: `<pre>${frappe.utils.escape_html(JSON.stringify(data, null, 2))}</pre>`,
					});
				},
			});
		});
	},
});

function isConversationAdmin() {
	return frappe.session.user === "Administrator" || frappe.user_roles.includes("System Manager");
}

async function openMoveToPrivateDialog(frm) {
	const existing = (frm.doc.private_members || []).map((row) => row.raven_user).filter(Boolean);
	let ravenUserOptions = [];
	try {
		ravenUserOptions = await fetchActiveRavenUsers(existing);
	} catch (error) {
		frappe.msgprint({
			title: __("Could not Load Raven Users"),
			message: __("Please check Raven User setup and try again."),
			indicator: "red",
		});
		return;
	}

	if (!ravenUserOptions.length) {
		frappe.msgprint({
			title: __("No Active Raven Users"),
			message: __("No enabled Raven User records were found."),
			indicator: "orange",
		});
		return;
	}

	const d = new frappe.ui.Dialog({
		title: __("Move to Private Channel"),
		fields: [
			{
				fieldname: "channel_name",
				fieldtype: "Data",
				label: __("Private Channel Display Name"),
				description: __("Optional. Default: WhatsApp - <contact label>"),
			},
			{
				fieldname: "raven_users",
				fieldtype: "MultiCheck",
				label: __("Raven Users"),
				description: __("Select Raven users who should access and reply in the private channel."),
				reqd: 1,
				options: ravenUserOptions,
			},
		],
		primary_action_label: __("Move"),
		primary_action(values) {
			const selectedUsers = Array.isArray(values.raven_users)
				? values.raven_users.filter(Boolean)
				: [];
			if (!selectedUsers.length) {
				frappe.msgprint(__("Please select at least one Raven User."));
				return;
			}
			frappe.call({
				method: "whatsapp_raven_bridge.api.conversation.move_to_private_channel",
				args: {
					conversation: frm.doc.name,
					raven_users: selectedUsers,
					channel_name: values.channel_name,
				},
				callback: (r) => {
					d.hide();
					frm.reload_doc();
					const data = r.message || {};
					frappe.show_alert({
						message: __("Moved to private channel: {0}", [data.private_channel_name || data.private_channel || ""]),
						indicator: "green",
					});
					if (data.private_channel_url) {
						frappe.msgprint({
							title: __("Conversation Moved"),
							message: __(
								"Open the private channel from the Raven sidebar. Channel URL: {0}",
								[data.private_channel_url]
							),
						});
					}
				},
			});
		},
	});
	d.show();
}

async function fetchActiveRavenUsers(existing = []) {
	const response = await frappe.call({
		method: "whatsapp_raven_bridge.api.conversation.list_active_raven_users",
	});
	const rows = response.message || [];
	const selected = new Set(existing || []);
	return rows.map((row) => ({
		label: row.label || row.value,
		value: row.value,
		checked: selected.has(row.value),
	}));
}
