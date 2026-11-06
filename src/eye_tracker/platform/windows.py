"""Windows integration: the platform class the factory instantiates.

The Win32 binding lives in ``windows_api.py`` and the policy in ``windows_platform.py``
(phase 4 / B2). This module stays the import point used by ``platform/__init__.py``.
"""

from __future__ import annotations

from .windows_platform import WindowsPlatform

__all__ = ["WindowsPlatform"]
