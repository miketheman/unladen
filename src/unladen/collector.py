"""Phase 1: The Nest — Dependency Discovery.

Finds declared dependencies and maps them to their installed source locations.
"""

import ast
import configparser
import importlib.metadata
import os
import re
import tomllib
from pathlib import Path
from typing import NotRequired, TypedDict

from unladen._parsing import is_setup_call as _is_setup_call
from unladen._parsing import parse_file as _parse_file

# Match the package name portion of a PEP 508 dependency string,
# stopping before version specifiers, extras, or markers.
_RE_DEP_NAME = re.compile(r"^([A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?)")
# PEP 503: runs of [-_.] are equivalent and collapsed to a single hyphen.
_RE_SEPARATOR = re.compile(r"[-_.]+")
# Inline comment in a requirements line: '#' preceded by whitespace.
# A bare '#' inside a URL fragment (e.g. '#egg=pkg') is not a comment.
_RE_INLINE_COMMENT = re.compile(r"\s#")


class DepInfo(TypedDict):
    """Metadata for a single declared dependency."""

    version: str | None
    import_names: list[str]
    paths: list[Path]
    installed: bool
    # PEP 508 environment marker text (after ';'), if the dependency
    # was declared conditionally (e.g. 'sys_platform == "win32"').
    marker: NotRequired[str | None]


def collect_dependencies(
    project_path: Path,
    *,
    site_packages: Path | None = None,
    requirements: str | None = None,
) -> dict[str, DepInfo]:
    """Collect declared dependencies and locate their installed source code."""
    specs, _ = _find_dependencies(project_path, requirements=requirements)
    if not specs:
        return {}

    sp = site_packages or discover_site_packages(project_path)
    if sp is None:
        raise FileNotFoundError(
            "Could not discover a virtual environment. "
            "Use --site-packages to specify one explicitly."
        )

    declared, markers = _names_and_markers(specs)
    return resolve_installed(declared, sp, markers=markers)


def parse_dependencies(
    project_path: Path,
    *,
    requirements: str | None = None,
) -> list[str]:
    """Parse dependency names from project metadata or requirements files.

    Returns a list of PEP 508 dependency names (without version specifiers).
    """
    specs, _ = _find_dependencies(project_path, requirements=requirements)
    return [_normalize_dep_name(spec) for spec in specs]


def _names_and_markers(specs: list[str]) -> tuple[list[str], dict[str, str]]:
    """Split raw PEP 508 specs into normalized names and their markers.

    Returns (names, {name: marker}) where markers only holds entries
    for conditionally declared deps.  First marker wins on duplicates.
    """
    names: list[str] = []
    markers: dict[str, str] = {}
    for spec in specs:
        name = _normalize_dep_name(spec)
        names.append(name)
        marker = _extract_marker(spec)
        if marker and name not in markers:
            markers[name] = marker
    return names, markers


def _extract_marker(spec: str) -> str | None:
    """Extract the environment marker text from a PEP 508 spec.

    ``'colorama ; sys_platform == "win32"'`` -> ``'sys_platform == "win32"'``
    ``'requests>=2.0'`` -> ``None``
    """
    _, sep, marker = spec.partition(";")
    if not sep:
        return None
    marker = marker.strip()
    return marker or None


def dependency_source(
    project_path: Path,
    *,
    requirements: str | None = None,
) -> str:
    """Return the filename where dependencies were found."""
    _, source = _find_dependencies(project_path, requirements=requirements)
    return source


