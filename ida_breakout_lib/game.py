import bisect
import math
import random
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


class Phase(Enum):
    READY = auto()
    PLAYING = auto()
    LOST = auto()
    WON = auto()


@dataclass
class Brick:
    x: int
    y: int
    w: int
    h: int
    alive: bool = True
    # Local background (r, g, b) sampled around the brick at detection time —
    # the line/token highlight it sits on. None → erase with the global bg.
    bg: Optional[tuple] = None
    # (x, y, w, h) to paint over when the brick dies: the ink rect grown to
    # cover the anti-aliasing halo, but clamped at the midpoint of the gap to
    # neighbouring lines/tokens so their glyphs never get shaved. The pixel
    # detector always sets this; the overlay assumes it is present (None only
    # occurs in headless game-logic tests that never paint).
    erase: Optional[tuple] = None


@dataclass
class Paddle:
    x: float
    y: float
    w: float = 80.0
    h: float = 8.0
    speed: float = 6.0


@dataclass
class Ball:
    x: float
    y: float
    vx: float
    vy: float
    # No default: every spawn path must take the radius from
    # GameState.ball_radius, so there is a single source of truth.
    r: float


SPEED_PER_BRICK = 0.01
SPEED_CAP = 2.0
MULTIBALL_INTERVAL = 15
MAX_BALLS = 5
MAX_PADDLE_ANGLE = math.pi / 3  # 60° — matches launch angle range
MULTIBALL_ANGLE_NOISE = 0.25    # ≈ ±14° angular jitter on split


def _velocity_from_polar(speed, angle):
    """(vx, vy) with |v| == speed at math-convention `angle` (0 = +x, CCW
    toward +y = screen-down). The one place a velocity vector is built from
    an angle: every launch/split/bounce must preserve magnitude — an additive
    formulation accumulates speed on edge hits (up to +73%), making straight
    balls slow and diagonal balls fast.
    """
    return speed * math.cos(angle), speed * math.sin(angle)


def _velocity_from_angle(speed, angle):
    """(vx, vy) for an upward launch at `angle` (0 = straight up), with
    |v| == speed."""
    return _velocity_from_polar(speed, angle - math.pi / 2)


_BOUNCE_EPS = 0.1  # push-out past the struck face so the next substep starts outside


def _aabb_entry_faces(ball, prev_x, prev_y, bx0, by0, bx1, by1):
    """Which face(s) of the box [bx0, bx1) x [by0, by1) the ball entered
    through this substep, using where its box was BEFORE the move
    (prev_x/prev_y). Returns (bounce_x, bounce_y, from_left, from_top):
    when bounce_x, the struck vertical face is left iff from_left; likewise
    bounce_y with from_top. Shared by brick AND paddle collisions so both
    obey the same physics.

    Minimum penetration depth is the wrong signal here: bricks are text
    tokens — wide and thin — so a ball clipping a brick's END from below
    penetrates less horizontally than vertically, and a depth heuristic
    flips vx instead of vy, letting the ball sail straight on through the
    line (~3% of bottom-approach hits, measured). The previous position
    knows which side the ball came from.

    Corner entries (outside on both axes) resolve to the axis whose overlap
    began LATER along the actual displacement — the swept-AABB entry-time
    rule; an exact tie flags both. If the previous box already overlapped
    on both axes (spawned in contact, a neighbouring brick died between
    substeps, or the paddle slid into the ball) there is no approach side
    to recover, so fall back to minimum penetration.
    """
    r = ball.r
    from_left = prev_x + r <= bx0
    from_right = prev_x - r >= bx1
    from_top = prev_y + r <= by0
    from_bottom = prev_y - r >= by1

    bounce_x = from_left or from_right
    bounce_y = from_top or from_bottom
    if bounce_x and bounce_y:
        # Entry time along each axis in units of this substep's displacement;
        # a zero displacement component means that axis was touching from the
        # start (entry time -inf), so the other axis wins.
        dx = ball.x - prev_x
        dy = ball.y - prev_y
        if from_left:
            tx = (bx0 - (prev_x + r)) / dx if dx else float("-inf")
        else:
            tx = (bx1 - (prev_x - r)) / dx if dx else float("-inf")
        if from_top:
            ty = (by0 - (prev_y + r)) / dy if dy else float("-inf")
        else:
            ty = (by1 - (prev_y - r)) / dy if dy else float("-inf")
        bounce_x = tx >= ty
        bounce_y = ty >= tx
    elif not (bounce_x or bounce_y):
        pen_left = (ball.x + r) - bx0
        pen_right = bx1 - (ball.x - r)
        pen_top = (ball.y + r) - by0
        pen_bottom = by1 - (ball.y - r)
        min_pen = min(pen_left, pen_right, pen_top, pen_bottom)
        from_left = min_pen == pen_left
        from_right = not from_left and min_pen == pen_right
        from_top = not (from_left or from_right) and min_pen == pen_top
        from_bottom = not (from_left or from_right or from_top)
        bounce_x = from_left or from_right
        bounce_y = from_top or from_bottom
    return bounce_x, bounce_y, from_left, from_top


