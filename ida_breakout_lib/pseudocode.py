import logging
import math
from collections import Counter

from PySide6 import QtCore, QtGui, QtWidgets
try:
    import numpy as np
except Exception:  # pragma: no cover - IDA bundles numpy, but keep a safe fallback.
    np = None

from ida_breakout_lib.game import Brick


logger = logging.getLogger(__name__)


def sample_viewport_bg_colors(viewport, max_colors=4, min_count_pct=0.02, dedupe_dist=60, grab=None):
    """Return up to `max_colors` distinct dominant colors in the viewport image,
    sorted by frequency. The first one is the primary background; subsequent
    ones are typically the current-line highlight, selection background,
    indent-guide color, etc.

    Treating ALL of these as "not ink" prevents the brick detector from picking
    up empty highlighted regions as fake bricks.

    - Coarse 4x grid sampling for speed.
    - Colors are counted EXACTLY (no quantization): the overlay erases dead
      bricks by filling with these colors, so an off-by-a-few value shows up
      as a visibly wrong rectangle. Flat fills (bg, line highlight) dominate
      the counts anyway; anti-aliasing variants each stay below
      `min_count_pct` and never make the cut.
    - `dedupe_dist` Manhattan distance threshold prevents near-identical colors.
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

    counter = Counter()
    step = 4
    if np is not None:
        arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)[::step, ::step]
        packed = (
            (arr[..., 2].astype(np.uint32) << 16)
            | (arr[..., 1].astype(np.uint32) << 8)
            | arr[..., 0].astype(np.uint32)
        )
        uniq, counts = np.unique(packed, return_counts=True)
        for p, c in zip(uniq.tolist(), counts.tolist()):
            counter[((p >> 16) & 0xFF, (p >> 8) & 0xFF, p & 0xFF)] = c
    else:
        for y in range(0, h, step):
            row_off = y * w * 4
            for x in range(0, w, step):
                off = row_off + x * 4
                counter[(buf[off + 2], buf[off + 1], buf[off])] += 1

    if not counter:
        return []

    total = sum(counter.values())
    threshold = max(1, int(total * min_count_pct))

    result = []
    for (r, g, b), count in counter.most_common(max_colors * 8):
        if count < threshold:
            break
        if any(
            abs(r - rc.red()) + abs(g - rc.green()) + abs(b - rc.blue()) <= dedupe_dist
            for rc in result
        ):
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

    Strategy:
      1. QAbstractScrollArea + viewport() — works for QPlainTextEdit-style hosts.
      2. Match an IDA custom-viewer class by name hint (_CUSTOM_CONTROL_HINTS).
      3. Pick the largest descendant QWidget that has a real geometry.
      4. Fall back to the outer qwidget itself.

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
        logger.info(
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
        try:
            logical_w = max(1, viewport.width())
            dpr = w / float(logical_w)
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
    color_threshold=40,
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
    to device px with the grab's dpr, so the same font splits into the same
    bricks on 1x and HiDPI displays. The defaults reproduce the historical
    Retina (dpr=2) device values exactly.

    `max_run_w_ratio` drops bricks wider than that fraction of the viewport,
    which would otherwise be a full-line highlight rather than a real token.

    `grab` lets the caller share an already-captured viewport buffer with the
    bg-color sampler (avoids a second viewport.grab() round-trip on cold start).
    """
    if not bg_colors:
        return []
    if isinstance(bg_colors, QtGui.QColor):
        bg_colors = [bg_colors]
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

    # Logical-px tunables → device px (like caret_max_w_dp / margin_dp below).
    col_gap_dp = max(1, int(round(column_gap_tolerance * dpr)))
    line_gap_dp = max(1, int(round(line_gap_tolerance * dpr)))
    min_run_w_dp = max(1, int(round(min_run_w * dpr)))
    min_run_h_dp = max(1, int(round(min_run_h * dpr)))
    padding_dp = max(1, int(round(padding * dpr)))

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

    bg_channels = tuple((c.blue(), c.green(), c.red()) for c in bg_colors)

    ink_mask = None
    pix_u8 = None
    if np is not None:
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
        row_has_ink = ink_mask[:, ::2].any(axis=1)
    else:
        def is_ink(off):
            b = buf[off]
            g = buf[off + 1]
            r = buf[off + 2]
            for bg_b, bg_g, bg_r in bg_channels:
                if (abs(r - bg_r) + abs(g - bg_g) + abs(b - bg_b)) <= color_threshold:
                    return False
            return True

        row_has_ink = bytearray(h)
        for y in range(h):
            base = y * w * 4
            for x in range(0, w, 2):
                if is_ink(base + x * 4):
                    row_has_ink[y] = 1
                    break

    line_ranges = []
    y = 0
    while y < h:
        if row_has_ink[y]:
            start = y
            while y < h and row_has_ink[y]:
                y += 1
            end = y
            while line_ranges and start - line_ranges[-1][1] <= line_gap_dp:
                start = line_ranges[-1][0]
                line_ranges.pop()
            if end - start >= min_run_h_dp:
                line_ranges.append((start, end))
        else:
            y += 1

    max_run_w_dp = int(w * max_run_w_ratio)

    def _rect_colors(x0_dp, x1_dp, y0_dp, y1_dp, want_ink):
        """Counter of exact (r, g, b) colors of the ink (or non-ink) pixels
        inside the clamped device rect.
        """
        xs = max(0, x0_dp)
        xe = min(w, x1_dp)
        ys = max(0, y0_dp)
        ye = min(h, y1_dp)
        colors = Counter()
        if xe <= xs or ye <= ys:
            return colors
        if ink_mask is not None:
            m = ink_mask[ys:ye, xs:xe]
            if not want_ink:
                m = ~m
            if m.any():
                vals = pix_u8[ys:ye, xs:xe][m].astype(np.int32)  # B,G,R
                packed = (vals[:, 2] << 16) | (vals[:, 1] << 8) | vals[:, 0]
                uniq, counts = np.unique(packed, return_counts=True)
                for p, c in zip(uniq.tolist(), counts.tolist()):
                    colors[((p >> 16) & 0xFF, (p >> 8) & 0xFF, p & 0xFF)] = c
            return colors
        for yy in range(ys, ye):
            base = yy * w * 4
            for xx in range(xs, xe):
                off = base + xx * 4
                if is_ink(off) == want_ink:
                    colors[(buf[off + 2], buf[off + 1], buf[off])] += 1
        return colors

    def _sample_brick_bg(x0_dp, x1_dp, y0_dp, y1_dp):
        """Most common non-ink color in and just around the brick's device
        rect — its LOCAL background (the line/token highlight it sits on), so
        the overlay can erase with a pixel-perfect fill instead of the global
        bg color. None if every sampled pixel is ink.
        """
        colors = _rect_colors(x0_dp - 3, x1_dp + 3, y0_dp, y1_dp, want_ink=False)
        return colors.most_common(1)[0][0] if colors else None

    # Anything up to ~2 logical px wide is caret-shaped.
    caret_max_w_dp = int(round(2 * dpr)) + 1

    # How far an erase rect may grow past the ink to cover the anti-aliasing
    # halo (~2 logical px) — but never past the midpoint of the gap to the
    # neighbouring line/token, so erasing a dead brick can't shave the glyphs
    # next to it.
    margin_dp = max(1, int(round(2 * dpr)))

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

    def _emit_brick(run_start_dp, last_ink_dp, y_start_dp, y_end_dp, erase_dp):
        width_dp = last_ink_dp - run_start_dp + 1
        if width_dp < min_run_w_dp:
            return
        if width_dp > max_run_w_dp:
            return
        # The text caret and indent-guide fragments are thin SOLID-color
        # bars; the caret is focus-transient, so it lands in the grab but
        # vanishes from screen once the game overlay takes focus — becoming
        # an invisible brick. Real glyphs are anti-aliased and always carry
        # 3+ distinct ink shades, so thin + (near-)monochrome ⇒ not text.
        if width_dp <= caret_max_w_dp and len(_rect_colors(
            run_start_dp, last_ink_dp + 1, y_start_dp, y_end_dp, want_ink=True
        )) <= 2:
            logger.info(
                "ida-breakout: dropping caret/guide-like brick at dp(%d,%d) w=%d",
                run_start_dp, y_start_dp, width_dp,
            )
            return
        x_log, y_log, w_log, h_log = _rect_to_logical(
            max(0, run_start_dp - padding_dp),
            max(0, y_start_dp - padding_dp),
            last_ink_dp + 1 + padding_dp,
            y_end_dp + padding_dp,
        )
        bricks.append(
            Brick(
                x=x_log, y=y_log, w=w_log, h=h_log,
                bg=_sample_brick_bg(run_start_dp, last_ink_dp + 1, y_start_dp, y_end_dp),
                erase=_rect_to_logical(*erase_dp),
            )
        )

    bricks = []
    for li, (y_start, y_end) in enumerate(line_ranges):
        if ink_mask is not None:
            col_has_ink = ink_mask[y_start:y_end, :].any(axis=0)
        else:
            col_has_ink = bytearray(w)
            for x in range(w):
                for yy in range(y_start, y_end):
                    if is_ink((yy * w + x) * 4):
                        col_has_ink[x] = 1
                        break

        runs = []
        in_run = False
        run_start = 0
        last_ink = 0
        x = 0
        while x < w:
            if col_has_ink[x]:
                if not in_run:
                    in_run = True
                    run_start = x
                last_ink = x
                x += 1
            else:
                if in_run and (x - last_ink) > col_gap_dp:
                    runs.append((run_start, last_ink))
                    in_run = False
                x += 1
        if in_run:
            runs.append((run_start, last_ink))

        erase_y0, erase_y1 = _erase_span(
            y_start,
            y_end,
            line_ranges[li - 1][1] if li > 0 else None,
            line_ranges[li + 1][0] if li + 1 < len(line_ranges) else None,
            h,
        )

        for ri, (run_start, last_ink) in enumerate(runs):
            erase_x0, erase_x1 = _erase_span(
                run_start,
                last_ink + 1,
                runs[ri - 1][1] + 1 if ri > 0 else None,
                runs[ri + 1][0] if ri + 1 < len(runs) else None,
                w,
            )
            _emit_brick(
                run_start, last_ink, y_start, y_end,
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
