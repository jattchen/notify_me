#!/usr/bin/env python3
"""Skill-local wrapper; the implementation lives at the plugin root."""

import sys
import os
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[3]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
os.environ["NOTIFY_ME_PLUGIN_ROOT"] = str(PLUGIN_ROOT)

from notify_me.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
