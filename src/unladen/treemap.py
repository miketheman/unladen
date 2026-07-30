"""Treemap visualization of dependency heft using Rich.

Renders a character-cell treemap where each rectangle's area is proportional
to the dependency's total LLOC.
Within each tile the bottom rows are filled with the tile's bright color
(representing active LLOC) and the top rows are dim (inactive LLOC),
like a container being filled from the bottom.

Layout structure::

    ┌──┐┌───────────────┐┌──────────┐
    │  ││               ││          │
    │ P││               ││          │
    │ R││               ││celery    │
    │ O││django         ││820/8200  │
    │ J││4625/18500     ││10% used  │
    │ E││25% used       │└──────────┘
    │ C│└───────────────┘┌──────────┐
    │ T│┌───────────────┐│          │
    │  ││pydantic       ││rich      │
    │  ││2040/6800      ││30% used  │
    └──┘└───────────────┘└──────────┘

The project tile (left, steel blue) is always pinned to the left edge at
full height.
Dependency tiles are laid out in the remaining space using the
squarified treemap algorithm (Bruls, Huizing, van Wijk, 2000).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.color import Color
from rich.segment import Segment
from rich.style import Style

from unladen.reporter import format_lloc

if TYPE_CHECKING:
    from rich.console import Console, ConsoleOptions, RenderResult


# Tableau 10 palette — cycles if more deps than colors.
# Index 0 (steel blue) is reserved for the project tile.
_TILE_COLORS = [
    "#4e79a7",  # steel blue
    "#f28e2b",  # orange
    "#e15759",  # red
    "#76b7b2",  # teal
    "#59a14f",  # green
    "#edc948",  # yellow
    "#b07aa1",  # purple
    "#ff9da7",  # pink
    "#9c755f",  # brown
    "#bab0ac",  # gray
]

# Rows reserved for title + blank + legend + blank
_CHROME_ROWS = 4


def _scale_color(hex_color: str, factor: float) -> str:
    """Scale a hex color's RGB channels by *factor*.

    Used to derive dim (factor=1/3) and label-background (factor=3/5)
    variants from each tile's base color.
    """
    color = Color.parse(hex_color)
    r, g, b = color.get_truecolor()
    return f"#{int(r * factor):02x}{int(g * factor):02x}{int(b * factor):02x}"


@dataclass
class TileData:
    """Data for a single treemap tile.

    Provided by the caller (CLI) for each dependency, plus an optional
    synthetic "project" tile for the local codebase.
    """

    label: str
    total_lloc: int
    active_lloc: int
    heft_ratio: float  # active_lloc / total_lloc


@dataclass
class _Rect:
    """A positioned rectangle in character-cell coordinates.

    Origin (x=0, y=0) is the top-left of the treemap grid.
    """

    x: int
    y: int
    w: int
    h: int


# ---------------------------------------------------------------------------
# Squarified treemap layout
#
# The algorithm works by greedily packing items into rows (horizontal or
# vertical strips), choosing the orientation that keeps aspect ratios
# closest to 1:1.  Items are processed largest-first but the output
# preserves the original input order via an index mapping.
# ---------------------------------------------------------------------------


def _squarify(
    values: list[float],
    x: int,
    y: int,
    w: int,
    h: int,
) -> list[_Rect]:
    """Lay out rectangles using the squarified treemap algorithm.

    Returns a list of _Rect in the **same order** as *values* — the
    internal sort-by-size is only used for layout decisions, not output.
    """
    if not values or w <= 0 or h <= 0:
        return []

    total = sum(values)
    if total <= 0:
        return [_Rect(x, y, 0, 0) for _ in values]

    # Normalize values into character-cell areas
    area = w * h
    normalized = [v / total * area for v in values]

    # rects is indexed by original position; filled in by _layout_recursive
    rects: list[_Rect | None] = [None] * len(values)
    indices = list(range(len(values)))
    indices.sort(key=lambda i: normalized[i], reverse=True)

    _layout_recursive(normalized, indices, rects, x, y, w, h)

    return [r if r is not None else _Rect(x, y, 0, 0) for r in rects]


def _worst_ratio(areas: list[float], indices: list[int], side: float) -> float:
    """Compute worst aspect ratio for a row of items along *side*.

    Lower is better — a ratio of 1.0 means a perfect square.
    Used to decide when adding another item to a row would make the
    layout worse.
    """
    row_area = sum(areas[i] for i in indices)
    if row_area <= 0 or side <= 0:
        return float("inf")
    worst = 0.0
    for i in indices:
        # Each item spans the full row depth (`span = row_area / side`)
        # and gets a width proportional to its share (`item_size`).
        item_size = areas[i] * side / row_area
        span = row_area / side
        if item_size > 0 and span > 0:
            ratio = max(span / item_size, item_size / span)
            worst = max(worst, ratio)
    return worst


def _layout_recursive(
    areas: list[float],
    indices: list[int],
    rects: list[_Rect | None],
    x: int,
    y: int,
    w: int,
    h: int,
) -> None:
    """Recursively lay out tiles using the squarified algorithm.

    Splits the remaining space into a "row" (a strip of tiles) and a
    "rest" region, then recurses on the rest.  Chooses horizontal vs
    vertical strips based on which dimension is larger.
    """
    if not indices or w <= 0 or h <= 0:
        return

    if len(indices) == 1:
        rects[indices[0]] = _Rect(x, y, max(w, 1), max(h, 1))
        return

    remaining_area = sum(areas[i] for i in indices)
    if remaining_area <= 0:
        for i in indices:
            rects[i] = _Rect(x, y, 0, 0)
        return

    # Split along the longer axis for squarer tiles
    horizontal = w >= h
    side = float(h if horizontal else w)

    # Greedily add items to a row until aspect ratio worsens
    row: list[int] = [indices[0]]
    for k in range(1, len(indices)):
        candidate = [*row, indices[k]]
        if _worst_ratio(areas, candidate, side) <= _worst_ratio(areas, row, side):
            row.append(indices[k])
        else:
            break

    row_area = sum(areas[i] for i in row)

    if horizontal:
        # Row occupies a vertical strip on the left
        row_w = round(row_area / remaining_area * w) if remaining_area > 0 else 0
        row_w = max(min(row_w, w), 1)
        cy = y
        for ri in row:
            frac = areas[ri] / row_area if row_area > 0 else 0
            item_h = round(frac * h)
            item_h = max(item_h, 1)
            item_h = min(item_h, y + h - cy)
            rects[ri] = _Rect(x, cy, row_w, item_h)
            cy += item_h
        # Stretch the last item to fill any rounding gap
        if row:
            last = rects[row[-1]]
            assert last is not None
            rects[row[-1]] = _Rect(last.x, last.y, row_w, max(1, y + h - last.y))
        rest = [i for i in indices if i not in row]
        _layout_recursive(areas, rest, rects, x + row_w, y, w - row_w, h)
    else:
        # Row occupies a horizontal strip on the top
        row_h = round(row_area / remaining_area * h) if remaining_area > 0 else 0
        row_h = max(min(row_h, h), 1)
        cx = x
        for ri in row:
            frac = areas[ri] / row_area if row_area > 0 else 0
            item_w = round(frac * w)
            item_w = max(item_w, 1)
            item_w = min(item_w, x + w - cx)
            rects[ri] = _Rect(cx, y, item_w, row_h)
            cx += item_w
        # Stretch the last item to fill any rounding gap
        if row:
            last = rects[row[-1]]
            assert last is not None
            rects[row[-1]] = _Rect(last.x, last.y, max(1, x + w - last.x), row_h)
        rest = [i for i in indices if i not in row]
        _layout_recursive(areas, rest, rects, x, y + row_h, w, h - row_h)


# ---------------------------------------------------------------------------
# Label rendering
# ---------------------------------------------------------------------------


def _build_label_cells(
    tile: TileData, w: int, h: int, *, vertical: bool = False
) -> dict[tuple[int, int], str]:
    """Build a sparse grid of label characters for a tile.

    Returns {(dy, dx): char} in tile-local coordinates (not global grid).
    Coordinates include the border rows/columns so callers can look up
    any (dy, dx) directly.

    *vertical*: one character per row, vertically centered (project tile).
    *horizontal*: up to 3 lines (name, LLOC, percent) bottom-aligned
    against the active fill region so the label sits on bright color.
    """
    # Interior = area inside the box-drawing border (1 cell each side)
    interior_h = h - 2
    interior_w = w - 2
    if interior_h < 1 or interior_w < 1:
        return {}

    if vertical:
        text = tile.label.upper()
        chars = list(text[:interior_h])
        start_dy = 1 + (interior_h - len(chars)) // 2
        center_dx = 1 + interior_w // 2
        return {(start_dy + i, center_dx): ch for i, ch in enumerate(chars)}

    # -- Horizontal label (dependency tiles) --
    usable = interior_w
    if usable < 3 or h < 3:
        return {}

    # Truncate long names with ellipsis, leaving 1 char padding
    name = tile.label
    if len(name) > usable - 1:
        name = name[: usable - 2] + "\u2026"

    # Build up to 3 lines depending on available height:
    #   h >= 4: name, active/total, NN% used
    #   h >= 3: name, NN% used
    #   h <  3: (no label — handled by early return above)
    lines: list[str] = [name]
    if h >= 4:
        lloc = format_lloc(tile.active_lloc, tile.total_lloc)
        pct = f"{tile.heft_ratio * 100:.0f}% used"
        if len(lloc) <= usable - 1:
            lines.append(lloc)
        if len(pct) <= usable - 1:
            lines.append(pct)
    elif h >= 3:
        pct = f"{tile.heft_ratio * 100:.0f}% used"
        if len(pct) <= usable - 1:
            lines.append(pct)

    lines = lines[:interior_h]

    # Bottom-align: place lines just above the bottom border
    start_dy = h - 2 - len(lines) + 1
    cells: dict[tuple[int, int], str] = {}
    for row_i, line in enumerate(lines):
        dy = start_dy + row_i
        # Fill the entire interior width so the label background spans
        # the full row, not just the text characters.
        for dx in range(1, w - 1):
            char_idx = dx - 1
            ch = line[char_idx] if char_idx < len(line) else " "
            cells[(dy, dx)] = ch
    return cells


def _border_char(dy: int, dx: int, w: int, h: int) -> str | None:
    """Return the box-drawing character for a cell, or None if interior.

    Each tile is fully enclosed::

        ┌────┐
        │    │
        └────┘
    """
    on_top = dy == 0
    on_bottom = dy == h - 1
    on_left = dx == 0
    on_right = dx == w - 1

    if on_top and on_left:
        return "\u250c"  # ┌
    if on_top and on_right:
        return "\u2510"  # ┐
    if on_bottom and on_left:
        return "\u2514"  # └
    if on_bottom and on_right:
        return "\u2518"  # ┘
    if on_top or on_bottom:
        return "\u2500"  # ─
    if on_left or on_right:
        return "\u2502"  # │
    return None


def _terminal_height() -> int:
    """Get terminal height, with a sensible default.

    Falls back to 24 when stdout is not a TTY (e.g. piped output,
    CI environments, or Rich's ``Console(file=StringIO())``).
    """
    try:
        return os.get_terminal_size().lines
    except OSError:
        return 24


class Treemap:
    """A Rich renderable that draws a treemap of dependency heft.

    Each tile's area is proportional to total LLOC.
    Within each tile, the bottom rows are filled with a bright color
    (active LLOC) and the top rows are dim (inactive),
    like a container being filled from the bottom.

    The project tile (local source LLOC) is always pinned to the left
    edge at full height with a vertical "PROJECT" label.
    Dependency tiles are squarified in the remaining space.
    Small dependencies below *others_threshold* are bucketed into a
    single "others" tile to avoid visual clutter.

    Implements the ``__rich_console__`` protocol so it can be passed
    directly to ``Console.print()``.

    Usage::

        from rich.console import Console
        treemap = Treemap(tiles, project_lloc=1200)
        Console().print(treemap)
    """

    def __init__(
        self,
        tiles: list[TileData],
        *,
        project_lloc: int = 0,
        height: int | None = None,
        others_threshold: float = 0.015,
        title: str = "LLOC Treemap",
    ) -> None:
        self.title = title
        self.others_threshold = others_threshold

        # Auto-size: use terminal height minus chrome, clamp to [8, 40]
        if height is None:
            height = max(8, min(40, _terminal_height() - _CHROME_ROWS))
        self.height = height

        # Project tile is pinned to the left column and always gets
        # index 0 in self.tiles (steel blue from _TILE_COLORS[0]).
        # heft_ratio=1.0 because all project code is "active" by definition.
        self._project_tile: TileData | None = None
        if project_lloc > 0:
            self._project_tile = TileData(
                label="project",
                total_lloc=project_lloc,
                active_lloc=project_lloc,
                heft_ratio=1.0,
            )

        # Bucket dependencies below the threshold into "others".
        # The threshold is relative to total dep LLOC (not including
        # the project), and only applies when there are more than 3
        # deps — with fewer, every tile is worth showing individually.
        self._dep_tiles: list[TileData] = []
        total = sum(t.total_lloc for t in tiles)
        others_total = 0
        others_active = 0

        for t in sorted(tiles, key=lambda t: t.total_lloc, reverse=True):
            if total > 0 and t.total_lloc / total < others_threshold and len(tiles) > 3:
                others_total += t.total_lloc
                others_active += t.active_lloc
            else:
                self._dep_tiles.append(t)

        if others_total > 0:
            ratio = others_active / others_total if others_total else 0.0
            self._dep_tiles.append(
                TileData(
                    label="others",
                    total_lloc=others_total,
                    active_lloc=others_active,
                    heft_ratio=ratio,
                )
            )

        # Combined list for rendering (project first, then deps).
        # This ordering determines color assignment from _TILE_COLORS.
        self.tiles: list[TileData] = []
        if self._project_tile is not None:
            self.tiles.append(self._project_tile)
        self.tiles.extend(self._dep_tiles)

    def _compute_layout(self, width: int, height: int) -> list[_Rect]:
        """Compute tile rectangles.

        The project tile is not part of the squarified layout — it gets
        a fixed left column sized proportionally to its LLOC share, with
        a minimum width of 4 (enough for a 2-char interior + borders).
        Dependencies fill the remaining space via ``_squarify``.
        """
        if self._project_tile is not None:
            total_lloc = sum(t.total_lloc for t in self.tiles)
            proj_frac = self._project_tile.total_lloc / total_lloc if total_lloc else 0
            proj_w = max(4, round(proj_frac * width))
            proj_w = min(proj_w, width - 4)  # leave room for deps
            proj_rect = _Rect(0, 0, proj_w, height)

            dep_values = [float(t.total_lloc) for t in self._dep_tiles]
            dep_rects = _squarify(dep_values, proj_w, 0, width - proj_w, height)
            return [proj_rect, *dep_rects]

        values = [float(t.total_lloc) for t in self.tiles]
        return _squarify(values, 0, 0, width, height)

    def _render_tile(
        self,
        idx: int,
        tile: TileData,
        rect: _Rect,
        grid: list[list[tuple[str, Style]]],
        width: int,
        height: int,
    ) -> None:
        """Render a single tile into the character grid.

        Each tile has 5 style variants derived from its base color:

        - **active**: bright fill for the "used" portion (bottom rows)
        - **inactive**: dim fill for the "unused" portion (top rows)
        - **label**: mid-brightness background behind label text
        - **border_active / border_inactive**: border chars inherit the
          fill color of their row so borders blend with the fill.

        The active/inactive split is computed from ``heft_ratio``:
        interior rows at or below ``split_dy`` get the active style.
        """
        color = _TILE_COLORS[idx % len(_TILE_COLORS)]
        dim = _scale_color(color, 1 / 3)
        lbl_bg = _scale_color(color, 3 / 5)
        active_style = Style(color="white", bgcolor=color, bold=True)
        inactive_style = Style(color="grey62", bgcolor=dim)
        label_style = Style(color="white", bgcolor=lbl_bg, bold=True)
        border_active = Style(color="grey78", bgcolor=color)
        border_inactive = Style(color="grey42", bgcolor=dim)

        # Bottom-to-top fill: the bottom N interior rows are "active"
        # (bright) and the rest are "inactive" (dim).
        interior_h = max(rect.h - 2, 0)
        active_rows = min(round(interior_h * tile.heft_ratio), interior_h)
        # split_dy is the first active row (in tile-local dy coords).
        # Rows 0 and rect.h-1 are borders, 1..split_dy-1 are inactive,
        # split_dy..rect.h-2 are active.
        split_dy = rect.h - 1 - active_rows

        is_project = idx == 0 and self._project_tile is not None
        label_cells = _build_label_cells(tile, rect.w, rect.h, vertical=is_project)

        for dy in range(rect.h):
            for dx in range(rect.w):
                gx, gy = rect.x + dx, rect.y + dy
                if gx >= width or gy >= height:
                    continue

                # Interior rows between the borders; active = below split
                is_active = 1 <= dy < rect.h - 1 and dy >= split_dy
                in_label = (dy, dx) in label_cells

                if in_label:
                    base_style = label_style
                elif is_active:
                    base_style = active_style
                else:
                    base_style = inactive_style

                # Border characters override fill; their background
                # matches the active/inactive region they sit in.
                bch = _border_char(dy, dx, rect.w, rect.h)
                if bch is not None:
                    bdr_style = border_active if is_active else border_inactive
                    grid[gy][gx] = (bch, bdr_style)
                else:
                    ch = label_cells.get((dy, dx), " ")
                    grid[gy][gx] = (ch, base_style)

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        width = options.max_width
        height = self.height

        if not self.tiles:
            yield Segment("No dependency data to visualize.", Style(dim=True))
            yield Segment.line()
            return

        # Title — centered with padding
        title = f" {self.title} "
        pad = max(0, (width - len(title)) // 2)
        yield Segment(" " * pad)
        yield Segment(title, Style(bold=True))
        yield Segment.line()

        # Build a character grid, then yield it row-by-row as Segments.
        # Using a grid (rather than yielding per-tile) means tiles can
        # overlap at shared edges without artifacts.
        rects = self._compute_layout(width, height)
        default = (" ", Style())
        grid: list[list[tuple[str, Style]]] = [[default] * width for _ in range(height)]

        for idx, (tile, rect) in enumerate(zip(self.tiles, rects)):
            if rect.w > 0 and rect.h > 0:
                self._render_tile(idx, tile, rect, grid, width, height)

        for row in grid:
            for ch, style in row:
                yield Segment(ch, style)
            yield Segment.line()

        # Legend — show project swatch, then bright/dim pairs from
        # a few actual tile colors so the pattern is clearly universal.
        yield Segment.line()
        dim_style = Style(dim=True)
        yield Segment(" \u2588\u2588", Style(color=_TILE_COLORS[0]))
        yield Segment(" project  ", dim_style)
        # Show bright/dim pairs from 2-3 dep colors to convey the pattern
        sample_colors = _TILE_COLORS[1:4]
        for color in sample_colors:
            yield Segment("\u2588", Style(color=color))
        yield Segment(" active  ", dim_style)
        for color in sample_colors:
            yield Segment("\u2588", Style(color=_scale_color(color, 1 / 3)))
        yield Segment(" inactive", dim_style)
        yield Segment.line()
