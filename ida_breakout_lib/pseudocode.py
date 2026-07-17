import logging
import math
from collections import Counter

from PySide6 import QtCore, QtGui, QtWidgets
# Hard requirement: the pixel scans are numpy-vectorized (~65ms vs several
# hundred ms pure-python at Retina fullscreen), and a pure-python fallback
# meant maintaining two bit-identical implementations of every scan. The
# entry shim's should_load() gates on numpy, so a missing install disables
# the plugin up front with a log hint instead of failing here.
import numpy as np

from ida_breakout_lib.game import Brick


logger = logging.getLogger(__name__)

# Manhattan RGB distance under which two colors count as "the same". Shared
# default of the bg sampler's `dedupe_dist` AND the brick detector's
# `color_threshold` so the invariant dedupe_dist <= color_threshold holds by
# construction: a color deduped out of the bg list is judged against the KEPT
# colors downstream, and any gap between the two thresholds is a blind spot
# where a pure background band lands in neither class and turns into phantom
# bricks (reproduced with a synthetic buffer back when dedupe was 60;
# test_sampled_bg_never_yields_background_bricks guards the regression).
# Tune THIS constant, not the two call-site defaults separately.
COLOR_THRESHOLD = 40


def _np_pack_colors(bgr):
    """Pack a (..., 3) B,G,R uint8 array into 0xRRGGBB uint32s — one per
    pixel, channel axis dropped, remaining shape preserved — so exact colors
    compare and count as scalars."""
    c = bgr.astype(np.uint32)
    return (c[..., 2] << 16) | (c[..., 1] << 8) | c[..., 0]


def _unpack_color(p):
    """Packed 0xRRGGBB int → (r, g, b)."""
    return (p >> 16) & 0xFF, (p >> 8) & 0xFF, p & 0xFF


def _np_count_colors(packed):
    """Counter of exact (r, g, b) colors from packed 0xRRGGBB values."""
    uniq, counts = np.unique(packed, return_counts=True)
    colors = Counter()
    for p, c in zip(uniq.tolist(), counts.tolist()):
        colors[_unpack_color(p)] = c
    return colors


def _run_edges(flags):
    """(starts, ends) flat indices of True runs in a bool array. 2-D input is
    zero-padded per row so runs never span a row boundary and the k-th start
    always pairs with the k-th end; 1-D input is one row."""
    flags2 = np.atleast_2d(flags)
    padded = np.zeros((flags2.shape[0], flags2.shape[1] + 2), dtype=np.int8)
    padded[:, 1:-1] = flags2
    edges = np.diff(padded, axis=1)
    return np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)


def _scan_runs(flags, gap_tol, min_len=1):
    """Half-open [start, end) runs of True in a 1-D bool array, merged across
    gaps <= gap_tol. A merged run shorter than min_len is dropped as it is
    finalized and does not bridge later merges (its neighbours see the gap
    across it, not the run). The single run scanner behind both the line-band
    and per-band column scans — one gap semantics for both axes."""
    starts, ends = _run_edges(flags)
    kept = []
    for s, e in zip(starts.tolist(), ends.tolist()):
        while kept and s - kept[-1][1] <= gap_tol:
            s = kept[-1][0]
            kept.pop()
        if e - s >= min_len:
            kept.append((s, e))
    return kept


