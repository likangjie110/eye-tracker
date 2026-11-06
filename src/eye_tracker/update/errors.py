"""Errors shared by the update transport and its rules (TD-ARCH-006).

Extracted from ``update/fetch.py`` so ``winhttp.py`` can raise the same error without importing
``fetch`` (which imports ``winhttp`` lazily for the default transport). The message is meant for
the user.
"""

from __future__ import annotations


class NetworkError(OSError):
    """A request that failed or was refused."""


class Cancelled(Exception):
    """The user cancelled a download."""
