import logging
import os
import sys

import ida_kernwin


logger = logging.getLogger(__name__)


_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)


def should_load() -> bool:
    if not ida_kernwin.is_idaq():
        return False
    raw_version = ida_kernwin.get_kernel_version()
    parts = tuple(int(p) for p in raw_version.split(".") if p.isdigit())
    # Compare the major only: non-digit fragments get filtered out above, so
    # a "9"-shaped result would make a tuple compare ((9,) < (9, 0)) refuse
    # to load on a valid IDA 9.
    if parts and parts[0] < 9:
        logger.warning("ida-breakout requires IDA 9.0+ (got %s)", raw_version)
        return False
    try:
        from PySide6 import QtCore, QtGui, QtWidgets  # noqa: F401
    except Exception:
        logger.warning("ida-breakout requires PySide6 (normally bundled with IDA 9.x)")
        return False
    try:
        import ida_hexrays  # noqa: F401
    except Exception:
        logger.warning("ida-breakout requires the Hex-Rays decompiler")
        return False
    return True


if should_load():
    from ida_breakout import breakout_plugin_t

    def PLUGIN_ENTRY():
        return breakout_plugin_t()

else:
    import ida_idaapi

    class _BreakoutNopPlugin(ida_idaapi.plugin_t):
        flags = ida_idaapi.PLUGIN_HIDE | ida_idaapi.PLUGIN_UNL
        wanted_name = "ida-breakout (disabled)"
        comment = "ida-breakout is disabled in this environment"
        help = ""
        wanted_hotkey = ""

        def init(self):
            return ida_idaapi.PLUGIN_SKIP

        def run(self, arg):
            pass

        def term(self):
            pass

    def PLUGIN_ENTRY():
        return _BreakoutNopPlugin()
