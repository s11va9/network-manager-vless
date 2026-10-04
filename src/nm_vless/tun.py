# SPDX-License-Identifier: GPL-2.0-or-later
"""Creation of Linux TUN devices.

The privileged service creates the device and passes its file descriptor to an
unprivileged Xray process. The device disappears when the last descriptor is
closed, so no explicit cleanup is required.
"""

from __future__ import annotations

import fcntl
import os
import struct
from typing import Final

__all__ = ["DEFAULT_NAME_TEMPLATE", "create_tun"]

_TUN_CLONE_DEVICE: Final = "/dev/net/tun"
_TUNSETIFF: Final = 0x400454CA
_IFF_TUN: Final = 0x0001
_IFF_NO_PI: Final = 0x1000  # Xray requires raw IP packets without a packet-info header
_IFNAMSIZ: Final = 16
_IFREQ_FORMAT: Final = f"{_IFNAMSIZ}sH22x"  # struct ifreq: name + flags, 40 bytes

#: The kernel replaces ``%d`` with the first free index.
DEFAULT_NAME_TEMPLATE: Final = "vless%d"


def create_tun(name_template: str = DEFAULT_NAME_TEMPLATE) -> tuple[int, str]:
    """Create a TUN device and return ``(fd, interface_name)``.

    The descriptor is close-on-exec; the caller passes it to the child
    explicitly and owns closing it. Requires ``CAP_NET_ADMIN``.
    """
    encoded = name_template.encode("ascii")
    if not encoded or len(encoded) >= _IFNAMSIZ:
        raise ValueError(f"interface name template must be 1-{_IFNAMSIZ - 1} characters")

    fd = os.open(_TUN_CLONE_DEVICE, os.O_RDWR | os.O_CLOEXEC)
    try:
        request = struct.pack(_IFREQ_FORMAT, encoded, _IFF_TUN | _IFF_NO_PI)
        reply = fcntl.ioctl(fd, _TUNSETIFF, request)
    except BaseException:
        os.close(fd)
        raise
    name = reply[:_IFNAMSIZ].split(b"\0", 1)[0].decode("ascii")
    return fd, name
