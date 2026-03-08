"""Tests for LLOC counting and TYPE_CHECKING detection."""

import ast
import textwrap

import pytest

from unladen._lloc import count_lloc, is_type_checking_block


class TestCountLloc:
    """Test logical lines of code counting."""

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.py"
        f.write_text("")
        assert count_lloc(f) == 0

    def test_comments_and_blanks_excluded(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            # A comment

            # Another comment
        """)
        )
        assert count_lloc(f) == 0

    def test_docstrings_excluded(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent('''\
            """Module docstring."""

            def foo():
                """Function docstring."""
                return 1
        ''')
        )
        # Only `def foo():` and `return 1` are logical lines
        assert count_lloc(f) == 2

    def test_counts_statements(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            x = 1
            y = 2
            z = x + y
        """)
        )
        assert count_lloc(f) == 3

    def test_function_def_counts(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            def add(a, b):
                return a + b

            def mul(a, b):
                result = a * b
                return result
        """)
        )
        # def add, return a+b, def mul, result=, return result
        assert count_lloc(f) == 5

    def test_class_def_counts(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            class Foo:
                def __init__(self):
                    self.x = 1

                def method(self):
                    return self.x
        """)
        )
        # class Foo, def __init__, self.x=1, def method, return self.x
        assert count_lloc(f) == 5

    def test_type_checking_block_excluded(self, tmp_path):
        """Statements inside `if TYPE_CHECKING:` should not be counted."""
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            from __future__ import annotations
            from typing import TYPE_CHECKING

            if TYPE_CHECKING:
                from os import PathLike
                from typing import Any

            def real_func():
                return 1
        """)
        )
        # from __future__ (1) + from typing (1) + def real_func (1) + return (1) = 4
        # The TYPE_CHECKING block (if + 2 imports) should be excluded
        assert count_lloc(f) == 4

    def test_typing_dot_type_checking_excluded(self, tmp_path):
        """Statements inside `if typing.TYPE_CHECKING:` should not be counted."""
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            import typing

            if typing.TYPE_CHECKING:
                from collections.abc import Sequence

            def real_func():
                return 1
        """)
        )
        # import typing (1) + def real_func (1) + return (1) = 3
        assert count_lloc(f) == 3

    def test_syntax_error_returns_zero(self, tmp_path):
        f = tmp_path / "bad.py"
        f.write_text("def broken(\n")
        assert count_lloc(f) == 0

    def test_non_string_constant_not_docstring(self, tmp_path):
        """An integer Expr at module level should be counted (not a docstring)."""
        f = tmp_path / "mod.py"
        f.write_text("42\nx = 1\n")
        # 42 is an Expr with Constant(int), x = 1 is Assign
        assert count_lloc(f) == 2

    def test_match_statement_counts_case_bodies(self, tmp_path):
        """Statements inside match/case clauses should be counted."""
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            def handle(cmd):
                match cmd:
                    case "start":
                        x = 1
                        return x
                    case "stop":
                        return 0
                    case _:
                        raise ValueError(cmd)
        """)
        )
        # def handle (1) + match (1) + x=1 (1) + return x (1)
        # + return 0 (1) + raise (1) = 6
        assert count_lloc(f) == 6

    def test_string_not_first_in_body(self, tmp_path):
        """A string Expr that is not the first statement should be counted."""
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            x = 1
            "not a docstring"
            y = 2
        """)
        )
        # x=1, "not a docstring" (counted — not first), y=2
        assert count_lloc(f) == 3


class TestIsTypeCheckingBlock:
    """Test detection of TYPE_CHECKING guard blocks."""

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("if TYPE_CHECKING:\n    pass\n", True),
            ("if typing.TYPE_CHECKING:\n    pass\n", True),
            ("if True:\n    pass\n", False),
            ("x = 1\n", False),
        ],
        ids=["bare", "typing-dot", "regular-if", "non-if"],
    )
    def test_detection(self, code, expected):
        tree = ast.parse(code)
        node = tree.body[0]
        assert is_type_checking_block(node) is expected