def _find_dependencies(
    project_path: Path,
    *,
    requirements: str | None = None,
) -> tuple[list[str], str]:
    """Find dependencies and which file they came from.

    Args:
        project_path: Root of the project to scan.
        requirements: Explicit requirements file path (relative or absolute).
            When provided, skips all auto-detection.

    Returns (raw_dep_specs, source_filename).  Specs keep their version
    constraints and environment markers; callers normalize as needed.
    """
    if requirements:
        req_path = Path(requirements)
        if not req_path.is_absolute():
            req_path = project_path / req_path
        if not req_path.exists():
            raise FileNotFoundError(f"Requirements file not found: {req_path}")
        deps = _parse_explicit(req_path)
        if deps:
            try:
                label = str(req_path.relative_to(project_path))
            except ValueError:
                label = str(req_path)
            return deps, label
        raise FileNotFoundError(f"No dependencies found in {req_path}")

    pyproject_path = project_path / "pyproject.toml"
    if pyproject_path.exists():
        deps = _parse_pyproject_toml(pyproject_path)
        if deps:
            return deps, "pyproject.toml"

    setup_path = project_path / "setup.py"
    if setup_path.exists():
        deps = _parse_setup_py(setup_path)
        if deps:
            return deps, "setup.py"

    setup_cfg_path = project_path / "setup.cfg"
    if setup_cfg_path.exists():
        deps = _parse_setup_cfg(setup_cfg_path)
        if deps:
            return deps, "setup.cfg"

    # requirements.txt as last fallback
    req_path = project_path / "requirements.txt"
    if req_path.exists():
        deps = _parse_requirements_txt(req_path)
        if deps:
            return deps, "requirements.txt"

    raise FileNotFoundError(
        f"No dependencies found in pyproject.toml, setup.py, "
        f"setup.cfg, or requirements.txt at {project_path}"
    )


def _parse_explicit(file_path: Path) -> list[str]:
    """Parse an explicitly provided file, dispatching by name/extension."""
    name = file_path.name
    if name == "pyproject.toml":
        return _parse_pyproject_toml(file_path)
    if name == "setup.py":
        return _parse_setup_py(file_path)
    if name == "setup.cfg":
        return _parse_setup_cfg(file_path)
    # Default: treat as requirements.txt format
    return _parse_requirements_txt(file_path)


def _parse_pyproject_toml(pyproject_path: Path) -> list[str]:
    """Parse dependencies from pyproject.toml.

    Supports both PEP 621 [project] dependencies and Poetry's
    [tool.poetry.dependencies] table.
    """
    with open(pyproject_path, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"Invalid TOML in {pyproject_path}: {exc}") from exc

    # PEP 621: [project] dependencies = [...]
    raw_deps = data.get("project", {}).get("dependencies", [])
    if raw_deps:
        return [dep for dep in raw_deps if isinstance(dep, str)]

    # Poetry: [tool.poetry.dependencies] as a dict of name -> version spec
    poetry_deps = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
    if poetry_deps:
        specs = []
        for name, value in poetry_deps.items():
            if name.lower() == "python":
                continue
            # Poetry's dict form carries markers separately:
            # colorama = { version = "*", markers = "sys_platform == 'win32'" }
            if isinstance(value, dict) and value.get("markers"):
                specs.append(f"{name} ; {value['markers']}")
            else:
                specs.append(name)
        return specs

    return []


def _parse_setup_py(setup_path: Path) -> list[str]:
    """Parse install_requires from setup.py using AST only.

    Handles two common patterns:
    1. Literal list passed directly: setup(install_requires=["pkg", ...])
    2. Variable assignment: reqs = [...]; setup(install_requires=reqs)

    Never executes setup.py code.
    """
    tree = _parse_file(setup_path)
    if tree is None:
        return []

    variables = _collect_list_variables(tree)
    strings = _resolve_setup_list_kwarg(tree, "install_requires", variables)
    if strings is not None:
        return strings
    return []


def _collect_list_variables(tree) -> dict[str, list[str]]:
    """Collect top-level variable assignments of string lists."""

    variables: dict[str, list[str]] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and isinstance(node.value, ast.List):
                strings = _extract_string_list(node.value)
                if strings is not None:
                    variables[target.id] = strings
    return variables


def _resolve_setup_list_kwarg(
    tree, kwarg_name: str, variables: dict[str, list[str]]
) -> list[str] | None:
    """Find a setup() keyword argument and resolve it to a string list."""

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_setup_call(node):
            continue
        for kw in node.keywords:
            if kw.arg != kwarg_name:
                continue
            if isinstance(kw.value, ast.List):
                strings = _extract_string_list(kw.value)
                if strings is not None:
                    return strings
            if isinstance(kw.value, ast.Name) and kw.value.id in variables:
                return variables[kw.value.id]
    return None


