# SPDX-License-Identifier: GPL-2.0-or-later
"""Installation paths recorded by Meson in the generated ``_build_paths`` module."""

from __future__ import annotations

import gettext
import importlib


def build_value(name: str, default: str = "") -> str:
    """Return a value from ``nm_vless._build_paths``, or *default* in a source tree."""
    try:
        module = importlib.import_module("nm_vless._build_paths")
    except ImportError:
        return default
    return str(getattr(module, name, default))


def translation() -> gettext.NullTranslations:
    """Translations of the ``NetworkManager-vless`` domain (none in a source tree)."""
    localedir = build_value("LOCALEDIR")
    if not localedir:
        return gettext.NullTranslations()
    return gettext.translation("NetworkManager-vless", localedir, fallback=True)
