/* SPDX-License-Identifier: GPL-2.0-or-later */
/*
 * Helpers shared by the editor plugins. Link parsing and subscriptions are
 * delegated to the "nm-vless" command, so the rules live in one place (Python).
 */
#include "config.h"

#include "nm-vless-common.h"

#include <string.h>

const char *
nm_vless_cli_path(void)
{
    const char *path = g_getenv("NM_VLESS_CLI");

    return (path && *path) ? path : NM_VLESS_CLI_PATH;
}

static GSubprocess *
spawn_cli(const char *const *args, GError **error)
{
    g_autoptr(GPtrArray) argv = g_ptr_array_new();

    g_ptr_array_add(argv, (gpointer) nm_vless_cli_path());
    for (const char *const *arg = args; *arg; arg++)
        g_ptr_array_add(argv, (gpointer) *arg);
    g_ptr_array_add(argv, NULL);

    return g_subprocess_newv((const char *const *) argv->pdata,
                             G_SUBPROCESS_FLAGS_STDIN_PIPE | G_SUBPROCESS_FLAGS_STDOUT_PIPE
                                 | G_SUBPROCESS_FLAGS_STDERR_PIPE,
                             error);
}

/* Turn the result of the command into its output or a readable error. */
static char *
check_result(GSubprocess *proc, char *out, char *err, GError **error)
{
    g_autofree char *stdout_text = out;
    g_autofree char *stderr_text = err;
    g_auto(GStrv) lines          = NULL;
    const char *message          = NULL;

    if (g_subprocess_get_successful(proc))
        return g_steal_pointer(&stdout_text);

    /* The last line of stderr carries the error; earlier lines are progress notes. */
    lines = g_strsplit(stderr_text ? stderr_text : "", "\n", -1);
    for (guint i = 0; lines[i]; i++) {
        char *line = g_strstrip(lines[i]);

        if (*line)
            message = line;
    }
    if (message) {
        if (g_str_has_prefix(message, "nm-vless: "))
            message += strlen("nm-vless: ");
        g_set_error_literal(error, G_IO_ERROR, G_IO_ERROR_INVALID_DATA, message);
    } else {
        g_set_error(error, G_IO_ERROR, G_IO_ERROR_FAILED, "%s failed", nm_vless_cli_path());
    }
    return NULL;
}

char *
nm_vless_cli_sync(const char *const *args,
                  const char        *input,
                  GCancellable      *cancellable,
                  GError           **error)
{
    g_autoptr(GSubprocess) proc = NULL;
    char *out = NULL;
    char *err = NULL;

    proc = spawn_cli(args, error);
    if (!proc)
        return NULL;
    if (!g_subprocess_communicate_utf8(proc, input, cancellable, &out, &err, error))
        return NULL;
    return check_result(proc, out, err, error);
}

static void
communicate_cb(GObject *source, GAsyncResult *result, gpointer user_data)
{
    GSubprocess    *proc  = G_SUBPROCESS(source);
    g_autoptr(GTask) task = user_data;
    GError         *error = NULL;
    char           *out   = NULL;
    char           *err   = NULL;
    char           *text;

    if (!g_subprocess_communicate_utf8_finish(proc, result, &out, &err, &error)) {
        g_task_return_error(task, error);
        return;
    }
    text = check_result(proc, out, err, &error);
    if (text)
        g_task_return_pointer(task, text, g_free);
    else
        g_task_return_error(task, error);
}

void
nm_vless_cli_async(const char *const  *args,
                   const char         *input,
                   GCancellable       *cancellable,
                   GAsyncReadyCallback callback,
                   gpointer            user_data)
{
    g_autoptr(GTask) task       = g_task_new(NULL, cancellable, callback, user_data);
    g_autoptr(GSubprocess) proc = NULL;
    GError *error               = NULL;

    g_task_set_source_tag(task, nm_vless_cli_async);
    proc = spawn_cli(args, &error);
    if (!proc) {
        g_task_return_error(task, error);
        return;
    }
    g_subprocess_communicate_utf8_async(proc,
                                        input,
                                        cancellable,
                                        communicate_cb,
                                        g_steal_pointer(&task));
}

