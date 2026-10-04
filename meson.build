# SPDX-License-Identifier: GPL-2.0-or-later
project('NetworkManager-vless',
  version: '0.5.0',
  license: 'GPL-2.0-or-later',
  meson_version: '>= 1.1.0',
  default_options: ['c_std=gnu11', 'warning_level=2', 'werror=false'],
)

gnome = import('gnome')
i18n = import('i18n')
python = import('python').find_installation(get_option('python'), modules: ['gi'])

gettext_package = 'NetworkManager-vless'
prefix = get_option('prefix')
bindir = prefix / get_option('bindir')
libexecdir = prefix / get_option('libexecdir')
pkglibexecdir = libexecdir / 'nm-vless'
pkgdatadir = prefix / get_option('datadir') / 'nm-vless'
docdir = get_option('docdir') != '' ? get_option('docdir') : prefix / get_option('datadir') / 'doc' / 'NetworkManager-vless'

# Integration directories are absolute: NetworkManager, D-Bus and systemd only
# look in fixed locations, independently of the installation prefix.
nm_vpn_dir = get_option('nm_vpn_dir')
if nm_vpn_dir == ''
  nm_vpn_dir = '/usr/lib/NetworkManager/VPN'
endif
systemd = dependency('systemd', required: false)
sysusers_dir = get_option('sysusers_dir')
if sysusers_dir == ''
  sysusers_dir = systemd.get_variable(pkgconfig: 'sysusersdir', default_value: '/usr/lib/sysusers.d')
endif
user_unit_dir = get_option('systemd_user_unit_dir')
if user_unit_dir == ''
  user_unit_dir = systemd.get_variable(pkgconfig: 'systemduserunitdir',
    default_value: '/usr/lib/systemd/user')
endif

# ---- GNOME Settings editor (C, GTK 4) -----------------------------------------------------------

editor_opt = get_option('editor')
libnm_dep = dependency('libnm', version: '>= 1.24', required: editor_opt)
gtk4_dep = dependency('gtk4', version: '>= 4.6', required: editor_opt)
build_editor = libnm_dep.found() and gtk4_dep.found() and add_languages('c', required: editor_opt)

nm_plugin_dir = prefix / get_option('libdir') / 'NetworkManager'
libnm_section = ''
if build_editor
  gmodule_dep = dependency('gmodule-2.0', version: '>= 2.70')
  dl_dep = meson.get_compiler('c').find_library('dl', required: false)
  add_project_arguments(
    meson.get_compiler('c').get_supported_arguments(
      '-Wno-unused-parameter', '-Wno-missing-field-initializers', '-Wshadow',
      '-Wstrict-prototypes', '-Wformat=2', '-Werror=implicit-function-declaration'),
    language: 'c')
  libnm_section = '\n[libnm]\nplugin=' + nm_plugin_dir / 'libnm-vpn-plugin-vless.so\n'
  subdir('src/editor')
  subdir('po')
endif

# ---- bundled Xray-core ----------------------------------------------------------------------------

bundled_xray = get_option('bundled_xray')
bundled_xray_path = ''
if bundled_xray != ''
  bundled_xray_path = pkglibexecdir / 'xray'
  install_data(bundled_xray, rename: 'xray', install_dir: pkglibexecdir,
    install_mode: 'rwxr-xr-x')
endif

# ---- Python package, service and tool ------------------------------------------------------------

conf = configuration_data()
conf.set('PYTHON', python.full_path())
conf.set('PKGDATADIR', pkgdatadir)
conf.set('LIBEXECDIR', libexecdir)
conf.set('BINDIR', bindir)
conf.set('DOCDIR', docdir)
conf.set('BUNDLED_XRAY', bundled_xray_path)
conf.set('LOCALEDIR', prefix / get_option('localedir'))
conf.set('LIBNM_SECTION', libnm_section)

install_subdir('src/nm_vless',
  install_dir: pkgdatadir,
  exclude_directories: ['__pycache__'],
  exclude_files: ['_build_paths.py', '_build_paths.py.in'],
)
configure_file(
  input: 'src/nm_vless/_build_paths.py.in',
  output: '_build_paths.py',
  configuration: conf,
  install_dir: pkgdatadir / 'nm_vless',
)

configure_file(
  input: 'src/nm-vless-service.in',
  output: 'nm-vless-service',
  configuration: conf,
  install_dir: libexecdir,
  install_mode: 'rwxr-xr-x',
)
configure_file(
  input: 'src/nm-vless-auth-dialog.in',
  output: 'nm-vless-auth-dialog',
  configuration: conf,
  install_dir: libexecdir,
  install_mode: 'rwxr-xr-x',
)
configure_file(
  input: 'src/nm-vless.in',
  output: 'nm-vless',
  configuration: conf,
  install_dir: bindir,
  install_mode: 'rwxr-xr-x',
)
configure_file(
  input: 'data/nm-vless-service.name.in',
  output: 'nm-vless-service.name',
  configuration: conf,
  install_dir: nm_vpn_dir,
)
configure_file(
  input: 'data/nm-vless-subscriptions.service.in',
  output: 'nm-vless-subscriptions.service',
  configuration: conf,
  install_dir: user_unit_dir,
)
configure_file(
  input: 'data/nm-vless-proxy-guard.service.in',
  output: 'nm-vless-proxy-guard.service',
  configuration: conf,
  install_dir: user_unit_dir,
)
install_data('data/nm-vless-subscriptions.timer', install_dir: user_unit_dir)
install_data('data/nm-vless-service.conf', install_dir: get_option('dbus_policy_dir'))
install_data('data/nm-vless.sysusers.conf', rename: 'nm-vless.conf', install_dir: sysusers_dir)
install_data('README.md', 'README.ru.md', 'CHANGELOG.md', install_dir: docdir)

meson.add_install_script('build-aux/meson-postinstall.sh', sysusers_dir / 'nm-vless.conf')

pytest = find_program('pytest', required: false)
if pytest.found()
  test('pytest', pytest,
    args: ['-q', meson.project_source_root() / 'tests'],
    workdir: meson.project_source_root(),
    env: {'NM_VLESS_BUILD_DIR': meson.project_build_root()},
    timeout: 300,
  )
endif

summary({
  'prefix': prefix,
  'python': python.full_path(),
  'NetworkManager VPN dir': nm_vpn_dir,
  'GNOME Settings editor': build_editor,
  'libnm plugin dir': build_editor ? nm_plugin_dir : '-',
  'bundled xray': bundled_xray != '' ? bundled_xray_path : 'no (uses /usr/local/bin/xray)',
  'D-Bus policy dir': get_option('dbus_policy_dir'),
  'sysusers.d': sysusers_dir,
  'systemd user units': user_unit_dir,
})
