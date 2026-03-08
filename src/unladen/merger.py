"""Phase 2.5: Merge inspector output into per-dependency summaries.

Bridges the gap between Phase 2 (inspector) and Phase 3 (tracer) by
aggregating per-import-name usage data into per-distribution summaries.
Handles namespace package disambiguation and dynamic dispatch detection.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from unladen.inspector import ImportInfo, StringRef


@dataclass
class DepUsageSummary:
    """Merged usage data for a single dependency across all its import names.

    A single distribution (e.g. "setuptools") can expose multiple import
    names (e.g. "setuptools", "pkg_resources").  This dataclass aggregates
    the inspector's per-import-name results into a single view.
    """

    used_names: set[str] = field(default_factory=set)
    imports: list[ImportInfo] = field(default_factory=list)
    files: set[Path] = field(default_factory=set)
    matched_import_names: list[str] = field(default_factory=list)
    string_refs: list[StringRef] = field(default_factory=list)

    @property
    def has_imports(self) -> bool:
        """True if any import statement references this dependency."""
        return bool(self.matched_import_names)

    @property
    def is_used(self) -> bool:
        """True if the dep is activated by imports OR string references.

        A dep can be "used" without imports when activated solely via
        Django-style dotted path strings (e.g. INSTALLED_APPS).
        """
        return self.has_imports or bool(self.string_refs)

    @property
    def import_count(self) -> int:
        return len(self.imports)

    @property
    def file_count(self) -> int:
        return len(self.files)


def merge_dep_usage(
    import_names: list[str],
    usage: dict,
    dep_paths: list[Path] | None = None,
) -> DepUsageSummary:
    """Merge inspector output across all import names for a dependency.

    Extracts leaf names from string references for heft computation.

    When *dep_paths* is provided, filters imports to only those whose
    module path falls under the dependency's owned subpackages.  This
    disambiguates namespace packages like ``zope.*`` where multiple
    distributions share the same top-level import name.
    """
    owned = _owned_module_prefixes(import_names, dep_paths) if dep_paths else None
    summary = DepUsageSummary()
    for imp_name in import_names:
        if imp_name not in usage:
            continue
        imp_usage = usage[imp_name]

        # Filter imports to this dep's owned subpackages.
        # used_names is kept unfiltered: extra names from sibling
        # namespace packages are harmless — the tracer only matches
        # names that exist in the dependency's own definitions.
        if owned:
            imports = [
                imp
                for imp in imp_usage.get("imports", [])
                if _matches_owned(imp.module, owned)
            ]
            used_names = imp_usage.get("used_names", set())
            files = {imp.source_file for imp in imports}
        else:
            imports = imp_usage.get("imports", [])
            used_names = imp_usage.get("used_names", set())
            files = imp_usage.get("files", set())

        if owned:
            string_refs = [
                ref
                for ref in imp_usage.get("string_refs", [])
                if _matches_owned(ref.value, owned)
            ]
        else:
            string_refs = imp_usage.get("string_refs", [])

        if imports:
            summary.matched_import_names.append(imp_name)
        summary.used_names.update(used_names)
        summary.imports.extend(imports)
        summary.files.update(files)
        summary.string_refs.extend(string_refs)

    # Extract leaf names from dotted string references for heft tracing.
    # e.g. "whitenoise.middleware.WhiteNoiseMiddleware" -> "WhiteNoiseMiddleware"
    # This lets the tracer find the actual class/function activated by
    # Django-style settings (INSTALLED_APPS, MIDDLEWARE, etc.).
    for ref in summary.string_refs:
        summary.files.add(ref.source_file)
        parts = ref.value.split(".")
        if len(parts) >= 2:
            summary.used_names.add(parts[-1])

    return summary


def _owned_module_prefixes(
    import_names: list[str], dep_paths: list[Path]
) -> set[str] | None:
    """Compute owned module prefixes for namespace package disambiguation.

    For regular packages (path name matches an import name), the prefix
    is just the import name — no filtering needed.
    For namespace subpackages (e.g. ``zope/sqlalchemy`` under import name
    ``zope``), the prefix is ``zope.sqlalchemy``.

    Returns None if all paths are regular packages (no disambiguation
    needed), avoiding unnecessary filtering overhead.
    """
    prefixes: set[str] = set()
    is_namespace = False
    for path in dep_paths:
        name = path.stem if path.is_file() else path.name
        if name in import_names:
            prefixes.add(name)
        else:
            # Namespace subpackage: parent dir is the import name
            parent = path.parent.name
            if parent in import_names:
                prefixes.add(f"{parent}.{name}")
                is_namespace = True
    if not is_namespace:
        return None
    return prefixes or None


def _matches_owned(module: str, owned_prefixes: set[str]) -> bool:
    """Check if a dotted module path belongs to an owned prefix.

    ``zope.sqlalchemy`` matches prefix ``zope.sqlalchemy``.
    ``zope.sqlalchemy.datamanager`` matches prefix ``zope.sqlalchemy``.
    ``zope.interface`` does NOT match prefix ``zope.sqlalchemy``.
    """
    return any(
        module == prefix or module.startswith(prefix + ".") for prefix in owned_prefixes
    )


# Patterns that suggest runtime dispatch where static analysis
# underestimates actual code activation (plugin registries, dynamic
# imports, factory methods that resolve targets at runtime).
_DYNAMIC_DISPATCH_PATTERNS = [
    (re.compile(r"^get_\w+_by_\w+$"), "registry lookup by name"),
    (re.compile(r"^find_\w+$"), "dynamic finder"),
    (re.compile(r"^load_\w+$"), "dynamic loader"),
    (re.compile(r"^create_\w+_from_\w+$"), "dynamic factory"),
    (re.compile(r"^import_module$"), "dynamic import"),
    (re.compile(r"^__import__$"), "dynamic import"),
    (re.compile(r"^entry_points$"), "plugin registry"),
    (re.compile(r"^iter_entry_points$"), "plugin registry"),
    (re.compile(r"^resolve$"), "plugin resolution"),
]


def detect_dynamic_dispatch(used_names: set[str]) -> dict[str, str]:
    """Detect used names that suggest dynamic/runtime dispatch.

    Returns {name: reason} for names matching known patterns where
    static analysis underestimates actual code activation.
    """
    found: dict[str, str] = {}
    for name in used_names:
        for pattern, reason in _DYNAMIC_DISPATCH_PATTERNS:
            if pattern.match(name):
                found[name] = reason
                break
    return found
