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


class BreakoutOverlay(QtWidgets.QWidget):
    """Transparent child widget over the pseudocode viewport that hosts the game."""

    def __init__(
        self,
        viewport,
        scroll_area,
        bricks,
        bg_color,
        playfield_height=None,
        on_exit=None,
    ):
        super().__init__(viewport)
        self.viewport_widget = viewport
        self.scroll_area = scroll_area
        self.on_exit = on_exit or (lambda: None)
        self._stopped = False

        self.setAttribute(QtCore.Qt.WA_NoSystemBackground, True)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setMouseTracking(False)
        self.setGeometry(0, 0, viewport.width(), viewport.height())
        self.hide()

        w, h = self.width(), self.height()
        eff_h = int(playfield_height) if playfield_height else h
        eff_h = max(60, min(eff_h, h))
        paddle = Paddle(
            x=max(0.0, (w - PADDLE_W) / 2.0),
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
        self.state.spawn_ball_on_paddle()
        self._playfield_h = eff_h

        self._bg_color = bg_color
        self._fill_cache = {}
        # Dead-brick erase fills are baked incrementally into this pixmap so
        # paintEvent blits one layer instead of re-drawing every dead brick.
        self._erase_layer = None
        self._erased_count = 0
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

        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(TICK_MS)
        self.timer.timeout.connect(self._tick)

        viewport.installEventFilter(self)
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
        self.update()

    def _fire_exit(self):
        try:
            self.on_exit()
        except Exception:
            logger.exception("on_exit raised")

    def _restart(self):
        self.state.reset()
        if not self.timer.isActive():
            self.timer.start()
        self.update()

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
        p.setPen(QtCore.Qt.NoPen)

        p.setFont(self._status_font)
        p.setPen(self._fg_status)
        status = "score: {0}    lives: {1}".format(self.state.score, self.state.lives)
        if self.state.speed_factor > 1.05:
            status += "    speed: {0:.1f}x".format(self.state.speed_factor)
        if len(self.state.balls) > 1:
            status += "    balls: {0}".format(len(self.state.balls))
        if self.state.phase is Phase.READY:
            status += "    [SPACE to launch]"
        p.drawText(
            self.rect().adjusted(8, 6, -8, 0),
            QtCore.Qt.AlignTop | QtCore.Qt.AlignRight,
            status,
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

    def keyPressEvent(self, ev):
        if not self._handle_key(ev, pressed=True):
            super().keyPressEvent(ev)

    def keyReleaseEvent(self, ev):
        if not self._handle_key(ev, pressed=False):
            super().keyReleaseEvent(ev)

    def eventFilter(self, obj, ev):
        et = ev.type()
        if et == QtCore.QEvent.KeyPress:
            if self._handle_key(ev, pressed=True):
                return True
        elif et == QtCore.QEvent.KeyRelease:
            if self._handle_key(ev, pressed=False):
                return True
        elif et == QtCore.QEvent.Wheel:
            return True
        return super().eventFilter(obj, ev)

    def _handle_key(self, ev, pressed):
        k = ev.key()
        if k in (QtCore.Qt.Key_Left, QtCore.Qt.Key_H, QtCore.Qt.Key_A):
            self.state.moving_left = pressed
            return True
        if k in (QtCore.Qt.Key_Right, QtCore.Qt.Key_L, QtCore.Qt.Key_D):
            self.state.moving_right = pressed
            return True
        if pressed and k == QtCore.Qt.Key_Space:
            self.state.launch_if_ready()
            return True
        if pressed and k == QtCore.Qt.Key_R and self.state.phase in (Phase.WON, Phase.LOST):
            self._restart()
            return True
        if pressed and k == QtCore.Qt.Key_Escape:
            self._fire_exit()
            return True
        return False

    def resizeEvent(self, ev):
        self.state.width = max(1, self.width())
        new_h = min(self._playfield_h, max(1, self.height()))
        self.state.height = new_h
        self.state.paddle.y = max(0.0, new_h - PADDLE_BOTTOM_GAP)
        super().resizeEvent(ev)