def sample_viewport_bg_colors(
    viewport, max_colors=4, min_count_pct=0.005, dedupe_dist=COLOR_THRESHOLD,
    min_band_w_frac=0.2, grab=None,
):
    """Return up to `max_colors` distinct dominant colors in the viewport image,
    sorted by frequency. The first one is the primary background; subsequent
    ones are typically the current-line highlight, selection background,
    indent-guide color, etc.

    Treating ALL of these as "not ink" prevents the brick detector from picking
    up empty highlighted regions as fake bricks.

    - Coarse 4x grid sampling for speed.
    - Colors are counted EXACTLY (no quantization): the overlay erases dead
      bricks by filling with these colors, so an off-by-a-few value shows up
      as a visibly wrong rectangle.
    - `min_count_pct` is sized so a SINGLE current-line highlight still makes
      the cut on a tall viewport (one ~18px line of a ~1400px window is
      ~1.3%; 0.005 leaves headroom to ~200 lines). Missing it is not benign:
      on the light theme the highlight grey (~229 vs white 255, Manhattan 78)
      is ink-distance from bg, so the un-sampled band scans as full-width ink,
      gets dropped by `max_run_w_ratio`, and swallows every real token on
      that line (plus a pixel-adjacent neighbour line via band merging) —
      observed live. Dark themes never showed it: their highlight sits within
      `color_threshold` of bg and is background without being sampled.
    - `min_band_w_frac`: a candidate only qualifies as background if somewhere
      in the (subsampled) grid it forms a solid horizontal run at least this
      fraction of the width. At the low count cut, frequency alone cannot
      tell a background fill from a common INK color (a solid text color on
      a dense screen clears 0.5% easily), and admitting an ink color
      silently erases its tokens from detection. Fills (bg, line highlight,
      selection) paint wide bands; glyph shades occur in short scattered
      runs, so the band test is the discriminator.
    - `dedupe_dist` Manhattan distance threshold prevents near-identical colors.
      It must stay <= the detector's `color_threshold`; both default to the
      shared `COLOR_THRESHOLD` constant so the invariant holds by
      construction — tune that constant, not the two defaults separately.
      A color deduped away here is classified relative to the KEPT colors
      downstream, so if it can be farther than `color_threshold` from all of
      them it becomes ink — and an empty background band in that distance
      window turns into phantom bricks.
    - `grab` lets the caller share an already-captured viewport buffer; saves
      a re-grab when the brick detector is going to run right after.
    """
    if grab is None:
        if viewport is None:
            return []
        grab = grab_viewport_buffer(viewport)
        if grab is None:
            return []
    buf, w, h, _dpr = grab

    step = 4
    arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)[::step, ::step]
    packed_grid = _np_pack_colors(arr[..., :3])
    counter = _np_count_colors(packed_grid)

    if not counter:
        return []

    total = sum(counter.values())
    threshold = max(1, int(total * min_count_pct))

    min_band_cells = max(1, int(packed_grid.shape[1] * min_band_w_frac))

    def _has_wide_band(r, g, b):
        starts, ends = _run_edges(packed_grid == ((r << 16) | (g << 8) | b))
        if starts.size == 0:
            return False
        return int((ends - starts).max()) >= min_band_cells

    result = []
    # No fixed candidate window: the `count < threshold` break already bounds
    # the scan. A top-N window sized for the old 2% cut could be exhausted by
    # gate-rejected ink shades at the 0.5% cut, starving a real highlight
    # ranked just below them.
    for (r, g, b), count in counter.most_common():
        if count < threshold:
            break
        if any(
            abs(r - rc.red()) + abs(g - rc.green()) + abs(b - rc.blue()) <= dedupe_dist
            for rc in result
        ):
            continue
        if min_band_w_frac > 0 and not _has_wide_band(r, g, b):
            logger.info(
                "ida-breakout: bg candidate (%d, %d, %d) rejected: no wide "
                "horizontal band (ink-like)", r, g, b,
            )
            continue
        result.append(QtGui.QColor(r, g, b))
        if len(result) >= max_colors:
            break
    return result


# Substring hints matched against Qt class names. An exact class name is its
# own substring, so unknown IDA builds are handled by appending here.
# Known outer viewers: TEAViewer, TGraphViewer.
_VIEWER_CLASS_HINTS = ("Viewer", "Editor", "TEA")

# Known custom controls: TCustomControl, IDACustomViewer/Control, PyCustomViewer.
_CUSTOM_CONTROL_HINTS = ("CustomViewer", "CustomControl")


