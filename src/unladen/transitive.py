"""Phase 3.5: Transitive dependency measurement (experimental).

Walks the dependency graph breadth-first from the project's used direct
dependencies.  For each dependency, the names activated by the project
(or by an upstream dependency) are traced through its call graph; the
modules containing reached definitions are considered *active*, and only
imports appearing in active modules propagate usage to the next level of
the graph.  This keeps the measurement usage-driven: the heft reported
for a transitive dependency reflects what the project's own code
ultimately activates, not everything the intermediate dependency could
ever use.

Module classification follows ty's search-path resolution order
(first-party source, then third-party, then stdlib): an import found in
a dependency's source is first-party if it resolves to the dependency's
own import names, third-party if it resolves to an import name of one of
its declared (``Requires-Dist``) dependencies, and stdlib if it appears
in ``sys.stdlib_module_names``.  Unlike ty, declared third-party names
win over stdlib names so backport packages (e.g. ``legacy-cgi``
providing ``cgi``) attribute to the declaring dependency.
"""

import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from unladen._types import HeftResult
from unladen.collector import DepInfo, collect_package_deps
from unladen.inspector import inspect_source_files
from unladen.merger import merge_dep_usage
from unladen.tracer import _collect_source_files, active_modules, index_dependency


class ModuleOrigin(Enum):
    """Where an imported module resolves from, ty-style."""

    FIRST_PARTY = "first-party"
    THIRD_PARTY = "third-party"
    STDLIB = "stdlib"
    UNKNOWN = "unknown"


def classify_module(
    top_level: str,
    own_import_names: set[str],
    declared_third_party: set[str],
) -> ModuleOrigin:
    """Classify a top-level import name by ordered resolution.

    Mirrors ty's search-path priority: the dependency's own source wins
    over everything, then declared third-party dependencies, then the
    stdlib.  Declared names beat stdlib names so that backport
    distributions shadowing stdlib modules are attributed correctly.
    """
    if top_level in own_import_names:
        return ModuleOrigin.FIRST_PARTY
    if top_level in declared_third_party:
        return ModuleOrigin.THIRD_PARTY
    if top_level in sys.stdlib_module_names:
        return ModuleOrigin.STDLIB
    return ModuleOrigin.UNKNOWN


