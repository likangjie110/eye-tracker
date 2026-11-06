"""Persistence of calibrations as a small, human-readable JSON file.

Only numbers are stored: the feature vectors (head pose angles, head position,
iris offsets), the screen points they were labelled with and the fitted model.
Camera frames are never part of a calibration.

A calibration is only valid for the setup it was recorded on: the monitor
layout, the vision backend (and the version of its measurements) and the
camera. A laptop that moves between desks therefore needs one calibration per
desk, so the file holds a small :class:`CalibrationLibrary` of *profiles*, one
per setup, most recently used first. Version 1 files, which held a single
calibration, are read as a library with one profile.

The file is pretty-printed with one sample per line so it stays readable and
reasonably small (a two-monitor calibration is roughly 50 KB). Loading never
raises: a missing, unreadable, corrupt or too new file yields an empty library
(``None`` from :func:`load_calibration`) and the app asks for a new calibration;
a single corrupt profile is skipped and the others are kept.

The JSON document itself is parsed and written by
:mod:`~eye_tracker.gaze.store_docs`; this module adds the record type, the
library and the compatibility policy.
"""

from __future__ import annotations

import logging
import math
import weakref
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from ..config import atomic_write_text
from ..types import Monitor, layout_signature, virtual_bounds
from .calibration import (
    CalibrationSample,
    axis_errors_from_dict,
    per_axis_errors,
    samples_to_arrays,
)
from .model import SUPPORTED_DEGREES, GazeModel, lopo_predictions
from .store_docs import (
    CALIBRATION_VERSION,
    FILE_FORMAT,
    _camera_from_doc,
    _compact,
    _dump_profile,
    _dumps,
    _frame_size_from_doc,
    _json_safe,
    _list,
    _monitor_from_dict,
    _monitor_to_dict,
    _NewerVersion,
    _profile_from_doc,
    _profile_to_doc,
    _profiles_from_doc,
    _read_profiles,
    _round_model,
    _samples_from_list,
    _samples_to_list,
    _str,
)

log = logging.getLogger(__name__)

__all__ = [
    "ASPECT_TOLERANCE",
    "BACKEND_ALIASES",
    "CALIBRATION_VERSION",
    "FILE_FORMAT",
    "MAX_PROFILES",
    "CalibrationData",
    "CalibrationLibrary",
    "ProfileKey",
    "_NewerVersion",
    "_camera_from_doc",
    "_compact",
    "_dump_profile",
    "_dumps",
    "_frame_size_from_doc",
    "_json_safe",
    "_list",
    "_monitor_from_dict",
    "_monitor_to_dict",
    "_profile_from_doc",
    "_profile_to_doc",
    "_profiles_from_doc",
    "_read_profiles",
    "_round_model",
    "_samples_from_list",
    "_samples_to_list",
    "_str",
    "axis_error",
    "canonical_backend",
    "load_calibration",
    "same_aspect",
    "save_calibration",
    "utc_now_iso",
]

#: Profiles kept by a :class:`CalibrationLibrary`; the least recently used go first.
MAX_PROFILES = 8

#: Camera frames whose aspect ratios differ by more than this (relative) show a
#: different field of view. The resolution alone does not matter: every backend
#: normalises its measurements by the frame size.
ASPECT_TOLERANCE = 0.01

#: ``CalibrationData.key``: (layout signature, backend, feature version, camera).
ProfileKey = tuple[str, str, str, str]

#: Backend names of earlier versions whose measurements did not change with the
#: new name: the OpenCV/YuNet backend is now called ``lite`` (same features, same
#: ``feature_version``), so its calibrations stay valid. The MediaPipe backend's
#: successor, ``facemesh``, measures differently (another ``feature_version``),
#: so ``mediapipe`` calibrations are deliberately not carried over.
BACKEND_ALIASES: dict[str, str] = {"opencv": "lite"}


def canonical_backend(name: str) -> str:
    """The current name of a vision backend (see :data:`BACKEND_ALIASES`)."""
    return BACKEND_ALIASES.get(name, name)


