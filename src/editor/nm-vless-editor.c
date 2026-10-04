/* SPDX-License-Identifier: GPL-2.0-or-later */
/*
 * libnm-gtk4-vpn-plugin-vless-editor.so: the GTK 4 page shown by GNOME Settings
 * for VLESS connections. It reads and writes the vpn.data/vpn.secrets keys
 * documented in docs/connection-settings.md and writes only the keys that apply
 * to the selected transport and security, which the service validates strictly.
 */
#include "config.h"

#include "nm-vless-common.h"

#include <glib/gi18n-lib.h>
#include <gmodule.h>
#include <gtk/gtk.h>
#include <string.h>

#define NM_VLESS_UI_RESOURCE "/org/freedesktop/NetworkManager/vless/nm-vless-dialog.ui"

typedef struct {
    const char *value;
    const char *label; /* translatable, NULL: show the value */
} Choice;

static const Choice security_choices[] = {
    {"none", N_("None")},
    {"tls", "TLS"},
    {"reality", "REALITY"},
    {NULL, NULL},
};

static const Choice network_choices[] = {
    {"tcp", "TCP"},
    {"xhttp", "XHTTP"},
    {"ws", "WebSocket"},
    {"httpupgrade", "HTTPUpgrade"},
    {"grpc", "gRPC"},
    {NULL, NULL},
};

static const Choice fingerprint_choices[] = {
    {"", N_("Default")},
    {"chrome", "Chrome"},
    {"firefox", "Firefox"},
    {"safari", "Safari"},
    {"ios", "iOS"},
    {"android", "Android"},
    {"edge", "Edge"},
    {"360", "360"},
    {"qq", "QQ"},
    {"random", N_("Random")},
    {"randomized", N_("Randomized")},
    {NULL, NULL},
};

static const Choice grpc_mode_choices[] = {
    {"", N_("Default (gun)")},
    {"gun", "gun"},
    {"multi", "multi"},
    {NULL, NULL},
};

static const Choice xhttp_mode_choices[] = {
    {"", N_("Default (auto)")},
    {"auto", "auto"},
    {"packet-up", "packet-up"},
    {"stream-up", "stream-up"},
    {"stream-one", "stream-one"},
    {NULL, NULL},
};

/* Keys this editor manages; any other vpn.data key is preserved unchanged. */
static const char *const managed_keys[] = {
    NM_VLESS_KEY_ADDRESS,
    NM_VLESS_KEY_PORT,
    NM_VLESS_KEY_ENCRYPTION,
    NM_VLESS_KEY_FLOW,
    NM_VLESS_KEY_NETWORK,
    NM_VLESS_KEY_SECURITY,
    NM_VLESS_KEY_SNI,
    NM_VLESS_KEY_FINGERPRINT,
    NM_VLESS_KEY_ALPN,
    NM_VLESS_KEY_REALITY_PUBLIC_KEY,
    NM_VLESS_KEY_REALITY_SHORT_ID,
    NM_VLESS_KEY_REALITY_SPIDER_X,
    NM_VLESS_KEY_PATH,
    NM_VLESS_KEY_HOST,
    NM_VLESS_KEY_GRPC_SERVICE_NAME,
    NM_VLESS_KEY_MODE,
    NM_VLESS_KEY_XHTTP_EXTRA,
    NM_VLESS_KEY_MTU,
    NM_VLESS_KEY_DNS,
    NM_VLESS_KEY_DNS6,
    NM_VLESS_KEY_IPV6,
    NM_VLESS_KEY_UUID_FLAGS,
    NULL,
};

typedef struct {
    GtkDropDown   *widget;
    GtkStringList *model;
    GPtrArray     *values;
} DropDown;

#define NM_VLESS_TYPE_EDITOR (nm_vless_editor_get_type())
G_DECLARE_FINAL_TYPE(NMVlessEditor, nm_vless_editor, NM_VLESS, EDITOR, GObject)

