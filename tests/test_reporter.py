"""Tests for Phase 4: The Coconut — Actionable Insights."""

import json
from io import StringIO

import pytest
from rich.console import Console

from unladen.reporter import (
    DepReport,
    Recommendation,
    recommend,
    render_json,
    render_table,
)
from unladen.tracer import HeftResult


class TestRecommend:
    """Test the recommendation logic."""

    @pytest.mark.parametrize(
        ("total", "active", "ratio", "opaque", "expected"),
        [
            (1000, 300, 0.30, 0, Recommendation.KEEP),
            (1000, 251, 0.251, 0, Recommendation.KEEP),
            (1000, 100, 0.10, 0, Recommendation.REVIEW),
            (1000, 51, 0.051, 0, Recommendation.REVIEW),
            (200, 6, 0.03, 0, Recommendation.VENDOR),
            (5000, 10, 0.002, 0, Recommendation.REWRITE),
            (100, 0, 0.005, 0, Recommendation.VENDOR),
            (5000, 0, 0.0, 0, Recommendation.REWRITE),
            (100, 0, 0.0, 0, Recommendation.VENDOR),
            # Native extensions
            (4, 0, 0.0, 1, Recommendation.KEEP_NATIVE),
            # Mixed native with lots of Python is NOT native
            (2000, 500, 0.25, 3, Recommendation.REVIEW),
        ],
        ids=[
            "keep-high",
            "keep-boundary",
            "review-moderate",
            "review-boundary",
            "vendor-low-mass",
            "rewrite-high-mass",
            "vendor-very-low",
            "rewrite-unused-high-mass",
            "vendor-unused-low-mass",
            "native-extension",
            "mixed-native-high-python",
        ],
    )
    def test_recommendation(self, total, active, ratio, opaque, expected):
        heft = HeftResult(
            "pkg",
            total_lloc=total,
            active_lloc=active,
            heft_ratio=ratio,
            opaque_files=opaque,
        )
        assert recommend(heft) == expected


