"""Phase 3.5: Transitive dependency measurement (experimental).

Walks the dependency graph from the project's used direct dependencies
with a fixpoint worklist.  For each dependency, the names activated by
the project (or by an upstream dependency) are traced through its call
graph; the files containing reached definitions are considered
*active*, and only runtime imports appearing in active files propagate
usage onward.  When a later-discovered parent contributes new names to
an already-processed dependency, that dependency is re-enqueued and
re-propagated, so both its heft and its subtree converge on the full
accumulated usage regardless of discovery order.

Usage propagates through the import names of each dependency's declared
(``Requires-Dist``) children.  Shared top-level names (namespace
packages like ``zope.*``, where parent and child both provide ``zope``)
are kept, and a child only counts as activated when imports narrowed to
its *owned* subpackages match (``DepUsageSummary.is_used``) — a parent
importing its own namespace subpackages does not activate its siblings.

Dependencies that are also declared directly stay in the main report;
transitive usage still propagates *through* them so their subtrees are
discovered, but their own reported heft is unchanged (see FUTURE.md).
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unladen._types import DepIndex, HeftResult
from unladen.collector import (
    DepInfo,
    DistributionNotFound,
    _dist_info_index,
    collect_package_deps,
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


@dataclass
class _DepNode:
    """Internal worklist state for one distribution."""

    info: DepInfo
    used_names: set[str]
    depth: int  # first-discovery depth; 0 = direct dep
    is_direct: bool
    via: set[str] = field(default_factory=set)
    # Snapshot of used_names at the last trace, so an unchanged node
    # (or one seeded from the main report) skips a redundant BFS.
    traced_with: frozenset[str] | None = None


def trace_transitive(
    dep_map: dict[str, DepInfo],
    dep_used_names: dict[str, set[str]],
    site_packages: Path,
    *,
    exclude: frozenset[str] | set[str] = frozenset(),
    max_depth: int = 10,
    indexes: dict[str, DepIndex] | None = None,
    traces: dict[str, TraceResult] | None = None,
) -> list[TransitiveDep]:
    """Trace usage through the transitive dependency graph to a fixpoint.

    Args:
        dep_map: The project's direct dependencies (Phase 1 output).
        dep_used_names: Used names per direct dependency (Phase 2.5 output).
        site_packages: Where installed distributions live.
        exclude: Normalized distribution names to neither report nor
            traverse — ``[tool.unladen] exclude`` entries, and the
            target itself in package mode (so dependency cycles don't
            report the analyzed package as its own transitive dep).
        max_depth: Traversal depth limit (1 = deps of direct deps only).
        indexes: Optional pre-built DepIndex per direct dep (from the
            main report's bulk pass) to seed the index cache.
        traces: Optional TraceResults matching *dep_used_names* (from
            the main report) to seed the trace cache.

    Returns:
        TransitiveDeps sorted by (depth, name).  A dependency is
        re-processed whenever its accumulated used names grow, so heft
        and subtree discovery are independent of traversal order.
        Dependencies that are also declared directly are excluded from
        the report (already in the main report) but still propagate
        usage to their own children — including new names contributed
        transitively.
    """
    indexes = dict(indexes) if indexes else {}
    traces = dict(traces) if traces else {}
    info_cache: dict[str, DepInfo] = dict(dep_map)
    dist_index = _dist_info_index(site_packages)
    children_cache: dict[str, dict[str, DepInfo]] = {}

    nodes: dict[str, _DepNode] = {}
    queue: list[str] = []
    queued: set[str] = set()

    def enqueue(name: str) -> None:
        node = nodes[name]
        if (
            name not in queued
            and node.depth < max_depth
            and node.info["installed"]
            and node.info["paths"]
        ):
            queue.append(name)
            queued.add(name)

    for name, info in dep_map.items():
        used = dep_used_names.get(name)
        if used and info["installed"] and info["paths"]:
            node = _DepNode(info, set(used), 0, True)
            if name in traces:
                # Seeded trace was computed for exactly these names.
                node.traced_with = frozenset(used)
            nodes[name] = node
            enqueue(name)

    while queue:
        batch, queue, queued = queue, [], set()

        # Resolve children first (metadata only) so childless parents
        # skip indexing entirely, then batch-index the rest through
        # one worker pool.
        ready: dict[str, dict[str, DepInfo]] = {}
        for name in batch:
            children = children_cache.get(name)
            if children is None:
                children = children_cache[name] = _resolve_children(
                    name, site_packages, dist_index, info_cache
                )
            if _importable_names(children):
                ready[name] = children
        indexes.update(
            index_dependencies_bulk(
                [
                    (name, nodes[name].info["paths"])
                    for name in ready
                    if name not in indexes
                ]
            )
        )

        for name, children in ready.items():
            node = nodes[name]
            for child_name, child_info, child_used in _propagate(
                name, node, children, indexes[name], traces
            ):
                if child_name in exclude:
                    continue
                child = nodes.get(child_name)
                if child is None:
                    child = nodes[child_name] = _DepNode(
                        child_info,
                        set(child_used),
                        node.depth + 1,
                        child_name in dep_map,
                    )
                    if not child.is_direct:
                        child.via.add(name)
                    enqueue(child_name)
                    continue
                if not child.is_direct:
                    child.via.add(name)
                if not child_used <= child.used_names:
                    # Growth: re-enqueue so heft and the subtree both
                    # see the accumulated names (fixpoint iteration).
                    child.used_names |= child_used
                    enqueue(child_name)

    return _build_results(nodes, indexes, traces)


def _importable_names(children: dict[str, DepInfo]) -> set[str]:
    """Union of the children's import names — what can propagate."""
    return {n for child in children.values() for n in child["import_names"]}


def _resolve_children(
    parent: str,
    site_packages: Path,
    dist_index: dict[str, Path],
    info_cache: dict[str, DepInfo],
) -> dict[str, DepInfo]:
    """Resolve *parent*'s declared dependencies, memoizing per run.

    Delegates to the collector's Requires-Dist pipeline; *info_cache*
    (seeded with the direct deps) ensures each distribution's metadata
    is read once per run even when many parents declare it.
    """
    try:
        return collect_package_deps(
            parent, site_packages, index=dist_index, info_cache=info_cache
        )
    except DistributionNotFound:
        return {}


def _propagate(
    name: str,
    node: _DepNode,
    children: dict[str, DepInfo],
    index: DepIndex,
    traces: dict[str, TraceResult],
) -> list[tuple[str, DepInfo, set[str]]]:
    """Find which names *name*'s active code uses from its children.

    Returns (child_dist_name, child_info, used_names) tuples for each
    installed child genuinely referenced from an active file.  The
    trace is cached in *traces* (and reused when the node's used names
    haven't changed since the last trace); at the fixpoint the cached
    trace therefore reflects the node's final used names, which the
    heft pass relies on.
    """
    known = _importable_names(children)
    current = frozenset(node.used_names)
    prev = traces.get(name)
    if prev is not None and node.traced_with == current:
        trace = prev
    else:
        trace = trace_index(index, node.used_names, ti=prev.ti if prev else None)
        traces[name] = trace
        node.traced_with = current

    files = files_from_trace(index, trace, node.info["paths"])
    if not files:
        return []
    # runtime_only: an `if TYPE_CHECKING:` import must not mark a
    # child as activated at runtime.
    usage = inspect_source_files(files, known, runtime_only=True)

    results: list[tuple[str, DepInfo, set[str]]] = []
    for child_name, child_info in children.items():
        if not child_info["installed"]:
            continue
        summary = merge_dep_usage(
            child_info["import_names"], usage, child_info["paths"]
        )
        # is_used applies the namespace-owned filtering: a parent's
        # imports of its own (or a sibling's) subpackages must not
        # activate this child.  used_names alone is unfiltered.
        if summary.is_used and summary.used_names:
            results.append((child_name, child_info, summary.used_names))
    return results


def _build_results(
    nodes: dict[str, _DepNode],
    indexes: dict[str, DepIndex],
    traces: dict[str, TraceResult],
) -> list[TransitiveDep]:
    """Build reported deps and attach heft.

    Traced nodes reuse their final trace (guaranteed current at the
    fixpoint); indexed-but-stale nodes re-trace from the cached index;
    never-indexed leaves batch through the bulk pool.
    """
    results: list[TransitiveDep] = []
    bulk_work: list[tuple[str, list[Path], set[str]]] = []
    pending: dict[str, TransitiveDep] = {}

    for name, node in nodes.items():
        if node.is_direct:
            continue
        td = TransitiveDep(
            name=name,
            version=node.info["version"],
            used_names=node.used_names,
            via=node.via,
            depth=node.depth,
        )
        results.append(td)
        if not node.used_names:
            continue
        index = indexes.get(name)
        if index is not None:
            if node.traced_with == frozenset(node.used_names) and name in traces:
                td.heft = heft_from_trace(index, traces[name], name)
            else:
                td.heft = heft_from_index(index, node.used_names, name)
        elif node.info["paths"]:
            bulk_work.append((name, node.info["paths"], node.used_names))
            pending[name] = td

    for name, heft in compute_hefts_bulk(bulk_work).items():
        pending[name].heft = heft
    return sorted(results, key=lambda td: (td.depth, td.name))
