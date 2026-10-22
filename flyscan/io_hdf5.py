"""HDF5 output for fly-scan lines, and a reader that rebuilds the map.

One file per map. One group per line, one dataset per channel inside it, and
every number needed to interpret those datasets in attributes beside them.
The aim is that MATLAB can read a finished file with nothing but h5read and
h5readatt - no sidecar, no .m loader, no agreement about array order needed:

    ac   = h5read('map.h5', '/lines/line_0000/ac');     % points x segments
    gain = h5readatt('map.h5', '/lines/line_0000/ac', 'v_gain');
    off  = h5readatt('map.h5', '/lines/line_0000/ac', 'v_offset');
    volts = gain * double(ac) - off;          % LeCroy's own convention
    tt   = h5read('map.h5', '/lines/line_0000/trigtime');
    fs   = h5readatt('map.h5', '/lines/line_0000', 'sample_rate_hz');

Note the transpose: HDF5 is row-major and MATLAB is column-major, so an
(N_segments, P) dataset written here arrives in MATLAB as P x N_segments.
That is recorded in the `layout` attribute on every 2-D dataset rather than
left to be rediscovered.

Strings are written as fixed-length ASCII and numbers as float64 or int64
scalars, both of which MATLAB reads without special handling. Variable-length
strings, object references and compound types are avoided for the same
reason. Lists that could grow - the indices of missed triggers, say - are
written as small datasets rather than attributes, since HDF5 attributes have
a hard size limit.

Fallback without h5py
---------------------
If h5py is not installed, `FlyscanWriter` writes the same information as a
directory: `manifest.json` with every attribute, and one flat little-endian
`.bin` per channel. `open_file` reads either form, so nothing else in this
package cares which was used. MATLAB can read that form too, but it does then
need the manifest:

    m  = jsondecode(fileread('map.flyscan/manifest.json'));
    f  = fopen('map.flyscan/line_0000_ac.bin');
    ac = reshape(fread(f, Inf, 'int16=>double'), m.lines(1).channels(1).shape);

which is exactly the sidecar arrangement HDF5 exists to avoid - so install
h5py on the acquisition machine. `h5py_available()` says which form you are
about to get.

Reading
-------
`open_file` returns a reader for either form, with the same interface:

    reader.line_names()                    names of the stored lines
    reader.line_attrs(name)                that line's attributes, decoded
    reader.read_channel(name, 'ac')        volts and the sparsing factor
    reader.read_stored_channel(name, 'ac') the StoredChannel `add_line` took
    reader.read_trigtime(name)             the (n_segments, 2) array
    reader.process_line(name, ...)         the post-processing chain
    reader.read_map(...)                   every line, on one shared grid

`read_channel` is a two-field named tuple, so `volts, sparsing = ...` and
`channel.volts` both work; `read_stored_channel` is the symmetrical one -
raw counts plus the gain and offset that convert them.
"""

from dataclasses import dataclass, field, replace
from typing import NamedTuple
import datetime
import json
import math
import os
import time

import numpy as np

from .budget import AsopsTiming
from .postprocess import (LineResult, ProcessingConfig, SpatialGrid,
                          brillouin_metric, peak_to_peak, positions_from_anchors,
                          positions_from_velocity, process_flat,
                          process_segmented)

FORMAT_NAME = "flyscan-asops"
FORMAT_VERSION = 2

# The role names live in budget.py, which owns them; re-exported here so
# `io_hdf5.ROLE_AC` keeps working for anything that reaches for it.
from .budget import (ROLE_AC, ROLE_DC, ROLE_POSITION_ANALOG,  # noqa: F401
                     ROLE_SPARE, ROLE_TRIGGER, ROLE_TRIGTIME)


def h5py_available() -> bool:
    try:
        import h5py  # noqa: F401
        return True
    except Exception:
        return False


