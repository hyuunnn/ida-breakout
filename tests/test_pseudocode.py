"""Headless tests for the pixel brick detector (no IDA required).

Follows the approach documented in CLAUDE.md: run Qt offscreen and inject a
synthetic pixel buffer via the `grab=(bytes, w, h, dpr)` parameter, bypassing
the real viewport grab. numpy is a hard requirement of the detector (the
entry shim's should_load() refuses to load the plugin without it), so the
tests skip when it is missing.

Run from the repo root:

    python3 -m unittest discover -s tests
"""
import ast
import os
import subprocess
import sys
import unittest

# Must be set before any QApplication is created (the child-masking test
# instantiates one); harmless when tests run on a machine with a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

try:
    from PySide6 import QtGui, QtWidgets
except ImportError as exc:  # pragma: no cover
    raise unittest.SkipTest("PySide6 not available: {0}".format(exc))

try:
    import numpy  # noqa: F401 - hard dependency of the detector module
except ImportError as exc:  # pragma: no cover
    raise unittest.SkipTest("numpy not available: {0}".format(exc))

from ida_breakout_lib import pseudocode


BG = (40, 40, 40)          # primary background
INK = (230, 230, 230)      # plain glyph color (Manhattan dist from BG >> 40)
HILITE = (70, 70, 110)     # line-highlight-like secondary background

_APP = None


def _ensure_app():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _qcolor(rgb):
    return QtGui.QColor(*rgb)


def _rgb(qc):
    return (qc.red(), qc.green(), qc.blue())


class Canvas:
    """Synthetic Format_RGB32 buffer (B, G, R, X byte order) in device px."""

    def __init__(self, w, h, bg=BG):
        self.w, self.h = w, h
        self.buf = bytearray(w * h * 4)
        self.fill_rect(0, 0, w, h, bg)

    def fill_rect(self, x, y, rw, rh, rgb):
        r, g, b = rgb
        for yy in range(y, y + rh):
            base = yy * self.w * 4
            for xx in range(x, x + rw):
                off = base + xx * 4
                self.buf[off] = b
                self.buf[off + 1] = g
                self.buf[off + 2] = r
                self.buf[off + 3] = 255

    def grab(self, dpr=1.0):
        return (bytes(self.buf), self.w, self.h, dpr)


def _two_line_canvas(scale=1):
    """Two lines x two tokens; all geometry in logical units scaled by
    `scale` so the same layout can be rendered for different dpr values.
    Logical layout: tokens 20x8 at x in {10, 34}, y in {10, 22}."""
    s = scale
    c = Canvas(int(240 * s), int(60 * s))
    for y0 in (10, 22):
        for x0 in (10, 34):
            c.fill_rect(int(x0 * s), int(y0 * s), int(20 * s), int(8 * s), INK)
    return c


def _brick_key(b):
    return (b.x, b.y, b.w, b.h, b.erase, b.bg)


class SamplerTestsMixin:
    def test_none_viewport_without_grab_returns_empty(self):
        self.assertEqual(pseudocode.sample_viewport_bg_colors(None), [])

    def test_primary_and_highlight_sampled_exactly(self):
        c = Canvas(240, 100)
        c.fill_rect(0, 40, 240, 16, (90, 90, 140))  # 16% of rows: above 2% cut
        colors = pseudocode.sample_viewport_bg_colors(None, grab=c.grab())
        # Exact pixel values, most frequent first — no quantization drift.
        self.assertEqual([_rgb(qc) for qc in colors], [BG, (90, 90, 140)])

    def test_near_identical_color_deduped(self):
        c = Canvas(240, 100)
        c.fill_rect(0, 40, 240, 16, (60, 60, 60))  # Manhattan 60 from BG
        colors = pseudocode.sample_viewport_bg_colors(None, grab=c.grab())
        self.assertEqual([_rgb(qc) for qc in colors], [BG])

    def test_rare_color_below_min_count_pct_excluded(self):
        c = Canvas(240, 100)
        c.fill_rect(20, 40, 20, 8, (200, 0, 0))  # ~0.7% of grid samples
        colors = pseudocode.sample_viewport_bg_colors(None, grab=c.grab())
        self.assertEqual([_rgb(qc) for qc in colors], [BG])


