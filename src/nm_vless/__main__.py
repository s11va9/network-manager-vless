# SPDX-License-Identifier: GPL-2.0-or-later
"""Allow ``python -m nm_vless`` as an alias for the ``nm-vless`` command."""

import sys

from nm_vless.cli import main

sys.exit(main())
