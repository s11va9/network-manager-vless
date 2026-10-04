# SPDX-License-Identifier: GPL-2.0-or-later
# GNOME Settings integration: the libnm editor plugin and its GTK 4 editor.

editor_conf = configuration_data()
editor_conf.set_quoted('GETTEXT_PACKAGE', gettext_package)
editor_conf.set_quoted('NM_VLESS_LOCALEDIR', prefix / get_option('localedir'))
editor_conf.set_quoted('NM_VLESS_PLUGIN_DIR', nm_plugin_dir)
editor_conf.set_quoted('NM_VLESS_CLI_PATH', bindir / 'nm-vless')
editor_conf.set('G_LOG_DOMAIN', '"nm-vless"')
editor_conf.set('GLIB_VERSION_MIN_REQUIRED', 'GLIB_VERSION_2_70')
editor_conf.set('NM_VERSION_MIN_REQUIRED', 'NM_VERSION_1_24')
configure_file(output: 'config.h', configuration: editor_conf)

editor_c_args = ['-include', 'config.h']
common_sources = files('nm-vless-common.c')

libnm_vpn_plugin_vless = shared_module('nm-vpn-plugin-vless',
  common_sources + files('nm-vless-editor-plugin.c'),
  name_prefix: 'lib',
  c_args: editor_c_args,
  dependencies: [libnm_dep, gmodule_dep, dl_dep],
  gnu_symbol_visibility: 'hidden',
  install: true,
  install_dir: nm_plugin_dir,
)

editor_resources = gnome.compile_resources('nm-vless-editor-resources',
  'nm-vless-editor.gresource.xml',
  c_name: 'nm_vless_editor',
)

libnm_gtk4_vpn_plugin_vless_editor = shared_module('nm-gtk4-vpn-plugin-vless-editor',
  common_sources + files('nm-vless-editor.c') + editor_resources,
  name_prefix: 'lib',
  c_args: editor_c_args,
  dependencies: [libnm_dep, gtk4_dep, gmodule_dep],
  gnu_symbol_visibility: 'hidden',
  install: true,
  install_dir: nm_plugin_dir,
)
