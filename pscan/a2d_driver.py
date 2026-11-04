"""Back-compat shim: NIDriver is now the comedi backend.

The comedi implementation moved to pscan/daq/comedi_backend.py when a second
path to the hardware became necessary - an NI X Series card such as the
PCIe-6351 has no comedi driver at all, so comedi stopped being "the" driver
and became one of several.

`NIDriver` survives with its original name and constructor so the dev/ timing
scripts and any local experiment that imports it keep working. New code should
go through pscan.daq.open_device(), which picks the right backend instead of
hardcoding comedi.
"""

from pscan.daq.comedi_backend import (  # noqa: F401  (re-exported)
    FALLBACK_MAXDATA,
    FALLBACK_RANGES,
    TRIG_COUNT,
    TRIG_NOW,
    TRIG_TIMER,
    ComediBackend,
    comedi_cmd_struct,
    comedi_range,
)


class NIDriver(ComediBackend):
    """The comedi backend under its original name.

    Takes `device_node` as bytes, as the original did, so existing callers do
    not change.
    """

    def __init__(self, device_node=b"/dev/comedi0"):
        address = (device_node.decode()
                   if isinstance(device_node, bytes) else device_node)
        super().__init__(address=address)
