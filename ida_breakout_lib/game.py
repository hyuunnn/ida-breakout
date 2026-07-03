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
    r: float = 4.0


SPEED_PER_BRICK = 0.01
SPEED_CAP = 2.0
MULTIBALL_INTERVAL = 15
MAX_BALLS = 5
MAX_PADDLE_ANGLE = math.pi / 3  # 60° — matches launch angle range
MULTIBALL_ANGLE_NOISE = 0.25    # ≈ ±14° angular jitter on split


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
        speed_mag = math.hypot(self.base_speed, self.base_speed)
        self.balls.append(
            Ball(
                x=self.paddle.x + self.paddle.w / 2.0,
                y=self.paddle.y - self.ball_radius - 1.0,
                r=self.ball_radius,
                vx=speed_mag * math.sin(angle),
                vy=-speed_mag * math.cos(angle),
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

    def _step_balls(self, dt):
        new_balls = []
        pd = self.paddle
        for ball in self.balls:
            if ball.y - ball.r > self.height:
                continue

            ball.x += ball.vx * dt
            ball.y += ball.vy * dt

            if ball.x - ball.r <= 0:
                ball.x = ball.r
                ball.vx = -ball.vx
            elif ball.x + ball.r >= self.width:
                ball.x = self.width - ball.r
                ball.vx = -ball.vx
            if ball.y - ball.r <= 0:
                ball.y = ball.r
                ball.vy = -ball.vy

            if (
                ball.vy > 0
                and ball.y + ball.r >= pd.y
                and ball.y - ball.r <= pd.y + pd.h
                and pd.x <= ball.x <= pd.x + pd.w
            ):
                speed = math.hypot(ball.vx, ball.vy)
                offset = (ball.x - (pd.x + pd.w / 2.0)) / (pd.w / 2.0)
                offset = max(-1.0, min(1.0, offset))
                angle = offset * MAX_PADDLE_ANGLE
                ball.vx = speed * math.sin(angle)
                ball.vy = -speed * math.cos(angle)
                ball.y = pd.y - ball.r - 0.5

            ball_left = ball.x - ball.r
            ball_right = ball.x + ball.r
            ball_top = ball.y - ball.r
            ball_bottom = ball.y + ball.r

            for brick in self.bricks:
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

                pen_left = ball_right - bx0
                pen_right = bx1 - ball_left
                pen_top = ball_bottom - by0
                pen_bottom = by1 - ball_top
                min_pen = min(pen_left, pen_right, pen_top, pen_bottom)

                if min_pen == pen_top and ball.vy > 0:
                    ball.y = by0 - ball.r - 0.1
                    ball.vy = -ball.vy
                elif min_pen == pen_bottom and ball.vy < 0:
                    ball.y = by1 + ball.r + 0.1
                    ball.vy = -ball.vy
                elif min_pen == pen_left and ball.vx > 0:
                    ball.x = bx0 - ball.r - 0.1
                    ball.vx = -ball.vx
                elif min_pen == pen_right and ball.vx < 0:
                    ball.x = bx1 + ball.r + 0.1
                    ball.vx = -ball.vx
                else:
                    ball.vy = -ball.vy

                brick.alive = False
                self.dead_bricks.append(brick)
                self.score += 1
                self.speed_bricks += 1
                self.speed_factor = min(
                    SPEED_CAP, 1.0 + self.speed_bricks * SPEED_PER_BRICK
                )
                if (
                    self.score >= self.next_multiball_score
                    and len(self.balls) + len(new_balls) < MAX_BALLS
                ):
                    speed = math.hypot(ball.vx, ball.vy)
                    flip_angle = math.atan2(-ball.vy, -ball.vx) + random.uniform(
                        -MULTIBALL_ANGLE_NOISE, MULTIBALL_ANGLE_NOISE
                    )
                    new_balls.append(
                        Ball(
                            x=ball.x,
                            y=ball.y,
                            r=ball.r,
                            vx=speed * math.cos(flip_angle),
                            vy=speed * math.sin(flip_angle),
                        )
                    )
                    self.next_multiball_score += MULTIBALL_INTERVAL
                break
        if new_balls:
            self.balls.extend(new_balls)
