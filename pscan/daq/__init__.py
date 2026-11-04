"""Which card, reached how - decided once, here.

pscan used to have exactly one answer: libcomedi, on /dev/comedi0. That is no
longer enough. comedi's ni_pcimio driver covers the NI E and M series and no X
Series board, so a PCIe-6351 is unreachable through it, and NI's recent cards
are all in that position. Meanwhile the comedi cards already in service work
fine and are not going anywhere.

So pscan has to pick. The rule is: detect what is actually present, use it
when the answer is unambiguous, and refuse to guess when it is not.

That last clause is the important one. On an instrument, quietly choosing the
wrong card does not raise an error - it produces a complete, plausible-looking
set of .d files containing the wrong signal, and nothing downstream can tell.
So when more than one card could serve a block, this module stops and prints
the two lines to paste into the .con file. One extra line in a config is a far
better outcome than a scan that has to be repeated.

Auto-detection only ever considers comedi and DAQmx. The Phidget board and the
simulator are reachable by name alone - see DAQBackend.discoverable().
"""

from pscan.daq.base import (
    A2DConfig,
    DAQBackend,
    DEFAULT_TERMINAL,
    DeviceInfo,
    TERMINALS,
    clamp_volts,
    resolve_range,
)
from pscan.daq.comedi_backend import ComediBackend
from pscan.daq.daqmx_backend import DaqmxBackend
from pscan.daq.phidget_backend import PhidgetBackend
from pscan.daq.sim_backend import SimBackend

__all__ = [
    'A2DConfig', 'DAQBackend', 'DeviceInfo', 'TERMINALS', 'DEFAULT_TERMINAL',
    'BACKENDS', 'clamp_volts', 'resolve_range', 'available_backends',
    'discover', 'select_device', 'open_device', 'close_all',
    'format_candidates', 'DaqSelectionError',
]

#: Every backend, by the name a .con file's `backend` line uses.
BACKENDS = {
    cls.name: cls
    for cls in (ComediBackend, DaqmxBackend, PhidgetBackend, SimBackend)
}

#: Default address per backend, used when a backend is named without a device.
DEFAULT_ADDRESS = {
    'comedi': '/dev/comedi0',
    'phidget': 'phidget0',
    'sim': 'sim0',
    # daqmx has no sensible default: device names come from NI MAX and are
    # whatever the driver assigned. Its connect() resolves a lone device.
    'daqmx': None,
}

# (backend name, address) -> open session. A card is opened once per process:
# the PCIe-6351 carries 16 AI and 2 AO on one board, so a scan that reads a
# photodiode and drives a mirror from the same card must not open it twice.
_sessions = {}


class DaqSelectionError(RuntimeError):
    """Raised when which card to use cannot be settled without guessing."""


def available_backends():
    """{name: reason} for every backend, 'available' when it is usable.

    Printed by `pydaq-dev list`, because "no cards found" and "the driver
    isn't installed" need completely different responses and look identical
    from the outside.
    """
    status = {}
    for name, cls in BACKENDS.items():
        status[name] = ("available" if cls.available()
                        else cls.unavailable_reason())
    return status


def discover(need_ai=False, need_ao=False, include_hidden=False):
    """Every card that could serve the requested role.

    `include_hidden` adds the backends auto-detection skips (the Phidget board
    and the simulator), which is what the CLI wants when listing everything
    and what auto-detection must never do.
    """
    found = []
    for cls in BACKENDS.values():
        if not include_hidden and not cls.discoverable():
            continue
        if not cls.available():
            continue
        try:
            found.extend(cls.enumerate())
        except Exception as e:
            print(f"[WARNING] Enumerating the {cls.name} backend failed: {e}")

    if need_ai:
        found = [d for d in found if d.n_ai > 0]
    if need_ao:
        found = [d for d in found if d.n_ao > 0]
    return found


def format_candidates(devices, role):
    """Render candidate cards plus the config lines that pin one down."""
    lines = []
    for info in devices:
        lines.append(f"    {info.summary()}")
        lines.append(f"        -> add to the 'action {role}' block:  "
                     f"backend {info.backend}   /   device {info.address}")
    return "\n".join(lines)


