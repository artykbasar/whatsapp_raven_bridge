frappe.ui.form.on("WhatsApp Raven Conversation", {
	refresh(frm) {
		if (!isConversationAdmin()) {
			return;
		}

		frm.add_custom_button(__("Move to Private Channel"), () => {
			openMoveToPrivateDialog(frm);
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

function openMoveToPrivateDialog(frm) {
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
				fieldtype: "Small Text",
				label: __("Raven Users"),
				description: __("Comma-separated Raven User IDs or User IDs (email/login)."),
				reqd: 1,
			},
		],
		primary_action_label: __("Move"),
		primary_action(values) {
			frappe.call({
				method: "whatsapp_raven_bridge.api.conversation.move_to_private_channel",
				args: {
					conversation: frm.doc.name,
					raven_users: values.raven_users,
					channel_name: values.channel_name,
				},
				callback: (r) => {
					d.hide();
					frm.reload_doc();
					const data = r.message || {};
					frappe.show_alert({
						message: __("Moved to private channel: {0}", [data.private_channel || ""]),
						indicator: "green",
					});
				},
			});
		},
	});

	const existing = (frm.doc.private_members || [])
		.map((row) => row.raven_user)
		.filter(Boolean)
		.join(", ");
	if (existing) {
		d.set_value("raven_users", existing);
	}
	d.show();
}