def utc_now_iso() -> str:
    """Current time as ISO 8601 UTC with second precision, e.g. ``2026-09-30T12:00:00+00:00``."""
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(eq=False)
class CalibrationData:
    """A calibration and everything needed to decide whether it is still valid.

    ``camera`` is the camera device setting (``CameraSettings.device``, e.g.
    ``"0"``) and ``frame_size`` the ``(w, h)`` of the camera frames the samples
    were measured on (``CalibrationCollector.frame_size``). Head pose and face
    position are measured relative to the camera, so another camera, or the same
    one with another field of view, needs another calibration. ``""`` and
    ``(0, 0)`` mean unknown (files written before they were recorded) and are
    never a reason to reject the calibration.
    """

    backend: str
    feature_version: str
    layout_signature: str
    monitors: list[Monitor]
    samples: list[CalibrationSample]
    implicit_samples: list[CalibrationSample]
    model: GazeModel
    report: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)
    camera: str = ""
    frame_size: tuple[int, int] = (0, 0)

    @property
    def key(self) -> ProfileKey:
        """The setup this calibration belongs to; a library keeps one profile per key.

        The backend is given by its current name (:func:`canonical_backend`), so a
        profile of the renamed ``opencv`` backend and a new ``lite`` one are the same.
        """
        return (
            self.layout_signature,
            canonical_backend(self.backend),
            self.feature_version,
            self.camera.strip(),
        )

    def is_compatible(
        self,
        backend_name: str,
        feature_version: str,
        monitors: Sequence[Monitor],
        camera: str | None = None,
        frame_size: tuple[int, int] | None = None,
    ) -> tuple[bool, str]:
        """``(True, "")`` if usable with this backend, monitor layout and camera, else
        ``(False, reason)`` where ``reason`` is a short lower-case phrase such as
        ``"the monitor layout changed"``.

        ``camera`` and ``frame_size`` are the current camera device setting and
        frame size; ``None``, ``""`` or ``(0, 0)`` (here or stored) skip that check.
        Backend names are compared by their current names (:func:`canonical_backend`).
        """
        current, stored = canonical_backend(backend_name), canonical_backend(self.backend)
        if current != stored:
            return (
                False,
                f"calibrated with the {stored} backend, but {current} is in use",
            )
        if feature_version != self.feature_version:
            return False, (
                f"the {backend_name} backend's measurements changed "
                f"({self.feature_version} → {feature_version})"
            )
        if layout_signature(monitors) != self.layout_signature:
            return False, "the monitor layout changed"
        ok, reason = self.camera_matches(camera, frame_size)
        if not ok:
            return False, reason
        if not self.model.is_fitted:
            return False, "the calibration has no fitted model"
        return True, ""

    def camera_matches(
        self, camera: str | None = None, frame_size: tuple[int, int] | None = None
    ) -> tuple[bool, str]:
        """The camera part of :meth:`is_compatible`, e.g. for the first frame after
        the camera was (re)opened: ``(False, "the camera changed")`` for another
        device, ``(False, "the camera's aspect ratio changed (…)")`` for another
        field of view. Unknown values on either side pass."""
        stored, current = self.camera.strip(), (camera or "").strip()
        if stored and current and stored != current:
            return False, "the camera changed"
        if frame_size is not None and not same_aspect(self.frame_size, frame_size):
            (w0, h0), (w1, h1) = self.frame_size, frame_size
            return False, f"the camera's aspect ratio changed ({w0}x{h0} → {w1}x{h1})"
        return True, ""

    @property
    def grade(self) -> str | None:
        """Quality grade from the report (``"excellent"`` … ``"poor"``), if known."""
        value = self.report.get("grade")
        return value if isinstance(value, str) else None


def _build_profile(**fields: Any) -> CalibrationData:
    """Build a calibration from the fields parsed by :mod:`.store_docs`.

    The document module calls back into here rather than importing
    :class:`CalibrationData`: the dependency between the two modules stays one-way.
    The backend is stored under its current name, so the next save writes that.
    """
    fields["backend"] = canonical_backend(fields["backend"])
    return CalibrationData(**fields)


def same_aspect(a: Sequence[int], b: Sequence[int]) -> bool:
    """Whether frame sizes ``a`` and ``b`` ``(w, h)`` have the same aspect ratio
    (within :data:`ASPECT_TOLERANCE`); True if either is unknown (not positive)."""
    (wa, ha), (wb, hb) = a, b
    if min(wa, ha, wb, hb) <= 0:
        return True
    return abs((wa / ha) / (wb / hb) - 1.0) <= ASPECT_TOLERANCE


# ------------------------------------------------------------ gaze accuracy
#: Per-axis errors already worked out for a calibration (loaded, recomputed or
#: found impossible to recompute: ``{}``), so each profile is looked at once.
_AXIS_ERRORS: weakref.WeakKeyDictionary[CalibrationData, dict[int, tuple[float, float]]] = (
    weakref.WeakKeyDictionary()
)