def select_device(backend=None, address=None, need_ai=False, need_ao=False,
                  role='a2d'):
    """Settle on (backend class, address) for one config block.

    Naming both a backend and a device is taken at face value and not
    verified here - connect() will fail loudly and more informatively than a
    pre-flight enumeration could, and it means a card that enumeration cannot
    describe is still usable.
    """
    if backend is not None:
        if backend not in BACKENDS:
            raise DaqSelectionError(
                f"unknown backend '{backend}'. Known backends: "
                f"{', '.join(sorted(BACKENDS))}"
            )
        cls = BACKENDS[backend]

        if not cls.available():
            raise DaqSelectionError(
                f"the '{backend}' backend was requested but is not usable: "
                f"{cls.unavailable_reason()}"
            )

        if address is not None:
            return cls, address

        if not cls.discoverable():
            return cls, DEFAULT_ADDRESS.get(backend)

        candidates = [
            d for d in cls.enumerate()
            if (not need_ai or d.n_ai > 0) and (not need_ao or d.n_ao > 0)
        ]
        if len(candidates) == 1:
            return cls, candidates[0].address
        if not candidates:
            # Fall back to the default node rather than refusing: enumeration
            # can come up empty on a card it cannot describe, and connect()
            # gives the better error.
            return cls, DEFAULT_ADDRESS.get(backend)

        raise DaqSelectionError(
            f"the '{backend}' backend sees {len(candidates)} cards that could "
            f"serve this 'action {role}' block, and no 'device' line said "
            f"which:\n{format_candidates(candidates, role)}"
        )

    # ------------------------------------------------------------------
    # Auto. The address alone is enough to infer a backend, because comedi
    # addresses are device nodes and DAQmx addresses are not.
    # ------------------------------------------------------------------
    if address is not None:
        inferred = 'comedi' if str(address).startswith('/dev/') else 'daqmx'
        return select_device(
            backend=inferred, address=address,
            need_ai=need_ai, need_ao=need_ao, role=role,
        )

    candidates = discover(need_ai=need_ai, need_ao=need_ao)

    if not candidates:
        detail = "\n".join(
            f"    {name:<8} {reason}"
            for name, reason in sorted(available_backends().items())
            if BACKENDS[name].discoverable()
        )
        wanted = "analog input" if need_ai else "analog output"
        raise DaqSelectionError(
            f"no card with {wanted} was found for this 'action {role}' "
            f"block.\n  Backend status:\n{detail}\n"
            f"  Run 'pydaq-dev list' to see everything pscan can find."
        )

    if len(candidates) == 1:
        info = candidates[0]
        return BACKENDS[info.backend], info.address

    raise DaqSelectionError(
        f"{len(candidates)} cards could serve this 'action {role}' block, so "
        f"pscan will not pick one for you - the wrong choice produces a full "
        f"set of plausible files containing the wrong signal.\n"
        f"{format_candidates(candidates, role)}"
    )


def open_device(backend=None, address=None, need_ai=False, need_ao=False,
                role='a2d', announce=True, **kwargs):
    """Return a connected session for the card this block should use.

    Sessions are cached per (backend, address) for the life of the process, so
    a config with several a2d and d2a blocks on one card opens it once.
    """
    cls, resolved = select_device(
        backend=backend, address=address,
        need_ai=need_ai, need_ao=need_ao, role=role,
    )

    key = (cls.name, resolved)
    session = _sessions.get(key)
    if session is not None:
        return session

    session = cls(resolved, **kwargs) if resolved is not None else cls(**kwargs)
    session.connect()
    _sessions[(cls.name, session.address)] = session

    if announce:
        # One line per card, in the run's log. Months later this is the only
        # record of which card and which driver produced a given .d file.
        try:
            print(f" -> DAQ: {session.describe().summary()}")
        except Exception:
            print(f" -> DAQ: {cls.name} {session.address}")

    return session


def close_all():
    """Disconnect every open session. Safe to call more than once."""
    errors = []
    for key, session in list(_sessions.items()):
        try:
            session.disconnect()
        except Exception as e:
            errors.append(f"{key[0]} {key[1]}: {e}")
        finally:
            _sessions.pop(key, None)

    for message in errors:
        print(f"[WARNING] DAQ disconnect failed for {message}")


def open_sessions():
    """The sessions currently open, for teardown reporting."""
    return dict(_sessions)
