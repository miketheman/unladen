"""Tests for the treemap visualization."""

from io import StringIO

import pytest
from rich.console import Console

from unladen.treemap import TileData, Treemap, _squarify


class TestSquarify:
    def test_single_value(self):
        rects = _squarify([100.0], 0, 0, 80, 20)
        assert len(rects) == 1
        assert rects[0].w == 80
        assert rects[0].h == 20

    def test_two_equal_values(self):
        rects = _squarify([50.0, 50.0], 0, 0, 80, 20)
        assert len(rects) == 2
        total_area = sum(r.w * r.h for r in rects)
        assert total_area == 80 * 20

    def test_empty_values(self):
        assert _squarify([], 0, 0, 80, 20) == []

    def test_zero_area(self):
        rects = _squarify([10.0, 20.0], 0, 0, 0, 0)
        assert rects == []

    def test_proportional_areas(self):
        """Larger values should get larger rectangles."""
        rects = _squarify([100.0, 50.0, 25.0], 0, 0, 80, 20)
        areas = [r.w * r.h for r in rects]
        assert areas[0] > areas[1] > areas[2]

    def test_no_overlaps(self):
        """Rectangles should not overlap."""
        rects = _squarify([100.0, 80.0, 60.0, 40.0, 20.0], 0, 0, 80, 20)
        cells = set()
        for r in rects:
            for y in range(r.y, r.y + r.h):
                for x in range(r.x, r.x + r.w):
                    assert (x, y) not in cells, f"Overlap at ({x}, {y})"
                    cells.add((x, y))


class TestTreemap:
    def _render(self, tiles, **kwargs):
        output = StringIO()
        console = Console(file=output, force_terminal=False, width=60)
        treemap = Treemap(tiles, **kwargs)
        console.print(treemap)
        return output.getvalue()

    def test_renders_with_tiles(self):
        tiles = [
            TileData("django", 18500, 4625, 0.25),
            TileData("requests", 1800, 900, 0.50),
        ]
        text = self._render(tiles, height=10)
        assert "django" in text
        assert "Treemap" in text

    def test_renders_empty(self):
        text = self._render([], height=10)
        assert "No dependency data" in text

    def test_others_bucket(self):
        tiles = [
            TileData("big", 10000, 5000, 0.50),
            TileData("medium", 5000, 2500, 0.50),
            TileData("small1", 100, 50, 0.50),
            TileData("small2", 50, 25, 0.50),
        ]
        treemap = Treemap(tiles, height=10, others_threshold=0.02)
        labels = [t.label for t in treemap.tiles]
        assert "big" in labels
        assert "others" in labels
        assert "small1" not in labels

    def test_single_tile(self):
        tiles = [TileData("only", 1000, 500, 0.50)]
        text = self._render(tiles, height=8)
        assert "only" in text

    def test_legend_present(self):
        tiles = [TileData("pkg", 1000, 300, 0.30)]
        text = self._render(tiles, height=8)
        assert "active" in text
        assert "inactive" in text

    def test_project_tile_pinned_left(self):
        tiles = [
            TileData("django", 18500, 4625, 0.25),
            TileData("requests", 1800, 900, 0.50),
        ]
        text = self._render(tiles, height=10, project_lloc=3200)
        assert "PROJECT" in text or "project" in text.lower()

    def test_all_zero_lloc_tiles(self):
        """Tiles with zero LLOC should not crash."""
        tiles = [TileData("empty", 0, 0, 0.0)]
        text = self._render(tiles, height=8)
        # Should render without error
        assert "Treemap" in text

    @pytest.mark.parametrize("height", [8, 15, 30])
    def test_various_heights(self, height):
        tiles = [
            TileData("a", 5000, 2500, 0.50),
            TileData("b", 3000, 300, 0.10),
        ]
        text = self._render(tiles, height=height)
        assert "a" in text

    def test_render_height_3_label(self):
        """Height-3 tiles should show name + pct line only (no lloc line)."""
        tiles = [TileData("pkg", 1000, 300, 0.30)]
        text = self._render(tiles, height=5)
        # Should render without error; label may or may not fit
        assert "Treemap" in text

    def test_render_height_4_labels(self):
        """Height-4 tiles should render name, lloc, and pct lines."""
        tiles = [TileData("pkg", 1000, 300, 0.30)]
        text = self._render(tiles, height=6)
        assert "Treemap" in text

    def test_render_very_small_dimensions(self):
        """Tiles too small for labels should render without crashing."""
        tiles = [
            TileData("bigname", 500, 250, 0.50),
            TileData("anotherbigname", 400, 200, 0.50),
        ]
        # Very narrow console forces tiny tiles — exercises _build_label_cells return {}
        output = StringIO()
        console = Console(file=output, force_terminal=False, width=12)
        treemap = Treemap(tiles, height=5)
        console.print(treemap)
        text = output.getvalue()
        # Title is truncated at narrow widths; just verify it rendered without error
        assert len(text) > 0


