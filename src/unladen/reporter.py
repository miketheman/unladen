"""Phase 4: The Coconut — Actionable Insights.

Data structures and logic for dependency analysis results.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from rich.table import Table

if TYPE_CHECKING:
    from pathlib import Path

    from rich.console import Console

from unladen._types import HeftResult

# Mass threshold separating "small" from "large" dependencies.
# Below this, even low-heft deps are cheap enough to vendor.
# Above this, low-heft deps are costly enough to warrant rewriting
# the functionality inline.
MASS_THRESHOLD = 500


class Recommendation(Enum):
    """Dependency recommendation based on heft analysis."""

    KEEP = "Keep"
    KEEP_NATIVE = "Keep (native)"
    REVIEW = "Review"
    VENDOR = "Vendor"
    REWRITE = "Rewrite"
    REMOVE = "Remove?"


# Styling for each recommendation
_REC_STYLES = {
    Recommendation.KEEP: "green",
    Recommendation.KEEP_NATIVE: "green",
    Recommendation.REVIEW: "yellow",
    Recommendation.VENDOR: "cyan",
    Recommendation.REWRITE: "red",
    Recommendation.REMOVE: "magenta",
}


@dataclass
class DepReport:
    """Analysis report for a single dependency."""

    name: str
    version: str | None
    import_names: list[str]
    used_names: set[str]
    heft: HeftResult | None
    recommendation: Recommendation | None
    installed: bool = True
    import_count: int = 0  # total import statements referencing this dep
    file_count: int = 0  # number of files that import this dep
    matched_import_names: list[str] | None = None  # import names actually used
    string_ref_count: int = 0  # string references found (e.g. Django settings)

    @property
    def is_native(self) -> bool:
        return self.recommendation is Recommendation.KEEP_NATIVE

    @property
    def heft_pct(self) -> str:
        if self.heft is None:
            return "-"
        if self.is_native:
            return "n/a"
        return f"{self.heft.heft_ratio * 100:.1f}%"

    @property
    def lloc_display(self) -> str:
        if self.heft is None:
            return "-"
        if self.is_native:
            return _pluralize(self.heft.opaque_files, "extension", "extensions")
        return f"{self.heft.active_lloc}/{self.heft.total_lloc}"

    @property
    def status(self) -> str:
        if not self.installed:
            return "not installed"
        if not self.used_names and not self.string_ref_count:
            return "unused"
        return "used"

    @property
    def imports_display(self) -> str:
        """Show count of distinct names used from this dependency."""
        if self.used_names:
            return str(len(self.used_names))
        if self.string_ref_count:
            return _pluralize(self.string_ref_count, "ref")
        return "-"

    @property
    def name_display(self) -> str:
        """Show distribution name with actual import name if different.

        Suppresses import names that are obvious from the distribution name,
        e.g. 'zope' for 'zope-interface', or 'requests' for 'requests'.
        Shows genuinely different names like 'pkg_resources' for 'setuptools'.
        """
        if not self.matched_import_names:
            return self.name
        norm = self.name.replace("-", "_").lower()
        different = [
            n for n in self.matched_import_names if not _is_obvious_import(n, norm)
        ]
        if not different:
            return self.name
        return f"{self.name} ({', '.join(sorted(different))})"

    @property
    def rec_display(self) -> str:
        if self.recommendation is None:
            return "-"
        return self.recommendation.value

    @property
    def rec_style(self) -> str:
        if self.recommendation is None:
            return "dim"
        return _REC_STYLES.get(self.recommendation, "white")

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict suitable for JSON output."""
        d: dict[str, Any] = {
            "name": self.name,
            "version": self.version,
            "installed": self.installed,
            "status": self.status,
        }
        if self.installed:
            d["import_names"] = self.import_names
            d["used_names"] = sorted(self.used_names)
            d["import_count"] = self.import_count
            d["file_count"] = self.file_count
            d["string_ref_count"] = self.string_ref_count
            if self.heft is not None:
                d["heft"] = {
                    "ratio": self.heft.heft_ratio,
                    "active_lloc": self.heft.active_lloc,
                    "total_lloc": self.heft.total_lloc,
                    "opaque_files": self.heft.opaque_files,
                }
            else:
                d["heft"] = None
            d["recommendation"] = (
                self.recommendation.value if self.recommendation else None
            )
        return d


def _pluralize(count: int, singular: str, plural: str | None = None) -> str:
    """Return 'N thing' or 'N things'."""
    if plural is None:
        plural = singular + "s"
    return f"{count} {singular}" if count == 1 else f"{count} {plural}"


