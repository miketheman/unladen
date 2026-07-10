"""Phase 2: The Flight — Call Graph Mapping.

Statically analyzes local project code to find dependency usage.
"""

import ast
import tomllib
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from unladen._lloc import count_statements as _count_statements
from unladen._parsing import is_setup_call as _is_setup_call
from unladen._parsing import parse_file as _parse_file


@dataclass(frozen=True)
class ImportInfo:
    """Represents a single import statement found in source code."""

    module: str  # top-level or dotted module name (e.g. "requests", "os.path")
    name: str | None = (
        None  # specific name imported (e.g. "echo"), None for bare imports
    )
    alias: str | None = None  # local alias (e.g. "pd" for pandas)
    source_file: Path | None = None  # file where this import was found
    lineno: int | None = None  # line number of the import statement


@dataclass(frozen=True)
class StringRef:
    """A string literal that references a dependency module path."""

    value: str  # the dotted path string
    source_file: Path
    lineno: int


class UsageEntry(TypedDict, total=False):
    """Per-import-name usage data from project inspection."""

    used_names: set[str]
    files: set[Path]
    imports: list[ImportInfo]
    string_refs: list[StringRef]


_WALK_HANDLED_TYPES = frozenset(
    {
        ast.Constant,
        ast.Attribute,
        ast.Import,
        ast.ImportFrom,
        ast.ClassDef,
        ast.Assign,
        ast.AnnAssign,
        ast.AugAssign,
    }
)


def _walk_source_file(
    tree: ast.Module,
    file_path: Path,
    known_import_names: set[str],
    out_imports: list[ImportInfo],
    out_accesses: dict[str, set[str]],
    out_string_refs: dict[str, list[StringRef]],
) -> None:
    """Single-pass AST walk extracting imports, accesses, and string refs.

    Combines what would be three separate walks (imports, attribute
    accesses, string references) into one pass for performance.
    Results are appended to the caller's mutable output containers.
    """
    for node in ast.walk(tree):
        # Exact-class dispatch: ast.parse() never yields subclasses, so a
        # frozenset membership test on the class rejects the (majority of)
        # irrelevant nodes with one hash lookup instead of re-paying an
        # isinstance chain's C-call overhead on every node.
        cls = node.__class__
        if cls not in _WALK_HANDLED_TYPES:
            continue
        if cls is ast.Constant:
            # Dotted path strings (e.g. "whitenoise.middleware.WhiteNoise")
            # count anywhere.  Bare names are only collected from
            # settings-style assignments (see _collect_settings_refs) —
            # matching any single-word string would mark a dependency
            # "used" from an unrelated dict key or message text.
            value = node.value
            if (
                value.__class__ is not str
                or not value
                or " " in value
                or "/" in value
                or "." not in value
            ):
                continue
            top = value.split(".")[0]
            if top not in known_import_names:
                continue
            out_string_refs.setdefault(top, []).append(
                StringRef(value=value, source_file=file_path, lineno=node.lineno)
            )
        elif cls is ast.Attribute:
            if isinstance(node.value, ast.Name):
                out_accesses.setdefault(node.value.id, set()).add(node.attr)
            elif isinstance(node.value, ast.Attribute) and isinstance(
                node.value.value, ast.Name
            ):
                root = node.value.value.id
                mid = node.value.attr
                out_accesses.setdefault(root, set()).add(mid)
                out_accesses.setdefault(mid, set()).add(node.attr)
        elif cls is ast.Import:
            for alias in node.names:
                out_imports.append(
                    ImportInfo(
                        module=alias.name,
                        name=None,
                        alias=alias.asname,
                        source_file=file_path,
                        lineno=node.lineno,
                    )
                )
        elif cls is ast.ImportFrom:
            if node.level and node.level > 0:
                continue
            module = node.module or ""
            for alias in node.names:
                out_imports.append(
                    ImportInfo(
                        module=module,
                        name=alias.name,
                        alias=alias.asname,
                        source_file=file_path,
                        lineno=node.lineno,
                    )
                )
        elif cls is ast.ClassDef:
            for base in node.bases:
                if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
                    out_accesses.setdefault(base.value.id, set()).add(base.attr)
        elif cls is ast.Assign or cls is ast.AnnAssign or cls is ast.AugAssign:
            _collect_settings_refs(node, file_path, known_import_names, out_string_refs)