struct _NMVlessEditor {
    GObject       parent_instance;
    GtkBuilder   *builder;
    GtkWidget    *root;
    GCancellable *cancellable;
    GHashTable   *extra_data;
    char         *uuid_flags;
    gboolean      had_mtu;
    gboolean      had_ipv6;
    gboolean      loading;
    DropDown      security;
    DropDown      network;
    DropDown      fingerprint;
    DropDown      grpc_mode;
    DropDown      xhttp_mode;
};

static void nm_vless_editor_interface_init(NMVpnEditorInterface *iface);

G_DEFINE_TYPE_WITH_CODE(NMVlessEditor,
                        nm_vless_editor,
                        G_TYPE_OBJECT,
                        G_IMPLEMENT_INTERFACE(NM_TYPE_VPN_EDITOR, nm_vless_editor_interface_init))

/* ---- widget helpers ---------------------------------------------------------------------- */

static GtkWidget *
widget(NMVlessEditor *self, const char *id)
{
    GObject *object = gtk_builder_get_object(self->builder, id);

    g_return_val_if_fail(GTK_IS_WIDGET(object), NULL);
    return GTK_WIDGET(object);
}

/* Text of an entry without surrounding whitespace; empty string if unset. */
static char *
entry_text(NMVlessEditor *self, const char *id)
{
    return g_strstrip(g_strdup(gtk_editable_get_text(GTK_EDITABLE(widget(self, id)))));
}

static void
set_entry_text(NMVlessEditor *self, const char *id, const char *text)
{
    gtk_editable_set_text(GTK_EDITABLE(widget(self, id)), text ? text : "");
}

static void
dropdown_init(DropDown *dropdown, NMVlessEditor *self, const char *id, const Choice *choices)
{
    dropdown->widget = GTK_DROP_DOWN(widget(self, id));
    dropdown->model  = gtk_string_list_new(NULL);
    dropdown->values = g_ptr_array_new_with_free_func(g_free);
    for (const Choice *c = choices; c->value; c++) {
        gtk_string_list_append(dropdown->model, c->label ? _(c->label) : c->value);
        g_ptr_array_add(dropdown->values, g_strdup(c->value));
    }
    gtk_drop_down_set_model(dropdown->widget, G_LIST_MODEL(dropdown->model));
}

static void
dropdown_clear(DropDown *dropdown)
{
    g_clear_object(&dropdown->model);
    g_clear_pointer(&dropdown->values, g_ptr_array_unref);
}

static const char *
dropdown_get(const DropDown *dropdown)
{
    guint index = gtk_drop_down_get_selected(dropdown->widget);

    if (index >= dropdown->values->len)
        return "";
    return g_ptr_array_index(dropdown->values, index);
}

/* Select @value; values written by newer versions or other tools are added, not lost. */
static void
dropdown_set(DropDown *dropdown, const char *value)
{
    value = value ? value : "";
    for (guint i = 0; i < dropdown->values->len; i++) {
        if (g_str_equal(g_ptr_array_index(dropdown->values, i), value)) {
            gtk_drop_down_set_selected(dropdown->widget, i);
            return;
        }
    }
    gtk_string_list_append(dropdown->model, value);
    g_ptr_array_add(dropdown->values, g_strdup(value));
    gtk_drop_down_set_selected(dropdown->widget, dropdown->values->len - 1);
}

static gboolean
is_path_network(const char *network)
{
    return g_str_equal(network, "ws") || g_str_equal(network, "xhttp")
           || g_str_equal(network, "httpupgrade");
}