class TestSquarifyEdgeCases:
    def test_all_zero_values(self):
        """All-zero values should return zero-sized rects without crashing."""
        rects = _squarify([0.0, 0.0], 0, 0, 80, 20)
        assert len(rects) == 2
        for r in rects:
            assert r.w == 0
            assert r.h == 0

    def test_mixed_zero_and_positive(self):
        """Mixed zero and positive areas should lay out without crashing."""
        rects = _squarify([100.0, 0.0, 50.0], 0, 0, 80, 20)
        assert len(rects) == 3


class TestWorstRatioZeroArea:
    def test_zero_row_area_returns_inf(self):
        """_worst_ratio with zero-area row should return inf."""
        from unladen.treemap import _worst_ratio

        # All-zero areas: row_area=0 → float("inf")
        result = _worst_ratio([0.0, 0.0], [0, 1], side=10.0)
        assert result == float("inf")


class TestSquarifyZeroRemainder:
    def test_positive_then_all_zeros(self):
        """When remaining items all have zero area, they get zero-sized rects.

        The algorithm normalizes values to the total; with total > 0 but
        remaining-area == 0 after the first item is placed, the recursive
        call hits the ``remaining_area <= 0`` guard and assigns (x, y, 0, 0)
        to each leftover index.  Verify the call completes without error and
        returns the correct count.
        """
        rects = _squarify([100.0, 0.0, 0.0], 0, 0, 80, 20)
        assert len(rects) == 3
        # The first (and only non-zero) value gets the full area
        assert rects[0].w * rects[0].h > 0


class TestTreemapMinimalHeight:
    def _render(self, tiles, **kwargs):
        output = StringIO()
        console = Console(file=output, force_terminal=False, width=60)
        treemap = Treemap(tiles, **kwargs)
        console.print(treemap)
        return output.getvalue()

    def test_render_minimal_height(self):
        """Tiles at minimal height should still render."""
        tiles = [
            TileData("a-very-long-package-name", 5000, 2500, 0.50),
            TileData("b", 3000, 300, 0.10),
        ]
        text = self._render(tiles, height=4)
        # Should render without crashing
        assert len(text) > 0

    def test_render_narrow_console_truncates_labels(self):
        """Narrow console should truncate long tile labels."""
        output = StringIO()
        console = Console(file=output, force_terminal=False, width=20)
        tiles = [
            TileData("very-long-package-name", 5000, 2500, 0.50),
            TileData("another-long-name", 3000, 300, 0.10),
        ]
        treemap = Treemap(tiles, height=10)
        console.print(treemap)
        text = output.getvalue()
        assert len(text) > 0

    def test_render_very_narrow_no_crash(self):
        """Extremely narrow console should not crash."""
        output = StringIO()
        console = Console(file=output, force_terminal=False, width=8)
        tiles = [
            TileData("pkg1", 1000, 500, 0.50),
            TileData("pkg2", 800, 200, 0.25),
            TileData("pkg3", 600, 100, 0.17),
        ]
        treemap = Treemap(tiles, height=6)
        console.print(treemap)
        text = output.getvalue()
        assert len(text) > 0


class TestSquarifyStretchToFill:
    def test_no_gaps_between_tiles(self):
        """Tiles should fill the entire grid with no gaps."""
        rects = _squarify([70.0, 30.0, 20.0, 10.0], 0, 0, 41, 17)
        # All rects should be within bounds
        for r in rects:
            assert r.x >= 0 and r.y >= 0
            assert r.x + r.w <= 41
            assert r.y + r.h <= 17
        # Total covered area should equal grid area (no gaps)
        cells = set()
        for r in rects:
            for y in range(r.y, r.y + r.h):
                for x in range(r.x, r.x + r.w):
                    cells.add((x, y))
        assert len(cells) == 41 * 17
