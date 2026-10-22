"""Parser for the LeCroy WAVEDESC block returned by '<channel>:WF?'.

Standalone rather than imported from pscan.lecroy_driver, whose parse is
inlined inside its acquisition method and tied to its own file format. The
field offsets here are the same ones that driver uses, with two fields it
does not expose: TRIGTIME_ARRAY, which carries the per-segment trigger
times a fly scan needs, and SPARSING_FACTOR, which lets a caller confirm
the scope actually applied the sparsing it was asked for.
"""
import struct

import numpy as np

# Byte offsets into the WAVEDESC block, from the LeCroy template.
OFF_COMM_TYPE = 32          # 0 = int8 samples, 1 = int16
OFF_COMM_ORDER = 34         # 0 = HIFIRST (big-endian), 1 = LOFIRST (little)
OFF_WAVE_DESCRIPTOR = 36
OFF_USER_TEXT = 40
OFF_RES_DESC1 = 44
OFF_TRIGTIME_ARRAY = 48
OFF_RIS_TIME_ARRAY = 52
OFF_RES_ARRAY1 = 56
OFF_INSTRUMENT_NAME = 76
OFF_WAVE_ARRAY_COUNT = 116
OFF_FIRST_VALID_PNT = 124
OFF_LAST_VALID_PNT = 128
OFF_SPARSING_FACTOR = 136
OFF_SUBARRAY_COUNT = 144
OFF_SWEEPS_PER_ACQ = 148
OFF_VERTICAL_GAIN = 156
OFF_VERTICAL_OFFSET = 160
OFF_MAX_VALUE = 164
OFF_MIN_VALUE = 168
OFF_NOMINAL_BITS = 172
OFF_HORIZ_INTERVAL = 176
OFF_HORIZ_OFFSET = 180

# The six block lengths, in the order the blocks follow WAVEDESC. Their sum
# is where the sample data starts.
_BLOCK_LENGTH_OFFSETS = (
    OFF_WAVE_DESCRIPTOR, OFF_USER_TEXT, OFF_RES_DESC1,
    OFF_TRIGTIME_ARRAY, OFF_RIS_TIME_ARRAY, OFF_RES_ARRAY1,
)

# One (trigger_time, trigger_offset) float64 pair per segment.
TRIGTIME_PAIR_BYTES = 16


class WavedescError(ValueError):
    """Raised when a reply cannot be parsed as a waveform."""


def _byte_order(block):
    """Return the struct prefix for this waveform's byte order.

    COMM_ORDER is itself written in the order it describes, so its low byte
    identifies that order without needing to know it first. Reading these
    two fields the wrong way round only appears to work while CFMT is set to
    WORD, because then COMM_TYPE and COMM_ORDER are both 1.
    """
    return '<' if block[OFF_COMM_ORDER] == 1 else '>'