def _dump_widget_tree(qwidget, depth=0, max_depth=4):
    """Log the widget hierarchy under qwidget so we can identify the render surface."""
    if qwidget is None or depth > max_depth:
        return
    indent = "  " * depth
    try:
        cls = qwidget.metaObject().className()
        geom = (qwidget.x(), qwidget.y(), qwidget.width(), qwidget.height())
        font = qwidget.font().family()
        logger.info("ida-breakout: %s%s geom=%s font=%s", indent, cls, geom, font)
    except Exception:
        logger.exception("dump_widget_tree failed at depth %d", depth)
        return
    for child in qwidget.findChildren(
        QtWidgets.QWidget, options=QtCore.Qt.FindDirectChildrenOnly
    ):
        _dump_widget_tree(child, depth + 1, max_depth)


def find_pseudocode_viewport(qwidget):
    """Return the QWidget that actually paints the pseudocode text plus the
    enclosing scroll-bar host if there is one.

    Strategy (in code order):
      1. Outer widget matches _VIEWER_CLASS_HINTS (TEAViewer etc.): use its
         largest visible direct child if it covers >= 50% of the outer area,
         else the outer widget itself (WARNING logged).
      2. QAbstractScrollArea + viewport() — QPlainTextEdit-style hosts.
      3. Match an IDA custom-viewer class by name hint (_CUSTOM_CONTROL_HINTS).
      4. Largest visible descendant covering > 1/4 of the outer area.
      5. Fall back to the outer qwidget itself (WARNING logged).

    Always emits diagnostic logs so we can adapt to unknown IDA builds.
    """
    if qwidget is None:
        return None, None

    logger.info(
        "ida-breakout: find_pseudocode_viewport: outer class=%s size=%dx%d font=%s",
        qwidget.metaObject().className(),
        qwidget.width(),
        qwidget.height(),
        qwidget.font().family(),
    )
    logger.info("ida-breakout: widget tree:")
    _dump_widget_tree(qwidget)

    cls_name = qwidget.metaObject().className()
    if any(h in cls_name for h in _VIEWER_CLASS_HINTS):
        outer_area = max(1, qwidget.width() * qwidget.height())
        biggest = None
        biggest_area = 0
        for child in qwidget.findChildren(
            QtWidgets.QWidget, options=QtCore.Qt.FindDirectChildrenOnly
        ):
            if not child.isVisible():
                continue
            r = child.geometry()
            a = r.width() * r.height()
            if a > biggest_area:
                biggest_area = a
                biggest = child
        if biggest is not None and biggest_area >= outer_area * 0.5:
            logger.info(
                "ida-breakout: using main-content child of %s as viewport: %s geom=%s",
                cls_name,
                biggest.metaObject().className(),
                (biggest.x(), biggest.y(), biggest.width(), biggest.height()),
            )
            return biggest, None
        # CLAUDE.md promises a WARNING whenever detection falls back to the
        # outer widget, so users filtering at WARNING still see it.
        logger.warning(
            "ida-breakout: using outer widget directly as viewport: %s", cls_name
        )
        return qwidget, None

    scroll_areas = qwidget.findChildren(QtWidgets.QAbstractScrollArea)
    if scroll_areas:
        sa = scroll_areas[0]
        vp = sa.viewport()
        if vp is not None:
            logger.info(
                "ida-breakout: picked viewport via QAbstractScrollArea: %s (sa=%s)",
                vp.metaObject().className(), sa.metaObject().className(),
            )
            return vp, sa

    all_widgets = qwidget.findChildren(QtWidgets.QWidget)
    for w in all_widgets:
        try:
            cls = w.metaObject().className()
        except Exception:
            continue
        if any(h in cls for h in _CUSTOM_CONTROL_HINTS):
            logger.info("ida-breakout: picked viewport via known-class match: %s", cls)
            return w, None

    candidate = None
    best_area = qwidget.width() * qwidget.height() // 4
    for w in all_widgets:
        try:
            area = w.width() * w.height()
        except Exception:
            continue
        if area > best_area and w.isVisible():
            candidate = w
            best_area = area
    if candidate is not None:
        logger.info(
            "ida-breakout: picked viewport via largest-visible-child: %s area=%d",
            candidate.metaObject().className(), best_area,
        )
        return candidate, None

    logger.warning(
        "ida-breakout: no good child found; falling back to outer widget %s",
        qwidget.metaObject().className(),
    )
    return qwidget, None


