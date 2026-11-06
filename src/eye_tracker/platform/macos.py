"""macOS integration: the platform class the factory instantiates.

The bindings live in ``macos_api.py`` and the policy in ``macos_platform.py`` (phase 4 / B2).
This module stays the import point used by ``platform/__init__.py``.
"""

from __future__ import annotations

from .macos_platform import MacPlatform

__all__ = ["MacPlatform"]
