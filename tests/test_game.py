"""Unit tests for the pure-physics game logic (no Qt required).

Run from the repo root:

    python3 -m unittest discover -s tests
"""
import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ida_breakout_lib.game import Ball, Brick, GameState, Paddle, Phase


# A token-like brick: wide and thin, the shape that breaks penetration-depth
# face resolution. Box: x in [100, 160), y in [50, 60).
BRICK = dict(x=100, y=50, w=60, h=10)


def make_game(bricks, balls):
    g = GameState(width=400, height=300, paddle=Paddle(x=160, y=280))
    g.bricks = bricks
    g.balls = balls
    g.phase = Phase.PLAYING
    return g


def step_with_far_brick(ball):
    """One step with an unreachable brick keeping the instant-WIN check out
    of the way."""
    make_game([Brick(x=350, y=10, w=20, h=8)], [ball]).step()
    return ball


class BrickFaceResolutionTest(unittest.TestCase):
    def hit_once(self, ball):
        """One step against the reference brick; the brick must die."""
        g = make_game([Brick(**BRICK)], [ball])
        speed_before = math.hypot(ball.vx, ball.vy)
        g.step()
        self.assertFalse(g.bricks[0].alive, "ball never reached the brick")
        self.assertAlmostEqual(
            math.hypot(ball.vx, ball.vy), speed_before, places=9,
            msg="bounce must preserve speed magnitude",
        )
        return ball

    def test_bottom_face_center_hit_bounces_down(self):
        b = self.hit_once(Ball(x=130.0, y=66.0, vx=-1.0, vy=-4.0, r=5.0))
        self.assertGreater(b.vy, 0)

    def test_top_face_hit_bounces_up(self):
        b = self.hit_once(Ball(x=130.0, y=44.0, vx=1.0, vy=4.0, r=5.0))
        self.assertLess(b.vy, 0)

    def test_left_face_hit_bounces_left(self):
        b = self.hit_once(Ball(x=95.0, y=55.0, vx=4.0, vy=0.5, r=5.0))
        self.assertLess(b.vx, 0)
        self.assertAlmostEqual(b.vy, 0.5)

    def test_right_face_hit_bounces_right(self):
        b = self.hit_once(Ball(x=165.0, y=55.0, vx=-4.0, vy=0.5, r=5.0))
        self.assertGreater(b.vx, 0)
        self.assertAlmostEqual(b.vy, 0.5)

    def test_edge_clip_from_below_bounces_not_passes(self):
        """Regression: a mostly-vertical ball clipping the brick's right END
        from below used to get only vx flipped (side penetration < vertical
        penetration) and sailed straight up through the line."""
        b = self.hit_once(Ball(x=164.0, y=66.0, vx=-1.0, vy=-4.0, r=5.0))
        self.assertGreater(b.vy, 0, "came from below -> must bounce back down")

    def test_corner_entry_resolves_to_later_axis_x(self):
        """From below-left, x-overlap begins later (tx=0.83 > ty=0.5): the
        ball strikes the left face, vy keeps its sign."""
        b = self.hit_once(Ball(x=90.0, y=66.0, vx=6.0, vy=-2.0, r=5.0))
        self.assertLess(b.vx, 0)
        self.assertAlmostEqual(b.vy, -2.0)

    def test_corner_entry_resolves_to_later_axis_y(self):
        """From below-left, y-overlap begins later (ty=1.0 > tx=0.375): the
        ball strikes the bottom face, vx keeps its sign."""
        b = self.hit_once(Ball(x=92.0, y=68.0, vx=8.0, vy=-3.0, r=5.0))
        self.assertGreater(b.vy, 0)
        self.assertAlmostEqual(b.vx, 8.0)

    def test_prev_overlap_falls_back_to_min_penetration(self):
        """Ball already in contact before the substep (no approach side):
        resolves along least penetration and still kills the brick."""
        b = self.hit_once(Ball(x=130.0, y=55.0, vx=1.0, vy=-1.0, r=5.0))
        self.assertLess(b.vy, 0)  # nearest face is the top -> pushed up

    def test_no_pass_through_monte_carlo(self):
        """A ball that enters a brick through its BOTTOM face — it was fully
        below the row and its x-extent already overlapped that brick before
        the substep — must bounce back down. The old penetration-depth
        resolution sent ~3% of these straight on through. (A diagonal graze
        that only reaches a brick's vertical END face may legitimately keep
        rising, so those entries are excluded.)"""
        random.seed(42)
        speed = 3.0 * math.sqrt(2.0)
        hits = 0
        for _ in range(3000):
            bricks = [
                Brick(x=x, y=50, w=w, h=10)
                for x, w in ((40, 30), (80, 55), (145, 20), (175, 60))
            ]
            ang = random.uniform(-math.pi / 3, math.pi / 3)
            ball = Ball(
                x=random.uniform(20, 250), y=random.uniform(70, 90),
                vx=speed * math.sin(ang), vy=-speed * math.cos(ang), r=5.0,
            )
            g = make_game(bricks, [ball])
            for _ in range(60):
                px, py = ball.x, ball.y  # speed_factor 1.0 -> one substep
                was_fully_below = py - ball.r >= 60
                g.step()
                if g.dead_bricks:
                    k = g.dead_bricks[0]
                    x_overlapped_before = (
                        px - ball.r < k.x + k.w and px + ball.r > k.x
                    )
                    if was_fully_below and x_overlapped_before:
                        hits += 1
                        self.assertGreater(
                            ball.vy, 0,
                            f"pass-through at pos=({ball.x:.1f},{ball.y:.1f}) "
                            f"v=({ball.vx:.2f},{ball.vy:.2f})",
                        )
                    break
        self.assertGreater(hits, 1000, "harness stopped generating hits")