class DetectTestsMixin:
    def detect(self, canvas, bg_rgbs=(BG,), dpr=1.0, viewport=None, **kw):
        return pseudocode.detect_bricks_from_pixels(
            viewport, [_qcolor(c) for c in bg_rgbs], grab=canvas.grab(dpr), **kw
        )

    def test_brick_and_erase_geometry(self):
        """Padding growth, anti-aliasing margin, and the midpoint clamp
        between neighbouring lines/tokens, asserted exactly at dpr=1."""
        bricks = self.detect(_two_line_canvas())
        self.assertEqual(
            [(b.x, b.y, b.w, b.h) for b in bricks],
            # ink rect grown by padding=0.5 (→ 1 device px at dpr=1)
            [(9, 9, 22, 10), (33, 9, 22, 10), (9, 21, 22, 10), (33, 21, 22, 10)],
        )
        self.assertEqual(
            [b.erase for b in bricks],
            # margin 2px, clamped at the midpoints x=32 (token gap 30..34)
            # and y=20 (line gap 18..22) — neighbours never overlap.
            [(8, 8, 24, 12), (32, 8, 24, 12), (8, 20, 24, 12), (32, 20, 24, 12)],
        )
        self.assertEqual([b.bg for b in bricks], [BG] * 4)

    def test_brick_bg_is_local_highlight_color(self):
        """A token sitting on a line highlight erases with the highlight
        color, not the global background."""
        c = Canvas(240, 60)
        c.fill_rect(0, 20, 240, 20, HILITE)
        c.fill_rect(10, 26, 20, 8, INK)
        bricks = self.detect(c, bg_rgbs=(BG, HILITE))
        self.assertEqual([(b.x, b.y, b.w, b.h) for b in bricks], [(9, 25, 22, 10)])
        self.assertEqual(bricks[0].bg, HILITE)

    def test_full_line_highlight_dropped_by_width_ratio(self):
        """If the highlight color is NOT in bg_colors, the full-width band it
        forms must be rejected as too wide to be a token."""
        c = Canvas(240, 60)
        c.fill_rect(0, 20, 240, 20, HILITE)
        self.assertEqual(self.detect(c, bg_rgbs=(BG,)), [])

    def test_caret_bar_dropped(self):
        """A 2px-wide solid bar spanning its whole line band is caret/indent-
        guide shaped and must not become an (invisible) brick."""
        c = Canvas(240, 60)
        c.fill_rect(100, 10, 2, 12, INK)   # caret
        c.fill_rect(10, 40, 40, 8, INK)    # real token elsewhere
        bricks = self.detect(c)
        self.assertEqual(len(bricks), 1)
        self.assertEqual(bricks[0].y, 39)  # only the token survived

    def test_narrow_monochrome_stem_kept_by_row_coverage(self):
        """An AA-less glyph stem ('l', '|') is monochrome and thin like a
        caret, but does not cover ~all rows of its line band — kept."""
        c = Canvas(240, 60)
        c.fill_rect(10, 30, 30, 12, INK)   # token defining a 12-row band
        c.fill_rect(50, 33, 2, 6, INK)     # stem covering 50% of the band
        self.assertEqual(len(self.detect(c)), 2)

    def test_narrow_multicolor_bar_kept_by_ink_color_count(self):
        """Full-band-height but anti-aliased (3+ ink shades) → real glyph."""
        c = Canvas(240, 60)
        c.fill_rect(100, 10, 2, 4, (230, 230, 230))
        c.fill_rect(100, 14, 2, 4, (200, 60, 60))
        c.fill_rect(100, 18, 2, 4, (60, 200, 60))
        bricks = self.detect(c)
        self.assertEqual(len(bricks), 1)
        self.assertLessEqual(bricks[0].w, 5)

    def test_same_logical_layout_across_dpr_1_and_2(self):
        """The same font layout must produce identical logical bricks on 1x
        and Retina displays (the point of the _dp/_dp_min conversion)."""
        b1 = self.detect(_two_line_canvas(1), dpr=1.0)
        b2 = self.detect(_two_line_canvas(2), dpr=2.0)
        self.assertEqual([_brick_key(b) for b in b1], [_brick_key(b) for b in b2])

    def test_fractional_dpr_stays_within_quantization(self):
        """At dpr=1.5 results may differ by device-px quantization but must
        stay within 1 logical px of the dpr=1 layout."""
        b1 = self.detect(_two_line_canvas(1), dpr=1.0)
        b15 = self.detect(_two_line_canvas(1.5), dpr=1.5)
        self.assertEqual(len(b15), len(b1))
        for a, b in zip(b1, b15):
            for va, vb in zip((a.x, a.y, a.w, a.h), (b.x, b.y, b.w, b.h)):
                self.assertLessEqual(abs(va - vb), 1)

    def test_child_widget_rect_masked_before_scan(self):
        """A scrollbar painted into the grab is a full-height ink column that
        would merge every line band; masking must neutralize it BEFORE the
        scan so the two lines stay separate bricks."""
        _ensure_app()
        parent = QtWidgets.QWidget()
        parent.resize(200, 100)
        child = QtWidgets.QWidget(parent)          # fake scrollbar
        child.setGeometry(180, 0, 20, 100)
        parent.show()
        self.addCleanup(parent.close)

        c = Canvas(200, 100)
        c.fill_rect(180, 0, 20, 100, (120, 120, 120))  # scrollbar pixels
        c.fill_rect(10, 10, 20, 8, INK)
        c.fill_rect(10, 40, 20, 8, INK)

        unmasked = self.detect(c, viewport=None)
        self.assertTrue(
            any(b.h > 50 for b in unmasked),
            "harness self-check: without masking the bands must merge",
        )
        masked = self.detect(c, viewport=parent)
        self.assertEqual(
            [(b.x, b.y, b.w, b.h) for b in masked],
            [(9, 9, 22, 10), (9, 39, 22, 10)],
        )


