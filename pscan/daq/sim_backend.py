"""A card that isn't there, for working on pscan without the rig.

Selected only by asking for it - `backend sim` in a .con file, or
`pydaq-dev --backend sim` - never by auto-detection. It exists for three jobs:

  * Checking that a .con file's pipeline, grid and output files come out right
    on a laptop, which --dry-run cannot do because it stops before any
    acquisition happens.
  * Exercising the scan loop over several d2a axes without a galvo on the
    bench.
  * Giving the tests something to acquire from, so the output format and the
    axis walk are covered off-hardware.

The waveform is deterministic for a given channel and sample count, so a test
can assert on it: a 1 kHz sine at an amplitude that depends on the channel,
plus a small fixed dither so a trace is not suspiciously clean. It is clearly
synthetic and should never be mistaken for data.
"""

import numpy as np

from pscan.daq.base import DAQBackend, DeviceInfo, resolve_range

# Shaped after a PCIe-6351 so a config developed against the simulator asks
# for ranges that exist on the real card.
SIM_AI_RANGES = ((-10.0, 10.0), (-5.0, 5.0), (-1.0, 1.0), (-0.2, 0.2))
SIM_AO_RANGES = ((-10.0, 10.0), (-5.0, 5.0))
SIM_N_AI = 16
SIM_N_AO = 2
SIM_MAX_RATE = 1.25e6

# The synthetic signal's frequency, in Hz.
SIM_TONE_HZ = 1000.0


class SimBackend(DAQBackend):
    name = "sim"

    def __init__(self, address="sim0"):
        super().__init__(address=address)
        self._traces = 0

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    @classmethod
    def available(cls):
        return True

    @classmethod
    def unavailable_reason(cls):
        return "available"

    @classmethod
    def discoverable(cls):
        # Never auto-selected. A simulated card that silently stood in for a
        # real one would produce a full set of plausible .d files containing
        # nothing but a synthesised sine - the worst possible failure mode for
        # an instrument.
        return False

    @classmethod
    def enumerate(cls):
        return [DeviceInfo(
            backend=cls.name,
            address="sim0",
            model="simulated card (NOT REAL HARDWARE)",
            n_ai=SIM_N_AI,
            n_ao=SIM_N_AO,
            ai_ranges=SIM_AI_RANGES,
            ao_ranges=SIM_AO_RANGES,
            max_ai_rate=SIM_MAX_RATE,
        )]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self):
        if self._connected:
            return
        print(
            "[SIM] Simulated DAQ card in use. Every trace below is "
            "SYNTHETIC - do not treat any output of this run as data."
        )
        self._connected = True

    def _close_device(self):
        print(f"[SIM] Simulated card closed after {self._traces} trace(s).")

    def describe(self):
        return self.enumerate()[0]

    # ------------------------------------------------------------------
    # a2d
    # ------------------------------------------------------------------

    def _acquire_volts(self, config):
        # Resolved even though nothing needs it, so a range request that the
        # real card would refuse also fails here. Catching that on a laptop is
        # the whole point of the simulator.
        _index, lo, hi = resolve_range(
            SIM_AI_RANGES,
            range_index=config.range_index,
            v_min=config.v_min,
            v_max=config.v_max,
        )

        n = config.n_samples
        rate = float(config.sample_rate or 1)
        t = np.arange(n, dtype=np.float64) / rate

        amplitude = min(hi, 0.5 + 0.25 * config.channel)
        signal = amplitude * np.sin(2.0 * np.pi * SIM_TONE_HZ * t)

        # A fixed, seeded dither rather than fresh randomness, so two runs of
        # the same config produce identical files and a test can diff them.
        rng = np.random.default_rng(config.channel * 1000 + n)
        signal += rng.normal(0.0, amplitude * 0.01, size=n)

        self._traces += 1
        return np.clip(signal, lo, hi)

    # ------------------------------------------------------------------
    # d2a
    # ------------------------------------------------------------------

    def _write_ao(self, channel, volts):
        if channel >= SIM_N_AO:
            raise RuntimeError(
                f"simulated card has {SIM_N_AO} output channel(s); "
                f"ao{channel} does not exist"
            )
        return True

    # read_d2a() is deliberately left as the base implementation: the
    # simulator has no persistent state either, so it can only recall what
    # this session set, exactly like a real card that cannot be read back.