def _parse_setup_cfg(setup_cfg_path: Path) -> list[str]:
    """Parse install_requires from setup.cfg [options] section."""
    config = configparser.ConfigParser()
    try:
        config.read(setup_cfg_path, encoding="utf-8")
    except configparser.Error as exc:
        raise ValueError(f"Invalid setup.cfg at {setup_cfg_path}: {exc}") from exc

    raw = config.get("options", "install_requires", fallback="")
    if not raw.strip():
        return []

    return [line.strip() for line in raw.strip().splitlines() if line.strip()]


def _parse_requirements_txt(
    req_path: Path,
    *,
    _seen: set[Path] | None = None,
) -> list[str]:
    """Parse dependencies from a requirements.txt file.

    Handles:
    - Package specifiers: ``requests>=2.0``
    - Comments and blank lines
    - ``-r``/``--requirement`` includes (with cycle detection)

    Ignores pip options (``-i``, ``-e``, ``--find-links``, etc.)
    and lines with URL/path specifiers.
    """

    if _seen is None:
        _seen = set()

    resolved = req_path.resolve()
    if resolved in _seen:
        return []
    _seen.add(resolved)

    try:
        lines = req_path.read_text(encoding="utf-8").splitlines()
    except OSError, UnicodeDecodeError:
        return []

    deps: list[str] = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Strip inline comments first so a comment containing a URL or
        # option text doesn't trigger the skip checks below.
        comment = _RE_INLINE_COMMENT.search(line)
        if comment:
            line = line[: comment.start()].rstrip()
        if not line:
            continue

        # -r / --requirement includes
        include_match = re.match(r"^(?:-r|--requirement)\s+(.+)$", line)
        if include_match:
            include_path = Path(include_match.group(1).strip())
            if not include_path.is_absolute():
                include_path = req_path.parent / include_path
            deps.extend(_parse_requirements_txt(include_path, _seen=_seen))
            continue

        # Skip pip options and flags
        if line.startswith("-"):
            continue

        # Skip URLs, paths, and editable installs
        if "://" in line or line.startswith(".") or line.startswith("/"):
            continue

        deps.append(line)

    return deps


def _extract_string_list(node) -> list[str] | None:
    """Extract a list of constant strings from an ast.List node."""

    strings = []
    for elt in node.elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            strings.append(elt.value)
        else:
            return None  # non-string element, bail
    return strings


def discover_site_packages(project_path: Path) -> Path | None:
    """Discover site-packages for the project's virtual environment.

    Priority:
    1. .venv/ in project root (most specific — the project's own venv)
    2. VIRTUAL_ENV environment variable (may be the tool's own venv,
       so only used if the project doesn't have its own .venv)
    """
    # 1. .venv/ in project root
    dot_venv = project_path / ".venv"
    if dot_venv.is_dir():
        sp = _find_site_packages_in_venv(dot_venv)
        if sp:
            return sp

    # 2. VIRTUAL_ENV env var (fallback)
    venv_env = os.environ.get("VIRTUAL_ENV")
    if venv_env:
        sp = _find_site_packages_in_venv(Path(venv_env))
        if sp:
            return sp

    return None


def resolve_installed(
    dep_names: list[str],
    site_packages: Path,
    markers: dict[str, str] | None = None,
) -> dict[str, DepInfo]:
    """Resolve declared dependencies to their installed locations.

    Uses ``importlib.metadata.Distribution.at()`` with a direct path to
    the .dist-info directory, avoiding ``importlib.metadata.distributions()``
    which only searches the current interpreter's sys.path.  This lets
    us analyze a project's venv without activating it.

    *markers* maps dep names to their PEP 508 environment marker text.
    A marker-gated dep that is absent was most likely skipped by the
    installer because its marker is false in this environment, so the
    reporter shows it as "not applicable" rather than "not installed".
    """
    result: dict[str, DepInfo] = {}
    dist_infos = _dist_info_index(site_packages)
    markers = markers or {}

    for dep_name in dep_names:
        try:
            dist = importlib.metadata.Distribution.at(
                _find_dist_info(dep_name, site_packages, dist_infos)
            )
        except FileNotFoundError:
            # Dependency declared but not installed
            dep_info: DepInfo = {
                "version": None,
                "import_names": [],
                "paths": [],
                "installed": False,
                "marker": markers.get(dep_name),
            }
            result[dep_name] = dep_info
            continue

        version = dist.metadata["Version"]
        import_names = _get_import_names(dist, dep_name, site_packages)
        paths = _get_source_paths(import_names, site_packages, dist)

        dep_info: DepInfo = {
            "version": version,
            "import_names": import_names,
            "paths": paths,
            "installed": True,
            "marker": markers.get(dep_name),
        }
        result[dep_name] = dep_info

    return result


