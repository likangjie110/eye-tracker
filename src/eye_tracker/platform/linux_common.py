"""Linux platform internals (phase 4 / B2).

The former single ``platform/linux.py`` is split by mechanism: shared constants (``linux_common``),
pure desktop/session helpers (``linux_desktop``), X11 idle/EWMH (``linux_x11``), QtDBus
(``linux_dbus``) and the camera watcher (``linux_camera``). ``linux.py`` keeps ``LinuxPlatform``.
The mechanism modules import ``linux_common`` only, so there is no facade/backend cycle.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import TypeVar

log = logging.getLogger(__name__)


_T = TypeVar("_T")

#: Filesystem roots. Module-level so tests can point them at a fake tree.
PROC_ROOT = Path("/proc")
DEV_ROOT = Path("/dev")
SYS_ROOT = Path("/sys")

_ACTION_TIMEOUT_S = 5.0  # lock / display power: user-visible, may legitimately take a moment
_QUERY_TIMEOUT_S = 2.0  # polled queries must never stall a tick for long
_IDLE_QUERY_TIMEOUT_S = 1.0  # the idle time is read on every tick
_DBUS_TIMEOUT_MS = 500
_LOCKED_HINT_TTL_S = 2.0
_IDLE_TTL_S = 0.5
_RETRY_AFTER_FAILURE_S = 60.0
#: Retry delay after a failed idle query on a desktop where it worked before:
#: a busy gnome-shell must not hide the user's typing for a whole minute.
_IDLE_TRANSIENT_RETRY_S = 1.0
_SESSION_RETRY_S = 30.0
#: How long ``loginctl lock-session`` may take to make a locker appear.
_LOCK_CONFIRM_S = 2.0
_LOCK_CONFIRM_POLL_S = 0.2
#: An idle-timer reset this close to our own ``ydotool`` motion was caused by it.
_INJECTION_WINDOW_S = 0.5
#: While displays are off with DPMS enabled only for that, how often and how
#: long to watch for them waking up (so DPMS can be switched back off).
_DPMS_WATCH_INTERVAL_S = 1.0
_DPMS_WATCH_MAX_S = 12 * 3600.0

# X11 protocol constants (<X11/X.h>). Duplicated here so the EWMH logic below can
# be exercised with a fake display on machines without python-xlib.
_X_ANY_PROPERTY_TYPE = 0
_X_CURRENT_TIME = 0
_X_MOTION_NOTIFY = 6
_X_IS_VIEWABLE = 2
_X_SUBSTRUCTURE_NOTIFY_MASK = 1 << 19
_X_SUBSTRUCTURE_REDIRECT_MASK = 1 << 20
_DPMS_MODE_ON = 0  # Xlib.ext.dpms.DPMSModeOn
_DPMS_MODE_OFF = 3  # Xlib.ext.dpms.DPMSModeOff

#: ``_NET_ACTIVE_WINDOW`` source indication "pager": window managers apply no
#: focus-stealing prevention to requests that come from pagers and taskbars.
_EWMH_SOURCE_PAGER = 2

#: Window types that are part of the desktop shell, not user windows.
_SKIPPED_WINDOW_TYPES = (
    "_NET_WM_WINDOW_TYPE_DESKTOP",
    "_NET_WM_WINDOW_TYPE_DOCK",
    "_NET_WM_WINDOW_TYPE_SPLASH",
    "_NET_WM_WINDOW_TYPE_NOTIFICATION",
)

_VIDEO_NODE_RE = re.compile(r"video\d+")
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_.-]+")
_GDBUS_UINT_RE = re.compile(r"\(\s*(?:u?int(?:16|32|64)\s+)?(\d+)\s*,?\s*\)")
_XSET_DPMS_RE = re.compile(r"dpms is (enabled|disabled)")

#: Processes that keep camera nodes open to *monitor* them. They only count as
#: "using the camera" while they stream (see ``_maps_video_device``).
_CAMERA_BROKERS = ("pipewire", "wireplumber")

#: Screen lockers that run exactly while the screen is locked and do not report
#: it to logind. ``/proc/<pid>/comm`` holds at most 15 characters.
_LOCKER_COMMS = frozenset(
    name[:15]
    for name in (
        "i3lock",
        "swaylock",
        "hyprlock",
        "xsecurelock",
        "slock",
        "gtklock",
        "waylock",
        "xlock",
        "alock",
        "sflock",
        "xtrlock",
        "kscreenlocker_greet",
        "xscreensaver-auth",
    )
)

#: Desktops whose own locker reacts to logind's ``Lock`` signal, so
#: ``loginctl lock-session`` is trusted there without further evidence.
_LOGIND_LOCK_DESKTOPS = (
    "gnome",
    "kde",
    "plasma",
    "cinnamon",
    "mate",
    "xfce",
    "budgie",
    "unity",
    "deepin",
)

#: Distributions that brand GNOME Shell sessions with their own name in front
#: (``ubuntu:GNOME``, ``pop:GNOME``). Other desktops that list GNOME second do so
#: for compatibility only: Budgie (``Budgie:GNOME``) locks with budgie-screensaver,
#: a gnome-screensaver 3.6 fork that never reports to logind's ``LockedHint``.
_GNOME_SHELL_VENDORS = ("ubuntu", "pop", "zorin", "endless")

#: Variables set by tiling window managers / compositors for their IPC (sway
#: also sets ``I3SOCK``). See ``_standalone_compositor``.
_COMPOSITOR_SOCKET_VARS = (
    "SWAYSOCK",
    "I3SOCK",
    "HYPRLAND_INSTANCE_SIGNATURE",
    "NIRI_SOCKET",
    "WAYFIRE_SOCKET",
)

#: D-Bus errors meaning "this service / method does not exist here" (as opposed
#: to a timeout or a busy service), in QtDBus error names and gdbus messages.
_DBUS_MISSING_MARKERS = (
    "ServiceUnknown",
    "UnknownMethod",
    "UnknownObject",
    "UnknownInterface",
    "NameHasNoOwner",
    "was not provided by any .service files",
)

_MUTTER_IDLE_SERVICE = "org.gnome.Mutter.IdleMonitor"
_MUTTER_IDLE_PATH = "/org/gnome/Mutter/IdleMonitor/Core"
_MUTTER_IDLE = (
    "gdbus",
    "call",
    "--session",
    "--dest",
    _MUTTER_IDLE_SERVICE,
    "--object-path",
    _MUTTER_IDLE_PATH,
    "--method",
    "org.gnome.Mutter.IdleMonitor.GetIdletime",
)
_LOGIN1 = "org.freedesktop.login1"
_LOGIN1_PATH = "/org/freedesktop/login1"
_LOGIN1_MANAGER = "org.freedesktop.login1.Manager"
_LOGIN1_SESSION = "org.freedesktop.login1.Session"
_DBUS_PROPERTIES = "org.freedesktop.DBus.Properties"

#: Where ``ydotoold`` listens when ``YDOTOOL_SOCKET`` is not set (besides
#: ``$XDG_RUNTIME_DIR/.ydotool_socket``). Module-level so tests can clear it.
_YDOTOOL_DEFAULT_SOCKETS: tuple[str, ...] = ("/tmp/.ydotool_socket",)

#: Indirection so tests can fake ``/proc/<pid>/fd`` symlinks portably.
_readlink = os.readlink


# ---------------------------------------------------------------------------
