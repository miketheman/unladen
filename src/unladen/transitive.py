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

A dependency's declared import names are classified by ordered
resolution inspired by ty's search-path model (``classify_module``):
first-party (the dependency's own import names), then third-party
(import names of its ``Requires-Dist`` deps), then stdlib
(``sys.stdlib_module_names``).  Only third-party names propagate usage.
Unlike ty, declared third-party names win over stdlib names so backport
packages (e.g. ``legacy-cgi`` providing ``cgi``) attribute to the
declaring dependency.  The STDLIB/UNKNOWN classifications are not yet
surfaced — see FUTURE.md for the undeclared-import idea they enable.
"""

import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from unladen._types import DepIndex, HeftResult
from unladen.collector import DepInfo, _dist_info_index, collect_package_deps
from unladen.inspector import inspect_source_files
from unladen.merger import merge_dep_usage
from unladen.tracer import (
    active_files,
    compute_hefts_bulk,
    heft_from_index,
    index_dependency,
)


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
    used_names: set[str]
    via: set[str]  # parent distribution names that activate this dep
    depth: int  # 1 = dependency of a direct dependency
    heft: HeftResult | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict suitable for JSON output."""
        return {
            "name": self.name,
            "version": self.version,
            "via": sorted(self.via),
            "depth": self.depth,
            "used_names": sorted(self.used_names),
            "heft": self.heft.to_dict() if self.heft is not None else None,
        }


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
    # Indexes built for parents are reused for their heft computation.
    indexes: dict[str, DepIndex] = {}
    paths: dict[str, list[Path]] = {}
    dist_index = _dist_info_index(site_packages)

    frontier: list[tuple[str, DepInfo, set[str]]] = []
    for name, info in dep_map.items():
        used = dep_used_names.get(name)
        if used and info["installed"] and info["paths"]:
            frontier.append((name, info, used))

    depth = 0
    while frontier and depth < max_depth:
        depth += 1
        # One record per child discovered this level: (info, used, via).
        pending: dict[str, tuple[DepInfo, set[str], set[str]]] = {}

        for name, info, used in frontier:
            for child_name, child_info, child_used in _child_usage(
                name, info, used, site_packages, indexes, dist_index
            ):
                if child_name in pending:
                    _, prev_used, prev_via = pending[child_name]
                    prev_used.update(child_used)
                    prev_via.add(name)
                else:
                    pending[child_name] = (child_info, child_used, {name})

        next_frontier: list[tuple[str, DepInfo, set[str]]] = []
        for child_name, (info, used_names, via) in pending.items():
            if child_name in direct:
                continue
            if child_name in found:
                found[child_name].used_names.update(used_names)
                found[child_name].via.update(via)
                continue
            td = TransitiveDep(
                name=child_name,
                version=info["version"],
                used_names=used_names,
                via=via,
                depth=depth,
            )
            found[child_name] = td
            paths[child_name] = info["paths"]
            next_frontier.append((child_name, info, td.used_names))
        frontier = next_frontier

    _attach_hefts(found, indexes, paths)
    return sorted(found.values(), key=lambda td: (td.depth, td.name))


def _child_usage(
    name: str,
    info: DepInfo,
    used: set[str],
    site_packages: Path,
    indexes: dict[str, DepIndex],
    dist_index: dict[str, Path],
) -> list[tuple[str, DepInfo, set[str]]]:
    """Find which names *name*'s active code uses from its own dependencies.

    Returns (child_dist_name, child_info, used_names) tuples for each
    installed child dependency referenced from an active module.
    The index built here is cached in *indexes* for reuse by the
    heft pass.
    """
    try:
        children = collect_package_deps(name, site_packages, index=dist_index)
    except FileNotFoundError:
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

    index = indexes.get(name)
    if index is None:
        index = indexes[name] = index_dependency(info["paths"])
    files = active_files(index, used, info["paths"])
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


def _attach_hefts(
    found: dict[str, TransitiveDep],
    indexes: dict[str, DepIndex],
    paths: dict[str, list[Path]],
) -> None:
    """Compute heft for each discovered transitive dep.

    Deps that served as parents already have an index cached — reuse it.
    Leaves (never indexed) go through the bulk pool in one pass.
    """
    bulk_work: list[tuple[str, list[Path], set[str]]] = []
    for name, td in found.items():
        if not td.used_names:
            continue
        if name in indexes:
            td.heft = heft_from_index(indexes[name], td.used_names, name)
        elif paths.get(name):
            bulk_work.append((name, paths[name], td.used_names))
    for name, heft in compute_hefts_bulk(bulk_work).items():
        found[name].heft = heft