@dataclass
class StoredChannel:
    """One channel as it goes into the file."""

    name: str                 # scope channel, e.g. 'C1'
    role: str                 # ac / dc / trigger / trigtime / spare
    data: np.ndarray          # (N, P) int16 counts or float volts
    sparsing: int = 1
    # LeCroy's convention, kept so that stored counts mean something:
    #   volts = v_gain * counts - v_offset
    v_gain: float = 1.0
    v_offset: float = 0.0
    units: str = "V"

    def volts(self) -> np.ndarray:
        return self.v_gain * np.asarray(self.data, dtype=np.float64) - self.v_offset

    def dt_s(self, sample_rate_hz) -> float:
        return self.sparsing / sample_rate_hz


class ReadChannel(NamedTuple):
    """What `read_channel` returns: volts, and the sparsing factor.

    Two fields on purpose, so `volts, sparsing = read_channel(...)` still
    works while `.volts` says which of the two is which. For the writer's own
    view of a channel - the raw stored counts with the gain and offset that
    convert them - use `read_stored_channel`, which hands back the same
    `StoredChannel` that `add_line` takes.
    """

    volts: np.ndarray
    sparsing: int

    def dt_s(self, sample_rate_hz) -> float:
        return self.sparsing / sample_rate_hz


def stored_channels_from_synth(record, quantise_bits: int = 0,
                               full_scale_v: float = 1.0) -> tuple:
    """Turn a synth record into channels ready to store.

    With `quantise_bits` set, channels are stored as int16 counts with the
    gain that recovers volts - the same shape the real scope delivers, so
    reading a synthetic file exercises the same conversion path as a real one.
    """
    pairs = [("C1", ROLE_AC, record.ac, 1)]
    if getattr(record, "dc", None) is not None:
        pairs.append(("C2", ROLE_DC, record.dc, record.dc_sparsing))
    if getattr(record, "trigger", None) is not None:
        pairs.append(("C3", ROLE_TRIGGER, record.trigger,
                      record.trigger_sparsing))

    out = []
    for name, role, data, sparsing in pairs:
        data = np.asarray(data)
        if quantise_bits:
            full = 2 ** (quantise_bits - 1) - 1
            v_gain = full_scale_v / full
            counts = np.clip(np.rint(data / v_gain), -full - 1, full)
            out.append(StoredChannel(name, role, counts.astype(np.int16),
                                     sparsing, v_gain, 0.0))
        else:
            out.append(StoredChannel(name, role, data.astype(np.float64),
                                     sparsing, 1.0, 0.0))
    return tuple(out)


# ---------------------------------------------------------------------------
# Attribute marshalling
# ---------------------------------------------------------------------------

def _as_float(value, default=float("nan")) -> float:
    """Read a stored number, tolerating the fallback's JSON null for NaN.

    JSON has no NaN, so the fallback writer stores one as null. A null here
    means "not recorded", which for every field that uses it means the same
    thing as NaN: derive it instead of trusting it.
    """
    if value is None:
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _scalar(value):
    """Coerce a Python value to something MATLAB reads without fuss."""
    if isinstance(value, bool):
        return np.int8(1 if value else 0)
    if isinstance(value, (int, np.integer)):
        return np.int64(value)
    if isinstance(value, (float, np.floating)):
        return np.float64(value)
    if isinstance(value, str):
        return np.bytes_(value.encode("ascii", "replace"))
    if isinstance(value, bytes):
        return np.bytes_(value)
    return None


def _flatten_stats(stats, prefix: str):
    """(scalars, arrays) from a stats object, with `prefix` on every key."""
    scalars, arrays = {}, {}
    if stats is None:
        return scalars, arrays
    source = stats.to_dict() if hasattr(stats, "to_dict") else dict(stats)
    for key, value in source.items():
        if key == "warnings":
            continue
        if isinstance(value, (list, tuple)):
            if len(value):
                arrays[prefix + key] = np.asarray(value, dtype=np.int64)
            continue
        if isinstance(value, np.ndarray):
            continue
        coerced = _scalar(value)
        if coerced is not None:
            scalars[prefix + key] = coerced
    return scalars, arrays