def axis_error(data: CalibrationData, monitor: Monitor) -> tuple[float, float] | None:
    """How far off the gaze estimate typically is on ``monitor``: ``(x_px, y_px)``.

    The answer comes, in this order, from

    1. the report's ``per_monitor_error_px`` (calibrations made by this version),
    2. the leave-one-point-out predictions recomputed from the calibration's own
       samples with the report's ``alpha`` and ``degree`` (older calibrations;
       the result is added to :attr:`CalibrationData.report`, so it is saved
       with the profile's next write), and
    3. the report's mean error divided by √2 on both axes.

    ``monitor`` is matched to the calibration's monitors by its rectangle (Qt may
    number the same screens differently after a restart), then by index.
    ``None`` when nothing is known.

    The first call for an old profile recomputes on the calling thread (the GUI
    thread, from the controller), on purpose: the leave-one-point-out
    predictions cost one design matrix and one small ``p x p`` solve per
    calibration dot, about 2 ms for a two-monitor calibration of 360 samples and
    4 ms for 960 (degree 3, measured on a desktop; ``tests/test_store.py`` keeps
    a generous bound). It happens once per profile and session; later calls are
    dictionary lookups. A background thread would cost more than it saves.
    """
    errors = _AXIS_ERRORS.get(data)
    if errors is None:
        errors = _axis_errors_of(data)
        _AXIS_ERRORS[data] = errors
    index = next((m.index for m in data.monitors if m.rect == monitor.rect), monitor.index)
    found = errors.get(index)
    if found is not None:
        return found
    mean = data.report.get("mean_error_px")
    if isinstance(mean, int | float) and not isinstance(mean, bool):
        value = float(mean) / math.sqrt(2.0)
        if math.isfinite(value) and value > 0.0:
            return (value, value)
    return None


def _axis_errors_of(data: CalibrationData) -> dict[int, tuple[float, float]]:
    try:
        stored = axis_errors_from_dict(data.report.get("per_monitor_error_px"))
    except ValueError:
        stored = {}
    if stored:
        return stored
    computed = _recompute_axis_errors(data)
    if computed:
        data.report["per_monitor_error_px"] = {str(k): list(v) for k, v in computed.items()}
        log.debug("Gaze error per axis recomputed for the calibration of %s", data.created_at)
    return computed


def _recompute_axis_errors(data: CalibrationData) -> dict[int, tuple[float, float]]:
    """Leave-one-point-out errors per monitor and axis of an older calibration."""
    known = {m.index for m in data.monitors}
    samples = [s for s in data.samples if s.monitor_index in known]
    groups = np.array([s.point_id for s in samples])
    if not samples or np.unique(groups).shape[0] < 2:
        return {}
    report = data.report
    alpha = report.get("alpha")
    degree = report.get("degree")
    if not isinstance(alpha, int | float) or isinstance(alpha, bool) or not alpha >= 0:
        alpha = data.model.alpha
    if not isinstance(degree, int) or isinstance(degree, bool) or degree not in SUPPORTED_DEGREES:
        degree = data.model.degree
    try:
        X, Y, W = samples_to_arrays(samples)
        preds = lopo_predictions(
            X,
            Y,
            groups,
            float(alpha),
            degree=int(degree),
            bounds=virtual_bounds(data.monitors),
            weights=W,
            nonlinear=data.model.nonlinear,
        )
        return per_axis_errors(preds, Y, [s.monitor_index for s in samples])
    except (ValueError, np.linalg.LinAlgError) as exc:
        log.debug("Cannot recompute the per-axis gaze error: %s", exc)
        return {}