def resolve_package_info(
    package_name: str,
    site_packages: Path,
) -> tuple[str | None, list[str], list[Path]]:
    """Resolve an installed package to its version, import names, and source paths.

    Used in package mode (``unladen check requests``) to get metadata
    for the target package itself, separate from its dependencies.
    Raises FileNotFoundError if the package is not installed.
    """
    dist = importlib.metadata.Distribution.at(
        _find_dist_info(package_name, site_packages)
    )
    version = dist.metadata["Version"]
    import_names = _get_import_names(dist, package_name, site_packages)
    paths = _get_source_paths(import_names, site_packages, dist)
    return version, import_names, paths


def collect_package_deps(
    package_name: str,
    site_packages: Path,
) -> dict[str, DepInfo]:
    """Collect dependencies declared in a package's Requires-Dist metadata.

    Reads the installed package's METADATA to find its declared
    dependencies, then resolves them in site-packages.
    Skips extras-only deps (``; extra == "dev"``) since those are
    optional and not activated by default installation.
    """
    dist = importlib.metadata.Distribution.at(
        _find_dist_info(package_name, site_packages)
    )
    requires = dist.metadata.get_all("Requires-Dist") or []
    specs = []
    for req in requires:
        # Skip extras-only deps: "foo ; extra == 'bar'"
        if "extra ==" in req or "extra==" in req:
            continue
        specs.append(req)

    if not specs:
        return {}
    dep_names, markers = _names_and_markers(specs)
    return resolve_installed(dep_names, site_packages, markers=markers)


def _normalize_dep_name(dep_spec: str) -> str:
    """Extract and normalize the package name from a PEP 508 dependency string.

    Strips version specifiers, extras, and markers, then applies PEP 503
    normalization (lowercase, runs of [-_.] collapsed to single hyphen).

    ``'requests>=2.28,<3'`` -> ``'requests'``
    ``'Jinja2[i18n]'`` -> ``'jinja2'``
    ``'zope.interface'`` -> ``'zope-interface'``
    """
    match = _RE_DEP_NAME.match(dep_spec)
    if not match:
        return dep_spec.strip().lower()
    return _RE_SEPARATOR.sub("-", match.group(1)).lower()


def _find_site_packages_in_venv(venv_path: Path) -> Path | None:
    """Find the site-packages directory within a venv."""
    # Unix: lib/pythonX.Y/site-packages
    lib_dir = venv_path / "lib"
    if lib_dir.is_dir():
        for child in lib_dir.iterdir():
            if child.name.startswith("python"):
                sp = child / "site-packages"
                if sp.is_dir():
                    return sp
    # Windows: Lib/site-packages
    sp = venv_path / "Lib" / "site-packages"
    if sp.is_dir():
        return sp
    return None


def _dist_info_index(site_packages: Path) -> dict[str, Path]:
    """Index .dist-info/.egg-info directories by normalized package name.

    Built once per site-packages scan so resolving many dependencies
    doesn't re-list the directory for each one.
    """
    index: dict[str, Path] = {}
    for entry in site_packages.iterdir():
        if entry.suffix in (".dist-info", ".egg-info"):
            # dist-info dirs are named like: package_name-version.dist-info
            dir_name = entry.stem.lower()  # e.g. "requests-2.31.0"
            pkg_part = _RE_SEPARATOR.sub("_", dir_name.rsplit("-", 1)[0])
            index.setdefault(pkg_part, entry)
    return index


def _find_dist_info(
    dep_name: str,
    site_packages: Path,
    index: dict[str, Path] | None = None,
) -> Path:
    """Find the .dist-info directory for a given distribution name.

    Names are matched with all separator runs ([-_.]+) collapsed to a
    single underscore, so legacy metadata dirs that keep dots
    (e.g. ``zope.interface-6.0.egg-info``) still match.
    """
    if index is None:
        index = _dist_info_index(site_packages)
    normalized = _RE_SEPARATOR.sub("_", dep_name.lower())
    try:
        return index[normalized]
    except KeyError:
        raise FileNotFoundError(f"No dist-info found for {dep_name}") from None


