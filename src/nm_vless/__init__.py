# SPDX-License-Identifier: GPL-2.0-or-later
"""NetworkManager VPN plugin for VLESS connections powered by Xray-core."""

from typing import Final

__version__: Final = "0.5.0"

#: D-Bus service name and ``vpn.service-type`` of connections handled by this plugin.
SERVICE_TYPE: Final = "org.freedesktop.NetworkManager.vless"