def _jsonable(mapping):
    out = {}
    for key, value in mapping.items():
        if isinstance(value, np.integer):
            out[key] = int(value)
        elif isinstance(value, (np.floating, float)):
            value = float(value)
            out[key] = None if math.isnan(value) else value
        elif isinstance(value, np.bytes_):
            out[key] = value.tobytes().decode("ascii", "replace")
        elif isinstance(value, np.int8):
            out[key] = int(value)
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

class FlyscanWriter:
    """Write lines to one file as they are acquired."""

    def __init__(self, path, timing: AsopsTiming = AsopsTiming(),
                 overwrite: bool = False, compression=None, notes: str = "",
                 force_fallback: bool = False, extra_attrs: dict = None):
        self.path = str(path)
        self.timing = timing
        self.compression = compression
        self.n_lines = 0
        self._closed = False
        self.use_hdf5 = h5py_available() and not force_fallback

        root_attrs = {
            "format": FORMAT_NAME,
            "format_version": FORMAT_VERSION,
            "created_iso": datetime.datetime.now().isoformat(timespec="seconds"),
            "created_unix": time.time(),
            "program": "flyscan",
            "rep_rate_hz": timing.rep_rate_hz,
            "sweep_rate_hz": timing.sweep_rate_hz,
            "stretch_factor": timing.stretch_factor,
            "delay_window_s": timing.delay_window_s,
            "intrinsic_delay_resolution_s": timing.intrinsic_delay_resolution_s,
            "voltage_convention": "volts = v_gain * counts - v_offset",
            "trigtime_convention":
                "sample j of segment i at trigtime(i,1) + trigtime(i,2) "
                "+ j / sample_rate_hz",
            "notes": notes,
        }
        if extra_attrs:
            root_attrs.update(extra_attrs)

        if self.use_hdf5:
            import h5py
            if os.path.exists(self.path) and not overwrite:
                raise FileExistsError(
                    f"{self.path} exists; pass overwrite=True to replace it")
            self._h5 = h5py.File(self.path, "w")
            for key, value in root_attrs.items():
                coerced = _scalar(value)
                if coerced is not None:
                    self._h5.attrs[key] = coerced
            self._h5.create_group("lines")
        else:
            root = self.path
            if not root.endswith(".flyscan"):
                root = os.path.splitext(root)[0] + ".flyscan"
            if os.path.exists(root) and not overwrite:
                raise FileExistsError(
                    f"{root} exists; pass overwrite=True to replace it")
            os.makedirs(root, exist_ok=True)
            self.path = root
            self._manifest = {"root": _jsonable(root_attrs), "lines": []}

    def add_line(self, index: int, channels, trigtime=None,
                 slow_axis_um: float = 0.0, direction: int = 1,
                 velocity_mm_s: float = float("nan"),
                 x_start_um: float = 0.0,
                 sample_rate_hz: float = float("nan"),
                 mode: str = "sequence", counts_per_mm: float = float("nan"),
                 processing: ProcessingConfig = None, result: LineResult = None,
                 timestamp_unix: float = None, extra_attrs: dict = None):
        """Store one line: its channels and everything needed to read them.

        Pass `result` and the per-line diagnostics - TRIGTIME statistics,
        copeak drift, DC level, measured velocity - are stored as attributes
        too, so a finished file says how the acquisition behaved and not just
        what it recorded.
        """
        if self._closed:
            raise RuntimeError("writer is closed")
        channels = tuple(channels)
        if not channels:
            raise ValueError("a line needs at least one channel")

        stamp = time.time() if timestamp_unix is None else float(timestamp_unix)
        attrs = {
            "line_index": int(index),
            "slow_axis_um": float(slow_axis_um),
            "direction": int(direction),
            "velocity_mm_s": float(velocity_mm_s),
            "x_start_um": float(x_start_um),
            "sample_rate_hz": float(sample_rate_hz),
            "sweep_rate_hz": self.timing.sweep_rate_hz,
            "rep_rate_hz": self.timing.rep_rate_hz,
            "stretch_factor": self.timing.stretch_factor,
            "mode": mode,
            "counts_per_mm": float(counts_per_mm),
            "timestamp_unix": stamp,
            "timestamp_iso": datetime.datetime.fromtimestamp(
                stamp).isoformat(timespec="seconds"),
            "channel_map": ", ".join(f"{c.name}={c.role}" for c in channels),
            "n_channels": len(channels),
        }
        if processing is not None:
            attrs.update({
                "position_source": processing.position.source,
                "trigger_interval_um": processing.position.trigger_interval_um,
                "first_pulse_position_um":
                    processing.position.first_pulse_position_um,
                "trigger_latency_s": processing.position.latency_s,
                "segment_pre_delay_s": processing.segments.pre_delay_s,
                "segment_post_delay_s": processing.segments.post_delay_s,
                "segment_align": processing.segments.align,
                "dc_offset_v": processing.dc.offset_v,
                "dc_method": processing.dc.method,
                "pixel_pitch_um": processing.pixel_pitch_um,
            })
        if extra_attrs:
            attrs.update(extra_attrs)

        arrays = {}
        if result is not None:
            for stats, prefix in ((result.trigtime_stats, "trig_"),
                                  (result.copeak_stats, "copeak_"),
                                  (result.dc, "dc_"),
                                  (result.position, "pos_")):
                scalars, stat_arrays = _flatten_stats(stats, prefix)
                attrs.update(scalars)
                arrays.update(stat_arrays)

        name = f"line_{int(index):04d}"
        if self.use_hdf5:
            group = self._h5["lines"].create_group(name)
            for key, value in attrs.items():
                coerced = _scalar(value)
                if coerced is not None:
                    group.attrs[key] = coerced
            for key, value in arrays.items():
                group.create_dataset(key, data=value)
            if trigtime is not None:
                dataset = group.create_dataset(
                    ROLE_TRIGTIME,
                    data=np.asarray(trigtime, dtype=np.float64))
                dataset.attrs["layout"] = _scalar(
                    "(n_segments, 2): trigger_time_s, trigger_offset_s; "
                    "transposed in MATLAB")
                dataset.attrs["units"] = _scalar("s")
            for channel in channels:
                data = np.asarray(channel.data)
                dataset = group.create_dataset(
                    channel.role, data=data, compression=self.compression)
                for key, value in (
                    ("channel", channel.name), ("role", channel.role),
                    ("sparsing", channel.sparsing),
                    ("v_gain", channel.v_gain), ("v_offset", channel.v_offset),
                    ("units", channel.units),
                    ("dt_s", channel.sparsing / sample_rate_hz
                     if sample_rate_hz else float("nan")),
                    ("layout",
                     "(n_segments, points_per_segment); transposed in MATLAB"
                     if data.ndim == 2 else "(n_samples,)"),
                ):
                    coerced = _scalar(value)
                    if coerced is not None:
                        dataset.attrs[key] = coerced
        else:
            entry = {"name": name, "attrs": _jsonable(attrs),
                     "arrays": {k: v.tolist() for k, v in arrays.items()},
                     "channels": []}
            if trigtime is not None:
                tt = np.asarray(trigtime, dtype=np.float64)
                tt.astype("<f8").tofile(
                    os.path.join(self.path, f"{name}_trigtime.bin"))
                entry["trigtime"] = {"file": f"{name}_trigtime.bin",
                                     "dtype": "<f8", "shape": list(tt.shape)}
            for channel in channels:
                data = np.asarray(channel.data)
                filename = f"{name}_{channel.role}.bin"
                data.astype(data.dtype.newbyteorder("<")).tofile(
                    os.path.join(self.path, filename))
                entry["channels"].append({
                    "role": channel.role, "channel": channel.name,
                    "file": filename, "dtype": data.dtype.str,
                    "shape": list(data.shape), "sparsing": channel.sparsing,
                    "v_gain": channel.v_gain, "v_offset": channel.v_offset,
                    "units": channel.units,
                })
            self._manifest["lines"].append(entry)

        self.n_lines += 1
        return name

    def add_synth_line(self, index, record, quantise_bits: int = 0,
                       slow_axis_um: float = 0.0, **kwargs):
        """Store a synthetic record, for testing the reader end to end."""
        trigtime = getattr(record, "trigtime", None)
        return self.add_line(
            index, stored_channels_from_synth(record, quantise_bits),
            trigtime=trigtime, slow_axis_um=slow_axis_um,
            direction=record.direction, velocity_mm_s=record.velocity_mm_s,
            x_start_um=float(record.truth.get("x_at_t_zero_um",
                                              record.config.x_start_um)),
            sample_rate_hz=record.sample_rate_hz,
            mode="sequence" if trigtime is not None else "flat", **kwargs)

    def close(self):
        if self._closed:
            return
        if self.use_hdf5:
            self._h5.attrs["n_lines"] = np.int64(self.n_lines)
            self._h5.close()
        else:
            self._manifest["root"]["n_lines"] = self.n_lines
            with open(os.path.join(self.path, "manifest.json"), "w") as handle:
                json.dump(self._manifest, handle, indent=1)
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------