class TestDepReport:
    """Test the DepReport data structure."""

    def test_from_heft_result(self):
        heft = HeftResult("requests", total_lloc=1000, active_lloc=50, heft_ratio=0.05)
        report = DepReport(
            name="requests",
            version="2.31.0",
            import_names=["requests"],
            used_names={"get", "post"},
            heft=heft,
            recommendation=Recommendation.REVIEW,
            import_count=5,
            file_count=3,
        )
        assert report.name == "requests"
        assert report.heft_pct == "5.0%"
        assert report.imports_display == "2"
        assert report.file_count == 3

    def test_unused_report(self):
        report = DepReport(
            name="click",
            version="8.1.7",
            import_names=["click"],
            used_names=set(),
            heft=None,
            recommendation=Recommendation.REWRITE,
            installed=True,
        )
        assert report.heft_pct == "-"
        assert report.imports_display == "-"

    def test_name_display_same_as_dist(self):
        report = DepReport(
            name="requests",
            version="2.31.0",
            import_names=["requests"],
            used_names={"get"},
            heft=None,
            recommendation=Recommendation.KEEP,
            matched_import_names=["requests"],
        )
        assert report.name_display == "requests"

    def test_name_display_different_import(self):
        report = DepReport(
            name="setuptools",
            version="81.0.0",
            import_names=["setuptools", "pkg_resources"],
            used_names={"get_distribution"},
            heft=None,
            recommendation=Recommendation.REWRITE,
            matched_import_names=["pkg_resources"],
        )
        assert report.name_display == "setuptools (pkg_resources)"

    def test_name_display_multiple_imports(self):
        report = DepReport(
            name="setuptools",
            version="81.0.0",
            import_names=["setuptools", "pkg_resources"],
            used_names={"get_distribution", "setup"},
            heft=None,
            recommendation=Recommendation.REVIEW,
            matched_import_names=["setuptools", "pkg_resources"],
        )
        assert report.name_display == "setuptools (pkg_resources)"

    def test_name_display_namespace_prefix_suppressed(self):
        report = DepReport(
            name="zope-interface",
            version="8.2",
            import_names=["zope"],
            used_names={"Interface"},
            heft=None,
            recommendation=Recommendation.KEEP,
            matched_import_names=["zope"],
        )
        assert report.name_display == "zope-interface"

    def test_name_display_no_matched(self):
        report = DepReport(
            name="click",
            version="8.1.7",
            import_names=["click"],
            used_names=set(),
            heft=None,
            recommendation=Recommendation.VENDOR,
        )
        assert report.name_display == "click"

    def test_native_report_display(self):
        heft = HeftResult(
            "nh3",
            total_lloc=4,
            active_lloc=0,
            heft_ratio=0.0,
            opaque_files=1,
        )
        report = DepReport(
            name="nh3",
            version="0.3.2",
            import_names=["nh3"],
            used_names={"clean"},
            heft=heft,
            recommendation=Recommendation.KEEP_NATIVE,
            import_count=1,
            file_count=1,
            matched_import_names=["nh3"],
        )
        assert report.heft_pct == "n/a"
        assert report.lloc_display == "1 extension"
        assert report.is_native

    def test_not_installed_report(self):
        report = DepReport(
            name="missing",
            version=None,
            import_names=[],
            used_names=set(),
            heft=None,
            recommendation=None,
            installed=False,
        )
        assert report.status == "not installed"

    def test_lloc_display_multiple_native_extensions(self):
        """Plural 'extensions' when opaque_files > 1."""
        heft = HeftResult(
            "pkg",
            total_lloc=10,
            active_lloc=0,
            heft_ratio=0.0,
            opaque_files=3,
        )
        report = DepReport(
            name="pkg",
            version="1.0",
            import_names=["pkg"],
            used_names={"func"},
            heft=heft,
            recommendation=Recommendation.KEEP_NATIVE,
        )
        assert report.lloc_display == "3 extensions"

    def test_imports_display_string_refs(self):
        """String ref count shown when no used_names."""
        report = DepReport(
            name="whitenoise",
            version="6.0",
            import_names=["whitenoise"],
            used_names=set(),
            heft=None,
            recommendation=Recommendation.REVIEW,
            string_ref_count=2,
        )
        assert report.imports_display == "2 refs"

    def test_imports_display_single_string_ref(self):
        """Singular 'ref' for single string reference."""
        report = DepReport(
            name="whitenoise",
            version="6.0",
            import_names=["whitenoise"],
            used_names=set(),
            heft=None,
            recommendation=Recommendation.REVIEW,
            string_ref_count=1,
        )
        assert report.imports_display == "1 ref"

    def test_rec_display_none(self):
        """None recommendation shows dash."""
        report = DepReport(
            name="pkg",
            version=None,
            import_names=[],
            used_names=set(),
            heft=None,
            recommendation=None,
            installed=False,
        )
        assert report.rec_display == "-"
        assert report.rec_style == "dim"

    def test_lloc_display_no_heft(self):
        """lloc_display returns '-' when heft is None."""
        report = DepReport(
            name="unused",
            version="1.0",
            import_names=["unused"],
            used_names=set(),
            heft=None,
            recommendation=Recommendation.REMOVE,
        )
        assert report.lloc_display == "-"

    def test_remove_recommendation(self):
        """REMOVE recommendation for installed but unused dep."""
        report = DepReport(
            name="unused",
            version="1.0",
            import_names=["unused"],
            used_names=set(),
            heft=None,
            recommendation=Recommendation.REMOVE,
        )
        assert report.rec_display == "Remove?"
        assert report.rec_style == "magenta"
        assert report.status == "unused"


