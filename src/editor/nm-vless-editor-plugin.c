/* SPDX-License-Identifier: GPL-2.0-or-later */
/*
 * libnm-vpn-plugin-vless.so: the NMVpnEditorPlugin loaded by NetworkManager
 * clients (GNOME Settings, nmcli). It has no GTK dependency; the GTK 4 editor
 * lives in a separate module that is loaded on demand, as in other VPN plugins.
 */
#define _GNU_SOURCE
#include "config.h"

#include "nm-vless-common.h"

#include <dlfcn.h>
#include <glib/gi18n-lib.h>
#include <gmodule.h>

#define NM_VLESS_EDITOR_MODULE  "libnm-gtk4-vpn-plugin-vless-editor.so"
#define NM_VLESS_EDITOR_FACTORY "nm_vless_editor_new"
#define NM_VLESS_MAX_IMPORT_SIZE (64 * 1024)

typedef NMVpnEditor *(*NMVlessEditorFactory)(NMVpnEditorPlugin *plugin,
                                             NMConnection      *connection,
                                             GError           **error);

enum {
    PROP_0,
    PROP_NAME,
    PROP_DESCRIPTION,
    PROP_SERVICE,
};

#define NM_VLESS_TYPE_EDITOR_PLUGIN (nm_vless_editor_plugin_get_type())
G_DECLARE_FINAL_TYPE(NMVlessEditorPlugin, nm_vless_editor_plugin, NM_VLESS, EDITOR_PLUGIN, GObject)

struct _NMVlessEditorPlugin {
    GObject parent_instance;
};

static void nm_vless_editor_plugin_interface_init(NMVpnEditorPluginInterface *iface);

G_DEFINE_TYPE_WITH_CODE(NMVlessEditorPlugin,
                        nm_vless_editor_plugin,
                        G_TYPE_OBJECT,
                        G_IMPLEMENT_INTERFACE(NM_TYPE_VPN_EDITOR_PLUGIN,
                                              nm_vless_editor_plugin_interface_init))

/* Directory of this module, so the editor is found next to it (also in the build tree). */
static char *
module_directory(void)
{
    Dl_info info;

    if (dladdr((void *) module_directory, &info) && info.dli_fname)
        return g_path_get_dirname(info.dli_fname);
    return g_strdup(NM_VLESS_PLUGIN_DIR);
}

static gboolean
host_uses_gtk3(void)
{
    GModule *self = g_module_open(NULL, 0);
    gpointer symbol;
    gboolean gtk3;

    if (!self)
        return FALSE;
    /* gtk_container_add() exists in GTK 3 only. */
    gtk3 = g_module_symbol(self, "gtk_container_add", &symbol);
    g_module_close(self);
    return gtk3;
}

static NMVpnEditor *
get_editor(NMVpnEditorPlugin *plugin, NMConnection *connection, GError **error)
{
    static NMVlessEditorFactory factory;

    if (host_uses_gtk3()) {
        g_set_error_literal(error,
                            NM_VPN_PLUGIN_ERROR,
                            NM_VPN_PLUGIN_ERROR_FAILED,
                            _("The VLESS editor requires GTK 4. Use GNOME Settings or nm-vless."));
        return NULL;
    }

    if (!factory) {
        g_autofree char *dir  = module_directory();
        g_autofree char *path = g_build_filename(dir, NM_VLESS_EDITOR_MODULE, NULL);
        GModule         *module;

        module = g_module_open(path, G_MODULE_BIND_LAZY | G_MODULE_BIND_LOCAL);
        if (!module) {
            g_set_error(error,
                        NM_VPN_PLUGIN_ERROR,
                        NM_VPN_PLUGIN_ERROR_FAILED,
                        _("Cannot load the VLESS editor: %s"),
                        g_module_error());
            return NULL;
        }
        if (!g_module_symbol(module, NM_VLESS_EDITOR_FACTORY, (gpointer *) &factory)) {
            g_set_error(error,
                        NM_VPN_PLUGIN_ERROR,
                        NM_VPN_PLUGIN_ERROR_FAILED,
                        _("Cannot load the VLESS editor: %s"),
                        g_module_error());
            g_module_close(module);
            return NULL;
        }
        g_module_make_resident(module);
    }
    return factory(plugin, connection, error);
}

static NMVpnEditorPluginCapability
get_capabilities(NMVpnEditorPlugin *plugin)
{
    return NM_VPN_EDITOR_PLUGIN_CAPABILITY_IMPORT | NM_VPN_EDITOR_PLUGIN_CAPABILITY_IPV6;
}

/* Import a text file that contains a vless:// link (for example saved from a browser). */
static NMConnection *
import_from_file(NMVpnEditorPlugin *plugin, const char *path, GError **error)
{
    g_autofree char *contents = NULL;
    g_autofree char *link     = NULL;
    g_autofree char *keyfile  = NULL;
    gsize            length   = 0;

    if (!g_file_get_contents(path, &contents, &length, error))
        return NULL;
    if (length > NM_VLESS_MAX_IMPORT_SIZE || !g_utf8_validate(contents, length, NULL)
        || !(link = nm_vless_find_link(contents))) {
        g_set_error_literal(error,
                            NM_CONNECTION_ERROR,
                            NM_CONNECTION_ERROR_FAILED,
                            _("The file does not contain a vless:// link."));
        return NULL;
    }
    keyfile = nm_vless_parse_link_sync(link, NULL, error);
    if (!keyfile)
        return NULL;
    return nm_vless_connection_from_keyfile(keyfile, error);
}

static void
get_property(GObject *object, guint prop_id, GValue *value, GParamSpec *pspec)
{
    switch (prop_id) {
    case PROP_NAME:
        g_value_set_string(value, _("VLESS (Xray)"));
        break;
    case PROP_DESCRIPTION:
        g_value_set_string(value,
                           _("Connect to VLESS servers with TLS or REALITY over TCP, XHTTP, "
                             "WebSocket, HTTPUpgrade or gRPC."));
        break;
    case PROP_SERVICE:
        g_value_set_string(value, NM_VLESS_SERVICE_TYPE);
        break;
    default:
        G_OBJECT_WARN_INVALID_PROPERTY_ID(object, prop_id, pspec);
        break;
    }
}

static void
nm_vless_editor_plugin_init(NMVlessEditorPlugin *self)
{}

static void
nm_vless_editor_plugin_class_init(NMVlessEditorPluginClass *klass)
{
    GObjectClass *object_class = G_OBJECT_CLASS(klass);

    object_class->get_property = get_property;
    g_object_class_override_property(object_class, PROP_NAME, NM_VPN_EDITOR_PLUGIN_NAME);
    g_object_class_override_property(object_class,
                                     PROP_DESCRIPTION,
                                     NM_VPN_EDITOR_PLUGIN_DESCRIPTION);
    g_object_class_override_property(object_class, PROP_SERVICE, NM_VPN_EDITOR_PLUGIN_SERVICE);
}

static void
nm_vless_editor_plugin_interface_init(NMVpnEditorPluginInterface *iface)
{
    iface->get_editor       = get_editor;
    iface->get_capabilities = get_capabilities;
    iface->import_from_file = import_from_file;
}

G_MODULE_EXPORT NMVpnEditorPlugin *
nm_vpn_editor_plugin_factory(GError **error)
{
    g_return_val_if_fail(!error || !*error, NULL);

    bindtextdomain(GETTEXT_PACKAGE, NM_VLESS_LOCALEDIR);
    bind_textdomain_codeset(GETTEXT_PACKAGE, "UTF-8");
    return g_object_new(NM_VLESS_TYPE_EDITOR_PLUGIN, NULL);
}