static void
update_visibility(NMVlessEditor *self)
{
    const char *security = dropdown_get(&self->security);
    const char *network  = dropdown_get(&self->network);
    gboolean    tls      = g_str_equal(security, "tls");
    gboolean    reality  = g_str_equal(security, "reality");

    gtk_widget_set_visible(widget(self, "sni_row"), tls || reality);
    gtk_widget_set_visible(widget(self, "fingerprint_row"), tls || reality);
    gtk_widget_set_visible(widget(self, "alpn_row"), tls);
    gtk_widget_set_visible(widget(self, "public_key_row"), reality);
    gtk_widget_set_visible(widget(self, "short_id_row"), reality);
    gtk_widget_set_visible(widget(self, "spider_x_row"), reality);
    gtk_widget_set_visible(widget(self, "flow_check"),
                           (tls || reality) && g_str_equal(network, "tcp"));
    gtk_widget_set_visible(widget(self, "path_row"), is_path_network(network));
    gtk_widget_set_visible(widget(self, "host_row"), is_path_network(network));
    gtk_widget_set_visible(widget(self, "service_name_row"), g_str_equal(network, "grpc"));
    gtk_widget_set_visible(widget(self, "grpc_mode_row"), g_str_equal(network, "grpc"));
    gtk_widget_set_visible(widget(self, "xhttp_mode_row"), g_str_equal(network, "xhttp"));
    gtk_widget_set_visible(widget(self, "xhttp_extra_row"), g_str_equal(network, "xhttp"));
    gtk_widget_set_sensitive(widget(self, "dns6_row"),
                             gtk_check_button_get_active(
                                 GTK_CHECK_BUTTON(widget(self, "ipv6_check"))));
}

static void
emit_changed(NMVlessEditor *self)
{
    update_visibility(self);
    if (!self->loading)
        g_signal_emit_by_name(self, "changed");
}

static void
show_status(NMVlessEditor *self, const char *message, gboolean is_error)
{
    GtkWidget *label = widget(self, "link_status");

    gtk_label_set_text(GTK_LABEL(label), message);
    gtk_widget_set_visible(label, message != NULL);
    if (is_error) {
        gtk_widget_add_css_class(label, "error");
        gtk_widget_remove_css_class(label, "dim-label");
    } else {
        gtk_widget_add_css_class(label, "dim-label");
        gtk_widget_remove_css_class(label, "error");
    }
}

/* ---- loading ----------------------------------------------------------------------------- */

static const char *
data_item(NMSettingVpn *s_vpn, const char *key)
{
    const char *value = s_vpn ? nm_setting_vpn_get_data_item(s_vpn, key) : NULL;

    return value ? value : "";
}

/* Fill the server, security and transport fields. Tunnel options are not touched. */
static void
load_server(NMVlessEditor *self, NMSettingVpn *s_vpn)
{
    const char *port   = data_item(s_vpn, NM_VLESS_KEY_PORT);
    const char *uuid   = s_vpn ? nm_setting_vpn_get_secret(s_vpn, NM_VLESS_SECRET_UUID) : NULL;
    const char *mode   = data_item(s_vpn, NM_VLESS_KEY_MODE);
    const char *network;

    set_entry_text(self, "address_entry", data_item(s_vpn, NM_VLESS_KEY_ADDRESS));
    gtk_spin_button_set_value(GTK_SPIN_BUTTON(widget(self, "port_spin")),
                              *port ? g_ascii_strtod(port, NULL) : NM_VLESS_DEFAULT_PORT);
    if (uuid)
        set_entry_text(self, "uuid_entry", uuid);
    set_entry_text(self, "encryption_entry", data_item(s_vpn, NM_VLESS_KEY_ENCRYPTION));

    dropdown_set(&self->security,
                 *data_item(s_vpn, NM_VLESS_KEY_SECURITY) ? data_item(s_vpn, NM_VLESS_KEY_SECURITY)
                                                          : "none");
    set_entry_text(self, "sni_entry", data_item(s_vpn, NM_VLESS_KEY_SNI));
    dropdown_set(&self->fingerprint, data_item(s_vpn, NM_VLESS_KEY_FINGERPRINT));
    set_entry_text(self, "alpn_entry", data_item(s_vpn, NM_VLESS_KEY_ALPN));
    set_entry_text(self, "public_key_entry", data_item(s_vpn, NM_VLESS_KEY_REALITY_PUBLIC_KEY));
    set_entry_text(self, "short_id_entry", data_item(s_vpn, NM_VLESS_KEY_REALITY_SHORT_ID));
    set_entry_text(self, "spider_x_entry", data_item(s_vpn, NM_VLESS_KEY_REALITY_SPIDER_X));
    gtk_check_button_set_active(GTK_CHECK_BUTTON(widget(self, "flow_check")),
                                g_str_equal(data_item(s_vpn, NM_VLESS_KEY_FLOW),
                                            NM_VLESS_FLOW_VISION));

    network = *data_item(s_vpn, NM_VLESS_KEY_NETWORK) ? data_item(s_vpn, NM_VLESS_KEY_NETWORK)
                                                      : "tcp";
    dropdown_set(&self->network, network);
    set_entry_text(self, "path_entry", data_item(s_vpn, NM_VLESS_KEY_PATH));
    set_entry_text(self, "host_entry", data_item(s_vpn, NM_VLESS_KEY_HOST));
    set_entry_text(self, "service_name_entry", data_item(s_vpn, NM_VLESS_KEY_GRPC_SERVICE_NAME));
    dropdown_set(&self->grpc_mode, g_str_equal(network, "grpc") ? mode : "");
    dropdown_set(&self->xhttp_mode, g_str_equal(network, "xhttp") ? mode : "");
    set_entry_text(self, "xhttp_extra_entry", data_item(s_vpn, NM_VLESS_KEY_XHTTP_EXTRA));
}