class TestRenderTable:
    """Test the rich table rendering."""

    def test_renders_with_reports(self):
        heft = HeftResult("requests", total_lloc=1000, active_lloc=300, heft_ratio=0.30)
        reports = [
            DepReport(
                name="requests",
                version="2.31.0",
                import_names=["requests"],
                used_names={"get", "post"},
                heft=heft,
                recommendation=Recommendation.KEEP,
                import_count=5,
                file_count=3,
            ),
        ]
        output = StringIO()
        render_table(reports, console=Console(file=output, force_terminal=False))
        text = output.getvalue()
        assert "requests" in text
        assert "30.0%" in text
        assert "Keep" in text
        assert "2" in text  # distinct names used
        assert "3" in text  # file count

    def test_renders_unused_dep(self):
        reports = [
            DepReport(
                name="click",
                version="8.1.7",
                import_names=["click"],
                used_names=set(),
                heft=None,
                recommendation=Recommendation.REWRITE,
                installed=True,
            ),
        ]
        output = StringIO()
        render_table(reports, console=Console(file=output, force_terminal=False))
        text = output.getvalue()
        assert "click" in text
        assert "unused" in text.lower() or "Rewrite" in text

    def test_renders_not_installed(self):
        reports = [
            DepReport(
                name="missing",
                version=None,
                import_names=[],
                used_names=set(),
                heft=None,
                recommendation=None,
                installed=False,
            ),
        ]
        output = StringIO()
        render_table(reports, console=Console(file=output, force_terminal=False))
        text = output.getvalue()
        assert "missing" in text
        assert "installed" in text.lower()

    def test_renders_empty_reports(self):
        output = StringIO()
        render_table([], console=Console(file=output, force_terminal=False))
        text = output.getvalue()
        assert "No dependencies" in text

    def test_renders_string_ref_only_dep(self):
        """Dep with string refs but no heft should show string ref row."""
        reports = [
            DepReport(
                name="whitenoise",
                version="6.0",
                import_names=["whitenoise"],
                used_names=set(),
                heft=None,
                recommendation=Recommendation.REVIEW,
                string_ref_count=2,
                file_count=1,
            ),
        ]
        output = StringIO()
        render_table(reports, console=Console(file=output, force_terminal=False))
        text = output.getvalue()
        assert "whitenoise" in text
        assert "string ref" in text.lower()

    def test_renders_multiple_deps_sorted(self):
        heft_a = HeftResult("alpha", total_lloc=100, active_lloc=50, heft_ratio=0.50)
        heft_b = HeftResult("beta", total_lloc=500, active_lloc=5, heft_ratio=0.01)
        reports = [
            DepReport(
                name="beta",
                version="1.0",
                import_names=["beta"],
                used_names={"b_func"},
                heft=heft_b,
                recommendation=Recommendation.VENDOR,
            ),
            DepReport(
                name="alpha",
                version="2.0",
                import_names=["alpha"],
                used_names={"a_func"},
                heft=heft_a,
                recommendation=Recommendation.KEEP,
            ),
        ]
        output = StringIO()
        render_table(reports, console=Console(file=output, force_terminal=False))
        text = output.getvalue()
        # Both should appear
        assert "alpha" in text
        assert "beta" in text

    def test_excluded_footnote(self):
        """Footnote should appear when excluded_count > 0."""
        heft = HeftResult(total_lloc=100, active_lloc=50, heft_ratio=0.5, dep_name="x")
        reports = [
            DepReport(
                name="requests",
                version="2.31",
                import_names=["requests"],
                used_names={"get"},
                heft=heft,
                recommendation=Recommendation.KEEP,
            )
        ]
        output = StringIO()
        render_table(
            reports,
            console=Console(file=output, force_terminal=False),
            excluded_count=3,
        )
        text = output.getvalue()
        assert "3 dependencies excluded" in text

    def test_no_excluded_footnote_when_zero(self):
        """No footnote when excluded_count is 0."""
        heft = HeftResult(total_lloc=100, active_lloc=50, heft_ratio=0.5, dep_name="x")
        reports = [
            DepReport(
                name="requests",
                version="2.31",
                import_names=["requests"],
                used_names={"get"},
                heft=heft,
                recommendation=Recommendation.KEEP,
            )
        ]
        output = StringIO()
        render_table(
            reports,
            console=Console(file=output, force_terminal=False),
            excluded_count=0,
        )
        text = output.getvalue()
        assert "excluded" not in text.lower()

    def test_excluded_footnote_singular(self):
        """Footnote should use singular for 1 dependency."""
        reports = [
            DepReport(
                name="click",
                version="8.0",
                import_names=["click"],
                used_names=set(),
                heft=None,
                recommendation=Recommendation.REMOVE,
                installed=True,
            )
        ]
        output = StringIO()
        render_table(
            reports,
            console=Console(file=output, force_terminal=False),
            excluded_count=1,
        )
        text = output.getvalue()
        assert "1 dependency excluded" in text


