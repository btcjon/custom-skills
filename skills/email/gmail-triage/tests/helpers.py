"""Shared test helpers: import the skill scripts without installing a package."""

from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PACKAGE_ROOT / "scripts"
FIXTURES = PACKAGE_ROOT / "fixtures"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import triage_core  # noqa: E402
import triage_state  # noqa: E402
import unsubscribe_oneclick  # noqa: E402

__all__ = ["PACKAGE_ROOT", "SCRIPTS", "FIXTURES", "triage_core", "triage_state", "unsubscribe_oneclick"]