@dataclass
class MapResult:
    """A finished map, rebuilt from a file."""

    x_um: np.ndarray             # fast-axis pixel centres
    y_um: np.ndarray             # slow-axis position of each line
    delay_axis_s: np.ndarray
    amplitude: np.ndarray        # (n_lines, n_pixels)
    frequency_hz: np.ndarray     # (n_lines, n_pixels)
    counts: np.ndarray           # sweeps averaged into each pixel
    cube: np.ndarray             # (n_lines, n_pixels, width) or None
    grid: SpatialGrid
    line_results: list = field(default_factory=list)
    quantity: str = ""
    warnings: list = field(default_factory=list)

    def summary(self) -> str:
        occupied = int(np.count_nonzero(self.counts))
        mean = (float(np.mean(self.counts[self.counts > 0])) if occupied
                else 0.0)
        return "\n".join([
            "FLY-SCAN MAP",
            f"  lines                    {self.y_um.size}",
            f"  pixels per line          {self.x_um.size} of "
            f"{self.grid.pitch_um:.4g} um from {self.grid.origin_um:.4f} um",
            f"  filled pixels            {occupied} of {self.counts.size}",
            f"  sweeps per pixel         mean {mean:.2f}",
            f"  quantity                 {self.quantity}",
            f"  delay window             {self.delay_axis_s[-1] * 1e12:,.1f} "
            f"ps, {self.delay_axis_s.size} samples",
        ])