def _collect_settings_refs(
    node: ast.Assign | ast.AnnAssign | ast.AugAssign,
    file_path: Path,
    known_import_names: set[str],
    out_string_refs: dict[str, list[StringRef]],
) -> None:
    """Collect bare-name string refs from settings-style assignments.

    Django activates dependencies via bare names in ALL_CAPS list
    settings (``INSTALLED_APPS = ["allauth"]``).  Only that shape —
    a list/tuple/set literal assigned to an ALL_CAPS name — counts a
    dotless string as a dependency reference.  Dotted strings are
    handled by the generic constant branch in ``_walk_source_file``.
    """
    if isinstance(node, ast.Assign):
        targets = node.targets
    else:
        targets = [node.target]
    if node.value is None or not isinstance(node.value, (ast.List, ast.Tuple, ast.Set)):
        return
    if not any(isinstance(t, ast.Name) and t.id.isupper() for t in targets):
        return
    for elt in node.value.elts:
        if not isinstance(elt, ast.Constant) or not isinstance(elt.value, str):
            continue
        value = elt.value
        if not value or " " in value or "/" in value or "." in value:
            continue
        if value in known_import_names:
            out_string_refs.setdefault(value, []).append(
                StringRef(value=value, source_file=file_path, lineno=elt.lineno)
            )


# Directories to never scan when discovering project source.
# - tests/test: test suites (not runtime code)
# - docs/doc: documentation (Sphinx conf.py etc.)
# - .venv/venv/.tox/.nox: virtual environments
# - .git/.hg: version control internals
# - __pycache__: bytecode cache
# - node_modules: JavaScript deps (monorepos)
# - .eggs/build/dist: build artifacts
_EXCLUDED_DIRS = frozenset(
    {
        "tests",
        "test",
        "docs",
        "doc",
        ".venv",
        "venv",
        ".tox",
        ".nox",
        ".git",
        ".hg",
        "__pycache__",
        "node_modules",
        ".eggs",
        "build",
        "dist",
    }
)


def find_project_source(project_path: Path) -> list[Path]:
    """Discover Python source files belonging to the project.

    Discovery strategy:
    1. Check pyproject.toml/setup.py for explicit source root.
    2. If src/ exists, scan all Python packages under it.
    3. Otherwise, scan Python packages at the project root.

    Excludes well-known non-source directories (tests, .venv, etc.).
    """
    # Try to detect source root from config files
    source_root = _detect_source_root(project_path)
    if source_root:
        source_dir = project_path / source_root
        if source_dir.is_dir():
            return _collect_packages(source_dir, _EXCLUDED_DIRS)

    # Fallback: check for src/ directory
    src_dir = project_path / "src"
    if src_dir.is_dir():
        return _collect_packages(src_dir, _EXCLUDED_DIRS)

    # Flat layout: scan packages at project root
    return _collect_packages(project_path, _EXCLUDED_DIRS)


def _collect_packages(base_dir: Path, excluded: frozenset[str]) -> list[Path]:
    """Find Python source files under base_dir.

    Collects both packages (directories with ``__init__.py``) and
    standalone ``.py`` modules at the top level — this supports flat
    layouts like a single ``main.py`` in the repo root.
    """
    py_files: list[Path] = []

    for child in sorted(base_dir.iterdir()):
        if child.name.startswith("."):
            continue
        if child.is_file() and child.suffix == ".py":
            # Skip setup.py/conftest.py — not project source
            if child.name in ("setup.py", "conftest.py"):
                continue
            py_files.append(child)
        elif child.is_dir():
            if child.name in excluded:
                continue
            if (child / "__init__.py").exists():
                py_files.extend(sorted(child.rglob("*.py")))
            else:
                # PEP 420 namespace package (no __init__.py) or plain
                # source directory: recurse with the same exclusion
                # rules so its modules aren't silently invisible.
                py_files.extend(_collect_packages(child, excluded))

    return py_files


