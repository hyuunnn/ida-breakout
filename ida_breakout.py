import logging

import ida_idaapi
import ida_kernwin
import ida_hexrays

from PySide6 import QtWidgets

from ida_breakout_lib.overlay import BreakoutOverlay
from ida_breakout_lib.pseudocode import (
    compute_playfield_height,
    detect_bricks_from_pixels,
    find_pseudocode_viewport,
    grab_viewport_buffer,
    sample_viewport_bg_colors,
)


logger = logging.getLogger(__name__)

ACTION_NAME = "ida_breakout:start"
ACTION_LABEL = "ida-breakout: Start brick break"
ACTION_HOTKEY = "Ctrl-Alt-K"
ACTION_TOOLTIP = "Turn this Pseudocode view into a Breakout game."


class _StartGameHandler(ida_kernwin.action_handler_t):
    def __init__(self, plugmod):
        super().__init__()
        self.plugmod = plugmod

    def activate(self, ctx):
        self.plugmod.toggle_game()
        return 1

    def update(self, ctx):
        if ctx.widget_type == ida_kernwin.BWN_PSEUDOCODE:
            return ida_kernwin.AST_ENABLE_FOR_WIDGET
        return ida_kernwin.AST_DISABLE_FOR_WIDGET


class _UIHooks(ida_kernwin.UI_Hooks):
    def __init__(self, plugmod):
        super().__init__()
        self.plugmod = plugmod

    def widget_invisible(self, twidget):
        if (
            self.plugmod.active_overlay is not None
            and twidget is not None
            and twidget == self.plugmod.active_twidget
        ):
            logger.info("ida-breakout: pseudocode tab going away, stopping game")
            self.plugmod.stop_game()

    def finish_populating_widget_popup(self, widget, popup):
        if ida_kernwin.get_widget_type(widget) == ida_kernwin.BWN_PSEUDOCODE:
            ida_kernwin.attach_action_to_popup(widget, popup, ACTION_NAME, None)


class _HexraysHooks(ida_hexrays.Hexrays_Hooks):
    def __init__(self, plugmod):
        super().__init__()
        self.plugmod = plugmod

    def refresh_pseudocode(self, vu):
        # Only the tab hosting the game — an F5 in another pseudocode tab
        # must not kill a running game.
        if (
            self.plugmod.active_overlay is not None
            and vu.ct == self.plugmod.active_twidget
        ):
            logger.info("ida-breakout: pseudocode refreshed (F5), stopping game")
            self.plugmod.stop_game()
        return 0


