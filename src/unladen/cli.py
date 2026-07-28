"""CLI entry point for unladen.

Orchestrates the four analysis phases and renders results.
Heavy imports (collector, inspector, tracer, reporter) are deferred to
the command functions to keep ``unladen --help`` fast.
"""

import argparse
import os
import site
import sys
from pathlib import Path

from unladen._config import load_dep_map, load_exclude_set


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="unladen",
        description="Measure the logical mass of your Python dependencies.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_get_version()}",
    )

    subparsers = parser.add_subparsers(dest="command")

    # unladen check
    check_parser = subparsers.add_parser(
        "check",
        help="Analyze dependency usage in a project.",
    )
    check_parser.add_argument(
        "target",
        default=".",
        nargs="?",
        help="Project directory or installed package name (default: cwd).",
    )
    check_parser.add_argument(
        "--site-packages",
        type=Path,
        default=None,
        help="Explicit path to site-packages directory.",
    )
    check_parser.add_argument(
        "-r",
        "--requirements",
        default=None,
        help="Explicit requirements file to read dependencies from.",
    )
    check_parser.add_argument(
        "--treemap",
        action="store_true",
        help="Show a treemap visualization of dependency LLOC.",
    )
    check_parser.add_argument(
        "--transitive",
        action="store_true",
        help="Trace usage through transitive dependencies (experimental).",
    )
    check_parser.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        dest="output_format",
        help="Output format (default: table).",
    )
    # unladen show
    show_parser = subparsers.add_parser(
        "show",
        help="Show detailed usage of a specific dependency.",
    )
    show_parser.add_argument(
        "dep",
        help="Dependency name to inspect (e.g. 'setuptools').",
    )
    show_parser.add_argument(
        "path",
        type=Path,
        default=Path("."),
        nargs="?",
        help="Path to the project root (default: current directory).",
    )
    show_parser.add_argument(
        "--site-packages",
        type=Path,
        default=None,
        help="Explicit path to site-packages directory.",
    )
    show_parser.add_argument(
        "-r",
        "--requirements",
        default=None,
        help="Explicit requirements file to read dependencies from.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    match args.command:
        case "check":
            return _cmd_check(args)
        case "show":
            from unladen._cmd_show import cmd_show

            return cmd_show(args)
        case _:
            parser.print_help()
            return 0


def _cmd_check(args: argparse.Namespace) -> int:
    """Run dependency heft analysis.

    Supports two modes:
    - **Project mode**: ``unladen check /path/to/project``
    - **Package mode**: ``unladen check requests``
    """
    project_path, req_file, package_name = _resolve_check_target(args)
    if project_path is None and package_name is None:
        return 1  # error already printed

    if package_name:
        return _cmd_check_package(args, package_name)
    return _cmd_check_project(args, project_path, req_file)


def _cmd_check_project(args, project_path, req_file) -> int:
    """Check a project directory's dependency usage."""
    from unladen.collector import _normalize_dep_name
    from unladen.inspector import find_project_source, inspect_source_files_counted
    from unladen.reporter import render_json, render_table

    dep_map = load_dep_map(project_path, req_file, args.site_packages)
    if dep_map is None:
        return 1
    if not dep_map:
        print("No dependencies found.")
        return 0

    exclude = load_exclude_set(project_path)
    excluded = {name for name in dep_map if _normalize_dep_name(name) in exclude}
    for name in excluded:
        del dep_map[name]
    if not dep_map:
        print("No dependencies found (all excluded).")
        return 0

    all_import_names: set[str] = set()
    for info in dep_map.values():
        all_import_names.update(info["import_names"])

    project_source = find_project_source(project_path)
    if not project_source:
        print(
            f"Warning: no Python source files found under {project_path}; "
            "all dependencies will appear unused.",
            file=sys.stderr,
        )
    usage, project_lloc = inspect_source_files_counted(project_source, all_import_names)
    dep_summaries, hefts = _analyze_deps(dep_map, usage)

    reports = _build_reports(dep_map, dep_summaries, hefts)

    transitive = None
    if args.transitive:
        from unladen.collector import discover_site_packages

        sp = args.site_packages or discover_site_packages(project_path)
        # Pass the exclusions so excluded deps can't reappear as
        # transitive deps of a kept dependency.
        transitive = _trace_transitive_deps(dep_map, dep_summaries, sp, exclude)

    if args.output_format == "json":
        print(
            render_json(
                reports,
                excluded_count=len(excluded),
                project_lloc=project_lloc,
                transitive=transitive,
            )
        )
        return 0

    from rich.console import Console

    console = Console()
    render_table(reports, console=console, excluded_count=len(excluded))

    if transitive is not None:
        _render_transitive(console, transitive)

    if args.treemap:
        _render_treemap_from_reports(
            console, reports, project_lloc, _treemap_title(len(excluded))
        )

    return 0


def _cmd_check_package(args, package_name) -> int:
    """Check an installed package's dependency usage."""

    from unladen.collector import collect_package_deps, resolve_package_info
    from unladen.inspector import inspect_source_files_counted
    from unladen.reporter import render_json, render_table

    sp = args.site_packages or _discover_site_packages()
    if sp is None:
        print(
            "Error: could not find site-packages. Use --site-packages to specify one.",
            file=sys.stderr,
        )
        return 1

    try:
        version, import_names, source_paths = resolve_package_info(package_name, sp)
    except FileNotFoundError:
        print(
            f"Error: package '{package_name}' not found in {sp}",
            file=sys.stderr,
        )
        return 1

    dep_map = collect_package_deps(package_name, sp)
    if not dep_map:
        print(f"{package_name} v{version or '?'} has no declared dependencies.")
        return 0

    # Collect .py files from the target package's source
    source_files = _collect_py_files(source_paths)

    all_import_names: set[str] = set()
    for info in dep_map.values():
        all_import_names.update(info["import_names"])

    usage, project_lloc = inspect_source_files_counted(source_files, all_import_names)
    dep_summaries, hefts = _analyze_deps(dep_map, usage)

    reports = _build_reports(dep_map, dep_summaries, hefts)

    transitive = None
    if args.transitive:
        from unladen.collector import _normalize_dep_name

        # Exclude the target itself so a dependency cycle back to it
        # can't list the analyzed package as its own transitive dep.
        transitive = _trace_transitive_deps(
            dep_map, dep_summaries, sp, {_normalize_dep_name(package_name)}
        )

    if args.output_format == "json":
        print(render_json(reports, project_lloc=project_lloc, transitive=transitive))
        return 0

    from rich.console import Console

    console = Console()
    console.print(
        f"\n[bold]{package_name}[/bold] v{version or '?'} "
        f"— {len(dep_map)} dependencies\n"
    )
    render_table(reports, console=console)

    if transitive is not None:
        _render_transitive(console, transitive)

    if args.treemap:
        _render_treemap_from_reports(console, reports, project_lloc)

    return 0


def _analyze_deps(dep_map, usage):
    """Bridge Phase 2 (inspector) to Phase 3 (tracer).

    Merges per-import-name usage into per-dep summaries,
    then computes heft ratios for all used deps in a single bulk pass.
    """
    from unladen.merger import DepUsageSummary, merge_dep_usage
    from unladen.tracer import compute_hefts_bulk

    dep_summaries: dict[str, DepUsageSummary] = {}
    heft_work: list[tuple[str, list, set[str]]] = []

    for name, info in dep_map.items():
        summary = merge_dep_usage(info["import_names"], usage, info["paths"])
        dep_summaries[name] = summary
        if summary.is_used and info["paths"] and summary.used_names:
            heft_work.append((name, info["paths"], summary.used_names))

    hefts = compute_hefts_bulk(heft_work)
    return dep_summaries, hefts


def _trace_transitive_deps(dep_map, dep_summaries, site_packages, exclude=frozenset()):
    """Bridge Phase 2.5 output to transitive tracing (``--transitive``).

    Returns None when site-packages could not be resolved (with a
    stderr note), so a skipped analysis stays distinguishable from a
    genuine empty result — the JSON payload omits the "transitive" key
    instead of emitting an empty list.
    """
    if site_packages is None:
        print(
            "Warning: could not resolve site-packages; skipping transitive analysis.",
            file=sys.stderr,
        )
        return None

    from unladen.transitive import trace_transitive

    used_map = {
        name: summary.used_names
        for name, summary in dep_summaries.items()
        if summary.used_names
    }
    return trace_transitive(dep_map, used_map, site_packages, exclude=exclude)


def _render_transitive(console, transitive) -> None:
    """Render the transitive table after the main report."""
    from unladen.reporter import render_transitive_table

    console.print()
    render_transitive_table(transitive, console=console)


def _discover_site_packages() -> Path | None:
    """Find site-packages without a project directory.

    Fallback chain for package mode (no project dir to search for .venv):
    1. VIRTUAL_ENV env var — active venv, most likely correct.
    2. ``site.getsitepackages()`` — system-level site-packages.
    """
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        from unladen.collector import _find_site_packages_in_venv

        sp = _find_site_packages_in_venv(Path(venv))
        if sp:
            return sp
    for p in site.getsitepackages():
        pp = Path(p)
        if pp.is_dir():
            return pp
    return None


def _collect_py_files(source_paths: list[Path]) -> list[Path]:
    """Collect .py files from installed package source paths.

    Skips ``__pycache__`` directories which contain only bytecode.
    """
    py_files: list[Path] = []
    for path in source_paths:
        if path.is_file() and path.suffix == ".py":
            py_files.append(path)
        elif path.is_dir():
            for root, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for f in files:
                    if f.endswith(".py"):
                        py_files.append(Path(root, f))
    return py_files


def _build_reports(dep_map, dep_summaries, hefts):
    """Build DepReport objects from analysis results."""
    from unladen.reporter import DepReport

    reports = []
    for name, info in dep_map.items():
        summary = dep_summaries[name]
        heft, rec = _classify_dep(name, info, summary, hefts)
        reports.append(
            DepReport(
                name=name,
                version=info["version"],
                import_names=info["import_names"],
                used_names=summary.used_names,
                import_count=summary.import_count,
                file_count=summary.file_count,
                heft=heft,
                recommendation=rec,
                installed=info["installed"],
                matched_import_names=summary.matched_import_names,
                string_ref_count=len(summary.string_refs),
                marker=info.get("marker"),
            )
        )
    return reports


def _classify_dep(name, info, summary, hefts):
    """Classify a dependency into one of four states.

    Returns (heft, recommendation) based on:
    1. Heft computed -> use the heft ratio to recommend.
    2. Used but no heft (no source paths or no used names) -> REVIEW.
    3. Installed but not used -> REMOVE.
    4. Not installed -> no recommendation (None, None).
    """
    from unladen.reporter import Recommendation, recommend

    if name in hefts:
        return hefts[name], recommend(hefts[name])
    if summary.is_used and info["installed"]:
        return None, Recommendation.REVIEW
    if info["installed"]:
        return None, Recommendation.REMOVE
    return None, None


def _treemap_title(excluded_count: int = 0) -> str:
    """Build the treemap title, noting exclusions if any."""
    title = "LLOC Treemap"
    if excluded_count:
        title += f" ({excluded_count} excluded)"
    return title


def _render_treemap_from_reports(
    console, reports, project_lloc=0, title="LLOC Treemap"
):
    """Render the LLOC treemap from report objects."""
    from unladen.treemap import TileData, Treemap

    tiles = [
        TileData(
            label=r.name,
            total_lloc=r.heft.total_lloc,
            active_lloc=r.heft.active_lloc,
            heft_ratio=r.heft.heft_ratio,
        )
        for r in reports
        if r.heft is not None and r.installed
    ]
    if tiles:
        console.print()
        console.print(Treemap(tiles, project_lloc=project_lloc, title=title))


def _resolve_check_target(args: argparse.Namespace):
    """Determine whether the check target is a directory or package name.

    Returns (project_path, req_file, package_name) where either
    project_path or package_name is set, not both.
    """
    target = args.target
    req_file = str(Path(args.requirements).resolve()) if args.requirements else None
    target_path = Path(target).resolve()

    if target_path.is_dir():
        return target_path, req_file, None

    # Not a directory — treat as a package name
    if req_file:
        print(
            "Error: -r/--requirements is not supported in package mode.",
            file=sys.stderr,
        )
        return None, None, None
    return None, None, target


def _get_version() -> str:
    from unladen import __version__

    return __version__