def _detect_source_root(project_path: Path) -> str | None:
    """Detect the source root directory from project config files.

    Checks pyproject.toml and setup.py (via AST) for package directory
    declarations like ``package_dir = {"": "src"}`` or
    ``[tool.setuptools.packages.find] where = ["lib"]``.
    """
    # Check pyproject.toml
    pyproject_path = project_path / "pyproject.toml"
    if pyproject_path.exists():
        root = _source_root_from_pyproject(pyproject_path)
        if root:
            return root

    # Check setup.py via AST
    setup_path = project_path / "setup.py"
    if setup_path.exists():
        root = _source_root_from_setup_py(setup_path)
        if root:
            return root

    return None


def _source_root_from_pyproject(pyproject_path: Path) -> str | None:
    """Extract source root from pyproject.toml."""
    try:
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return None

    # [tool.setuptools.packages.find] where = ["src"]
    packages = data.get("tool", {}).get("setuptools", {}).get("packages", {})
    if not isinstance(packages, dict):
        packages = {}
    find = packages.get("find", {})
    where = find.get("where")
    if isinstance(where, list) and len(where) == 1:
        return where[0]

    # [tool.setuptools.package-dir] "" = "src"
    pkg_dir = data.get("tool", {}).get("setuptools", {}).get("package-dir", {})
    root = pkg_dir.get("")
    if root:
        return root

    return None


def _source_root_from_setup_py(setup_path: Path) -> str | None:
    """Extract source root from setup.py using AST only.

    Looks for package_dir={"": "src"} in setup() calls.
    Never executes setup.py.
    """
    tree = _parse_file(setup_path)
    if tree is None:
        return None

    # Collect variable assignments
    variables: dict[str, dict[str, str] | None] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and isinstance(node.value, ast.Dict):
                variables[target.id] = _extract_string_dict(node.value)

    # Find setup() call and look for package_dir kwarg
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_setup_call(node):
            continue
        for kw in node.keywords:
            if kw.arg != "package_dir":
                continue
            if isinstance(kw.value, ast.Dict):
                d = _extract_string_dict(kw.value)
            elif isinstance(kw.value, ast.Name) and kw.value.id in variables:
                d = variables[kw.value.id]
            else:
                continue
            if d and "" in d:
                return d[""]

    return None