class breakout_plugmod_t(ida_idaapi.plugmod_t):
    def __init__(self):
        self.active_overlay = None
        self.active_twidget = None
        self.ui_hooks = None
        self.hexrays_hooks = None
        self._action_registered = False
        self._hexrays_available = False
        self.init()

    def register_action(self):
        desc = ida_kernwin.action_desc_t(
            ACTION_NAME,
            ACTION_LABEL,
            _StartGameHandler(self),
            ACTION_HOTKEY,
            ACTION_TOOLTIP,
            -1,
        )
        if ida_kernwin.register_action(desc):
            self._action_registered = True
            logger.info(
                "ida-breakout: registered action %s with hotkey %s",
                ACTION_NAME,
                ACTION_HOTKEY,
            )
        else:
            logger.warning("ida-breakout: failed to register action %s", ACTION_NAME)

    def unregister_action(self):
        if self._action_registered:
            ida_kernwin.unregister_action(ACTION_NAME)
            self._action_registered = False

    def register_ui_hooks(self):
        self.ui_hooks = _UIHooks(self)
        self.ui_hooks.hook()

    def unregister_ui_hooks(self):
        if self.ui_hooks is not None:
            self.ui_hooks.unhook()
            self.ui_hooks = None

    def register_hexrays_hooks(self):
        if not self._hexrays_available:
            return
        self.hexrays_hooks = _HexraysHooks(self)
        self.hexrays_hooks.hook()

    def unregister_hexrays_hooks(self):
        if self.hexrays_hooks is not None:
            self.hexrays_hooks.unhook()
            self.hexrays_hooks = None

    def init(self):
        self._hexrays_available = bool(ida_hexrays.init_hexrays_plugin())
        if not self._hexrays_available:
            logger.warning(
                "ida-breakout: Hex-Rays decompiler not available; the action will be a no-op"
            )
        self.register_action()
        self.register_ui_hooks()
        self.register_hexrays_hooks()
        logger.info("ida-breakout loaded")

    def run(self, arg):
        self.toggle_game()

    def term(self):
        self.stop_game()
        self.unregister_hexrays_hooks()
        self.unregister_ui_hooks()
        self.unregister_action()

    def __del__(self):
        # IDAPython never dispatches plugmod_t.term() (plugmod_t is a bare
        # pass-through class with no term binding) — per the plugin skill
        # convention, teardown runs here when IDA drops its plugmod
        # reference on unload. Swallow errors: __del__ can fire during
        # interpreter shutdown when IDA modules are already torn down.
        try:
            self.term()
        except Exception:
            pass

    def toggle_game(self):
        if self.active_overlay is None:
            self.start_game()
            return
        current = ida_kernwin.get_current_widget()
        previous = self.active_twidget
        self.stop_game()
        # Toggling from a DIFFERENT pseudocode tab means the user wants a
        # game there, not a remote stop of the one running off-screen.
        # stop_game() just re-activated the old tab, so restore focus to the
        # invoking tab and start there (single-overlay architecture: the old
        # game moves rather than coexists).
        if (
            current is not None
            and current != previous
            and ida_kernwin.get_widget_type(current) == ida_kernwin.BWN_PSEUDOCODE
        ):
            try:
                ida_kernwin.activate_widget(current, True)
            except Exception:
                pass
            self.start_game(current)

    def start_game(self, twidget=None):
        if twidget is None:
            twidget = ida_kernwin.get_current_widget()
        if twidget is None or ida_kernwin.get_widget_type(twidget) != ida_kernwin.BWN_PSEUDOCODE:
            ida_kernwin.warning("ida-breakout: focus a Pseudocode view first.")
            return
        vdui = ida_hexrays.get_widget_vdui(twidget)
        if vdui is None or vdui.cfunc is None:
            ida_kernwin.warning("ida-breakout: no decompiled function in this view.")
            return

        qwidget = ida_kernwin.PluginForm.TWidgetToPyQtWidget(twidget)
        if qwidget is None:
            ida_kernwin.warning("ida-breakout: could not convert pseudocode TWidget to QWidget.")
            return

        viewport, scroll_area = find_pseudocode_viewport(qwidget)
        if viewport is None:
            ida_kernwin.warning("ida-breakout: could not locate the pseudocode viewport.")
            return

        # The text caret (a thin focus-only vertical bar) gets painted into
        # the grab, but disappears from screen the moment the overlay steals
        # focus — leaving an invisible brick the ball bounces off. Drop focus
        # BEFORE grabbing so the capture is caret-free; the overlay takes
        # focus right after anyway.
        focus_w = QtWidgets.QApplication.focusWidget()
        if focus_w is not None and (focus_w is qwidget or qwidget.isAncestorOf(focus_w)):
            focus_w.clearFocus()

        grab = grab_viewport_buffer(viewport)
        if grab is None:
            ida_kernwin.warning("ida-breakout: could not capture the pseudocode viewport.")
            return

        bg_colors = sample_viewport_bg_colors(viewport, grab=grab)
        if not bg_colors:
            ida_kernwin.warning(
                "ida-breakout: could not sample viewport background colors."
            )
            return

        bricks = detect_bricks_from_pixels(viewport, bg_colors, grab=grab)
        if not bricks:
            ida_kernwin.warning(
                "ida-breakout: no ink detected in the pseudocode viewport."
            )
            return

        playfield_h = compute_playfield_height(viewport)
        # All scroll bars under the outer widget, wherever they live:
        # TEAViewer keeps them as plain children (no QAbstractScrollArea),
        # so the overlay must filter them inert or a mid-game drag scrolls
        # the text out from under the frozen brick layout.
        scroll_bars = qwidget.findChildren(QtWidgets.QScrollBar)
        overlay = BreakoutOverlay(
            viewport,
            scroll_area,
            bricks,
            bg_color=bg_colors[0],
            playfield_height=playfield_h,
            on_exit=self.stop_game,
            scroll_bars=scroll_bars,
        )
        # Register BEFORE start(): __init__ already installed event filters,
        # so if start() raises the overlay must stay reachable for
        # stop_game() to tear down — otherwise it leaks as an orphan that
        # keeps swallowing input over the pseudocode view.
        self.active_overlay = overlay
        self.active_twidget = twidget
        try:
            overlay.start()
        except Exception:
            self.stop_game()
            raise
        ida_kernwin.msg("[ida-breakout] started ({0} bricks)\n".format(len(bricks)))

    def stop_game(self):
        overlay = self.active_overlay
        twidget = self.active_twidget
        self.active_overlay = None
        self.active_twidget = None
        if overlay is None:
            return
        ida_kernwin.msg("[ida-breakout] stopped\n")
        try:
            overlay.stop()
        except Exception:
            logger.debug("ida-breakout: overlay.stop() raised during teardown", exc_info=True)
        try:
            overlay.deleteLater()
        except Exception:
            logger.debug("ida-breakout: overlay.deleteLater() raised during teardown", exc_info=True)
        # twidget is always set alongside active_overlay, so it is non-None
        # past the early return above.
        try:
            ida_kernwin.activate_widget(twidget, True)
        except Exception:
            pass
        try:
            ida_kernwin.refresh_idaview_anyway()
        except Exception:
            pass


class breakout_plugin_t(ida_idaapi.plugin_t):
    flags = ida_idaapi.PLUGIN_MULTI
    comment = "Pseudocode Breakout"
    help = "Press {0} inside a Pseudocode view to play.".format(ACTION_HOTKEY)
    wanted_name = "ida-breakout"
    wanted_hotkey = ""

    def init(self):
        return breakout_plugmod_t()