def compute_playfield_height(viewport):
    """Return the y-coordinate of the topmost bottom-anchored child widget,
    or the full viewport height if none. Lets the game floor sit just above
    a status bar / footer.
    """
    if viewport is None:
        return 0
    h = viewport.height()
    eff = h
    try:
        for child in viewport.findChildren(
            QtWidgets.QWidget, options=QtCore.Qt.FindDirectChildrenOnly
        ):
            if not child.isVisible():
                continue
            r = child.geometry()
            if r.y() + r.height() >= h - 5 and r.y() > h // 2:
                eff = min(eff, r.y())
    except Exception:
        logger.exception("compute_playfield_height failed")
    return eff


def grab_viewport_buffer(viewport):
    """Return (rgba_bytes, width, height, dpr) for fast pixel access, or None.

    On HiDPI / Retina, viewport.grab() returns a pixmap sized in *device*
    pixels (2x logical on macOS Retina). The QImage we get from .toImage()
    is the same device-pixel size. The dpr lets the caller convert back
    to logical (Qt overlay) coordinates.
    """
    if viewport is None:
        return None
    try:
        pixmap = viewport.grab()
        if pixmap.isNull():
            return None
        img = pixmap.toImage().convertToFormat(QtGui.QImage.Format_RGB32)
        if img.isNull() or img.width() < 4 or img.height() < 4:
            return None
        w, h = img.width(), img.height()
        # Read the dpr Qt stamped on the pixmap instead of re-deriving it as
        # w / viewport.width(): that ratio inherits the pixmap's integer size
        # rounding (logical 1001 @ 1.5x → 1502 px → dpr 1.50050), and the
        # ceil-based tunable conversion downstream is discontinuous just above
        # integer products, so the noise would inflate every tunable by a
        # device px and flip brick segmentation on 1-px window resizes.
        try:
            dpr = float(pixmap.devicePixelRatio())
        except Exception:
            dpr = 1.0
        if dpr <= 0:
            dpr = 1.0
        return bytes(img.constBits()), w, h, dpr
    except Exception:
        logger.exception("grab_viewport_buffer failed")
        return None