@dataclass
class TransitiveDep:
    """A dependency reached transitively from the project's direct deps."""

    name: str
    version: str | None
    import_names: list[str]
    used_names: set[str]
    via: set[str]  # parent distribution names that activate this dep
    depth: int  # 1 = dependency of a direct dependency
    heft: HeftResult | None = None
    _paths: list[Path] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict suitable for JSON output."""
        d: dict[str, Any] = {
            "name": self.name,
            "version": self.version,
            "via": sorted(self.via),
            "depth": self.depth,
            "used_names": sorted(self.used_names),
            "heft": None,
        }
        if self.heft is not None:
            d["heft"] = {
                "ratio": self.heft.heft_ratio,
                "active_lloc": self.heft.active_lloc,
                "total_lloc": self.heft.total_lloc,
                "opaque_files": self.heft.opaque_files,
            }
        return d


def trace_transitive(
    dep_map: dict[str, DepInfo],
    dep_used_names: dict[str, set[str]],
    site_packages: Path,
    *,
    max_depth: int = 10,
) -> list[TransitiveDep]:
    """Trace usage through the transitive dependency graph.

    Args:
        dep_map: The project's direct dependencies (Phase 1 output).
        dep_used_names: Used names per direct dependency (Phase 2.5 output).
        site_packages: Where installed distributions live.
        max_depth: Traversal depth limit (1 = deps of direct deps only).

    Returns:
        TransitiveDeps sorted by (depth, name), with heft computed from
        the accumulated used names.  Dependencies that are also declared
        directly are excluded — they are already measured in the main
        report (their transitive contributions are a known limitation).

    Each dependency is processed once, with the used names accumulated
    at the time it is dequeued; names contributed by parents discovered
    later still count toward its heft but do not re-propagate (a
    fixpoint iteration is future work — dependency cycles are rare).
    """
    direct = set(dep_map)
    found: dict[str, TransitiveDep] = {}

    frontier: list[tuple[str, DepInfo, set[str]]] = []
    for name, info in dep_map.items():
        used = dep_used_names.get(name)
        if used and info["installed"] and info["paths"]:
            frontier.append((name, info, used))

    depth = 0
    while frontier and depth < max_depth:
        depth += 1
        contributions: dict[str, set[str]] = {}
        child_infos: dict[str, DepInfo] = {}
        child_via: dict[str, set[str]] = {}

        for name, info, used in frontier:
            for child_name, child_info, child_used in _child_usage(
                name, info, used, site_packages
            ):
                contributions.setdefault(child_name, set()).update(child_used)
                child_infos[child_name] = child_info
                child_via.setdefault(child_name, set()).add(name)

        next_frontier: list[tuple[str, DepInfo, set[str]]] = []
        for child_name, used_names in contributions.items():
            if child_name in direct:
                continue
            if child_name in found:
                found[child_name].used_names.update(used_names)
                found[child_name].via.update(child_via[child_name])
                continue
            info = child_infos[child_name]
            td = TransitiveDep(
                name=child_name,
                version=info["version"],
                import_names=info["import_names"],
                used_names=set(used_names),
                via=set(child_via[child_name]),
                depth=depth,
                _paths=info["paths"],
            )
            found[child_name] = td
            next_frontier.append((child_name, info, td.used_names))
        frontier = next_frontier

    _attach_hefts(found)
    return sorted(found.values(), key=lambda td: (td.depth, td.name))


def _child_usage(
    name: str,
    info: DepInfo,
    used: set[str],
    site_packages: Path,
) -> list[tuple[str, DepInfo, set[str]]]:
    """Find which names *name*'s active code uses from its own dependencies.

    Returns (child_dist_name, child_info, used_names) tuples for each
    installed child dependency referenced from an active module.
    """
    try:
        children = collect_package_deps(name, site_packages)
    except FileNotFoundError:
        return []
    if not children:
        return []

    own = set(info["import_names"])
    declared: set[str] = set()
    for child_info in children.values():
        declared.update(child_info["import_names"])
    known = {
        n
        for n in declared
        if classify_module(n, own, declared) is ModuleOrigin.THIRD_PARTY
    }
    if not known:
        return []

    index = index_dependency(info["paths"])
    active = active_modules(index, used)
    files = _active_files(info["paths"], active)
    if not files:
        return []
    usage = inspect_source_files(files, known)

    results: list[tuple[str, DepInfo, set[str]]] = []
    for child_name, child_info in children.items():
        if not child_info["installed"]:
            continue
        summary = merge_dep_usage(
            child_info["import_names"], usage, child_info["paths"]
        )
        if summary.used_names:
            results.append((child_name, child_info, summary.used_names))
    return results


def _active_files(dep_paths: list[Path], active: set[str]) -> list[Path]:
    """Select source files whose module is in the active set.

    Importing anything from a package executes every ancestor
    ``__init__.py`` on the way down, so those are included as active
    alongside the matched modules themselves.
    """
    py_files, _ = _collect_source_files(dep_paths)
    roots = {p.resolve() for p in dep_paths if p.is_dir()}
    selected: set[Path] = set()
    for f in py_files:
        module = f.parent.name if f.stem == "__init__" else f.stem
        if module in active:
            selected.add(f)

    for f in list(selected):
        root = next(
            (r for r in roots if f.resolve().is_relative_to(r)),
            None,
        )
        if root is None:
            continue
        d = f.parent
        while d.resolve().is_relative_to(root):
            init = d / "__init__.py"
            if init.exists():
                selected.add(init)
            if d.resolve() == root:
                break
            d = d.parent
    return sorted(selected)


def _attach_hefts(found: dict[str, TransitiveDep]) -> None:
    """Compute heft for each discovered transitive dep in one bulk pass."""
    from unladen.tracer import compute_hefts_bulk

    work = [
        (name, td._paths, td.used_names)
        for name, td in found.items()
        if td._paths and td.used_names
    ]
    hefts = compute_hefts_bulk(work)
    for name, td in found.items():
        td.heft = hefts.get(name)