class TestToDict:
    """Test DepReport.to_dict() serialization."""

    def test_installed_with_heft(self):
        heft = HeftResult("pkg", total_lloc=1000, active_lloc=300, heft_ratio=0.30)
        report = DepReport(
            name="requests",
            version="2.31.0",
            import_names=["requests"],
            used_names={"get", "post"},
            heft=heft,
            recommendation=Recommendation.KEEP,
            import_count=5,
            file_count=3,
            string_ref_count=1,
        )
        d = report.to_dict()
        assert d["name"] == "requests"
        assert d["version"] == "2.31.0"
        assert d["installed"] is True
        assert d["status"] == "used"
        assert d["used_names"] == ["get", "post"]
        assert d["import_count"] == 5
        assert d["file_count"] == 3
        assert d["string_ref_count"] == 1
        assert d["heft"]["ratio"] == 0.30
        assert d["heft"]["active_lloc"] == 300
        assert d["heft"]["total_lloc"] == 1000
        assert d["heft"]["opaque_files"] == 0
        assert d["recommendation"] == "Keep"

    def test_not_installed(self):
        report = DepReport(
            name="missing",
            version=None,
            import_names=[],
            used_names=set(),
            heft=None,
            recommendation=None,
            installed=False,
        )
        d = report.to_dict()
        assert d["name"] == "missing"
        assert d["installed"] is False
        assert d["status"] == "not installed"
        assert "heft" not in d
        assert "recommendation" not in d

    def test_installed_no_heft(self):
        report = DepReport(
            name="unused",
            version="1.0",
            import_names=["unused"],
            used_names=set(),
            heft=None,
            recommendation=Recommendation.REMOVE,
        )
        d = report.to_dict()
        assert d["heft"] is None
        assert d["recommendation"] == "Remove?"

    def test_roundtrips_through_json(self):
        heft = HeftResult("pkg", total_lloc=500, active_lloc=50, heft_ratio=0.10)
        report = DepReport(
            name="click",
            version="8.1.7",
            import_names=["click"],
            used_names={"echo", "group"},
            heft=heft,
            recommendation=Recommendation.REVIEW,
            import_count=2,
            file_count=1,
        )
        text = json.dumps(report.to_dict())
        parsed = json.loads(text)
        assert parsed["name"] == "click"
        assert parsed["heft"]["ratio"] == 0.10


class TestRenderJson:
    """Test JSON output rendering."""

    def test_basic_output(self):
        heft = HeftResult("pkg", total_lloc=1000, active_lloc=300, heft_ratio=0.30)
        reports = [
            DepReport(
                name="requests",
                version="2.31.0",
                import_names=["requests"],
                used_names={"get"},
                heft=heft,
                recommendation=Recommendation.KEEP,
                import_count=1,
                file_count=1,
            ),
        ]
        text = render_json(reports)
        data = json.loads(text)
        assert len(data["dependencies"]) == 1
        assert data["dependencies"][0]["name"] == "requests"
        assert data["summary"]["total"] == 1
        assert data["summary"]["installed"] == 1
        assert data["summary"]["used"] == 1
        assert data["summary"]["excluded"] == 0
        assert data["summary"]["project_lloc"] == 0

    def test_project_lloc_in_summary(self):
        data = json.loads(render_json([], project_lloc=1234))
        assert data["summary"]["project_lloc"] == 1234

    def test_sorted_output(self):
        reports = [
            DepReport(
                name="zlib",
                version="1.0",
                import_names=["zlib"],
                used_names=set(),
                heft=None,
                recommendation=Recommendation.REMOVE,
            ),
            DepReport(
                name="alpha",
                version="1.0",
                import_names=["alpha"],
                used_names=set(),
                heft=None,
                recommendation=Recommendation.REMOVE,
            ),
        ]
        data = json.loads(render_json(reports))
        names = [d["name"] for d in data["dependencies"]]
        assert names == ["alpha", "zlib"]

    def test_excluded_count(self):
        data = json.loads(render_json([], excluded_count=3))
        assert data["summary"]["excluded"] == 3

    def test_writes_to_file(self, tmp_path):
        reports = [
            DepReport(
                name="pkg",
                version="1.0",
                import_names=["pkg"],
                used_names={"func"},
                heft=None,
                recommendation=Recommendation.REVIEW,
            ),
        ]
        out = tmp_path / "report.json"
        render_json(reports, output=out)
        data = json.loads(out.read_text())
        assert data["dependencies"][0]["name"] == "pkg"

    def test_empty_reports(self):
        data = json.loads(render_json([]))
        assert data["dependencies"] == []
        assert data["summary"]["total"] == 0


class TestStatusWithImportsOnly:
    """A dep referenced by ``import a.b`` with no resolved names is not unused."""

    def test_status_used_with_only_import_count(self):
        report = DepReport(
            name="pkg",
            version="1.0",
            import_names=["pkg"],
            used_names=set(),
            heft=None,
            recommendation=Recommendation.REVIEW,
            import_count=1,
        )
        assert report.status == "used"

    def test_status_unused_without_any_reference(self):
        report = DepReport(
            name="pkg",
            version="1.0",
            import_names=["pkg"],
            used_names=set(),
            heft=None,
            recommendation=Recommendation.REMOVE,
        )
        assert report.status == "unused"