# Runs in a subprocess because QT_SCALE_FACTOR must be set before the (per-
# process, already created here) QApplication exists.
_GRAB_HIDPI_SCRIPT = """\
import sys
sys.path.insert(0, {root!r})
from PySide6 import QtWidgets
app = QtWidgets.QApplication([])
from ida_breakout_lib.pseudocode import grab_viewport_buffer
w = QtWidgets.QWidget()
w.resize(1001, 51)
grab = grab_viewport_buffer(w)
buf, gw, gh, dpr = grab
print((gw, gh, dpr, len(buf)))
"""


class TestGrabViewportBuffer(unittest.TestCase):
    def test_none_viewport_returns_none(self):
        self.assertIsNone(pseudocode.grab_viewport_buffer(None))

    def test_buffer_layout_and_size(self):
        _ensure_app()
        w = QtWidgets.QWidget()
        w.resize(120, 40)
        w.setAutoFillBackground(True)
        pal = w.palette()
        pal.setColor(QtGui.QPalette.Window, QtGui.QColor(10, 20, 30))
        w.setPalette(pal)
        grab = pseudocode.grab_viewport_buffer(w)
        self.assertIsNotNone(grab)
        buf, gw, gh, dpr = grab
        self.assertEqual(gw, int(round(120 * dpr)))
        self.assertEqual(gh, int(round(40 * dpr)))
        self.assertEqual(len(buf), gw * gh * 4)
        # B,G,R,x byte order — everything downstream depends on this.
        self.assertEqual((buf[2], buf[1], buf[0]), (10, 20, 30))

    def test_dpr_read_from_pixmap_not_from_size_ratio(self):
        """At QT_SCALE_FACTOR=1.5 a 1001px-wide widget grabs to a pixmap
        whose integer width makes width/1001 = 1.50050/1.49950 — deriving
        dpr from that ratio (the old code) is what made brick segmentation
        flip on 1-px window resizes. The exact 1.5 must come back."""
        env = dict(
            os.environ, QT_QPA_PLATFORM="offscreen", QT_SCALE_FACTOR="1.5"
        )
        proc = subprocess.run(
            [sys.executable, "-c", _GRAB_HIDPI_SCRIPT.format(root=_ROOT)],
            capture_output=True, text=True, env=env, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        gw, gh, dpr, nbytes = ast.literal_eval(proc.stdout.strip().splitlines()[-1])
        if dpr == 1.0 and gw == 1001:
            self.skipTest("QT_SCALE_FACTOR not honored by this Qt build")
        self.assertEqual(dpr, 1.5)
        # Sanity: the ratio really is noisy here, so the assert above could
        # not have passed via ratio-derived dpr.
        self.assertNotEqual(gw / 1001.0, dpr)
        self.assertEqual(nbytes, gw * gh * 4)


class TestSampler(SamplerTestsMixin, unittest.TestCase):
    pass


class TestDetect(DetectTestsMixin, unittest.TestCase):
    pass


if __name__ == "__main__":
    unittest.main()
