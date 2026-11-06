"""The JSON document behind the calibration file: reading, writing and pretty-printing.

Only numbers are stored: the feature vectors (head pose angles, head position,
iris offsets), the screen points they were labelled with and the fitted model.
Camera frames are never part of a calibration.

The document holds a list of *profiles*, one per setup, most recently used
first. Version 1 files, which held a single calibration, are read as a list with
one profile. It is pretty-printed with one sample per line so it stays readable
and reasonably small (a two-monitor calibration is roughly 50 KB).

Reading never raises: a missing, unreadable, corrupt or too new file yields an
empty list, a single corrupt profile is skipped and the others are kept.

Nothing here imports :mod:`eye_tracker.gaze.store` at runtime; the record class
is only a type hint. The read path takes a ``make_profile`` factory instead, so
the dependency between the two modules stays one-way.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from ..types import Monitor, Rect
from .calibration import CalibrationSample
from .model import GazeModel

if TYPE_CHECKING:
    from .store import CalibrationData

# The store's logger: splitting the module must not move the log records.
log = logging.getLogger("eye_tracker.gaze.store")

#: Version 2: a list of profiles. Version 1 (a single calibration) is still read.
CALIBRATION_VERSION = 2

#: Marker that identifies the file type.
FILE_FORMAT = "eye-tracker-calibration"

# Samples are measurements, so six decimals are far below their noise. Model
# parameters keep ten significant digits so a reloaded model predicts the same
# pixels as the one that was saved.
_SAMPLE_DECIMALS = 6
_MODEL_DIGITS = 10

# Everything a malformed (hand-edited or damaged) file can raise while being
# parsed: json.JSONDecodeError is a ValueError, int(inf) an OverflowError and
# very deep nesting a RecursionError.
_PARSE_ERRORS = (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError)


# ------------------------------------------------------------------ reading
class _NewerVersion(Exception):
    pass


def _read_profiles(
    path: Path, make_profile: Callable[..., CalibrationData]
) -> list[CalibrationData]:
    try:
        # utf-8-sig: a file saved by an editor (or PowerShell 5.1) with a BOM is
        # still the user's calibration, not a corrupt file.
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("Could not read calibration %s: %s", path, exc)
        return []
    try:
        doc = json.loads(text)
        return _profiles_from_doc(doc, path, make_profile)
    except _NewerVersion as exc:
        log.warning("Calibration %s was written by a newer Eye Tracker (%s); ignored", path, exc)
    except _PARSE_ERRORS as exc:
        log.warning("Ignoring corrupt calibration file %s: %s", path, exc)
    return []


def _profiles_from_doc(
    doc: Any, path: Path, make_profile: Callable[..., CalibrationData]
) -> list[CalibrationData]:
    if not isinstance(doc, dict):
        raise ValueError("root is not an object")
    if doc.get("format", FILE_FORMAT) != FILE_FORMAT:
        raise ValueError(f"not a calibration file (format {doc.get('format')!r})")
    version = doc.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError(f"invalid version {version!r}")
    if version > CALIBRATION_VERSION:
        raise _NewerVersion(f"version {version}")
    if version == 1:
        return [_profile_from_doc(doc, make_profile)]

    profiles = []
    for i, item in enumerate(_list(doc["profiles"], "profiles")):
        try:
            profiles.append(_profile_from_doc(item, make_profile))
        except _PARSE_ERRORS as exc:
            log.warning("Ignoring corrupt calibration profile %d in %s: %s", i + 1, path, exc)
    return profiles


def _profile_from_doc(doc: Any, make_profile: Callable[..., CalibrationData]) -> CalibrationData:
    if not isinstance(doc, dict):
        raise ValueError("profile is not an object")
    model = GazeModel.from_dict(doc["model"])
    samples = _samples_from_list(doc.get("samples", []), "samples")
    implicit = _samples_from_list(doc.get("implicit_samples", []), "implicit_samples")
    dims = {s.features.shape[0] for s in [*samples, *implicit]}
    if model.is_fitted:
        dims.add(model.n_features)
    if len(dims) > 1:
        raise ValueError(f"inconsistent feature lengths {sorted(dims)}")

    report = doc.get("report", {})
    return make_profile(
        backend=_str(doc["backend"], "backend"),
        feature_version=_str(doc["feature_version"], "feature_version"),
        layout_signature=_str(doc["layout_signature"], "layout_signature"),
        monitors=[_monitor_from_dict(m) for m in _list(doc["monitors"], "monitors")],
        samples=samples,
        implicit_samples=implicit,
        model=model,
        report=report if isinstance(report, dict) else {},
        created_at=str(doc.get("created_at", "")),
        camera=_camera_from_doc(doc.get("camera")),
        frame_size=_frame_size_from_doc(doc.get("frame_size")),
    )


def _camera_from_doc(value: Any) -> str:
    # Optional metadata: a malformed value means "unknown", not a lost calibration.
    return value if isinstance(value, str) else ""


def _frame_size_from_doc(value: Any) -> tuple[int, int]:
    if (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in value)
    ):
        return (value[0], value[1])
    return (0, 0)


# ------------------------------------------------------------------ writing
def _profile_to_doc(data: CalibrationData) -> dict[str, Any]:
    width, height = data.frame_size
    return {
        "created_at": data.created_at,
        "backend": data.backend,
        "feature_version": data.feature_version,
        "layout_signature": data.layout_signature,
        "camera": data.camera,
        "frame_size": [int(width), int(height)],
        "monitors": [_monitor_to_dict(m) for m in data.monitors],
        "report": _json_safe(data.report),
        "model": _round_model(data.model.to_dict()),
        "samples": _samples_to_list(data.samples),
        "implicit_samples": _samples_to_list(data.implicit_samples),
    }


# ------------------------------------------------------------------- monitors
def _monitor_to_dict(m: Monitor) -> dict[str, Any]:
    return {
        "index": m.index,
        "name": m.name,
        "rect": m.rect.to_list(),
        "primary": m.primary,
        "scale": m.scale,
    }


def _monitor_from_dict(d: Any) -> Monitor:
    if not isinstance(d, dict):
        raise ValueError("monitor entry is not an object")
    scale = float(d.get("scale", 1.0))
    if not math.isfinite(scale):
        raise ValueError("monitor scale is not a finite number")
    return Monitor(
        index=int(d["index"]),
        name=str(d.get("name", "")),
        rect=Rect.from_list(d["rect"]),
        primary=bool(d.get("primary", False)),
        scale=scale,
    )


# -------------------------------------------------------------------- samples
def _samples_to_list(samples: Iterable[CalibrationSample]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    dropped = 0
    for s in samples:
        features = np.asarray(s.features, dtype=np.float64).reshape(-1)
        values = [*features.tolist(), s.x, s.y, s.weight]
        if not all(math.isfinite(v) for v in values):
            dropped += 1
            continue
        out.append(
            {
                "features": [round(v, _SAMPLE_DECIMALS) for v in features.tolist()],
                "x": round(float(s.x), _SAMPLE_DECIMALS),
                "y": round(float(s.y), _SAMPLE_DECIMALS),
                "monitor": int(s.monitor_index),
                "point": int(s.point_id),
                "weight": round(float(s.weight), _SAMPLE_DECIMALS),
            }
        )
    if dropped:
        log.warning("Not saving %d calibration samples with non-finite values", dropped)
    return out


def _samples_from_list(items: Any, name: str) -> list[CalibrationSample]:
    out = []
    for item in _list(items, name):
        if not isinstance(item, dict):
            raise ValueError(f"{name}: entry is not an object")
        features = np.asarray(item["features"], dtype=np.float64)
        if features.ndim != 1 or features.size == 0:
            raise ValueError(f"{name}: features must be a non-empty list of numbers")
        sample = CalibrationSample(
            features=features,
            x=float(item["x"]),
            y=float(item["y"]),
            monitor_index=int(item["monitor"]),
            point_id=int(item["point"]),
            weight=float(item.get("weight", 1.0)),
        )
        if not (
            np.all(np.isfinite(features))
            and math.isfinite(sample.x)
            and math.isfinite(sample.y)
            and math.isfinite(sample.weight)
            and sample.weight >= 0
        ):
            raise ValueError(f"{name}: invalid numbers")
        out.append(sample)
    return out


# ------------------------------------------------------------------- helpers
def _round_model(d: dict[str, Any]) -> dict[str, Any]:
    def rnd(v: Any) -> Any:
        if isinstance(v, float):
            return float(f"{v:.{_MODEL_DIGITS}g}")
        if isinstance(v, list):
            return [rnd(x) for x in v]
        if isinstance(v, dict):  # the look-away regions
            return {k: rnd(x) for k, x in v.items()}
        return v

    return {k: rnd(v) for k, v in d.items()}


def _json_safe(value: Any) -> Any:
    """Plain JSON types only: numpy scalars unwrapped, NaN/Infinity as null."""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), allow_nan=False, ensure_ascii=False)


def _dumps(doc: dict[str, Any]) -> str:
    """Pretty JSON: each profile's bulky parts (samples, model arrays) one item per line."""
    lines = []
    for key, value in doc.items():
        if key == "profiles":
            items = ",\n".join("    " + _dump_profile(p, "    ") for p in value)
            body = "[\n" + items + "\n  ]" if value else "[]"
        else:
            body = json.dumps(value, allow_nan=False, ensure_ascii=False)
        lines.append(f"  {json.dumps(key)}: {body}")
    return "{\n" + ",\n".join(lines) + "\n}\n"


def _dump_profile(profile: dict[str, Any], indent: str) -> str:
    inner = indent + "  "
    lines = []
    for key, value in profile.items():
        if key in ("samples", "implicit_samples", "monitors") and value:
            rows = ",\n".join(f"{inner}  {_compact(v)}" for v in value)
            body = "[\n" + rows + f"\n{inner}]"
        elif key == "model" and value:
            rows = ",\n".join(f"{inner}  {json.dumps(k)}: {_compact(v)}" for k, v in value.items())
            body = "{\n" + rows + f"\n{inner}}}"
        elif key == "frame_size":
            body = _compact(value)
        else:
            body = json.dumps(value, indent=2, allow_nan=False, ensure_ascii=False)
            body = body.replace("\n", "\n" + inner)
        lines.append(f"{inner}{json.dumps(key)}: {body}")
    return "{\n" + ",\n".join(lines) + f"\n{indent}}}"


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    return value


def _str(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value
