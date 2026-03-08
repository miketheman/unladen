"""The ``unladen show`` command.

Displays detailed usage information for a single dependency,
including import statements, string references, heft ratio,
and dynamic dispatch warnings.
"""

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from unladen._config import load_dep_map, load_exclude_set
from unladen.collector import _normalize_dep_name, dependency_source
from unladen.inspector import inspect_project
from unladen.merger import (
    _owned_module_prefixes,
    detect_dynamic_dispatch,
    merge_dep_usage,
)
from unladen.tracer import compute_heft


def cmd_show(args: argparse.Namespace) -> int:
    """Show detailed usage information for a single dependency."""
    project_path, req_file = _resolve_show_args(args)
    dep_map = load_dep_map(project_path, req_file, args.site_packages)
    if dep_map is None:
        return 1

    dep_name = args.dep
    if dep_name not in dep_map:
        print(f"Error: '{dep_name}' is not a declared dependency.", file=sys.stderr)
        return 1

    info = dep_map[dep_name]
    console = Console()

    _show_header(console, dep_name, info, project_path, req_file)

    if not info["installed"]:
        console.print("[dim]Declared but not installed.[/dim]\n")
        return 0

    # Phase 2: inspect
    all_import_names: set[str] = set()
    for dep_info in dep_map.values():
        all_import_names.update(dep_info["import_names"])
    usage = inspect_project(project_path, all_import_names)

    summary = merge_dep_usage(info["import_names"], usage, info["paths"])
    console.print()

    if not summary.is_used:
        console.print("[dim]Not imported by this project.[/dim]")
        return 0

    if summary.imports:
        _show_imports_table(console, summary.imports, project_path)

    if summary.string_refs:
        _show_string_refs_table(console, summary.string_refs, project_path)
        if not summary.imports:
            console.print(
                "\n[dim]This dependency is activated via string "
                "references only (e.g. Django settings).[/dim]"
            )

    if info["paths"]:
        heft = compute_heft(info["paths"], summary.used_names, dep_name)
        console.print(
            f"\n[bold]Heft:[/bold] {heft.active_lloc}/{heft.total_lloc} "
            f"LLOC ({heft.heft_ratio * 100:.1f}%)"
        )
        if heft.opaque_files:
            console.print(f"  {heft.opaque_files} binary extension(s) not analyzed")

    dynamic = detect_dynamic_dispatch(summary.used_names)
    if dynamic:
        console.print(
            "\n[yellow bold]Note:[/yellow bold] "
            "Dynamic dispatch detected — actual runtime "
            "usage may be higher:"
        )
        for name, reason in sorted(dynamic.items()):
            console.print(f"  [yellow]{name}[/yellow]: {reason}")

    console.print()
    return 0


def _resolve_show_args(args: argparse.Namespace):
    """Resolve show command args to project path and requirements file."""
    project_path = args.path.resolve()
    req_file = str(Path(args.requirements).resolve()) if args.requirements else None
    return project_path, req_file


def _show_header(console, dep_name, info, project_path, req_file):
    """Render the show command header (name, version, source, exclusion)."""
    exclude = load_exclude_set(project_path)
    is_excluded = _normalize_dep_name(dep_name) in exclude

    try:
        dep_source = dependency_source(project_path, requirements=req_file)
    except FileNotFoundError:
        dep_source = "unknown"

    import_names = info["import_names"]
    display_names = import_names
    if info["paths"]:
        owned = _owned_module_prefixes(import_names, info["paths"])
        if owned:
            display_names = sorted(owned)

    console.print(f"\n[bold]{dep_name}[/bold] v{info['version'] or '?'}")
    console.print(f"Declared in: {dep_source}")
    console.print(f"Import names: {', '.join(display_names)}")
    if is_excluded:
        console.print("[dim]* Excluded from analysis via \\[tool.unladen] config[/dim]")


def _show_imports_table(console, imports: list, project_path: Path) -> None:
    """Render import statements as a Rich table."""
    table = Table(title="Import Statements", show_lines=True)
    table.add_column("Location", style="cyan")
    table.add_column("Import")

    sorted_imports = sorted(imports, key=lambda i: (str(i.source_file), i.lineno or 0))
    for imp in sorted_imports:
        rel_path = str(imp.source_file.relative_to(project_path))
        location = f"{rel_path}:{imp.lineno}" if imp.lineno else rel_path
        if imp.name:
            stmt = f"from {imp.module} import {imp.name}"
            if imp.alias:
                stmt += f" as {imp.alias}"
        else:
            stmt = f"import {imp.module}"
            if imp.alias:
                stmt += f" as {imp.alias}"
        table.add_row(location, stmt)
    console.print(table)


def _show_string_refs_table(console, refs: list, project_path: Path) -> None:
    """Render string references as a Rich table."""
    table = Table(title="String References", show_lines=True)
    table.add_column("Location", style="cyan")
    table.add_column("Reference")

    sorted_refs = sorted(refs, key=lambda r: (str(r.source_file), r.lineno))
    for ref in sorted_refs:
        rel_path = str(ref.source_file.relative_to(project_path))
        table.add_row(f"{rel_path}:{ref.lineno}", ref.value)
    console.print(table)
