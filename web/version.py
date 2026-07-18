"""Single source of truth for the app version shown in the UI.

Reads it straight from pyproject.toml's ``[project] version`` so there's
exactly one place to bump. A plain regex rather than tomllib keeps this
working on Python 3.10 (tomllib landed in 3.11, and requires-python is
>=3.10). Read once at import and cached — pyproject doesn't change at
runtime.
"""

from __future__ import annotations

import os
import re

_PYPROJECT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pyproject.toml")


def _read_version() -> str:
    try:
        with open(_PYPROJECT, encoding="utf-8") as f:
            text = f.read()
        # First `version = "..."` — that's the [project] one (well before
        # [tool.ruff]'s target-version, which anyway starts with "target-").
        m = re.search(r'^\s*version\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
        if m:
            return m.group(1)
    except OSError:
        pass
    return "unknown"


VERSION = _read_version()


def get_version() -> str:
    return VERSION
