"""Phase 3.5: Transitive dependency measurement (experimental).

Walks the dependency graph breadth-first from the project's used direct
dependencies.  For each dependency, the names activated by the project
(or by an upstream dependency) are traced through its call graph; the
files containing reached definitions are considered *active*, and only
imports appearing in active files propagate usage to the next level of
the graph.  This keeps the measurement usage-driven: the heft reported
for a transitive dependency reflects what the project's own code
ultimately activates, not everything the intermediate dependency could
ever use.

Usage propagates through the import names of each dependency's declared
(``Requires-Dist``) children.  Shared top-level names (namespace
packages like ``zope.*``, where parent and child both provide ``zope``)
are kept — ``merge_dep_usage`` narrows imports to each child's owned
subpackages, so attribution stays per-distribution.

Dependencies that are also declared directly stay in the main report;
transitive usage still propagates *through* them so their subtrees are
discovered, but their own reported heft is unchanged (see FUTURE.md).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unladen._types import DepIndex, HeftResult
from unladen.collector import (
    DepInfo,
    DistributionNotFound,
    _dist_info_index,
    _names_and_markers,
    package_requires,
    resolve_installed,
)
from unladen.inspector import inspect_source_files
from unladen.merger import merge_dep_usage
from unladen.tracer import (
    TraceResult,
    compute_hefts_bulk,
    files_from_trace,
    heft_from_index,
    heft_from_trace,
    index_dependencies_bulk,
    trace_index,
)


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
    exclude: frozenset[str] | set[str] = frozenset(),
    max_depth: int = 10,
) -> list[TransitiveDep]:
    """Trace usage through the transitive dependency graph.

    Args:
        dep_map: The project's direct dependencies (Phase 1 output).
        dep_used_names: Used names per direct dependency (Phase 2.5 output).
        site_packages: Where installed distributions live.
        exclude: Normalized distribution names to neither report nor
            traverse — ``[tool.unladen] exclude`` entries, and the
            target itself in package mode (so dependency cycles don't
            report the analyzed package as its own transitive dep).
        max_depth: Traversal depth limit (1 = deps of direct deps only).

    Returns:
        TransitiveDeps sorted by (depth, name), with heft computed from
        the accumulated used names.  Dependencies that are also declared
        directly are excluded from the report (already in the main
        report) but still propagate usage to their own children.

    Each dependency is processed once, with the used names accumulated
    at the time it is dequeued; names contributed by parents discovered
    later still count toward its heft but do not re-propagate (a
    fixpoint iteration is future work — dependency cycles are rare).
    """
    direct = set(dep_map)
    found: dict[str, TransitiveDep] = {}
    paths: dict[str, list[Path]] = {}
    # Per-run caches: parent indexes (reused for heft), their traces
    # (reused when used_names didn't grow), resolved child DepInfos
    # (each distribution's metadata read once), and the dist-info
    # directory listing (built once).
    indexes: dict[str, DepIndex] = {}
    traces: dict[str, tuple[frozenset[str], TraceResult]] = {}
    info_cache: dict[str, DepInfo] = dict(dep_map)
    dist_index = _dist_info_index(site_packages)

    frontier: list[tuple[str, DepInfo, set[str]]] = []
    for name, info in dep_map.items():
        used = dep_used_names.get(name)
        if used and info["installed"] and info["paths"]:
            frontier.append((name, info, used))
    processed = {name for name, _, _ in frontier}

    depth = 0
    while frontier and depth < max_depth:
        depth += 1

        # Resolve each parent's declared children (metadata only) so
        # childless parents skip indexing entirely.
        prep: dict[str, tuple[dict[str, DepInfo], set[str]]] = {}
        for name, _, _ in frontier:
            children = _resolve_children(name, site_packages, dist_index, info_cache)
            known = {n for c in children.values() for n in c["import_names"]}
            if known:
                prep[name] = (children, known)

        # Batch-index all parents that need it through one worker pool.
        indexes.update(
            index_dependencies_bulk(
                [
                    (name, info["paths"])
                    for name, info, _ in frontier
                    if name in prep and name not in indexes
                ]
            )
        )

        # One record per child discovered this level: (info, used, via).
        pending: dict[str, tuple[DepInfo, set[str], set[str]]] = {}
        for name, info, used in frontier:
            if name not in prep:
                continue
            children, known = prep[name]
            for child_name, child_info, child_used in _propagate(
                name, used, children, known, indexes[name], info["paths"], traces
            ):
                if child_name in pending:
                    _, prev_used, prev_via = pending[child_name]
                    prev_used.update(child_used)
                    prev_via.add(name)
                else:
                    pending[child_name] = (child_info, child_used, {name})

        next_frontier: list[tuple[str, DepInfo, set[str]]] = []
        for child_name, (info, used_names, via) in pending.items():
            if child_name in exclude:
                continue
            if child_name in direct:
                # Not reported here (it's in the main report), but usage
                # still flows through it so its subtree is discovered —
                # even when the project itself never imports it.
                if child_name not in processed and info["installed"] and info["paths"]:
                    processed.add(child_name)
                    next_frontier.append((child_name, info, used_names))
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

    _attach_hefts(found, indexes, traces, paths)
    return sorted(found.values(), key=lambda td: (td.depth, td.name))


def _resolve_children(
    parent: str,
    site_packages: Path,
    dist_index: dict[str, Path],
    info_cache: dict[str, DepInfo],
) -> dict[str, DepInfo]:
    """Resolve *parent*'s declared dependencies, memoizing per run.

    Children shared by many parents (urllib3, typing-extensions, ...)
    have their metadata read once; *info_cache* is seeded with the
    project's direct deps so those are never re-resolved.
    """
    try:
        specs = package_requires(parent, site_packages, index=dist_index)
    except DistributionNotFound:
        return {}
    if not specs:
        return {}
    names, markers = _names_and_markers(specs)
    ordered = list(dict.fromkeys(names))
    missing = [n for n in ordered if n not in info_cache]
    if missing:
        info_cache.update(
            resolve_installed(missing, site_packages, markers=markers, index=dist_index)
        )
    return {n: info_cache[n] for n in ordered}


def _propagate(
    name: str,
    used: set[str],
    children: dict[str, DepInfo],
    known: set[str],
    index: DepIndex,
    dep_paths: list[Path],
    traces: dict[str, tuple[frozenset[str], TraceResult]],
) -> list[tuple[str, DepInfo, set[str]]]:
    """Find which names *name*'s active code uses from its children.

    Returns (child_dist_name, child_info, used_names) tuples for each
    installed child referenced from an active file.  The trace is
    cached in *traces* so the heft pass can reuse it.
    """
    trace = trace_index(index, used)
    traces[name] = (frozenset(used), trace)
    files = files_from_trace(index, trace, dep_paths)
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
    traces: dict[str, tuple[frozenset[str], TraceResult]],
    paths: dict[str, list[Path]],
) -> None:
    """Compute heft for each discovered transitive dep.

    Deps that served as parents reuse their cached index — and, when
    their used names didn't grow after processing, the cached trace,
    skipping a second BFS.  Leaves (never indexed) batch through the
    bulk pool in one pass.
    """
    bulk_work: list[tuple[str, list[Path], set[str]]] = []
    for name, td in found.items():
        if not td.used_names:
            continue
        index = indexes.get(name)
        if index is not None:
            cached = traces.get(name)
            if cached is not None and cached[0] == td.used_names:
                td.heft = heft_from_trace(index, cached[1], name)
            else:
                td.heft = heft_from_index(index, td.used_names, name)
        elif paths.get(name):
            bulk_work.append((name, paths[name], td.used_names))
    for name, heft in compute_hefts_bulk(bulk_work).items():
        found[name].heft = heft