static void
load_connection(NMVlessEditor *self, NMConnection *connection)
{
    NMSettingVpn *s_vpn = connection ? nm_connection_get_setting_vpn(connection) : NULL;
    const char   *mtu   = data_item(s_vpn, NM_VLESS_KEY_MTU);
    const char   *ipv6  = data_item(s_vpn, NM_VLESS_KEY_IPV6);
    const char   *encryption;
    guint         n_keys = 0;
    const char  **keys;

    self->loading = TRUE;
    load_server(self, s_vpn);

    self->had_mtu = *mtu != '\0';
    gtk_spin_button_set_value(GTK_SPIN_BUTTON(widget(self, "mtu_spin")),
                              *mtu ? g_ascii_strtod(mtu, NULL) : NM_VLESS_DEFAULT_MTU);
    set_entry_text(self, "dns_entry", data_item(s_vpn, NM_VLESS_KEY_DNS));
    set_entry_text(self, "dns6_entry", data_item(s_vpn, NM_VLESS_KEY_DNS6));
    self->had_ipv6 = *ipv6 != '\0';
    gtk_check_button_set_active(GTK_CHECK_BUTTON(widget(self, "ipv6_check")),
                                !(g_str_equal(ipv6, "no") || g_str_equal(ipv6, "false")
                                  || g_str_equal(ipv6, "0")));

    g_free(self->uuid_flags);
    self->uuid_flags = g_strdup(data_item(s_vpn, NM_VLESS_KEY_UUID_FLAGS));

    keys = s_vpn ? nm_setting_vpn_get_data_keys(s_vpn, &n_keys) : NULL;
    for (guint i = 0; i < n_keys; i++) {
        if (!g_strv_contains(managed_keys, keys[i]))
            g_hash_table_insert(self->extra_data,
                                g_strdup(keys[i]),
                                g_strdup(nm_setting_vpn_get_data_item(s_vpn, keys[i])));
    }
    g_free(keys);

    encryption = data_item(s_vpn, NM_VLESS_KEY_ENCRYPTION);
    gtk_expander_set_expanded(GTK_EXPANDER(widget(self, "advanced_expander")),
                              self->had_mtu || self->had_ipv6
                                  || *data_item(s_vpn, NM_VLESS_KEY_DNS)
                                  || *data_item(s_vpn, NM_VLESS_KEY_DNS6)
                                  || !(g_str_equal(encryption, "")
                                       || g_str_equal(encryption, "none")));
    self->loading = FALSE;
    update_visibility(self);
}

/* ---- import from link ------------------------------------------------------------------ */