def open_file(path):
    """Open a flyscan file in either form."""
    path = str(path)
    if os.path.isdir(path) or path.endswith(".flyscan"):
        return _RawReader(path)
    if not h5py_available():
        raise RuntimeError(
            f"{path} looks like HDF5 but h5py is not installed; install h5py, "
            f"or re-acquire with force_fallback=True")
    return _H5Reader(path)


class _ReaderBase:
    """Shared reading, processing and map assembly."""

    def line_names(self):
        raise NotImplementedError

    def line_attrs(self, name):
        raise NotImplementedError

    def read_stored_channel(self, name, role) -> StoredChannel:
        """One channel as the writer would have taken it, or None.

        The raw stored array with the gain and offset that turn it into
        volts, so a round trip through this module is symmetrical:
        `add_line(... StoredChannel ...)` out, `read_stored_channel` back.
        """
        raise NotImplementedError

    def read_channel(self, name, role) -> ReadChannel:
        """Volts and sparsing for one channel, or None."""
        stored = self.read_stored_channel(name, role)
        if stored is None:
            return None
        return ReadChannel(stored.volts(), stored.sparsing)

    def read_trigtime(self, name):
        raise NotImplementedError

    def root_attrs(self):
        raise NotImplementedError

    def timing(self) -> AsopsTiming:
        attrs = self.root_attrs()
        return AsopsTiming(
            rep_rate_hz=float(attrs.get("rep_rate_hz", 80.0e6)),
            sweep_rate_hz=float(attrs.get("sweep_rate_hz", 10.0e3)))

    # --- per line ---------------------------------------------------------

    def _config_for(self, name, base: ProcessingConfig,
                    pitch_um=None) -> ProcessingConfig:
        """Fill a processing config from what the file records about a line."""
        attrs = self.line_attrs(name)
        # The file records which position source the acquisition was set up
        # for, and that is the right default. A caller who passes something
        # other than the library default has asked for it deliberately -
        # usually 'velocity', to read a file whose trigger channel turned out
        # to be unusable - so their choice wins.
        from .postprocess import PositionConfig as _PositionConfig
        stored_source = str(attrs.get("position_source",
                                      base.position.source))
        asked = base.position.source
        position = replace(
            base.position,
            source=asked if asked != _PositionConfig().source else stored_source,
            trigger_interval_um=_as_float(
                attrs.get("trigger_interval_um"),
                base.position.trigger_interval_um),
            first_pulse_position_um=_as_float(
                attrs.get("first_pulse_position_um"),
                base.position.first_pulse_position_um),
            latency_s=_as_float(attrs.get("trigger_latency_s"),
                                base.position.latency_s),
            direction=int(attrs.get("direction", base.position.direction)) or 1,
            velocity_mm_s=_as_float(attrs.get("velocity_mm_s"),
                                    base.position.velocity_mm_s),
            x_start_um=_as_float(attrs.get("x_start_um"),
                                 base.position.x_start_um))
        segments = replace(
            base.segments,
            pre_delay_s=_as_float(attrs.get("segment_pre_delay_s"),
                                  base.segments.pre_delay_s),
            post_delay_s=_as_float(attrs.get("segment_post_delay_s"),
                                   base.segments.post_delay_s),
            align=str(attrs.get("segment_align", base.segments.align)))
        dc = replace(base.dc,
                     offset_v=_as_float(attrs.get("dc_offset_v"),
                                        base.dc.offset_v),
                     method=str(attrs.get("dc_method", base.dc.method)))
        pitch = (pitch_um if pitch_um is not None
                 else _as_float(attrs.get("pixel_pitch_um"),
                                base.pixel_pitch_um))
        return replace(base, timing=self.timing(), position=position,
                       segments=segments, dc=dc, pixel_pitch_um=pitch)

    def line_position_extent(self, name, config: ProcessingConfig):
        """(min, max) position of a line without reading its AC record.

        Only TRIGTIME and the trigger channel are needed to know where a line
        went, and both are tiny, so a shared grid can be built over a whole
        map without touching the heavy datasets twice.
        """
        cfg = self._config_for(name, config)
        attrs = self.line_attrs(name)
        trigtime = self.read_trigtime(name)
        trigger = self.read_channel(name, ROLE_TRIGGER)
        if trigtime is None:
            raise ValueError(f"{name} has no trigtime; cannot place it")
        times = trigtime[:, 0]
        if trigger is not None and cfg.position.source == "anchors":
            result = positions_from_anchors(
                trigger.volts,
                trigger.dt_s(_as_float(attrs.get("sample_rate_hz"))),
                times, times, cfg.position)
        else:
            result = positions_from_velocity(times, cfg.position)
        finite = result.positions_um[np.isfinite(result.positions_um)]
        if finite.size == 0:
            raise ValueError(f"{name} produced no finite positions")
        return float(np.min(finite)), float(np.max(finite))

    def process_line(self, name, config: ProcessingConfig = ProcessingConfig(),
                     pitch_um=None, grid: SpatialGrid = None) -> LineResult:
        """Run the post-processing chain on one stored line."""
        cfg = self._config_for(name, config, pitch_um=pitch_um)
        attrs = self.line_attrs(name)
        ac = self.read_channel(name, ROLE_AC)
        if ac is None:
            raise ValueError(f"{name} has no AC channel")
        dc = self.read_channel(name, ROLE_DC)
        trigger = self.read_channel(name, ROLE_TRIGGER)
        trigtime = self.read_trigtime(name)
        sample_rate = _as_float(attrs.get("sample_rate_hz"))

        common = dict(
            config=cfg,
            velocity_mm_s=_as_float(attrs.get("velocity_mm_s")),
            x_start_um=_as_float(attrs.get("x_start_um"), 0.0),
            direction=int(attrs.get("direction", 1)) or 1,
            grid=grid,
            dc=None if dc is None else dc.volts,
            dc_sparsing=1 if dc is None else dc.sparsing,
            trigger=None if trigger is None else trigger.volts,
            trigger_sparsing=1 if trigger is None else trigger.sparsing)
        ac_volts = ac.volts

        mode = str(attrs.get("mode", "sequence"))
        if mode == "flat" or ac_volts.ndim == 1:
            return process_flat(ac_volts, sample_rate_hz=sample_rate, **common)
        if trigtime is None:
            raise ValueError(
                f"{name} is segmented but has no trigtime dataset")
        return process_segmented(ac_volts, sample_rate_hz=sample_rate,
                                 trigtime=trigtime, **common)

    # --- the map ----------------------------------------------------------

    def read_map(self, pitch_um=None,
                 config: ProcessingConfig = ProcessingConfig(),
                 grid: SpatialGrid = None, keep_cube: bool = True,
                 search_hz=(1.0e9, 20.0e9), skip_coincidence_s=0.0,
                 max_delay_s=0.0, metric: str = "brillouin") -> MapResult:
        """Rebuild a 2-D map from every line in the file.

        The grid is built once from the position extents of all the lines and
        then shared by all of them, which is what makes forward and reverse
        lines land on the same pixels.
        """
        names = self.line_names()
        if not names:
            raise ValueError("file contains no lines")
        warnings = []

        if grid is None:
            lo, hi = math.inf, -math.inf
            for name in names:
                low, high = self.line_position_extent(name, config)
                lo, hi = min(lo, low), max(hi, high)
            pitch = (pitch_um if pitch_um is not None
                     else self._config_for(names[0], config).pixel_pitch_um)
            n_pixels = int(math.floor((hi - lo) / pitch)) + 1
            grid = SpatialGrid(origin_um=lo, pitch_um=pitch,
                               n_pixels=max(n_pixels, 1))

        results, y_um = [], []
        cube = None
        amplitude = np.full((len(names), grid.n_pixels), np.nan)
        frequency = np.full((len(names), grid.n_pixels), np.nan)
        counts = np.zeros((len(names), grid.n_pixels), dtype=np.int64)
        delay_axis, quantity = None, ""

        for row, name in enumerate(names):
            result = self.process_line(name, config=config, pitch_um=pitch_um,
                                       grid=grid)
            results.append(result)
            y_um.append(float(self.line_attrs(name).get("slow_axis_um", row)))
            traces = result.gridded.traces
            counts[row] = result.gridded.counts
            quantity = result.gridded_from
            if delay_axis is None:
                delay_axis = result.delay_axis_s()
                if keep_cube:
                    cube = np.full(
                        (len(names), grid.n_pixels, traces.shape[1]),
                        np.nan, dtype=np.float32)
            if keep_cube and cube is not None:
                if traces.shape[1] == cube.shape[2]:
                    cube[row] = traces.astype(np.float32)
                else:
                    warnings.append(
                        f"{name} has {traces.shape[1]} delay samples but the "
                        f"cube is {cube.shape[2]} wide, so that line is not "
                        f"in the cube. Lines were processed with different "
                        f"segment windows.")
            if metric == "brillouin":
                amp, freq, _ = brillouin_metric(
                    traces, result.delay_axis_s(), search_hz=search_hz,
                    skip_coincidence_s=skip_coincidence_s,
                    max_delay_s=max_delay_s)
                amplitude[row], frequency[row] = amp, freq
            elif metric == "ptp":
                amplitude[row] = peak_to_peak(traces, result.delay_axis_s())
            else:
                raise ValueError("metric must be 'brillouin' or 'ptp'")
            warnings.extend(f"{name}: {item}" for item in result.warnings)

        order = np.argsort(np.asarray(y_um))
        return MapResult(
            x_um=grid.centers_um(), y_um=np.asarray(y_um)[order],
            delay_axis_s=delay_axis, amplitude=amplitude[order],
            frequency_hz=frequency[order], counts=counts[order],
            cube=None if cube is None else cube[order], grid=grid,
            line_results=[results[i] for i in order], quantity=quantity,
            warnings=warnings)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _H5Reader(_ReaderBase):
    def __init__(self, path):
        import h5py
        self.path = str(path)
        self._h5 = h5py.File(self.path, "r")

    def root_attrs(self):
        return {k: _decode(v) for k, v in self._h5.attrs.items()}

    def line_names(self):
        return sorted(self._h5["lines"].keys())

    def line_attrs(self, name):
        return {k: _decode(v) for k, v in self._h5["lines"][name].attrs.items()}

    def channel_roles(self, name):
        group = self._h5["lines"][name]
        return tuple(key for key in group.keys() if "role" in group[key].attrs)

    def read_stored_channel(self, name, role):
        group = self._h5["lines"][name]
        if role not in group:
            return None
        dataset = group[role]
        return StoredChannel(
            name=str(_decode(dataset.attrs.get("channel", role))),
            role=str(_decode(dataset.attrs.get("role", role))),
            data=np.asarray(dataset[...]),
            sparsing=int(dataset.attrs.get("sparsing", 1)),
            v_gain=float(dataset.attrs.get("v_gain", 1.0)),
            v_offset=float(dataset.attrs.get("v_offset", 0.0)),
            units=str(_decode(dataset.attrs.get("units", "V"))))

    def read_trigtime(self, name):
        group = self._h5["lines"][name]
        if ROLE_TRIGTIME not in group:
            return None
        return np.asarray(group[ROLE_TRIGTIME][...], dtype=np.float64)

    def line_array(self, name, key):
        group = self._h5["lines"][name]
        return None if key not in group else np.asarray(group[key][...])

    def close(self):
        self._h5.close()