# --------------------------------------------------------------------- library
class CalibrationLibrary:
    """The saved calibrations, one profile per setup (:attr:`CalibrationData.key`),
    most recently used first.

    Typical use by the engine::

        library = CalibrationLibrary.load(path)
        data = library.best(backend, feature_version, monitors, camera=device)
        ...after a calibration or a refit:   library.put(data); library.save(path)
        ...after switching to a profile:     library.mark_used(data); library.save(path)

    At most ``max_profiles`` profiles are kept; adding one more drops the least
    recently used. Profiles are held by reference, so changing a profile's model
    or implicit samples in place and calling :meth:`save` persists the change.
    """

    def __init__(
        self, profiles: Iterable[CalibrationData] = (), *, max_profiles: int = MAX_PROFILES
    ) -> None:
        if max_profiles < 1:
            raise ValueError("max_profiles must be >= 1")
        self.max_profiles = int(max_profiles)
        self._profiles: list[CalibrationData] = []
        # Given most recent first; adding in reverse keeps that order and drops
        # later duplicates of a key.
        for data in reversed(list(profiles)):
            self.put(data)

    # ------------------------------------------------------------- persistence
    @classmethod
    def load(cls, path: Path, *, max_profiles: int = MAX_PROFILES) -> CalibrationLibrary:
        """Read the calibration file; never raises (a bad file gives an empty library)."""
        return cls(_read_profiles(Path(path), _build_profile), max_profiles=max_profiles)

    def save(self, path: Path) -> None:
        """Write every profile to ``path`` atomically. Raises ``OSError`` if writing fails."""
        doc = {
            "format": FILE_FORMAT,
            "version": CALIBRATION_VERSION,
            "profiles": [_profile_to_doc(p) for p in self._profiles],
        }
        atomic_write_text(Path(path), _dumps(doc))
        log.debug("Saved %d calibration profile(s) to %s", len(self._profiles), path)

    # ------------------------------------------------------------- access
    @property
    def profiles(self) -> list[CalibrationData]:
        """All profiles, most recently used first."""
        return list(self._profiles)

    @property
    def latest(self) -> CalibrationData | None:
        """The most recently used profile (``None`` when empty)."""
        return self._profiles[0] if self._profiles else None

    def __len__(self) -> int:
        return len(self._profiles)

    def __iter__(self) -> Iterator[CalibrationData]:
        return iter(list(self._profiles))

    def __contains__(self, data: object) -> bool:
        return any(p is data for p in self._profiles)

    def get(self, key: ProfileKey) -> CalibrationData | None:
        """The profile stored for ``key`` (see :attr:`CalibrationData.key`)."""
        return next((p for p in self._profiles if p.key == key), None)

    def match(
        self,
        backend_name: str,
        feature_version: str,
        monitors: Sequence[Monitor],
        camera: str | None = None,
        frame_size: tuple[int, int] | None = None,
    ) -> tuple[CalibrationData | None, str]:
        """The best profile for the current setup, or ``(None, reason)``.

        Among compatible profiles (:meth:`CalibrationData.is_compatible`), one
        whose camera and frame size are known to match beats one where they are
        unknown; otherwise the most recently used wins. Without any, ``reason``
        explains what is wrong with the most recently used profile
        (``"not calibrated yet"`` for an empty library).
        """
        best: tuple[int, int, CalibrationData] | None = None
        for position, data in enumerate(self._profiles):
            ok, _ = data.is_compatible(backend_name, feature_version, monitors, camera, frame_size)
            if not ok:
                continue
            specific = int(bool(data.camera.strip() and (camera or "").strip()))
            specific += int(frame_size is not None and min(*data.frame_size, *frame_size) > 0)
            if best is None or (-specific, position) < (-best[0], best[1]):
                best = (specific, position, data)
        if best is not None:
            return best[2], ""
        latest = self.latest
        if latest is None:
            return None, "not calibrated yet"
        _, reason = latest.is_compatible(
            backend_name, feature_version, monitors, camera, frame_size
        )
        return None, reason

    def best(
        self,
        backend_name: str,
        feature_version: str,
        monitors: Sequence[Monitor],
        camera: str | None = None,
        frame_size: tuple[int, int] | None = None,
    ) -> CalibrationData | None:
        """The profile :meth:`match` chooses, or ``None``."""
        return self.match(backend_name, feature_version, monitors, camera, frame_size)[0]

    # ------------------------------------------------------------- changes
    def put(self, data: CalibrationData) -> list[CalibrationData]:
        """Add ``data`` as the most recently used profile; returns the profiles it evicted.

        It replaces the profile with the same :attr:`~CalibrationData.key`, and a
        profile of the same setup whose camera is unknown (``""``, from an older
        file), which was most likely made with the same camera. Profiles beyond
        ``max_profiles`` are then dropped, least recently used first.
        """
        layout, backend, version, camera = data.key
        replaced = [
            p
            for p in self._profiles
            if p is data
            or p.key == data.key
            or (camera and p.key[:3] == (layout, backend, version) and not p.key[3])
        ]
        self._profiles = [data, *(p for p in self._profiles if p not in replaced)]
        evicted = self._profiles[self.max_profiles :]
        del self._profiles[self.max_profiles :]
        for p in evicted:
            log.info("Dropping the least recently used calibration (%s)", p.created_at)
        return [p for p in replaced if p is not data] + evicted

    def mark_used(self, data: CalibrationData) -> bool:
        """Make a stored profile the most recently used; False if it is not stored."""
        if data not in self:
            return False
        self._profiles = [data, *(p for p in self._profiles if p is not data)]
        return True

    def remove(self, data: CalibrationData) -> bool:
        """Remove a stored profile; False if it is not stored."""
        if data not in self:
            return False
        self._profiles = [p for p in self._profiles if p is not data]
        return True

    def clear(self) -> None:
        """Remove every profile."""
        self._profiles.clear()


# ------------------------------------------------------------ single profile
def save_calibration(path: Path, data: CalibrationData) -> None:
    """Store ``data`` in the calibration file at ``path`` as the most recently used
    profile, replacing the profile of the same setup and keeping the others.

    Writes atomically; raises ``OSError`` if writing fails. A caller that holds a
    :class:`CalibrationLibrary` should use :meth:`CalibrationLibrary.put` and
    :meth:`~CalibrationLibrary.save` instead, which avoids re-reading the file.
    """
    library = CalibrationLibrary.load(path)
    library.put(data)
    library.save(path)


def load_calibration(path: Path) -> CalibrationData | None:
    """The most recently used calibration in the file at ``path``; ``None`` if the file
    is missing, corrupt or unsupported. Never raises."""
    return CalibrationLibrary.load(path).latest
