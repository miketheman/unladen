"""Configuration loading and shared CLI helpers.

Functions used by both the ``check`` and ``show`` commands
to load project configuration and resolve dependency maps.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from unladen.collector import DepInfo


def load_config(project_path: Path) -> dict[str, Any]:
    """Load ``[tool.unladen]`` configuration from pyproject.toml.

    Returns an empty dict if pyproject.toml is missing or has no
    ``[tool.unladen]`` section.
    """
    pyproject = project_path / "pyproject.toml"
    if not pyproject.exists():
        return {}
    try:
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return {}
    return data.get("tool", {}).get("unladen", {}) or {}


def load_exclude_set(project_path: Path) -> set[str]:
    """Load and normalize the ``exclude`` list from ``[tool.unladen]``."""
    from unladen.collector import _normalize_dep_name

    config = load_config(project_path)
    return {_normalize_dep_name(n) for n in config.get("exclude", [])}


def load_dep_map(
    project_path: Path,
    req_file: str | None,
    site_packages: Path | None = None,
) -> tuple[dict[str, DepInfo], Path | None] | None:
    """Load dependency map, returning None on error.

    Returns ``(dep_map, site_packages)`` so callers needing the
    environment later (e.g. ``--transitive``) analyze the same
    site-packages Phase 1 resolved, instead of re-discovering it.

    Handles missing project paths, missing dependency files
    (FileNotFoundError from collector when no pyproject.toml,
    setup.py, setup.cfg, or requirements.txt is found), and
    malformed configuration files (ValueError from collector).
    """
    from unladen.collector import collect_dependencies, discover_site_packages

    if not project_path.exists():
        print(f"Error: path does not exist: {project_path}", file=sys.stderr)
        return None

    # Resolve site-packages once; collect_dependencies would otherwise
    # discover internally and discard the path.
    sp = site_packages or discover_site_packages(project_path)
    try:
        dep_map = collect_dependencies(
            project_path,
            site_packages=sp,
            requirements=req_file,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return None
    return dep_map, sp