class TestConditionalDeps:
    """Marker-gated absent deps show as 'not applicable', not a problem."""

    def _conditional_report(self):
        return DepReport(
            name="colorama",
            version=None,
            import_names=[],
            used_names=set(),
            heft=None,
            recommendation=None,
            installed=False,
            marker='sys_platform == "win32"',
        )

    def test_status_not_applicable(self):
        assert self._conditional_report().status == "not applicable"

    def test_status_not_installed_without_marker(self):
        report = DepReport(
            name="gone",
            version=None,
            import_names=[],
            used_names=set(),
            heft=None,
            recommendation=None,
            installed=False,
        )
        assert report.status == "not installed"

    def test_table_shows_conditional(self):
        output = StringIO()
        render_table(
            [self._conditional_report()],
            console=Console(file=output, force_terminal=False, width=120),
        )
        text = output.getvalue()
        assert "conditional" in text
        assert "not installed" not in text

    def test_to_dict_includes_marker(self):
        d = self._conditional_report().to_dict()
        assert d["marker"] == 'sys_platform == "win32"'
        assert d["status"] == "not applicable"


class TestRecommendHybridNative:
    """Hybrid native libraries never get Vendor/Rewrite advice."""

    @pytest.mark.parametrize(
        ("total", "active", "ratio", "opaque", "expected"),
        [
            # numpy-shaped: huge Python surface, tiny traced ratio,
            # compiled extensions carry the real mass -> Review, not Rewrite
            (10000, 50, 0.005, 3, Recommendation.REVIEW),
            (2000, 40, 0.02, 1, Recommendation.REVIEW),
            # hybrid with high Python usage stays Keep
            (10000, 3000, 0.30, 3, Recommendation.KEEP),
            # primarily-native unchanged
            (400, 0, 0.0, 2, Recommendation.KEEP_NATIVE),
            # pure Python unchanged: low ratio + high mass still Rewrite
            (10000, 50, 0.005, 0, Recommendation.REWRITE),
        ],
        ids=[
            "hybrid-low-ratio",
            "hybrid-vendor-range",
            "hybrid-high-ratio",
            "primarily-native",
            "pure-python-rewrite",
        ],
    )
    def test_hybrid_recommendations(self, total, active, ratio, opaque, expected):
        heft = HeftResult(
            "pkg",
            total_lloc=total,
            active_lloc=active,
            heft_ratio=ratio,
            opaque_files=opaque,
        )
        assert recommend(heft) == expected


class TestTransitiveRendering:
    """render_transitive_table shares the main table's display rules."""

    def _render(self, td):
        import io

        from rich.console import Console

        from unladen.reporter import render_transitive_table

        buf = io.StringIO()
        console = Console(file=buf, width=120)
        render_transitive_table([td], console=console)
        return buf.getvalue()

    def _transitive_dep(self, heft):
        from unladen.transitive import TransitiveDep

        return TransitiveDep(
            name="somedep",
            version="1.0",
            used_names={"f"},
            via={"parent"},
            depth=1,
            heft=heft,
        )

    def test_native_extension_dep_shows_na(self):
        """A compiled-extension transitive dep must render 'n/a', not
        '0.0%' — same rule as the main table (regression)."""
        heft = HeftResult(
            dep_name="somedep",
            total_lloc=3,
            active_lloc=0,
            heft_ratio=0.0,
            opaque_files=1,
        )
        out = self._render(self._transitive_dep(heft))
        assert "n/a" in out
        assert "1 extension" in out
        assert "0.0%" not in out

    def test_regular_dep_shows_ratio(self):
        heft = HeftResult(
            dep_name="somedep",
            total_lloc=100,
            active_lloc=25,
            heft_ratio=0.25,
            opaque_files=0,
        )
        out = self._render(self._transitive_dep(heft))
        assert "25.0%" in out
        assert "25/100" in out


class TestIsNativeHeft:
    def test_native(self):
        from unladen.reporter import is_native_heft

        heft = HeftResult("x", 10, 0, 0.0, opaque_files=2)
        assert is_native_heft(heft)

    def test_hybrid_large_python_not_native(self):
        from unladen.reporter import is_native_heft

        heft = HeftResult("x", 5000, 100, 0.02, opaque_files=2)
        assert not is_native_heft(heft)

    def test_pure_python_not_native(self):
        from unladen.reporter import is_native_heft

        heft = HeftResult("x", 100, 50, 0.5, opaque_files=0)
        assert not is_native_heft(heft)