def _get_import_names(
    dist: importlib.metadata.Distribution,
    dep_name: str,
    site_packages: Path,
) -> list[str]:
    """Determine the importable top-level names for a distribution."""
    # Try top_level.txt first
    top_level = dist.read_text("top_level.txt")
    if top_level:
        return [name.strip() for name in top_level.strip().splitlines() if name.strip()]

    # Fall back to RECORD: find top-level packages/modules
    record = dist.read_text("RECORD")
    if record:
        top_names: set[str] = set()
        for line in record.splitlines():
            path_str = line.split(",")[0]
            if not path_str:
                continue
            if "/" in path_str:
                top_dir = path_str.split("/")[0]
                # Skip dist-info, egg-info, and hidden directories
                if top_dir.endswith((".dist-info", ".egg-info", ".data")):
                    continue
                if top_dir.startswith(("_", ".")):
                    continue
                top_names.add(top_dir)
            elif path_str.endswith(".py"):
                # Top-level module (e.g. cgi.py from legacy-cgi)
                top_names.add(path_str.removesuffix(".py"))
        if top_names:
            return sorted(top_names)

    # Last resort: guess from distribution name
    return [dep_name.replace("-", "_")]


def _get_source_paths(
    import_names: list[str],
    site_packages: Path,
    dist: importlib.metadata.Distribution | None = None,
) -> list[Path]:
    """Find the actual source paths for the given import names.

    For namespace packages (directory without __init__.py), uses the
    distribution's RECORD to narrow to owned subdirectories only.
    """
    paths = []
    for name in import_names:
        paths.extend(_resolve_import_path(name, site_packages, dist))
    return paths


def _resolve_import_path(
    name: str,
    site_packages: Path,
    dist: importlib.metadata.Distribution | None,
) -> list[Path]:
    """Resolve a single import name to its source path(s)."""
    pkg_dir = site_packages / name
    if pkg_dir.is_dir():
        if (pkg_dir / "__init__.py").exists():
            return [pkg_dir]
        # Namespace package — narrow to owned subdirs via RECORD
        return _resolve_namespace_package(name, pkg_dir, site_packages, dist)

    # Single-file module
    py_file = site_packages / f"{name}.py"
    if py_file.is_file():
        return [py_file]
    return []


def _resolve_namespace_package(
    name: str,
    pkg_dir: Path,
    site_packages: Path,
    dist: importlib.metadata.Distribution | None,
) -> list[Path]:
    """Resolve a namespace package to its owned subdirectories.

    Namespace packages (`PEP 420 <https://peps.python.org/pep-0420/>`_)
    have no __init__.py and can be split
    across multiple distributions (e.g. ``zope.interface`` and
    ``zope.deprecation`` share the ``zope/`` directory).  We use the
    distribution's RECORD file to narrow to only the subdirectories
    that belong to this specific distribution.
    """
    owned = _owned_subpackages(name, dist) if dist else []
    if not owned:
        return [pkg_dir]
    paths = []
    for sub in owned:
        sub_dir = site_packages / sub
        if sub_dir.is_dir():
            paths.append(sub_dir)
    return paths or [pkg_dir]


def _owned_subpackages(
    namespace: str, dist: importlib.metadata.Distribution
) -> list[str]:
    """Find subpackages owned by a distribution within a namespace package.

    Parses RECORD to find paths like 'zope/deprecation/...' and returns
    the unique subpackage paths like 'zope/deprecation'.
    """
    record = dist.read_text("RECORD")
    if not record:
        return []

    prefix = namespace + "/"
    subpackages: set[str] = set()
    for line in record.splitlines():
        path_str = line.split(",")[0]
        if not path_str.startswith(prefix):
            continue
        # e.g. "zope/deprecation/__init__.py" -> "zope/deprecation"
        rest = path_str[len(prefix) :]
        if "/" in rest:
            subdir = rest.split("/")[0]
            if not subdir.endswith((".dist-info", ".egg-info")):
                subpackages.add(f"{namespace}/{subdir}")

    return sorted(subpackages)