static void
link_parsed_cb(GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(NMVlessEditor) self      = user_data;
    g_autoptr(GError) error           = NULL;
    g_autofree char *keyfile          = NULL;
    g_autoptr(NMConnection) connection = NULL;
    g_autofree char *message          = NULL;

    keyfile = nm_vless_parse_link_finish(result, &error);
    if (g_error_matches(error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        return;
    gtk_widget_set_sensitive(widget(self, "link_button"), TRUE);
    if (keyfile)
        connection = nm_vless_connection_from_keyfile(keyfile, &error);
    if (!connection) {
        message = g_strdup_printf(_("Cannot use this link: %s"), error->message);
        show_status(self, message, TRUE);
        return;
    }

    self->loading = TRUE;
    load_server(self, nm_connection_get_setting_vpn(connection));
    self->loading = FALSE;
    set_entry_text(self, "link_entry", "");
    message = g_strdup_printf(_("Filled in from “%s”."), nm_connection_get_id(connection));
    show_status(self, message, FALSE);
    emit_changed(self);
}

/* A subscription adds its servers as separate connections through "nm-vless". */
static void
subscription_added_cb(GObject *source, GAsyncResult *result, gpointer user_data)
{
    g_autoptr(NMVlessEditor) self = user_data;
    g_autoptr(GError) error      = NULL;
    g_autoptr(GKeyFile) keyfile  = g_key_file_new();
    g_autofree char *output      = NULL;
    g_autofree char *name        = NULL;
    g_autofree char *message     = NULL;
    int              servers;

    output = nm_vless_cli_finish(result, &error);
    if (g_error_matches(error, G_IO_ERROR, G_IO_ERROR_CANCELLED))
        return;
    gtk_widget_set_sensitive(widget(self, "link_button"), TRUE);
    if (!output || !g_key_file_load_from_data(keyfile, output, -1, G_KEY_FILE_NONE, &error)) {
        message = g_strdup_printf(_("Cannot add the subscription: %s"), error->message);
        show_status(self, message, TRUE);
        return;
    }

    name    = g_key_file_get_string(keyfile, "subscription", "name", NULL);
    servers = g_key_file_get_integer(keyfile, "subscription", "added", NULL)
              + g_key_file_get_integer(keyfile, "subscription", "updated", NULL)
              + g_key_file_get_integer(keyfile, "subscription", "unchanged", NULL);
    set_entry_text(self, "link_entry", "");
    message = g_strdup_printf(g_dngettext(GETTEXT_PACKAGE,
                                          "Subscription “%s” added: %d server is now in the "
                                          "VPN list and will be updated automatically. You "
                                          "can close this dialog.",
                                          "Subscription “%s” added: %d servers are now in the "
                                          "VPN list and will be updated automatically. You "
                                          "can close this dialog.",
                                          servers),
                              name ? name : "",
                              servers);
    show_status(self, message, FALSE);
}

typedef enum {
    LINK_NONE,
    LINK_SERVER,
    LINK_SUBSCRIPTION,
} LinkKind;

static LinkKind
entry_link_kind(NMVlessEditor *self)
{
    g_autofree char *text = entry_text(self, "link_entry");

    if (g_ascii_strncasecmp(text, "vless://", strlen("vless://")) == 0)
        return LINK_SERVER;
    if (g_ascii_strncasecmp(text, "https://", strlen("https://")) == 0 && text[8] != '\0')
        return LINK_SUBSCRIPTION;
    return LINK_NONE;
}

static const char *const subscription_add_args[] = {"subscription", "add", "-", "--keyfile", NULL};

static void
link_activated(NMVlessEditor *self)
{
    g_autofree char *text = entry_text(self, "link_entry");

    switch (entry_link_kind(self)) {
    case LINK_SERVER:
        gtk_widget_set_sensitive(widget(self, "link_button"), FALSE);
        show_status(self, _("Reading the link…"), FALSE);
        nm_vless_parse_link_async(text, self->cancellable, link_parsed_cb, g_object_ref(self));
        break;
    case LINK_SUBSCRIPTION:
        gtk_widget_set_sensitive(widget(self, "link_button"), FALSE);
        show_status(self, _("Downloading the subscription…"), FALSE);
        nm_vless_cli_async(subscription_add_args,
                           text,
                           self->cancellable,
                           subscription_added_cb,
                           g_object_ref(self));
        break;
    case LINK_NONE:
        break;
    }
}

static void
link_entry_changed(NMVlessEditor *self)
{
    LinkKind kind = entry_link_kind(self);

    gtk_widget_set_sensitive(widget(self, "link_button"), kind != LINK_NONE);
    gtk_button_set_label(GTK_BUTTON(widget(self, "link_button")),
                         kind == LINK_SUBSCRIPTION ? _("Add Subscription") : _("Fill In"));
}

/* ---- saving -------------------------------------------------------------------------------- */

static void
add_item(NMSettingVpn *s_vpn, const char *key, const char *value)
{
    if (value && *value)
        nm_setting_vpn_add_data_item(s_vpn, key, value);
}

static gboolean
invalid(GError **error, const char *message)
{
    g_set_error_literal(error, NM_CONNECTION_ERROR, NM_CONNECTION_ERROR_INVALID_PROPERTY, message);
    return FALSE;
}

/* Keep DNS queries in the tunnel unless the user chose a priority on the IP pages. */
static void
prefer_tunnel_dns(NMConnection *connection)
{
    NMSettingIPConfig *settings[] = {
        nm_connection_get_setting_ip4_config(connection),
        nm_connection_get_setting_ip6_config(connection),
    };

    for (gsize i = 0; i < G_N_ELEMENTS(settings); i++) {
        if (settings[i] && nm_setting_ip_config_get_dns_priority(settings[i]) == 0)
            g_object_set(settings[i],
                         NM_SETTING_IP_CONFIG_DNS_PRIORITY,
                         NM_VLESS_DNS_PRIORITY,
                         NULL);
    }
}

static gboolean
update_connection(NMVpnEditor *editor, NMConnection *connection, GError **error)
{
    NMVlessEditor   *self        = NM_VLESS_EDITOR(editor);
    const char      *security    = dropdown_get(&self->security);
    const char      *network     = dropdown_get(&self->network);
    gboolean         tls         = g_str_equal(security, "tls");
    gboolean         reality     = g_str_equal(security, "reality");
    gboolean         ipv6        = gtk_check_button_get_active(
        GTK_CHECK_BUTTON(widget(self, "ipv6_check")));
    int              mtu         = gtk_spin_button_get_value_as_int(
        GTK_SPIN_BUTTON(widget(self, "mtu_spin")));
    g_autofree char *address     = entry_text(self, "address_entry");
    g_autofree char *uuid        = entry_text(self, "uuid_entry");
    g_autofree char *encryption  = entry_text(self, "encryption_entry");
    g_autofree char *sni         = entry_text(self, "sni_entry");
    g_autofree char *alpn        = entry_text(self, "alpn_entry");
    g_autofree char *public_key  = entry_text(self, "public_key_entry");
    g_autofree char *short_id    = entry_text(self, "short_id_entry");
    g_autofree char *spider_x    = entry_text(self, "spider_x_entry");
    g_autofree char *path        = entry_text(self, "path_entry");
    g_autofree char *host        = entry_text(self, "host_entry");
    g_autofree char *service     = entry_text(self, "service_name_entry");
    g_autofree char *extra       = entry_text(self, "xhttp_extra_entry");
    g_autofree char *dns         = entry_text(self, "dns_entry");
    g_autofree char *dns6        = entry_text(self, "dns6_entry");
    g_autofree char *port        = NULL;
    g_autofree char *mtu_text    = NULL;
    NMSettingVpn    *s_vpn;
    GHashTableIter   iter;
    gpointer         key, value;

    if (!*address)
        return invalid(error, _("The server address is required."));
    if (!*uuid)
        return invalid(error, _("The user ID is required."));
    if (reality && !*sni)
        return invalid(error, _("REALITY requires a server name (SNI)."));
    if (reality && !*public_key)
        return invalid(error, _("REALITY requires a public key."));

    s_vpn = NM_SETTING_VPN(nm_setting_vpn_new());
    g_object_set(s_vpn, NM_SETTING_VPN_SERVICE_TYPE, NM_VLESS_SERVICE_TYPE, NULL);

    port = g_strdup_printf("%d",
                           gtk_spin_button_get_value_as_int(
                               GTK_SPIN_BUTTON(widget(self, "port_spin"))));
    add_item(s_vpn, NM_VLESS_KEY_ADDRESS, address);
    add_item(s_vpn, NM_VLESS_KEY_PORT, port);
    add_item(s_vpn, NM_VLESS_KEY_ENCRYPTION, *encryption ? encryption : "none");
    add_item(s_vpn, NM_VLESS_KEY_NETWORK, network);
    add_item(s_vpn, NM_VLESS_KEY_SECURITY, security);

    if ((tls || reality) && g_str_equal(network, "tcp")
        && gtk_check_button_get_active(GTK_CHECK_BUTTON(widget(self, "flow_check"))))
        add_item(s_vpn, NM_VLESS_KEY_FLOW, NM_VLESS_FLOW_VISION);
    if (tls || reality) {
        add_item(s_vpn, NM_VLESS_KEY_SNI, sni);
        add_item(s_vpn, NM_VLESS_KEY_FINGERPRINT, dropdown_get(&self->fingerprint));
    }
    if (tls)
        add_item(s_vpn, NM_VLESS_KEY_ALPN, alpn);
    if (reality) {
        add_item(s_vpn, NM_VLESS_KEY_REALITY_PUBLIC_KEY, public_key);
        add_item(s_vpn, NM_VLESS_KEY_REALITY_SHORT_ID, short_id);
        add_item(s_vpn, NM_VLESS_KEY_REALITY_SPIDER_X, spider_x);
    }
    if (is_path_network(network)) {
        add_item(s_vpn, NM_VLESS_KEY_PATH, path);
        add_item(s_vpn, NM_VLESS_KEY_HOST, host);
    }
    if (g_str_equal(network, "grpc")) {
        add_item(s_vpn, NM_VLESS_KEY_GRPC_SERVICE_NAME, service);
        add_item(s_vpn, NM_VLESS_KEY_MODE, dropdown_get(&self->grpc_mode));
    }
    if (g_str_equal(network, "xhttp")) {
        add_item(s_vpn, NM_VLESS_KEY_MODE, dropdown_get(&self->xhttp_mode));
        add_item(s_vpn, NM_VLESS_KEY_XHTTP_EXTRA, extra);
    }

    if (mtu != NM_VLESS_DEFAULT_MTU || self->had_mtu) {
        mtu_text = g_strdup_printf("%d", mtu);
        add_item(s_vpn, NM_VLESS_KEY_MTU, mtu_text);
    }
    add_item(s_vpn, NM_VLESS_KEY_DNS, dns);
    add_item(s_vpn, NM_VLESS_KEY_DNS6, dns6);
    if (!ipv6)
        add_item(s_vpn, NM_VLESS_KEY_IPV6, "no");
    else if (self->had_ipv6)
        add_item(s_vpn, NM_VLESS_KEY_IPV6, "yes");

    g_hash_table_iter_init(&iter, self->extra_data);
    while (g_hash_table_iter_next(&iter, &key, &value))
        add_item(s_vpn, key, value);

    /* The user ID is stored by NetworkManager unless another owner was chosen before. */
    add_item(s_vpn,
             NM_VLESS_KEY_UUID_FLAGS,
             self->uuid_flags && *self->uuid_flags ? self->uuid_flags : "0");
    nm_setting_vpn_add_secret(s_vpn, NM_VLESS_SECRET_UUID, uuid);

    nm_connection_add_setting(connection, NM_SETTING(s_vpn));
    prefer_tunnel_dns(connection);
    return TRUE;
}

/* ---- object ------------------------------------------------------------------------------- */

static GObject *
get_widget(NMVpnEditor *editor)
{
    return G_OBJECT(NM_VLESS_EDITOR(editor)->root);
}

/* Handlers are disconnected automatically when the editor is destroyed, even if the
 * host keeps the widget alive longer. */
static void
connect_swapped(NMVlessEditor *self, const char *id, const char *signal, GCallback callback)
{
    g_signal_connect_object(gtk_builder_get_object(self->builder, id),
                            signal,
                            callback,
                            self,
                            G_CONNECT_SWAPPED);
}

static void
connect_changed(NMVlessEditor *self, const char *id, const char *signal)
{
    connect_swapped(self, id, signal, G_CALLBACK(emit_changed));
}

static void
nm_vless_editor_init(NMVlessEditor *self)
{
    self->cancellable = g_cancellable_new();
    self->extra_data  = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, g_free);
}

static void
nm_vless_editor_dispose(GObject *object)
{
    NMVlessEditor *self = NM_VLESS_EDITOR(object);

    g_cancellable_cancel(self->cancellable);
    g_clear_object(&self->cancellable);
    g_clear_object(&self->root);
    g_clear_object(&self->builder);
    dropdown_clear(&self->security);
    dropdown_clear(&self->network);
    dropdown_clear(&self->fingerprint);
    dropdown_clear(&self->grpc_mode);
    dropdown_clear(&self->xhttp_mode);
    G_OBJECT_CLASS(nm_vless_editor_parent_class)->dispose(object);
}

static void
nm_vless_editor_finalize(GObject *object)
{
    NMVlessEditor *self = NM_VLESS_EDITOR(object);

    g_hash_table_unref(self->extra_data);
    g_free(self->uuid_flags);
    G_OBJECT_CLASS(nm_vless_editor_parent_class)->finalize(object);
}

static void
nm_vless_editor_class_init(NMVlessEditorClass *klass)
{
    GObjectClass *object_class = G_OBJECT_CLASS(klass);

    object_class->dispose  = nm_vless_editor_dispose;
    object_class->finalize = nm_vless_editor_finalize;
}

static void
nm_vless_editor_interface_init(NMVpnEditorInterface *iface)
{
    iface->get_widget        = get_widget;
    iface->update_connection = update_connection;
}

static const char *const changed_entries[] = {
    "address_entry",
    "uuid_entry",
    "encryption_entry",
    "sni_entry",
    "alpn_entry",
    "public_key_entry",
    "short_id_entry",
    "spider_x_entry",
    "path_entry",
    "host_entry",
    "service_name_entry",
    "xhttp_extra_entry",
    "dns_entry",
    "dns6_entry",
    NULL,
};

G_MODULE_EXPORT NMVpnEditor *
nm_vless_editor_new(NMVpnEditorPlugin *plugin, NMConnection *connection, GError **error)
{
    g_autoptr(NMVlessEditor) self = g_object_new(NM_VLESS_TYPE_EDITOR, NULL);

    self->builder = gtk_builder_new();
    gtk_builder_set_translation_domain(self->builder, GETTEXT_PACKAGE);
    if (!gtk_builder_add_from_resource(self->builder, NM_VLESS_UI_RESOURCE, error))
        return NULL;
    self->root = g_object_ref(widget(self, "vless_root"));

    dropdown_init(&self->security, self, "security_dropdown", security_choices);
    dropdown_init(&self->network, self, "network_dropdown", network_choices);
    dropdown_init(&self->fingerprint, self, "fingerprint_dropdown", fingerprint_choices);
    dropdown_init(&self->grpc_mode, self, "grpc_mode_dropdown", grpc_mode_choices);
    dropdown_init(&self->xhttp_mode, self, "xhttp_mode_dropdown", xhttp_mode_choices);

    load_connection(self, connection);

    for (const char *const *id = changed_entries; *id; id++)
        connect_changed(self, *id, "changed");
    connect_changed(self, "port_spin", "value-changed");
    connect_changed(self, "mtu_spin", "value-changed");
    connect_changed(self, "flow_check", "toggled");
    connect_changed(self, "ipv6_check", "toggled");
    connect_changed(self, "security_dropdown", "notify::selected");
    connect_changed(self, "network_dropdown", "notify::selected");
    connect_changed(self, "fingerprint_dropdown", "notify::selected");
    connect_changed(self, "grpc_mode_dropdown", "notify::selected");
    connect_changed(self, "xhttp_mode_dropdown", "notify::selected");

    connect_swapped(self, "link_entry", "changed", G_CALLBACK(link_entry_changed));
    connect_swapped(self, "link_entry", "activate", G_CALLBACK(link_activated));
    connect_swapped(self, "link_button", "clicked", G_CALLBACK(link_activated));

    return NM_VPN_EDITOR(g_steal_pointer(&self));
}
