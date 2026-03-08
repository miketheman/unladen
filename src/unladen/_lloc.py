"""Logical Lines of Code (LLOC) counting.

General-purpose AST utilities for counting executable statements,
excluding docstrings, comments, and ``if TYPE_CHECKING:`` blocks.
Used by both the tracer (for heft computation) and the CLI
(for project LLOC totals).
"""

import ast
from pathlib import Path

from unladen._parsing import parse_file as _parse_file


def count_lloc(file_path: Path) -> int:
    """Count Logical Lines of Code in a Python file.

    LLOC counts executable statements, excluding:
    - Blank lines
    - Comment-only lines
    - Docstrings (module, class, and function level)

    Uses ast to count statement nodes, which naturally excludes
    comments and blank lines. Docstring Expr nodes are also excluded.
    """
    tree = _parse_file(file_path)
    if tree is None:
        return 0
    return count_statements(tree)


def is_type_checking_block(node: ast.AST) -> bool:
    """Check if a node is an ``if TYPE_CHECKING:`` block.

    These blocks contain type stubs for static analysis tools and are
    never executed at runtime, so they should be excluded from LLOC
    counting and definition extraction.
    """
    if not isinstance(node, ast.If):
        return False
    test = node.test
    # `if TYPE_CHECKING:`
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    # `if typing.TYPE_CHECKING:`
    if (
        isinstance(test, ast.Attribute)
        and test.attr == "TYPE_CHECKING"
        and isinstance(test.value, ast.Name)
    ):
        return True
    return False


def count_statements(node: ast.AST) -> int:
    """Recursively count statement nodes, excluding docstrings."""
    count = 0

    for child in ast.iter_child_nodes(node):
        # Skip TYPE_CHECKING blocks — type stubs, never executed at runtime
        if is_type_checking_block(child):
            continue

        if isinstance(child, ast.stmt):
            # Skip docstrings (Expr nodes containing a Constant string)
            if _is_docstring(child, node):
                continue
            count += 1

        # Recurse into compound statements (if/for/with/try/class/function bodies)
        if _has_body(child):
            count += count_statements(child)

    return count


def _is_docstring(node: ast.stmt, parent: ast.AST) -> bool:
    """Check if a statement node is a docstring."""
    if not isinstance(node, ast.Expr):
        return False
    if not isinstance(node.value, ast.Constant):
        return False
    if not isinstance(node.value.value, str):
        return False
    # It's a string expression — check if it's the first statement in a body
    if isinstance(
        parent, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    ):
        body = parent.body
        if body and body[0] is node:
            return True
    return False


def _has_body(node: ast.AST) -> bool:
    """Check if a node contains a body to recurse into."""
    return isinstance(
        node,
        (
            ast.FunctionDef,
            ast.AsyncFunctionDef,
            ast.ClassDef,
            ast.If,
            ast.For,
            ast.AsyncFor,
            ast.While,
            ast.With,
            ast.AsyncWith,
            ast.Try,
            ast.TryStar,
            ast.ExceptHandler,
            ast.Match,
            ast.match_case,
        ),
    )