def parse_waveform(raw):
    """Parse a '<channel>:WF?' reply into metadata, samples and trigger times.

    Returns a dict. 'samples' is (segments, points_per_segment) int16 - the
    raw ADC codes, normalised to native byte order but deliberately not
    scaled, since the caller may want to defer the volts conversion.
    'trigtime' is (segments, 2) float64 of (trigger_time, trigger_offset)
    seconds, or None when the acquisition carried no such block, which is
    the normal case for a single-shot real-time capture.
    """
    start = raw.find(b'WAVEDESC')
    if start == -1:
        raise WavedescError("no WAVEDESC block in the reply")

    block = raw[start:]
    if len(block) < OFF_HORIZ_OFFSET + 8:
        raise WavedescError(
            f"reply truncated: {len(block)} bytes is shorter than a WAVEDESC"
        )

    fmt = _byte_order(block)

    def i32(offset):
        return struct.unpack_from(fmt + 'i', block, offset)[0]

    def f32(offset):
        return struct.unpack_from(fmt + 'f', block, offset)[0]

    comm_type = struct.unpack_from(fmt + 'h', block, OFF_COMM_TYPE)[0]
    sample_dtype = np.dtype(fmt + 'i2') if comm_type == 1 else np.dtype('i1')

    lengths = [i32(offset) for offset in _BLOCK_LENGTH_OFFSETS]
    data_offset = sum(lengths)

    total_points = i32(OFF_WAVE_ARRAY_COUNT)
    if total_points <= 0:
        raise WavedescError("waveform reports zero points")

    segments = i32(OFF_SUBARRAY_COUNT)
    if segments <= 0:
        segments = 1
    if total_points % segments:
        raise WavedescError(
            f"{total_points} points do not divide into {segments} segments"
        )
    points_per_segment = total_points // segments

    samples = np.frombuffer(
        block, dtype=sample_dtype, offset=data_offset, count=total_points
    )
    # astype() also normalises to native byte order. int8 transfers are
    # widened so callers see one sample type regardless of CFMT.
    samples = samples.astype(np.int16).reshape(segments, points_per_segment)

    meta = {
        'segments': segments,
        'points_per_segment': points_per_segment,
        'total_points': total_points,
        'sample_bytes': sample_dtype.itemsize,
        'byte_order': 'little' if fmt == '<' else 'big',
        'sparsing_factor': i32(OFF_SPARSING_FACTOR),
        'sweeps_per_acq': i32(OFF_SWEEPS_PER_ACQ),
        'vertical_gain': f32(OFF_VERTICAL_GAIN),
        'vertical_offset': f32(OFF_VERTICAL_OFFSET),
        'max_value': f32(OFF_MAX_VALUE),
        'min_value': f32(OFF_MIN_VALUE),
        'nominal_bits': struct.unpack_from(fmt + 'h', block, OFF_NOMINAL_BITS)[0],
        'horiz_interval': f32(OFF_HORIZ_INTERVAL),
        'horiz_offset': struct.unpack_from(fmt + 'd', block, OFF_HORIZ_OFFSET)[0],
        'instrument': block[OFF_INSTRUMENT_NAME:OFF_INSTRUMENT_NAME + 16]
                          .split(b'\x00')[0].decode('ascii', 'replace').strip(),
    }

    return {
        'meta': meta,
        'samples': samples,
        'trigtime': _parse_trigtime(block, lengths, fmt, segments),
    }


def _parse_trigtime(block, lengths, fmt, segments):
    """Read TRIGTIME_ARRAY, which sits after the first three blocks.

    In Sequence mode this holds one (trigger_time, trigger_offset) pair per
    segment: trigger_time is that segment's trigger relative to the first
    trigger of the acquisition, and trigger_offset is the sub-sample offset
    from the trigger to the first sample point. Together they are what makes
    a fly scan's position recoverable, and they also let per-segment trigger
    jitter be taken off the delay axis.
    """
    length = lengths[_BLOCK_LENGTH_OFFSETS.index(OFF_TRIGTIME_ARRAY)]
    if length < TRIGTIME_PAIR_BYTES:
        return None

    pairs = length // TRIGTIME_PAIR_BYTES
    offset = sum(lengths[:_BLOCK_LENGTH_OFFSETS.index(OFF_TRIGTIME_ARRAY)])

    flat = np.frombuffer(
        block, dtype=np.dtype(fmt + 'f8'), offset=offset, count=pairs * 2
    )
    trigtime = flat.reshape(pairs, 2).astype(np.float64)

    if pairs != segments:
        # Not fatal - the array describes what is on the wire, and a caller
        # may still want it - but worth surfacing, since a mismatch means
        # the scope and the descriptor disagree about the segment count.
        print(
            f"[WARNING] TRIGTIME_ARRAY has {pairs} entries for "
            f"{segments} segments"
        )
    return trigtime


def to_volts(samples, meta):
    """Convert raw ADC codes to volts.

    LeCroy's convention is value = vertical_gain * code - vertical_offset;
    note the minus, which is easy to get backwards.
    """
    return samples * meta['vertical_gain'] - meta['vertical_offset']


def segment_times(trigtime, meta):
    """Per-segment trigger time in seconds, relative to the first trigger.

    Falls back to assuming a uniform cadence when the acquisition carried no
    TRIGTIME_ARRAY, which a caller should treat as an estimate rather than a
    measurement - returns (times, measured) so it can tell the difference.
    """
    if trigtime is not None:
        return trigtime[:, 0], True

    span = meta['points_per_segment'] * meta['horiz_interval']
    return np.arange(meta['segments'], dtype=np.float64) * span, False