def _is_obvious_import(import_name: str, normalized_dist: str) -> bool:
    """Check if an import name is obviously derivable from the distribution name.

    'requests' is obvious for 'requests'.
    'zope' is obvious for 'zope_interface' (it's a prefix component).
    'pkg_resources' is NOT obvious for 'setuptools'.
    """
    norm_import = import_name.replace("-", "_").lower()
    if norm_import == normalized_dist:
        return True
    # Import name is a leading component: 'zope' matches 'zope_interface'
    if normalized_dist.startswith(norm_import + "_"):
        return True
    return False


def recommend(heft: HeftResult) -> Recommendation:
    """Determine a recommendation based on heft metrics.

    Thresholds (v0.1, built-in):
        > 25%        -> Keep
        5-25%        -> Review
        1-5% + Low   -> Vendor
        < 1% + High  -> Rewrite
        < 1% + Low   -> Vendor

    Native extensions (opaque_files > 0, minimal Python) get Keep (native)
    since LLOC-based analysis doesn't apply to compiled code.
    """
    # Primarily native: has compiled extensions with little Python to analyze
    if heft.opaque_files and heft.total_lloc < MASS_THRESHOLD:
        return Recommendation.KEEP_NATIVE

    ratio = heft.heft_ratio
    high_mass = heft.total_lloc >= MASS_THRESHOLD

    if ratio > 0.25:
        return Recommendation.KEEP
    if ratio > 0.05:
        return Recommendation.REVIEW
    if ratio > 0.01:
        return Recommendation.VENDOR
    # < 1%
    if high_mass:
        return Recommendation.REWRITE
    return Recommendation.VENDOR


def render_json(
    reports: list[DepReport],
    *,
    output: Path | None = None,
    excluded_count: int = 0,
    project_lloc: int = 0,
) -> str:
    """Serialize dependency reports as JSON.

    Returns the JSON string.
    When *output* is given, also writes to that file path.
    """
    payload: dict[str, Any] = {
        "dependencies": [r.to_dict() for r in sorted(reports, key=lambda r: r.name)],
        "summary": {
            "total": len(reports),
            "installed": sum(1 for r in reports if r.installed),
            "used": sum(1 for r in reports if r.status == "used"),
            "excluded": excluded_count,
            "project_lloc": project_lloc,
        },
    }
    text = json.dumps(payload, indent=2)
    if output is not None:
        output.write_text(text, encoding="utf-8")
    return text


def render_table(
    reports: list[DepReport],
    *,
    console: Console,
    excluded_count: int = 0,
) -> None:
    """Render the dependency analysis as a rich table."""
    if not reports:
        console.print("[dim]No dependencies to report.[/dim]")
        return

    table = Table(
        title="unladen - Dependency Heft Report",
        show_lines=True,
    )
    table.add_column("Dependency", style="bold")
    table.add_column("Version")
    table.add_column("Names\nUsed", justify="right")
    table.add_column("Files", justify="right")
    table.add_column("Heft %", justify="right")
    table.add_column("LLOC\n(active/total)", justify="right")
    table.add_column("Recommendation")

    for report in sorted(reports, key=lambda r: r.name):
        if not report.installed:
            table.add_row(
                report.name, "-", "-", "-", "-", "-", "[dim]not installed[/dim]"
            )
            continue

        rec_text = f"[{report.rec_style}]{report.rec_display}[/{report.rec_style}]"

        if report.status == "unused":
            table.add_row(
                report.name_display,
                report.version or "-",
                "[dim]-[/dim]",
                "[dim]-[/dim]",
                "-",
                "-",
                rec_text,
            )
            continue

        # String-ref-only deps without heft data
        if report.string_ref_count and report.heft is None:
            table.add_row(
                report.name_display,
                report.version or "-",
                report.imports_display,
                str(report.file_count) if report.file_count else "-",
                "[dim]-[/dim]",
                "[dim]string ref[/dim]",
                rec_text,
            )
            continue

        table.add_row(
            report.name_display,
            report.version or "-",
            report.imports_display,
            str(report.file_count),
            report.heft_pct,
            report.lloc_display,
            rec_text,
        )

    console.print(table)
    if excluded_count:
        s = "y" if excluded_count == 1 else "ies"
        console.print(
            f"[dim]* {excluded_count} dependenc{s} excluded "
            f"via \\[tool.unladen] config[/dim]"
        )