def _extract_string_dict(node: ast.Dict) -> dict[str, str] | None:
    """Extract a dict of string->string from an ast.Dict node."""
    result = {}
    for key, value in zip(node.keys, node.values):
        if (
            isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            result[key.value] = value.value
        else:
            return None
    return result


def inspect_project(
    project_path: Path,
    known_import_names: set[str],
) -> dict[str, UsageEntry]:
    """Inspect project source and map imports to known dependencies."""
    source_files = find_project_source(project_path)
    return inspect_source_files(source_files, known_import_names)


def inspect_source_files(
    source_files: list[Path],
    known_import_names: set[str],
) -> dict[str, UsageEntry]:
    """Inspect explicit source files for dependency usage.

    Extracted from ``inspect_project`` to support package mode,
    where source files are already known (from the installed
    package's source paths) and don't need project discovery.
    """
    usage, _ = inspect_source_files_counted(source_files, known_import_names)
    return usage


def inspect_source_files_counted(
    source_files: list[Path],
    known_import_names: set[str],
) -> tuple[dict[str, UsageEntry], int]:
    """Inspect source files and count their total LLOC in one parse pass.

    Returns ``(usage, total_lloc)``.  Parsing dominates inspection cost,
    so counting LLOC from the already-parsed tree avoids the second
    parse of every project file the CLI used to do via ``count_lloc``.
    """
    if not source_files:
        return {}, 0

    all_imports: list[ImportInfo] = []
    all_attr_accesses: dict[str, set[str]] = {}
    all_string_refs: dict[str, list[StringRef]] = {}
    total_lloc = 0

    for f in source_files:
        tree = _parse_file(f)
        if tree is None:
            warnings.warn(f"Could not parse {f}, skipping", stacklevel=2)
            continue
        total_lloc += _count_statements(tree)
        _walk_source_file(
            tree,
            f,
            known_import_names,
            all_imports,
            all_attr_accesses,
            all_string_refs,
        )

    usage = _resolve_imports(all_imports, all_attr_accesses, known_import_names)

    for imp_name, refs in all_string_refs.items():
        if imp_name not in usage:
            usage[imp_name] = {
                "used_names": set(),
                "files": set(),
                "imports": [],
                "string_refs": refs,
            }
        else:
            usage[imp_name].setdefault("string_refs", []).extend(refs)

    return usage, total_lloc


def _resolve_imports(
    all_imports: list[ImportInfo],
    all_attr_accesses: dict[str, set[str]],
    known_import_names: set[str],
) -> dict[str, UsageEntry]:
    """Map imports to known dependencies and resolve used names.

    For bare imports (``import X``), resolves attribute accesses like
    ``X.get()`` to used name ``get``. Handles submodule imports and aliases.
    """
    usage: dict[str, UsageEntry] = {}

    for imp in all_imports:
        top_level = _top_level_module(imp.module)
        if top_level not in known_import_names:
            continue

        if top_level not in usage:
            usage[top_level] = {
                "used_names": set(),
                "files": set(),
                "imports": [],
            }

        entry = usage[top_level]
        entry["imports"].append(imp)
        if imp.source_file:
            entry["files"].add(imp.source_file)

        if imp.name:
            entry["used_names"].add(imp.name)
            # Also resolve attribute accesses on the imported name.
            # Handles `from pkg import submod; submod.func()` where
            # submod is a submodule, not a leaf name.
            local_name = imp.alias or imp.name
            attrs = all_attr_accesses.get(local_name, set())
            if attrs:
                entry["used_names"].update(attrs)
        else:
            _resolve_bare_import(imp, all_attr_accesses, entry)

    return usage


def _resolve_bare_import(
    imp: ImportInfo,
    all_attr_accesses: dict[str, set[str]],
    entry: UsageEntry,
) -> None:
    """Resolve used names for a bare import (``import X`` or ``import X as Y``).

    Looks up attribute accesses on the local name and, for submodule imports
    like ``import pygments.lexers``, also checks accesses on the submodule name.

    If no attribute accesses are found (just ``import X`` with no ``X.anything``),
    we add the module name itself as a used name — the dependency is referenced
    even if we can't determine which specific functions are called.
    """
    local_name = imp.alias or imp.module
    attrs = all_attr_accesses.get(local_name, set())
    if attrs:
        entry["used_names"].update(attrs)

    # For ``import a.b``, also check accesses on the leaf module name ``b``
    # since code may write ``b.func()`` after ``import a.b``.
    if "." in imp.module:
        submodule = imp.module.rsplit(".", 1)[1]
        sub_attrs = all_attr_accesses.get(submodule, set())
        if sub_attrs:
            entry["used_names"].update(sub_attrs)

    # Fallback: no attribute accesses found — record the module itself.
    if not attrs and "." not in imp.module:
        entry["used_names"].add(imp.module)


def _top_level_module(dotted_name: str) -> str:
    """Extract the top-level module from a dotted module path.

    'requests.auth' -> 'requests'
    'xml.etree.ElementTree' -> 'xml'
    'click' -> 'click'
    """
    return dotted_name.split(".")[0]