def _resolve_brick_bounce(ball, prev_x, prev_y, brick):
    """Reflect `ball` off the brick face it actually entered through this
    substep (face selection: _aabb_entry_faces). Velocity signs are SET
    (abs), not negated: the ball must leave through the face it came from
    even if a same-substep wall/paddle bounce already touched that component.
    """
    r = ball.r
    bx0, by0 = brick.x, brick.y
    bx1, by1 = brick.x + brick.w, brick.y + brick.h
    bounce_x, bounce_y, from_left, from_top = _aabb_entry_faces(
        ball, prev_x, prev_y, bx0, by0, bx1, by1
    )

    if bounce_x:
        if from_left:
            ball.vx = -abs(ball.vx)
            ball.x = bx0 - r - _BOUNCE_EPS
        else:
            ball.vx = abs(ball.vx)
            ball.x = bx1 + r + _BOUNCE_EPS
    if bounce_y:
        if from_top:
            ball.vy = -abs(ball.vy)
            ball.y = by0 - r - _BOUNCE_EPS
        else:
            ball.vy = abs(ball.vy)
            ball.y = by1 + r + _BOUNCE_EPS


@dataclass
class GameState:
    width: int
    height: int
    paddle: Paddle
    balls: list = field(default_factory=list)
    bricks: list = field(default_factory=list)
    score: int = 0
    lives: int = 3
    phase: Phase = Phase.READY
    moving_left: bool = False
    moving_right: bool = False
    speed_factor: float = 1.0
    speed_bricks: int = 0
    next_multiball_score: int = MULTIBALL_INTERVAL
    ball_radius: float = 5.0
    base_speed: float = 3.0
    dead_bricks: list = field(default_factory=list)
    # Lazily built y-sorted view over `bricks` so collision checks only scan
    # the line bands the ball overlaps, instead of every brick ever detected
    # (dead ones included). Rebuilt when the list object or its length
    # changes; brick rects never move after detection, so coordinate
    # mutation is not a supported invalidation trigger.
    _index_src: Optional[list] = field(default=None, init=False, repr=False)
    _bricks_by_y: list = field(default_factory=list, init=False, repr=False)
    _brick_y0s: list = field(default_factory=list, init=False, repr=False)
    _max_brick_h: float = field(default=0.0, init=False, repr=False)

    def reset(self):
        for b in self.bricks:
            b.alive = True
        self.dead_bricks.clear()
        self.balls.clear()
        self.score = 0
        self.lives = 3
        self.phase = Phase.READY
        self.speed_factor = 1.0
        self.speed_bricks = 0
        self.next_multiball_score = MULTIBALL_INTERVAL
        self.moving_left = False
        self.moving_right = False
        self.paddle.x = max(0.0, (float(self.width) - self.paddle.w) / 2.0)
        self.spawn_ball_on_paddle()

    def spawn_ball_on_paddle(self):
        angle = random.uniform(-MAX_PADDLE_ANGLE, MAX_PADDLE_ANGLE)
        vx, vy = _velocity_from_angle(self.base_speed * math.sqrt(2.0), angle)
        self.balls.append(
            Ball(
                x=self.paddle.x + self.paddle.w / 2.0,
                y=self.paddle.y - self.ball_radius - 1.0,
                r=self.ball_radius,
                vx=vx,
                vy=vy,
            )
        )

    def launch_if_ready(self):
        if self.phase is Phase.READY:
            self.phase = Phase.PLAYING

    def step(self):
        if self.moving_left:
            self.paddle.x = max(0.0, self.paddle.x - self.paddle.speed)
        if self.moving_right:
            self.paddle.x = min(
                float(self.width) - self.paddle.w,
                self.paddle.x + self.paddle.speed,
            )

        if self.phase is Phase.READY:
            for b in self.balls:
                b.x = self.paddle.x + self.paddle.w / 2.0
                b.y = self.paddle.y - b.r - 1.0
            return

        if self.phase is not Phase.PLAYING:
            return

        n_sub = max(1, int(self.speed_factor + 0.5))
        sub_dt = self.speed_factor / n_sub
        for _ in range(n_sub):
            self._step_balls(sub_dt)

        self.balls = [b for b in self.balls if b.y - b.r <= self.height]

        # WIN must be checked before the no-balls branch: a side hit on the
        # last brick keeps vy downward, so the ball can drain in the same
        # frame — that's a win, not a lost life.
        if len(self.dead_bricks) == len(self.bricks):
            self.phase = Phase.WON
            return

        if not self.balls:
            self.lives -= 1
            if self.lives <= 0:
                self.phase = Phase.LOST
            else:
                self.phase = Phase.READY
                self.speed_factor = 1.0
                self.speed_bricks = 0
                self.spawn_ball_on_paddle()

    def _ensure_brick_index(self):
        bricks = self.bricks
        if self._index_src is bricks and len(self._brick_y0s) == len(bricks):
            return
        # Stable sort: bricks sharing a y keep detection order (left to right),
        # so multi-overlap resolution matches the old full-list scan.
        self._bricks_by_y = sorted(bricks, key=lambda b: b.y)
        self._brick_y0s = [b.y for b in self._bricks_by_y]
        self._max_brick_h = max((b.h for b in bricks), default=0)
        self._index_src = bricks

    def _step_balls(self, dt):
        self._ensure_brick_index()
        new_balls = []
        pd = self.paddle
        for ball in self.balls:
            if ball.y - ball.r > self.height:
                continue

            prev_x, prev_y = ball.x, ball.y
            ball.x += ball.vx * dt
            ball.y += ball.vy * dt

            # Wall bounces SET the sign instead of negating: a ball already
            # moving away (a multiball split spawned overlapping the wall)
            # must not be re-flipped back into it.
            if ball.x - ball.r <= 0:
                ball.x = ball.r
                ball.vx = abs(ball.vx)
            elif ball.x + ball.r >= self.width:
                ball.x = self.width - ball.r
                ball.vx = -abs(ball.vx)
            if ball.y - ball.r <= 0:
                ball.y = ball.r
                ball.vy = abs(ball.vy)

            # Radius-inclusive AABB + the same entry-face rules as bricks: a
            # center-only x test let edge grazes drain (the circle visibly
            # overlapped the paddle corner but the center was outside), and
            # unconditionally snapping to the paddle top teleported side hits
            # by up to paddle-height + ball-diameter in one frame.
            if (
                ball.vy > 0
                and ball.y + ball.r >= pd.y
                and ball.y - ball.r <= pd.y + pd.h
                and ball.x + ball.r >= pd.x
                and ball.x - ball.r <= pd.x + pd.w
            ):
                bounce_x, bounce_y, from_left, from_top = _aabb_entry_faces(
                    ball, prev_x, prev_y, pd.x, pd.y, pd.x + pd.w, pd.y + pd.h
                )
                if bounce_y and from_top:
                    # Top hit — classic Breakout: the offset from the paddle
                    # center picks the angle, magnitude is preserved.
                    speed = math.hypot(ball.vx, ball.vy)
                    offset = (ball.x - (pd.x + pd.w / 2.0)) / (pd.w / 2.0)
                    offset = max(-1.0, min(1.0, offset))
                    angle = offset * MAX_PADDLE_ANGLE
                    ball.vx, ball.vy = _velocity_from_angle(speed, angle)
                    ball.y = pd.y - ball.r - _BOUNCE_EPS
                elif bounce_x:
                    # Side hit: horizontal reflection only.
                    if from_left:
                        ball.vx = -abs(ball.vx)
                        ball.x = pd.x - ball.r - _BOUNCE_EPS
                    else:
                        ball.vx = abs(ball.vx)
                        ball.x = pd.x + pd.w + ball.r + _BOUNCE_EPS
                # Remaining case: bounce_y via the min-penetration fallback
                # picked the BOTTOM face (the paddle slid over a ball already
                # sunk below its top). A descending ball can't be saved from
                # there — let it keep falling and drain.

            ball_left = ball.x - ball.r
            ball_right = ball.x + ball.r
            ball_top = ball.y - ball.r
            ball_bottom = ball.y + ball.r

            # Candidate bricks by y band: y0 <= ball_bottom and
            # y0 >= ball_top - max_h is a superset of the exact AABB test
            # below, so only the 1-2 lines under the ball get scanned.
            lo = bisect.bisect_left(
                self._brick_y0s, ball_top - self._max_brick_h
            )
            hi = bisect.bisect_right(self._brick_y0s, ball_bottom)
            for brick in self._bricks_by_y[lo:hi]:
                if not brick.alive:
                    continue
                bx0 = brick.x
                by0 = brick.y
                bx1 = brick.x + brick.w
                by1 = brick.y + brick.h
                if (
                    ball_right < bx0
                    or ball_left > bx1
                    or ball_bottom < by0
                    or ball_top > by1
                ):
                    continue

                _resolve_brick_bounce(ball, prev_x, prev_y, brick)

                brick.alive = False
                self.dead_bricks.append(brick)
                self.score += 1
                self.speed_bricks += 1
                self.speed_factor = min(
                    SPEED_CAP, 1.0 + self.speed_bricks * SPEED_PER_BRICK
                )
                if self.score >= self.next_multiball_score:
                    # Count only live balls against the cap: a ball that
                    # drained this frame stays in self.balls until the
                    # post-substep filter and must not hold a slot.
                    n_alive = sum(
                        1 for b in self.balls if b.y - b.r <= self.height
                    )
                    if n_alive + len(new_balls) < MAX_BALLS:
                        speed = math.hypot(ball.vx, ball.vy)
                        flip_angle = math.atan2(-ball.vy, -ball.vx) + random.uniform(
                            -MULTIBALL_ANGLE_NOISE, MULTIBALL_ANGLE_NOISE
                        )
                        vx, vy = _velocity_from_polar(speed, flip_angle)
                        new_balls.append(
                            Ball(x=ball.x, y=ball.y, r=ball.r, vx=vx, vy=vy)
                        )
                        self.next_multiball_score += MULTIBALL_INTERVAL
                break
        if new_balls:
            self.balls.extend(new_balls)