class WallBounceTest(unittest.TestCase):
    def test_left_wall_bounces_right(self):
        b = step_with_far_brick(Ball(x=8.0, y=150.0, vx=-4.0, vy=1.0, r=5.0))
        self.assertGreater(b.vx, 0)
        self.assertGreaterEqual(b.x - b.r, 0)

    def test_right_wall_bounces_left(self):
        b = step_with_far_brick(Ball(x=392.0, y=150.0, vx=4.0, vy=1.0, r=5.0))
        self.assertLess(b.vx, 0)

    def test_top_wall_bounces_down(self):
        b = step_with_far_brick(Ball(x=200.0, y=8.0, vx=1.0, vy=-4.0, r=5.0))
        self.assertGreater(b.vy, 0)

    def test_wall_overlap_moving_away_is_not_reflipped(self):
        """A ball spawned overlapping the wall but already leaving it (e.g. a
        multiball split near the edge) must keep moving away; negating
        instead of sign-setting pinned it to the wall with a flip per
        substep."""
        b = step_with_far_brick(Ball(x=4.0, y=150.0, vx=0.5, vy=1.0, r=5.0))
        self.assertGreater(b.vx, 0)


class PaddleBounceTest(unittest.TestCase):
    """Paddle box (make_game): x in [160, 240), y in [280, 288), w=80 h=8."""

    def test_center_top_hit_bounces_up_preserving_speed(self):
        b = Ball(x=200.0, y=272.0, vx=1.0, vy=4.0, r=5.0)
        speed = math.hypot(b.vx, b.vy)
        step_with_far_brick(b)
        self.assertLess(b.vy, 0)
        self.assertAlmostEqual(math.hypot(b.vx, b.vy), speed, places=9)

    def test_edge_graze_saves(self):
        """Regression: the circle overlaps the paddle's left corner while the
        CENTER is outside the paddle span — must bounce, not drain (the old
        center-only x test missed exactly this)."""
        b = step_with_far_brick(Ball(x=157.0, y=272.0, vx=0.0, vy=4.0, r=5.0))
        self.assertLess(b.vy, 0)

    def test_side_hit_reflects_horizontally_without_snap(self):
        """Regression: a ball entering through the paddle's left FACE gets a
        plain horizontal reflection — the old code teleported it to the
        paddle top (the visible one-frame jump on edge hits)."""
        b = step_with_far_brick(Ball(x=152.0, y=284.0, vx=6.0, vy=1.0, r=5.0))
        self.assertLess(b.vx, 0)
        self.assertAlmostEqual(b.vy, 1.0)  # vertical motion untouched
        self.assertLess(b.x + b.r, 160.0)  # pushed out past the left face
        self.assertGreater(b.y, 280.0)     # NOT snapped to the paddle top


class GameFlowTest(unittest.TestCase):
    def test_same_frame_last_brick_and_drain_is_win(self):
        g = make_game([Brick(**BRICK, alive=False)],
                      [Ball(x=200.0, y=400.0, vx=0.0, vy=3.0, r=5.0)])
        g.dead_bricks.append(g.bricks[0])
        g.lives = 3
        g.step()
        self.assertIs(g.phase, Phase.WON)
        self.assertEqual(g.lives, 3)

    def test_drain_with_bricks_left_costs_a_life(self):
        g = make_game([Brick(**BRICK)],
                      [Ball(x=200.0, y=400.0, vx=0.0, vy=3.0, r=5.0)])
        g.lives = 3
        g.step()
        self.assertEqual(g.lives, 2)
        self.assertIs(g.phase, Phase.READY)

    def test_reset_restores_pristine_state(self):
        g = make_game([Brick(**BRICK)], [])
        g.spawn_ball_on_paddle()
        g.bricks[0].alive = False
        g.dead_bricks.append(g.bricks[0])
        g.score = 7
        g.reset()
        self.assertIs(g.phase, Phase.READY)
        self.assertTrue(g.bricks[0].alive)
        self.assertEqual(g.dead_bricks, [])
        self.assertEqual(g.score, 0)


if __name__ == "__main__":
    unittest.main()
