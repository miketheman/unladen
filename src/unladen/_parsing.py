"""Shared AST parsing and analysis utilities."""

import ast
import warnings
from pathlib import Path


def parse_file(file_path: Path) -> ast.Module | None:
    """Parse a Python file, returning None on failure.

    Suppresses ``SyntaxWarning`` because ``ast.parse`` emits warnings
    for invalid escape sequences in third-party code we don't control.
    """
    try:
        source = file_path.read_text(encoding="utf-8")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            return ast.parse(source, filename=str(file_path))
    except SyntaxError, UnicodeDecodeError:
        return None


def is_setup_call(node: ast.Call) -> bool:
    """Check if a Call node is ``setup()`` or ``setuptools.setup()``."""
    if isinstance(node.func, ast.Name) and node.func.id == "setup":
        return True
    if (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "setup"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "setuptools"
    ):
        return True
    return False