char *
nm_vless_cli_finish(GAsyncResult *result, GError **error)
{
    g_return_val_if_fail(g_task_is_valid(result, NULL), NULL);
    return g_task_propagate_pointer(G_TASK(result), error);
}

static const char *const parse_link_args[] = {"parse-link", NULL};

char *
nm_vless_parse_link_sync(const char *link, GCancellable *cancellable, GError **error)
{
    return nm_vless_cli_sync(parse_link_args, link, cancellable, error);
}

void
nm_vless_parse_link_async(const char         *link,
                          GCancellable       *cancellable,
                          GAsyncReadyCallback callback,
                          gpointer            user_data)
{
    nm_vless_cli_async(parse_link_args, link, cancellable, callback, user_data);
}

char *
nm_vless_parse_link_finish(GAsyncResult *result, GError **error)
{
    return nm_vless_cli_finish(result, error);
}

NMConnection *
nm_vless_connection_from_keyfile(const char *data, GError **error)
{
    g_autoptr(GKeyFile) keyfile     = g_key_file_new();
    g_autoptr(NMConnection) connection = NULL;
    g_auto(GStrv) data_keys         = NULL;
    g_auto(GStrv) secret_keys       = NULL;
    g_autofree char *id             = NULL;
    g_autofree char *uuid           = nm_utils_uuid_generate();
    NMSettingConnection *s_con;
    NMSettingVpn        *s_vpn;

    if (!g_key_file_load_from_data(keyfile, data, -1, G_KEY_FILE_NONE, error))
        return NULL;

    id = g_key_file_get_string(keyfile, "connection", "id", NULL);
    data_keys = g_key_file_get_keys(keyfile, "vpn", NULL, error);
    if (!data_keys)
        return NULL;
    secret_keys = g_key_file_get_keys(keyfile, "vpn-secrets", NULL, NULL);

    connection = nm_simple_connection_new();

    s_con = NM_SETTING_CONNECTION(nm_setting_connection_new());
    g_object_set(s_con,
                 NM_SETTING_CONNECTION_ID,
                 id ? id : "VLESS",
                 NM_SETTING_CONNECTION_UUID,
                 uuid,
                 NM_SETTING_CONNECTION_TYPE,
                 NM_SETTING_VPN_SETTING_NAME,
                 NULL);
    nm_connection_add_setting(connection, NM_SETTING(s_con));

    s_vpn = NM_SETTING_VPN(nm_setting_vpn_new());
    g_object_set(s_vpn, NM_SETTING_VPN_SERVICE_TYPE, NM_VLESS_SERVICE_TYPE, NULL);
    for (guint i = 0; data_keys[i]; i++) {
        g_autofree char *value = g_key_file_get_string(keyfile, "vpn", data_keys[i], NULL);

        if (value && *value)
            nm_setting_vpn_add_data_item(s_vpn, data_keys[i], value);
    }
    for (guint i = 0; secret_keys && secret_keys[i]; i++) {
        g_autofree char *value = g_key_file_get_string(keyfile, "vpn-secrets", secret_keys[i], NULL);

        if (value && *value)
            nm_setting_vpn_add_secret(s_vpn, secret_keys[i], value);
    }
    nm_connection_add_setting(connection, NM_SETTING(s_vpn));

    return g_steal_pointer(&connection);
}

char *
nm_vless_find_link(const char *text)
{
    g_auto(GStrv) lines = NULL;

    if (!text)
        return NULL;
    lines = g_strsplit(text, "\n", -1);
    for (guint i = 0; lines[i]; i++) {
        char *line = g_strstrip(lines[i]);

        if (g_ascii_strncasecmp(line, "vless://", strlen("vless://")) == 0)
            return g_strdup(line);
    }
    return NULL;
}
