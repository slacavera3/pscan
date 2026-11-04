"""One card, one session - and every session flushed at teardown.

This exists because of a real bug. select_device() returns address=None for a
backend whose device name is only known after connect() (daqmx picking the one
device it can see), so the cache LOOKUP key was ('daqmx', None) while the
STORE key was ('daqmx', 'Dev1'). Every block missed the cache and opened its
own session; all of them stored under the same resolved key, so only the last
survived in _sessions; close_all() flushed only that one; and the rest of the
queued traces died with their daemon writer threads when the process exited.

A 30-trace scan produced a 256-byte .d file and reported success. Nothing in
the log said anything was wrong. These tests pin the invariants that prevent
it: one session per card, and nothing left unflushed.
"""

import unittest

from pscan import daq
from pscan.daq import DAQBackend, DeviceInfo, register_backend


class _LateAddressCard(DAQBackend):
    """A card that only learns its own name during connect().

    This is daqmx's behaviour: the device name comes from the driver, so there
    is nothing to key a cache on until the session is open.
    """

    name = "lateaddr"
    opened = 0
    flushed = []

    def __init__(self, address=None):
        super().__init__(address=address)
        type(self).opened += 1

    @classmethod
    def available(cls):
        return True

    @classmethod
    def discoverable(cls):
        return False

    def connect(self):
        if self.address is None:
            self.address = "Dev1"
        self._connected = True

    def _close_device(self):
        type(self).flushed.append(self.address)

    def describe(self):
        return DeviceInfo(
            backend=self.name, address=self.address, model="late",
            n_ai=1, n_ao=1,
            ai_ranges=((-10.0, 10.0),), ao_ranges=((-10.0, 10.0),),
        )

    def _acquire_volts(self, config):
        raise AssertionError("not needed here")

    def _write_ao(self, channel, volts):
        return True


class SessionCacheTestCase(unittest.TestCase):

    def setUp(self):
        _LateAddressCard.opened = 0
        _LateAddressCard.flushed = []
        register_backend(_LateAddressCard)
        self.addCleanup(daq.unregister_backend, _LateAddressCard.name)
        self.addCleanup(daq.close_all)


class TestOneSessionPerCard(SessionCacheTestCase):

    def test_repeat_requests_reuse_one_session(self):
        first = daq.open_device(backend='lateaddr', need_ai=True,
                                role='a2d', announce=False)
        second = daq.open_device(backend='lateaddr', need_ao=True,
                                 role='d2a', announce=False)

        self.assertIs(first, second)
        self.assertEqual(
            _LateAddressCard.opened, 1,
            "the card was opened more than once for the same address"
        )

    def test_an_a2d_and_two_d2a_blocks_share_one_card(self):
        # The shape of a real config on a PCIe-6351: photodiode on an input,
        # a galvo pair on the two outputs, all one board.
        sessions = [
            daq.open_device(backend='lateaddr', need_ai=True, role='a2d',
                            announce=False),
            daq.open_device(backend='lateaddr', need_ao=True, role='d2a',
                            announce=False),
            daq.open_device(backend='lateaddr', need_ao=True, role='d2a',
                            announce=False),
        ]
        self.assertEqual(len({id(s) for s in sessions}), 1)
        self.assertEqual(_LateAddressCard.opened, 1)

    def test_the_resolved_address_also_hits_the_cache(self):
        # Asking by the name the card reported must find the same session as
        # asking with no name at all.
        implicit = daq.open_device(backend='lateaddr', need_ai=True,
                                   announce=False)
        explicit = daq.open_device(backend='lateaddr', address='Dev1',
                                   need_ai=True, announce=False)

        self.assertIs(implicit, explicit)
        self.assertEqual(_LateAddressCard.opened, 1)

    def test_open_sessions_reports_one_entry_per_card(self):
        daq.open_device(backend='lateaddr', need_ai=True, announce=False)
        daq.open_device(backend='lateaddr', need_ao=True, announce=False)
        self.assertEqual(len(daq.open_sessions()), 1)


class TestTeardown(SessionCacheTestCase):

    def test_close_all_flushes_the_card_exactly_once(self):
        daq.open_device(backend='lateaddr', need_ai=True, announce=False)
        daq.open_device(backend='lateaddr', need_ao=True, announce=False)

        daq.close_all()

        # Once, not twice: the session is registered under two keys, and
        # disconnecting it twice would flush an already-closed writer.
        self.assertEqual(_LateAddressCard.flushed, ['Dev1'])

    def test_close_all_leaves_nothing_open(self):
        daq.open_device(backend='lateaddr', need_ai=True, announce=False)
        daq.close_all()
        self.assertEqual(daq.open_sessions(), {})

    def test_close_all_is_idempotent(self):
        daq.open_device(backend='lateaddr', need_ai=True, announce=False)
        daq.close_all()
        daq.close_all()
        self.assertEqual(_LateAddressCard.flushed, ['Dev1'])

    def test_every_opened_session_is_flushed(self):
        # The invariant that actually protects the data: whatever was opened
        # gets closed, because closing is what flushes the writer thread.
        daq.open_device(backend='lateaddr', need_ai=True, announce=False)
        daq.open_device(backend='lateaddr', address='Dev1', need_ao=True,
                        announce=False)

        opened = _LateAddressCard.opened
        daq.close_all()
        self.assertEqual(len(_LateAddressCard.flushed), opened)


if __name__ == '__main__':
    unittest.main()