def detect_bricks_from_pixels(
    viewport,
    bg_colors,
    color_threshold=COLOR_THRESHOLD,
    column_gap_tolerance=2.0,
    line_gap_tolerance=0.5,
    min_run_w=1.0,
    min_run_h=1.0,
    padding=0.5,
    max_run_w_ratio=0.6,
    grab=None,
):
    """Build bricks by scanning the viewport image for ink (non-background) pixels.

    `bg_colors` is a list of QColor; a pixel is "ink" only if it differs from
    *all* of them by more than `color_threshold`. This keeps the
    current-line-highlight, selection, etc. from being mistaken for text.

    The geometry tunables (`column_gap_tolerance`, `line_gap_tolerance`,
    `min_run_w`, `min_run_h`, `padding`) are in LOGICAL px and get converted
    to device px with the grab's dpr (gaps/padding with ceil, minimum-size
    filters with round-to-nearest — see `_dp`/`_dp_min`; an explicit 0
    disables the knob either way), keeping the same font's brick split
    consistent across 1x and HiDPI displays — up to device-pixel
    quantization: sub-pixel values like 0.5 necessarily round up to a whole
    device px at dpr=1. The defaults reproduce the historical Retina (dpr=2)
    device values exactly.

    `max_run_w_ratio` drops bricks wider than that fraction of the viewport,
    which would otherwise be a full-line highlight rather than a real token.

    `grab` lets the caller share an already-captured viewport buffer with the
    bg-color sampler (avoids a second viewport.grab() round-trip on cold start).
    """
    if not bg_colors:
        return []
    if grab is None:
        grab = grab_viewport_buffer(viewport)
        if grab is None:
            return []
    buf, w, h, dpr = grab
    logger.info(
        "ida-breakout: grab: %dx%d dpr=%.2f bg_colors=%s",
        w, h, dpr,
        [(c.red(), c.green(), c.blue()) for c in bg_colors],
    )

    # Logical-px tunables → device px. ceil keeps sub-pixel values effective
    # at dpr=1 (ceil(0.5)=1), scales monotonically at fractional dpr where
    # round-half-to-even dips low (round(2.5)=2), and respects an explicit 0
    # (feature off). NOT used for caret_max_w_dp, which has its own +1 slack.
    def _dp(logical_px):
        return int(math.ceil(logical_px * dpr))

    # MINIMUM-size filters convert with round-to-nearest instead: ceil points
    # the wrong way for a lower bound — ceil(1.0 * 1.25) = 2 would drop
    # 1-device-px ink runs that both dpr=1 and dpr=2 keep. Positive values
    # stay >= 1; an explicit 0 is still off.
    def _dp_min(logical_px):
        return max(1, int(round(logical_px * dpr))) if logical_px > 0 else 0

    col_gap_dp = _dp(column_gap_tolerance)
    line_gap_dp = _dp(line_gap_tolerance)
    min_run_w_dp = _dp_min(min_run_w)
    min_run_h_dp = _dp_min(min_run_h)
    padding_dp = _dp(padding)

    def _clamp_rect(x0_dp, x1_dp, y0_dp, y1_dp):
        return max(0, x0_dp), min(w, x1_dp), max(0, y0_dp), min(h, y1_dp)

    masked_rects = []
    # viewport is None in headless tests that inject `grab` directly.
    if viewport is not None:
        for child in viewport.findChildren(
            QtWidgets.QWidget, options=QtCore.Qt.FindDirectChildrenOnly
        ):
            if not child.isVisible():
                continue
            r = child.geometry()
            if r.width() > 0 and r.height() > 0:
                masked_rects.append(r)
    if masked_rects:
        logger.info(
            "ida-breakout: masking child rects: %s",
            [(r.x(), r.y(), r.width(), r.height()) for r in masked_rects],
        )
        # Neutralize masked pixels BEFORE the ink scan, not just drop
        # overlapping bricks afterwards: a scrollbar painted into the grab
        # (outer-widget fallback path — QWidget.grab() renders children) is
        # a tall contiguous ink column that merges every line band it spans,
        # and the post-detection drop can't undo that segmentation damage.
        buf = bytearray(buf)
        prim = bg_colors[0]
        b0, g0, r0 = prim.blue(), prim.green(), prim.red()
        arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)
        for mr in masked_rects:
            # Logical child geometry → device px; floor/ceil so the whole
            # rect is covered.
            xs, xe, ys, ye = _clamp_rect(
                int(math.floor(mr.x() * dpr)),
                int(math.ceil((mr.x() + mr.width()) * dpr)),
                int(math.floor(mr.y() * dpr)),
                int(math.ceil((mr.y() + mr.height()) * dpr)),
            )
            if xe <= xs or ye <= ys:
                continue
            arr[ys:ye, xs:xe, 0] = b0
            arr[ys:ye, xs:xe, 1] = g0
            arr[ys:ye, xs:xe, 2] = r0

    bg_channels = tuple((c.blue(), c.green(), c.red()) for c in bg_colors)

    pix_u8 = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)[..., :3]  # B,G,R
    # Channel-split planes + reused (h, w) scratch buffers. Broadcasting
    # against all bg colors at once allocates h*w*n_bg*3 int16 temps
    # (~430MB peak / ~430ms at Retina fullscreen); a fresh (h, w, 3) diff
    # per color still re-allocates large temps every pass. In-place ops on
    # two scratch planes measure ~93MB / ~65ms for the same mask.
    chans = [pix_u8[:, :, i].astype(np.int16) for i in range(3)]
    tmp = np.empty((h, w), dtype=np.int16)
    acc = np.empty((h, w), dtype=np.int16)  # max 3*255 fits int16
    ink_mask = np.ones((h, w), dtype=bool)
    for bg_ch in bg_channels:
        acc[:] = 0
        for chan, c in zip(chans, bg_ch):
            np.subtract(chan, c, out=tmp)
            np.abs(tmp, out=tmp)
            acc += tmp
        ink_mask &= acc > color_threshold
    # Full-width scan: a stride-2 subsample here misses a 1-device-px ink
    # column at odd x (dpr=1 thin glyphs like '|'), so its line band never
    # opens even though the per-band column scan below would see it.
    line_ranges = _scan_runs(ink_mask.any(axis=1), line_gap_dp, min_run_h_dp)

    max_run_w_dp = int(w * max_run_w_ratio)

    def _sample_brick_bg(x0_dp, y0_dp, x1_dp, y1_dp):
        """Most common exact non-ink (r, g, b) inside the brick's ERASE rect —
        its LOCAL background (the line/token highlight it sits on), so the
        overlay can erase with a pixel-perfect fill instead of the global bg
        color. Sampling exactly the region that will be filled guarantees
        the fill color was observed everywhere it gets painted (a ring
        narrower than the erase margin lets the outer erased band be filled
        with a color never seen there, e.g. across a highlight boundary at
        fractional dpr). None if every sampled pixel is ink. Runs once per
        brick, so skip the full color Counter _np_count_colors would build —
        only the winner is needed, and at thousands of bricks the difference
        is a measurable slice of detection cold start.
        """
        xs, xe, ys, ye = _clamp_rect(x0_dp, x1_dp, y0_dp, y1_dp)
        if xe <= xs or ye <= ys:
            return None
        m = ~ink_mask[ys:ye, xs:xe]
        if not m.any():
            return None
        packed = _np_pack_colors(pix_u8[ys:ye, xs:xe][m])  # (n, 3) B,G,R
        uniq, counts = np.unique(packed, return_counts=True)
        # Ties: argmax picks the first (= lowest packed value).
        return _unpack_color(int(uniq[counts.argmax()]))

    def _rect_ink_color_count(x0_dp, x1_dp, y0_dp, y1_dp):
        """Number of distinct exact ink colors in the rect — the caret
        filter only asks whether there are more than 2.
        """
        xs, xe, ys, ye = _clamp_rect(x0_dp, x1_dp, y0_dp, y1_dp)
        if xe <= xs or ye <= ys:
            return 0
        m = ink_mask[ys:ye, xs:xe]
        if not m.any():
            return 0
        return int(np.unique(_np_pack_colors(pix_u8[ys:ye, xs:xe][m])).size)

    # Anything up to ~2 logical px wide is caret-shaped.
    caret_max_w_dp = int(round(2 * dpr)) + 1

    def _run_ink_rows(x0_dp, x1_dp, y0_dp, y1_dp):
        """Number of rows in [y0, y1) where the run [x0, x1) has any ink."""
        return int(ink_mask[y0_dp:y1_dp, x0_dp:x1_dp].any(axis=1).sum())

    # How far an erase rect may grow past the ink to cover the anti-aliasing
    # halo (~2 logical px) — but never past the midpoint of the gap to the
    # neighbouring line/token, so erasing a dead brick can't shave the glyphs
    # next to it.
    margin_dp = _dp(2)

    def _erase_span(lo_dp, hi_dp, prev_hi_dp, next_lo_dp, limit_dp):
        """Grow the ink span [lo, hi) by margin_dp, clamped at the midpoint of
        the gap to the neighbouring line/run (None = no neighbour) and to
        [0, limit). Used for both axes of the erase rect.
        """
        e0 = lo_dp - margin_dp
        if prev_hi_dp is not None:
            e0 = max(e0, (prev_hi_dp + lo_dp) // 2)
        e1 = hi_dp + margin_dp
        if next_lo_dp is not None:
            e1 = min(e1, (hi_dp + next_lo_dp) // 2)
        return max(0, e0), min(limit_dp, e1)

    def _rect_to_logical(x0_dp, y0_dp, x1_dp, y1_dp):
        """Device box → logical (x, y, w, h). Floor the top-left, ceil the
        bottom-right: truncating both shaves up to 1 logical px off the
        right/bottom edges, leaving un-erased glyph slivers.
        """
        x = int(math.floor(x0_dp / dpr))
        y = int(math.floor(y0_dp / dpr))
        return (
            x,
            y,
            max(1, int(math.ceil(x1_dp / dpr)) - x),
            max(1, int(math.ceil(y1_dp / dpr)) - y),
        )

    def _emit_brick(run_start_dp, run_end_dp, y_start_dp, y_end_dp, erase_dp):
        width_dp = run_end_dp - run_start_dp
        if width_dp < min_run_w_dp:
            return
        if width_dp > max_run_w_dp:
            return
        # The text caret and indent-guide fragments are thin SOLID-color
        # bars spanning the full line band; the caret is focus-transient, so
        # it lands in the grab but vanishes from screen once the game overlay
        # takes focus — becoming an invisible brick. Real glyphs are usually
        # anti-aliased and carry 3+ distinct ink shades — but with AA off
        # (bitmap fonts, some Linux setups) a lone '|'/'l' stem is monochrome
        # too, so also require the bar to cover ~all rows of the line band,
        # which carets/guides do and glyph stems generally don't.
        if (
            width_dp <= caret_max_w_dp
            and _run_ink_rows(
                run_start_dp, run_end_dp, y_start_dp, y_end_dp
            ) >= 0.9 * (y_end_dp - y_start_dp)
            and _rect_ink_color_count(
                run_start_dp, run_end_dp, y_start_dp, y_end_dp
            ) <= 2
        ):
            logger.info(
                "ida-breakout: dropping caret/guide-like brick at dp(%d,%d) w=%d",
                run_start_dp, y_start_dp, width_dp,
            )
            return
        x_log, y_log, w_log, h_log = _rect_to_logical(
            max(0, run_start_dp - padding_dp),
            max(0, y_start_dp - padding_dp),
            run_end_dp + padding_dp,
            y_end_dp + padding_dp,
        )
        bricks.append(
            Brick(
                x=x_log, y=y_log, w=w_log, h=h_log,
                bg=_sample_brick_bg(*erase_dp),
                erase=_rect_to_logical(*erase_dp),
            )
        )

    bricks = []
    for li, (y_start, y_end) in enumerate(line_ranges):
        runs = _scan_runs(ink_mask[y_start:y_end, :].any(axis=0), col_gap_dp)

        erase_y0, erase_y1 = _erase_span(
            y_start,
            y_end,
            line_ranges[li - 1][1] if li > 0 else None,
            line_ranges[li + 1][0] if li + 1 < len(line_ranges) else None,
            h,
        )

        for ri, (run_start, run_end) in enumerate(runs):
            erase_x0, erase_x1 = _erase_span(
                run_start,
                run_end,
                runs[ri - 1][1] if ri > 0 else None,
                runs[ri + 1][0] if ri + 1 < len(runs) else None,
                w,
            )
            _emit_brick(
                run_start, run_end, y_start, y_end,
                (erase_x0, erase_y0, erase_x1, erase_y1),
            )

    if masked_rects and bricks:
        before = len(bricks)
        bricks = [
            b for b in bricks
            if not any(mr.intersects(QtCore.QRect(b.x, b.y, b.w, b.h)) for mr in masked_rects)
        ]
        if before != len(bricks):
            logger.info(
                "ida-breakout: dropped %d bricks overlapping masked child widgets",
                before - len(bricks),
            )

    logger.info(
        "ida-breakout: detect_bricks_from_pixels: viewport=%dx%d lines=%d bricks=%d",
        w, h, len(line_ranges), len(bricks),
    )
    return bricks
