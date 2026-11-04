"""A card that isn't there, so the tests need no rig.

Deliberately NOT in pscan/: source holds one driver file per real piece of
hardware, and a simulated card is not one. Keeping it here also means it can
never be auto-selected during a scan - a fake card standing in for a real one
would produce a full set of plausible .d files containing nothing but a
synthesised sine, which is the worst failure mode this program has.

Tests register it with pscan.daq.register_backend() and tear it down again.

The waveform is deterministic for a given channel and sample count, so a test
can assert on it: a 1 kHz tone whose amplitude depends on the channel, plus a
seeded dither so a trace is not suspiciously clean.
"""

import numpy as np

from pscan.daq import DAQBackend, DeviceInfo, register_backend, resolve_range

# Shaped after a PCIe-6351, so a config exercised here asks for ranges that
# exist on the real card.
AI_RANGES = ((-10.0, 10.0), (-5.0, 5.0), (-1.0, 1.0), (-0.2, 0.2))
AO_RANGES = ((-10.0, 10.0), (-5.0, 5.0))
N_AI = 16
N_AO = 2
MAX_RATE = 1.25e6

#: The synthetic signal's frequency, in Hz.
TONE_HZ = 1000.0


@register_backend
class FakeCard(DAQBackend):
    name = "fake"

    def __init__(self, address="fake0"):
        super().__init__(address=address)
        self.traces = 0

    @classmethod
    def available(cls):
        return True

    @classmethod
    def unavailable_reason(cls):
        return "available"

    @classmethod
    def discoverable(cls):
        return False

    @classmethod
    def enumerate(cls):
        return [DeviceInfo(
            backend=cls.name,
            address="fake0",
            model="simulated card (NOT REAL HARDWARE)",
            n_ai=N_AI,
            n_ao=N_AO,
            ai_ranges=AI_RANGES,
            ao_ranges=AO_RANGES,
            max_ai_rate=MAX_RATE,
        )]

    def connect(self):
        self._connected = True

    def _close_device(self):
        pass

    def describe(self):
        return self.enumerate()[0]

    def _acquire_volts(self, config):
        # Resolved even though the waveform barely needs it, so a range
        # request the real card would refuse also fails here.
        _index, lo, hi = resolve_range(
            AI_RANGES,
            range_index=config.range_index,
            v_min=config.v_min,
            v_max=config.v_max,
        )

        n = config.n_samples
        t = np.arange(n, dtype=np.float64) / float(config.sample_rate or 1)

        amplitude = min(hi, 0.5 + 0.25 * config.channel)
        signal = amplitude * np.sin(2.0 * np.pi * TONE_HZ * t)

        # Seeded rather than fresh randomness, so two runs of the same config
        # produce identical files and a test can diff them.
        rng = np.random.default_rng(config.channel * 1000 + n)
        signal += rng.normal(0.0, amplitude * 0.01, size=n)

        self.traces += 1
        return np.clip(signal, lo, hi)

    def _write_ao(self, channel, volts):
        if channel >= N_AO:
            raise RuntimeError(
                f"simulated card has {N_AO} output channel(s); "
                f"ao{channel} does not exist"
            )
        return True
