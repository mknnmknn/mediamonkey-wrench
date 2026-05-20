"""
Tiny inline-SVG chart helpers. No JS, no build step, no chart library —
just enough to make the artist detail page glance-readable.

Each function returns a complete `<svg>` string suitable for embedding in
a Jinja template via `{{ chart | safe }}`.
"""
from __future__ import annotations

from html import escape as _esc


_DEFAULT_BAR = "#6a4d2b"
_DEFAULT_BAR_SOFT = "#c5a878"
_DEFAULT_FOREGROUND = "#8e5572"   # plum — used as the foreground/overlay curve


def dual_area_curve(values_bg: list[float], values_fg: list[float],
                    labels: list[str], *,
                    width: int = 720, height: int = 200,
                    bg_color: str = _DEFAULT_BAR,
                    bg_fill_opacity: float = 0.45,
                    fg_color: str = _DEFAULT_FOREGROUND,
                    fg_stroke_width: float = 2.2,
                    fg_dot_radius: float = 0.0,
                    pad_top: int = 10, pad_bot: int = 24, pad_x: int = 12,
                    max_x_labels: int = 8) -> str:
    """
    Two-series overlay chart sharing an x-axis. Each series is
    max-normalized to its own implicit y-scale (so a "plays per month" series
    in the 0–50 range and a "cumulative tracks" series in the 0–400 range can
    both occupy the full height without one squashing the other).

    Series rendering:
      - background: filled area from baseline up to the per-x value.
      - foreground: stroked line (no fill), drawn on top of the background.

    Linear segments between points — no bezier smoothing, so the curve never
    invents data between the actual buckets.
    """
    if not values_bg:
        return _empty_svg(width, height)
    if len(values_bg) != len(values_fg) or len(values_bg) != len(labels):
        raise ValueError("dual_area_curve: series + labels must be parallel")

    n = len(values_bg)
    max_bg = max(values_bg) or 1
    max_fg = max(values_fg) or 1
    drawable_h = height - pad_top - pad_bot
    baseline = pad_top + drawable_h

    # x positions across the drawable area
    if n == 1:
        xs = [width / 2]
    else:
        xs = [pad_x + i * (width - 2 * pad_x) / (n - 1) for i in range(n)]

    # y positions per series (SVG y grows downward, so subtract from baseline)
    ys_bg = [baseline - (v / max_bg) * drawable_h for v in values_bg]
    ys_fg = [baseline - (v / max_fg) * drawable_h for v in values_fg]

    # Background: closed polygon from baseline up to series, back to baseline
    bg_pts = [(xs[0], baseline)] + list(zip(xs, ys_bg)) + [(xs[-1], baseline)]
    bg_path = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in bg_pts) + " Z"

    # Foreground: open polyline
    fg_pts = list(zip(xs, ys_fg))
    fg_path = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in fg_pts)

    pieces: list[str] = [
        f'<path d="{bg_path}" fill="{bg_color}" fill-opacity="{bg_fill_opacity}" stroke="none"/>',
        f'<path d="{fg_path}" fill="none" stroke="{fg_color}" '
        f'stroke-width="{fg_stroke_width}" stroke-linejoin="round" stroke-linecap="round"/>',
    ]
    if fg_dot_radius > 0:
        for x, y in fg_pts:
            pieces.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{fg_dot_radius}" fill="{fg_color}"/>')

    # Hover targets so tooltips show values at each bucket
    slot = (width - 2 * pad_x) / max(1, n - 1) if n > 1 else width
    for i, (x, lbl) in enumerate(zip(xs, labels)):
        pieces.append(
            f'<rect x="{x - slot / 2:.1f}" y="{pad_top}" width="{slot:.1f}" '
            f'height="{drawable_h:.1f}" fill="transparent">'
            f'<title>{_esc(lbl)}: {values_bg[i]:.0f} plays · '
            f'{values_fg[i]:.0f} cumulative tracks</title></rect>'
        )

    # Sparse x-axis labels
    if max_x_labels > 1:
        step = max(1, n // (max_x_labels - 1))
    else:
        step = n
    idxs = sorted(set(list(range(0, n, step)) + [n - 1]))
    for i in idxs:
        pieces.append(
            f'<text x="{xs[i]:.1f}" y="{height - 6}" text-anchor="middle" '
            f'font-size="10" fill="#888">{_esc(labels[i])}</text>'
        )

    return (
        f'<svg viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" '
        f'style="width:100%;height:auto;display:block">'
        + "".join(pieces) + "</svg>"
    )


def bars_vertical(values: list[float], labels: list[str], *,
                  width: int = 720, height: int = 140,
                  bar_color: str = _DEFAULT_BAR,
                  empty_color: str = "#eee",
                  max_x_labels: int = 8) -> str:
    """
    Plays-over-time-style chart. One bar per bucket, evenly spaced. A sparse
    sample of x-axis labels along the bottom. Zero-value bars render as a
    faint stub so the gaps in listening are visible.
    """
    if not values:
        return _empty_svg(width, height)

    max_v = max(values) or 1
    n = len(values)
    pad_x, pad_top, pad_bot = 8, 8, 22
    slot = (width - 2 * pad_x) / n
    bar_w = max(1.5, slot * 0.72)
    drawable_h = height - pad_top - pad_bot

    pieces: list[str] = []
    for i, v in enumerate(values):
        x = pad_x + i * slot + (slot - bar_w) / 2
        if v > 0:
            h = max(1.0, v / max_v * drawable_h)
            y = pad_top + drawable_h - h
            fill = bar_color
        else:
            h = 1.0  # tiny stub line for visual continuity
            y = pad_top + drawable_h - h
            fill = empty_color
        pieces.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" '
            f'fill="{fill}"><title>{_esc(labels[i])}: {v:.0f}</title></rect>'
        )

    # Sparse x-axis labels — first, last, and a few evenly-spaced in between
    indices = set()
    step = max(1, n // (max_x_labels - 1)) if max_x_labels > 1 else n
    for i in range(0, n, step):
        indices.add(i)
    indices.add(n - 1)
    for i in sorted(indices):
        x = pad_x + i * slot + slot / 2
        pieces.append(
            f'<text x="{x:.1f}" y="{height - 6}" text-anchor="middle" '
            f'font-size="10" fill="#888">{_esc(labels[i])}</text>'
        )

    return (
        f'<svg viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" '
        f'style="width:100%;height:auto;display:block">'
        + "".join(pieces) + "</svg>"
    )


def bars_horizontal(values: list[float], labels: list[str], *,
                    width: int = 720, height_per_bar: int = 22,
                    bar_color: str = _DEFAULT_BAR,
                    label_col_px: int = 240,
                    truncate_label: int = 38) -> str:
    """
    Horizontal bars (descending). Used for the diversity track-distribution
    chart and similar "long thin sorted list" visuals. Track label on the
    left, bar in the middle, numeric value at the right edge of the bar.
    """
    if not values:
        return _empty_svg(width, 30)
    max_v = max(values) or 1
    n = len(values)
    total_h = n * height_per_bar
    bar_h = height_per_bar - 6
    label_pad = 8
    bar_x = label_col_px
    max_bar_len = width - label_col_px - 70  # leave room for trailing value

    rows: list[str] = []
    for i, v in enumerate(values):
        y = i * height_per_bar
        bar_len = (v / max_v) * max_bar_len if max_v else 0
        cy = y + bar_h / 2 + 4
        label = labels[i]
        if len(label) > truncate_label:
            label = label[:truncate_label - 1] + "…"
        rows.append(
            f'<text x="{bar_x - label_pad}" y="{cy:.0f}" text-anchor="end" '
            f'font-size="11" fill="#333">{_esc(label)}</text>'
            f'<rect x="{bar_x}" y="{y + 3:.0f}" width="{bar_len:.1f}" height="{bar_h}" '
            f'fill="{bar_color}" rx="2"/>'
            f'<text x="{bar_x + bar_len + 6:.1f}" y="{cy:.0f}" '
            f'font-size="11" fill="#666">{v:.0f}</text>'
        )
    return (
        f'<svg viewBox="0 0 {width} {total_h}" '
        f'xmlns="http://www.w3.org/2000/svg" '
        f'style="width:100%;height:auto;display:block">'
        + "".join(rows) + "</svg>"
    )


def _empty_svg(width: int, height: int) -> str:
    return (
        f'<svg viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" '
        f'style="width:100%;height:auto;display:block">'
        f'<text x="{width / 2}" y="{height / 2 + 5}" text-anchor="middle" '
        f'font-size="12" fill="#bbb">(no data)</text>'
        f'</svg>'
    )
