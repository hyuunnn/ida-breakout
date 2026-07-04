import logging

from PySide6 import QtCore, QtGui, QtWidgets

from ida_breakout_lib.game import GameState, Paddle, Phase


logger = logging.getLogger(__name__)

TICK_MS = 16
PADDLE_W = 80
PADDLE_H = 8
PADDLE_BOTTOM_GAP = 24
BALL_RADIUS = 5
END_SCREEN_HINT = "[R] restart    [Esc] exit"

# Mouse interactions must not reach the pseudocode surface (or its scroll
# bars) mid-game: a click moves the caret / changes the line highlight under
# the baked erase fills, a double-click navigates and the resulting
# re-decompile force-stops the game, and a scrollbar drag slides the text
# out from under the frozen brick layout.
_MOUSE_EVENT_TYPES = frozenset({
    QtCore.QEvent.MouseButtonPress,
    QtCore.QEvent.MouseButtonRelease,
    QtCore.QEvent.MouseButtonDblClick,
    QtCore.QEvent.MouseMove,
    QtCore.QEvent.ContextMenu,
})


class BreakoutOverlay(QtWidgets.QWidget):
    """Transparent child widget over the pseudocode viewport that hosts the game."""

    def __init__(
        self,
        viewport,
        scroll_area,
        bricks,
        bg_color,
        playfield_height,
        on_exit=None,
        scroll_bars=None,
    ):
        super().__init__(viewport)
        self.viewport_widget = viewport
        self.scroll_area = scroll_area
        self.on_exit = on_exit or (lambda: None)
        self._stopped = False

        self.setAttribute(QtCore.Qt.WA_NoSystemBackground, True)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setGeometry(0, 0, viewport.width(), viewport.height())
        self.hide()

        w, h = self.width(), self.height()
        # playfield_height comes from compute_playfield_height() and is a
        # positive int; the clamp is pure defence.
        eff_h = max(60, min(int(playfield_height), h))
        paddle = Paddle(
            x=0.0,  # centered by reset() below
            y=max(0.0, eff_h - PADDLE_BOTTOM_GAP),
            w=PADDLE_W,
            h=PADDLE_H,
        )
        self.state = GameState(
            width=w,
            height=eff_h,
            paddle=paddle,
            bricks=bricks,
            ball_radius=BALL_RADIUS,
            base_speed=3.0,
        )
        # reset() is the single definition of a pristine game (paddle
        # centering, ball spawn) — first game and [R] restart must match.
        self.state.reset()

        self._bg_color = bg_color
        self._fill_cache = {}
        # Dead-brick erase fills are baked incrementally into this pixmap so
        # paintEvent blits one layer instead of re-drawing every dead brick.
        self._erase_layer = None
        self._erased_count = 0
        # Dirty-region bookkeeping for _tick(): a full update() on a
        # translucent overlay makes Qt recomposite the whole viewport-sized
        # parent content every frame. Limiting update() to what actually
        # changed (paddle, balls, newly dead bricks, status line) keeps the
        # per-frame composite cost independent of window size.
        self._prev_dyn_region = QtGui.QRegion()
        self._dirty_dead = 0
        self._last_status = None
        logger.info(
            "ida-breakout: bg color rgb=(%d,%d,%d)",
            self._bg_color.red(), self._bg_color.green(), self._bg_color.blue(),
        )
        self._fg_paddle = QtGui.QColor("#5e81ac")
        self._fg_ball = QtGui.QColor("#bf616a")
        self._fg_ball_outline = QtGui.QColor(20, 20, 20)
        self._fg_status = QtGui.QColor("#d08770")
        self._fg_banner = QtGui.QColor("#bf616a")
        self._fg_win = QtGui.QColor("#a3be8c")

        self._ball_pen = QtGui.QPen(self._fg_ball_outline)
        self._ball_pen.setWidth(1)
        self._status_font = QtGui.QFont(self.font())
        self._status_font.setBold(True)
        self._banner_font = QtGui.QFont(self.font())
        self._banner_font.setBold(True)
        self._banner_font.setPointSize(self._banner_font.pointSize() + 18)
        # Full-width band the status text lives in (drawn at y=6, right-
        # aligned, so its left edge shifts as the text grows/shrinks).
        self._status_strip_h = 6 + QtGui.QFontMetrics(self._status_font).height() + 2

        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(TICK_MS)
        self.timer.timeout.connect(self._tick)

        viewport.installEventFilter(self)
        # Scroll bars NOT hosted by a QAbstractScrollArea (TEAViewer keeps
        # them as plain sibling children, so the policy trick below can't
        # reach them) stay live next to the overlay. Hiding them would
        # relayout the viewport and misalign the bricks detected from the
        # start-time grab — filter them inert instead: the eventFilter
        # swallows their mouse/wheel/key input.
        self._scroll_bars = list(scroll_bars or ())
        for sb in self._scroll_bars:
            sb.installEventFilter(self)
        self._saved_v_policy = None
        self._saved_h_policy = None
        if scroll_area is not None:
            scroll_area.installEventFilter(self)
            self._saved_v_policy = scroll_area.verticalScrollBarPolicy()
            self._saved_h_policy = scroll_area.horizontalScrollBarPolicy()
            scroll_area.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            scroll_area.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)

    def start(self):
        self.show()
        self.raise_()
        self.setFocus(QtCore.Qt.OtherFocusReason)
        self.timer.start()
        logger.info(
            "ida-breakout: started %dx%d, %d bricks", self.width(), self.height(), len(self.state.bricks)
        )

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        # 모든 정리는 죽은 C++ 객체(RuntimeError)를 만날 수 있어 조용히 넘긴다.
        try:
            self.timer.stop()
        except Exception:
            pass
        try:
            self.viewport_widget.removeEventFilter(self)
        except Exception:
            pass
        for sb in self._scroll_bars:
            try:
                sb.removeEventFilter(self)
            except Exception:
                pass
        if self.scroll_area is not None:
            try:
                self.scroll_area.removeEventFilter(self)
            except Exception:
                pass
            try:
                if self._saved_v_policy is not None:
                    self.scroll_area.setVerticalScrollBarPolicy(self._saved_v_policy)
                if self._saved_h_policy is not None:
                    self.scroll_area.setHorizontalScrollBarPolicy(self._saved_h_policy)
            except Exception:
                pass
        try:
            self.hide()
        except Exception:
            pass
        try:
            if self.viewport_widget is not None:
                self.viewport_widget.update()
        except Exception:
            pass

    def _tick(self):
        self.state.step()
        if self.state.phase in (Phase.WON, Phase.LOST):
            self.timer.stop()
            self.update()  # end banner covers the middle — repaint everything
            return
        self.update(self._dirty_region())

    def _dirty_region(self):
        """Region worth repainting this frame: dynamic objects at their old
        and new positions, erase rects of bricks that died since the last
        frame, and the status band when its text changed. Margins are 3px
        for anti-aliasing plus the 1px ball outline.
        """
        pd = self.state.paddle
        cur = QtGui.QRegion(
            QtCore.QRect(int(pd.x) - 3, int(pd.y) - 3, int(pd.w) + 7, int(pd.h) + 7)
        )
        for bl in self.state.balls:
            d = int(2 * bl.r) + 7
            cur |= QtCore.QRect(int(bl.x - bl.r) - 3, int(bl.y - bl.r) - 3, d, d)
        region = cur | self._prev_dyn_region
        self._prev_dyn_region = cur

        dead = self.state.dead_bricks
        if self._dirty_dead > len(dead):  # restart shrank the list
            self._dirty_dead = 0
        for brick in dead[self._dirty_dead:]:
            region |= QtCore.QRect(*brick.erase)
        self._dirty_dead = len(dead)

        status = self._status_text()
        if status != self._last_status:
            self._last_status = status
            region |= QtCore.QRect(0, 0, self.width(), self._status_strip_h)
        return region

    def _fire_exit(self):
        try:
            self.on_exit()
        except Exception:
            logger.exception("on_exit raised")

    def _restart(self):
        self.state.reset()
        self.timer.start()
        self.update()

    def _status_text(self):
        status = "score: {0}    lives: {1}".format(self.state.score, self.state.lives)
        if self.state.speed_factor > 1.05:
            status += "    speed: {0:.1f}x".format(self.state.speed_factor)
        if len(self.state.balls) > 1:
            status += "    balls: {0}".format(len(self.state.balls))
        if self.state.phase is Phase.READY:
            status += "    [SPACE to launch]"
        return status

    def _brick_fill_color(self, brick):
        bg = brick.bg
        if not bg:
            return self._bg_color
        color = self._fill_cache.get(bg)
        if color is None:
            color = QtGui.QColor(*bg)
            self._fill_cache[bg] = color
        return color

    def _sync_erase_layer(self):
        """Bake bricks that died since the last frame into the cached layer.
        The layer is rebuilt from scratch when the overlay size changes or
        dead_bricks shrank (restart)."""
        dead = self.state.dead_bricks
        dpr = self.devicePixelRatioF()
        want_w = max(1, int(round(self.width() * dpr)))
        want_h = max(1, int(round(self.height() * dpr)))
        layer = self._erase_layer
        if (
            layer is None
            or layer.width() != want_w
            or layer.height() != want_h
            or self._erased_count > len(dead)
        ):
            layer = QtGui.QPixmap(want_w, want_h)
            layer.setDevicePixelRatio(dpr)
            layer.fill(QtCore.Qt.transparent)
            self._erase_layer = layer
            self._erased_count = 0
        if self._erased_count < len(dead):
            lp = QtGui.QPainter(layer)
            # Erase with antialiasing OFF: AA blends the fill's edge pixels
            # with the text underneath, leaving a faint 1px ghost frame.
            lp.setRenderHint(QtGui.QPainter.Antialiasing, False)
            lp.setPen(QtCore.Qt.NoPen)
            for brick in dead[self._erased_count:]:
                lp.setBrush(self._brick_fill_color(brick))
                lp.drawRect(*brick.erase)
            lp.end()
            self._erased_count = len(dead)

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)

        self._sync_erase_layer()
        p.setRenderHint(QtGui.QPainter.Antialiasing, False)
        p.drawPixmap(0, 0, self._erase_layer)
        p.setPen(QtCore.Qt.NoPen)

        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        p.setBrush(self._fg_paddle)
        pd = self.state.paddle
        p.drawRoundedRect(
            QtCore.QRectF(pd.x, pd.y, pd.w, pd.h), pd.h / 2.0, pd.h / 2.0
        )

        p.setBrush(self._fg_ball)
        p.setPen(self._ball_pen)
        for bl in self.state.balls:
            p.drawEllipse(QtCore.QPointF(bl.x, bl.y), bl.r, bl.r)

        p.setFont(self._status_font)
        p.setPen(self._fg_status)
        p.drawText(
            self.rect().adjusted(8, 6, -8, 0),
            QtCore.Qt.AlignTop | QtCore.Qt.AlignRight,
            self._status_text(),
        )

        if self.state.phase in (Phase.WON, Phase.LOST):
            won = self.state.phase is Phase.WON
            text = "YOU WIN" if won else "GAME OVER"
            banner_metrics = QtGui.QFontMetrics(self._banner_font)
            hint_metrics = QtGui.QFontMetrics(self._status_font)
            banner_h = banner_metrics.height()
            hint_h = hint_metrics.height()
            gap = 8
            total_h = banner_h + gap + hint_h
            top = (self.height() - total_h) // 2

            p.setFont(self._banner_font)
            p.setPen(self._fg_win if won else self._fg_banner)
            p.drawText(
                QtCore.QRect(0, top, self.width(), banner_h),
                QtCore.Qt.AlignCenter,
                text,
            )

            p.setFont(self._status_font)
            p.setPen(self._fg_status)
            p.drawText(
                QtCore.QRect(0, top + banner_h + gap, self.width(), hint_h),
                QtCore.Qt.AlignCenter,
                END_SCREEN_HINT,
            )

        p.end()

    # Keys the game doesn't use are swallowed too (accept / return True):
    # an ignored key event propagates to the parent viewport, where PageUp/
    # Down & co. would scroll the code out from under the brick layout.
    # Action shortcuts (e.g. the toggle hotkey) are dispatched before
    # keyPressEvent delivery, so they still work. Mouse events are swallowed
    # for the same reason (see _MOUSE_EVENT_TYPES): ignored ones propagate
    # to the viewport and mutate the surface the game snapshotted.

    def keyPressEvent(self, ev):
        self._handle_key(ev, pressed=True)
        ev.accept()

    def keyReleaseEvent(self, ev):
        self._handle_key(ev, pressed=False)
        ev.accept()

    def mousePressEvent(self, ev):
        ev.accept()

    def mouseReleaseEvent(self, ev):
        ev.accept()

    def mouseMoveEvent(self, ev):
        ev.accept()

    def mouseDoubleClickEvent(self, ev):
        ev.accept()

    def wheelEvent(self, ev):
        ev.accept()

    def contextMenuEvent(self, ev):
        ev.accept()

    def eventFilter(self, obj, ev):
        et = ev.type()
        if et == QtCore.QEvent.KeyPress:
            self._handle_key(ev, pressed=True)
            return True
        elif et == QtCore.QEvent.KeyRelease:
            self._handle_key(ev, pressed=False)
            return True
        elif et == QtCore.QEvent.Wheel or et in _MOUSE_EVENT_TYPES:
            return True
        return super().eventFilter(obj, ev)

    def _handle_key(self, ev, pressed):
        # X11 auto-repeat delivers synthetic release/press pairs while a key
        # is held; acting on the fake release drops the movement flag for a
        # frame and makes the paddle stutter.
        if not pressed and ev.isAutoRepeat():
            return
        k = ev.key()
        if k in (QtCore.Qt.Key_Left, QtCore.Qt.Key_H, QtCore.Qt.Key_A):
            self.state.moving_left = pressed
        elif k in (QtCore.Qt.Key_Right, QtCore.Qt.Key_L, QtCore.Qt.Key_D):
            self.state.moving_right = pressed
        elif pressed and k == QtCore.Qt.Key_Space:
            self.state.launch_if_ready()
        elif pressed and k == QtCore.Qt.Key_R and self.state.phase in (Phase.WON, Phase.LOST):
            self._restart()
        elif pressed and k == QtCore.Qt.Key_Escape:
            self._fire_exit()