class _RawReader(_ReaderBase):
    def __init__(self, path):
        self.path = str(path)
        with open(os.path.join(self.path, "manifest.json")) as handle:
            self._manifest = json.load(handle)
        self._lines = {entry["name"]: entry
                       for entry in self._manifest["lines"]}

    def root_attrs(self):
        return dict(self._manifest["root"])

    def line_names(self):
        return sorted(self._lines.keys())

    def line_attrs(self, name):
        return dict(self._lines[name]["attrs"])

    def channel_roles(self, name):
        return tuple(c["role"] for c in self._lines[name]["channels"])

    def read_stored_channel(self, name, role):
        for channel in self._lines[name]["channels"]:
            if channel["role"] != role:
                continue
            raw = np.fromfile(os.path.join(self.path, channel["file"]),
                              dtype=np.dtype(channel["dtype"]))
            return StoredChannel(
                name=str(channel.get("channel", role)), role=role,
                data=raw.reshape(tuple(channel["shape"])),
                sparsing=int(channel.get("sparsing", 1)),
                v_gain=float(channel["v_gain"]),
                v_offset=float(channel["v_offset"]),
                units=str(channel.get("units", "V")))
        return None

    def read_trigtime(self, name):
        entry = self._lines[name].get("trigtime")
        if entry is None:
            return None
        raw = np.fromfile(os.path.join(self.path, entry["file"]),
                          dtype=np.dtype(entry["dtype"]))
        return raw.reshape(tuple(entry["shape"]))

    def line_array(self, name, key):
        values = self._lines[name].get("arrays", {}).get(key)
        return None if values is None else np.asarray(values, dtype=np.int64)


def _decode(value):
    if isinstance(value, bytes):
        return value.decode("ascii", "replace")
    if isinstance(value, np.bytes_):
        return value.tobytes().decode("ascii", "replace")
    if isinstance(value, np.generic):
        return value.item()
    return value


def read_map(path, **kwargs) -> MapResult:
    """Open `path`, rebuild the map, close it again."""
    reader = open_file(path)
    try:
        return reader.read_map(**kwargs)
    finally:
        reader.close()
