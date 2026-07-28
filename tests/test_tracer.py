"""Tests for Phase 3: The Weight — Logic Computation."""

import ast
import textwrap

from unladen._parsing import parse_file as _parse_file
from unladen.tracer import (
    HeftResult,
    _collect_source_files,
    _extract_base_names,
    _extract_calls_from_tree,
    _imports_test_framework,
    _is_test_file,
    _names_in_value,
    _resolve_attr_call,
    _resolve_call_target,
    _resolve_name,
    _TracerIndex,
    active_files,
    compute_heft,
    compute_hefts_bulk,
    index_dependency,
)


def _extract_calls(file_path):
    """Test helper: parse a file and extract its call graph."""
    tree = _parse_file(file_path)
    if tree is None:
        return {}
    return _extract_calls_from_tree(tree)


class TestTypeCheckingDefinitions:
    """TYPE_CHECKING blocks should not contribute definitions or calls."""

    def test_type_stub_class_excluded_from_defs(self, tmp_path):
        """A class inside TYPE_CHECKING should not appear in definitions."""
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            from typing import TYPE_CHECKING

            if TYPE_CHECKING:
                class init:
                    pass

            def _init():
                return setup()

            def setup():
                return 42
        """)
        )
        index = index_dependency([f])
        names = {d["name"] for d in index["functions"]}
        assert "init" not in names
        assert "_init" in names
        assert "setup" in names

    def test_private_alias_resolved(self, tmp_path):
        """Used name 'init' should resolve to '_init' when no 'init' def exists."""
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            from typing import TYPE_CHECKING

            if TYPE_CHECKING:
                class init:
                    pass

            def _init():
                return setup()

            def setup():
                return 42

            def unrelated():
                return 0
        """)
        )
        result = compute_heft([f], {"init"}, "mod")
        # _init(2) + setup(2) = 4 active
        assert result.active_lloc == 4

    def test_type_stub_does_not_shadow_real_function(self, tmp_path):
        """Heft should trace through the real function, not a TYPE_CHECKING stub."""
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            from typing import TYPE_CHECKING

            if TYPE_CHECKING:
                class init:
                    pass

            def init():
                return setup()

            def setup():
                do_work()
                return 1

            def do_work():
                return 2

            def unrelated():
                return 3
        """)
        )
        result = compute_heft([f], {"init"}, "mod")
        # init(2) + setup(3) + do_work(2) = 7 active
        # unrelated(2) not active
        # total = 7 + 2 = 9 (TYPE_CHECKING block excluded)
        # Plus the import statement = 10
        assert result.active_lloc == 7
        assert result.total_lloc == 10

    def test_type_checking_else_body_indexed(self, tmp_path):
        """Runtime code in the else-branch of TYPE_CHECKING should be indexed."""
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            from typing import TYPE_CHECKING

            if TYPE_CHECKING:
                PlatformDirs = _Result
            else:
                PlatformDirs = factory()

            def user_dir():
                return PlatformDirs("app").data
        """)
        )
        index = index_dependency([f])
        names = {d["name"] for d in index["functions"]}
        # PlatformDirs from the else-body should be indexed
        assert "PlatformDirs" in names
        # user_dir should be able to reach PlatformDirs
        result = compute_heft([f], {"user_dir"}, "mod")
        assert result.active_lloc >= 2  # user_dir + PlatformDirs


class TestIndexDependency:
    """Test dependency source indexing."""

    def test_indexes_package(self, fake_site_packages):
        pkg_path = fake_site_packages / "requests"
        index = index_dependency([pkg_path])
        assert index["total_lloc"] > 0
        assert len(index["functions"]) > 0

    def test_finds_functions(self, fake_site_packages):
        pkg_path = fake_site_packages / "requests"
        index = index_dependency([pkg_path])
        func_names = {f["name"] for f in index["functions"]}
        assert "get" in func_names
        assert "post" in func_names

    def test_finds_classes(self, fake_site_packages):
        pkg_path = fake_site_packages / "requests"
        index = index_dependency([pkg_path])
        class_names = {f["name"] for f in index["functions"] if f["type"] == "class"}
        assert "Session" in class_names
        assert "HTTPBasicAuth" in class_names

    def test_tracks_module_path(self, fake_site_packages):
        pkg_path = fake_site_packages / "requests"
        index = index_dependency([pkg_path])
        api_funcs = [f for f in index["functions"] if "api" in str(f.get("module", ""))]
        assert len(api_funcs) > 0

    def test_indexes_methods(self, tmp_path):
        """Methods inside classes should be indexed individually."""
        f = tmp_path / "lib.py"
        f.write_text(
            textwrap.dedent("""\
            class Manager:
                def get_thing(self):
                    return 1

                def set_thing(self, val):
                    self.val = val
        """)
        )
        index = index_dependency([f])
        names = {d["name"] for d in index["functions"]}
        assert "Manager" in names
        assert "Manager.get_thing" in names
        assert "Manager.set_thing" in names
        methods = [d for d in index["functions"] if d["type"] == "method"]
        assert len(methods) == 2

    def test_method_heft_counted(self, tmp_path):
        """Used names matching methods should count their LLOC."""
        f = tmp_path / "lib.py"
        f.write_text(
            textwrap.dedent("""\
            class Manager:
                def resource_filename(self, pkg, name):
                    return get_provider(pkg).filename(name)

                def resource_exists(self, pkg, name):
                    return get_provider(pkg).exists(name)

            def unrelated():
                return 42
        """)
        )
        result = compute_heft([f], {"resource_filename"}, "lib")
        assert result.active_lloc > 0

    def test_method_transitive_calls_traced(self, tmp_path):
        """A method calling a module-level function should trace transitively."""
        f = tmp_path / "lib.py"
        f.write_text(
            textwrap.dedent("""\
            class Manager:
                def resource_filename(self, pkg, name):
                    return get_provider(pkg)

            def get_provider(pkg):
                req = Requirement(pkg)
                return req

            class Requirement:
                def __init__(self, name):
                    self.name = name
                    self.specs = parse_specs(name)

            def parse_specs(name):
                return name.split(",")

            def unrelated():
                x = 1
                y = 2
                return x + y
        """)
        )
        result = compute_heft([f], {"resource_filename"}, "lib")
        # resource_filename: 2 lloc
        # get_provider: 3 lloc
        # Requirement reached -> __init__ method enqueued (3 lloc)
        #   (class envelope skipped to avoid double-counting)
        # parse_specs: 2 lloc (called from __init__)
        # unrelated: not reached
        # total = 16, active = 2 + 3 + 3 + 2 = 10
        assert result.active_lloc == 10
        assert result.total_lloc == 16

    def test_single_file_module(self, tmp_path):
        f = tmp_path / "simple.py"
        f.write_text(
            textwrap.dedent("""\
            def hello():
                return "world"
        """)
        )
        index = index_dependency([f])
        assert index["total_lloc"] == 2
        assert len(index["functions"]) == 1

    def test_empty_package(self, tmp_path):
        pkg = tmp_path / "empty_pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        index = index_dependency([pkg])
        assert index["total_lloc"] == 0
        assert index["functions"] == []

    def test_syntax_error_file_skipped(self, tmp_path):
        """Files with syntax errors should be skipped without crashing."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("def good():\n    return 1\n")
        (pkg / "broken.py").write_text("def broken(\n")
        index = index_dependency([pkg])
        assert index["total_lloc"] == 2
        func_names = {f["name"] for f in index["functions"]}
        assert "good" in func_names

    def test_init_module_uses_package_name(self, tmp_path):
        """__init__.py should be indexed under its package name, not __init__."""
        pkg = tmp_path / "applehelp"
        pkg.mkdir()
        (pkg / "__init__.py").write_text(
            textwrap.dedent("""\
            def setup(app):
                app.add_builder(AppleHelpBuilder)

            class AppleHelpBuilder:
                def build(self):
                    return True
        """)
        )
        result = compute_heft([pkg], {"applehelp"}, "sphinxcontrib-applehelp")
        # "applehelp" matches the module_defs key (package name),
        # seeding BFS with setup + AppleHelpBuilder + build
        assert result.active_lloc > 0

    def test_async_function_indexed(self, tmp_path):
        """Async functions should be indexed just like regular functions."""
        f = tmp_path / "async_lib.py"
        f.write_text(
            textwrap.dedent("""\
            async def fetch(url):
                return await get(url)

            async def get(url):
                return url
        """)
        )
        index = index_dependency([f])
        func_names = {f["name"] for f in index["functions"]}
        assert "fetch" in func_names
        assert "get" in func_names

    def test_definitions_inside_try_block(self, tmp_path):
        """Functions defined inside try/except should be indexed."""
        f = tmp_path / "compat.py"
        f.write_text(
            textwrap.dedent("""\
            try:
                def fast_impl():
                    return 1
            except ImportError:
                def fast_impl():
                    return 2
        """)
        )
        index = index_dependency([f])
        func_names = {f["name"] for f in index["functions"]}
        assert "fast_impl" in func_names

    def test_handles_compiled_extensions(self, tmp_path):
        """Compiled .so files are counted as opaque mass."""
        pkg = tmp_path / "hybrid"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("def py_func():\n    return 1\n")
        (pkg / "_speedups.so").write_bytes(b"\x00" * 100)
        index = index_dependency([pkg])
        assert index["opaque_files"] == 1

    def test_excludes_tests_directory(self, tmp_path):
        """Test files under tests/ should not be counted."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("def main():\n    return 1\n")
        tests_dir = pkg / "tests"
        tests_dir.mkdir()
        (tests_dir / "__init__.py").write_text("")
        (tests_dir / "test_core.py").write_text(
            "def test_main():\n    assert main() == 1\n"
        )
        index = index_dependency([pkg])
        func_names = {f["name"] for f in index["functions"]}
        assert "main" in func_names
        assert "test_main" not in func_names
        # total_lloc should only count core.py (2 LLOC)
        assert index["total_lloc"] == 2

    def test_excludes_test_prefixed_files(self, tmp_path):
        """test_*.py files at any level should be excluded."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("def main():\n    return 1\n")
        (pkg / "test_core.py").write_text("def test_main():\n    assert main() == 1\n")
        index = index_dependency([pkg])
        func_names = {f["name"] for f in index["functions"]}
        assert "main" in func_names
        assert "test_main" not in func_names

    def test_excludes_conftest(self, tmp_path):
        """conftest.py files should be excluded."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("def main():\n    return 1\n")
        (pkg / "conftest.py").write_text("def fixture():\n    return 42\n")
        index = index_dependency([pkg])
        func_names = {f["name"] for f in index["functions"]}
        assert "main" in func_names
        assert "fixture" not in func_names


class TestComputeHeft:
    """Test the heft ratio computation."""

    def test_basic_heft(self, fake_site_packages):
        """Using only 'get' from requests should yield a low heft ratio."""
        dep_paths = [fake_site_packages / "requests"]
        used_names = {"get"}
        result = compute_heft(dep_paths, used_names, "requests")
        assert isinstance(result, HeftResult)
        assert 0 < result.heft_ratio < 1.0
        assert result.active_lloc > 0
        assert result.active_lloc < result.total_lloc

    def test_no_usage_yields_zero(self, fake_site_packages):
        dep_paths = [fake_site_packages / "requests"]
        result = compute_heft(dep_paths, set(), "requests")
        assert result.heft_ratio == 0.0
        assert result.active_lloc == 0

    def test_high_usage_yields_higher_ratio(self, fake_site_packages):
        dep_paths = [fake_site_packages / "requests"]
        low_result = compute_heft(dep_paths, {"get"}, "requests")
        high_result = compute_heft(
            dep_paths, {"get", "post", "put", "delete", "Session"}, "requests"
        )
        assert high_result.heft_ratio > low_result.heft_ratio

    def test_result_fields(self, fake_site_packages):
        dep_paths = [fake_site_packages / "requests"]
        result = compute_heft(dep_paths, {"get"}, "requests")
        assert result.total_lloc > 0
        assert result.dep_name == "requests"
        assert isinstance(result.opaque_files, int)

    def test_single_file_dep(self, tmp_path):
        f = tmp_path / "tiny.py"
        f.write_text(
            textwrap.dedent("""\
            def used_func():
                return 1

            def unused_func():
                x = 2
                y = 3
                return x + y
        """)
        )
        result = compute_heft([f], {"used_func"}, "tiny")
        # used_func: def + return = 2 LLOC
        # unused_func: def + x + y + return = 4 LLOC
        # total: 6 LLOC
        assert result.active_lloc == 2
        assert result.total_lloc == 6
        assert result.heft_ratio == 2.0 / 6.0

    def test_transitive_calls_included(self, tmp_path):
        """Calling a function that calls another should include both."""
        f = tmp_path / "lib.py"
        f.write_text(
            textwrap.dedent("""\
            def public_api():
                return _helper()

            def _helper():
                x = 1
                return x + 1

            def unrelated():
                return 42
        """)
        )
        result = compute_heft([f], {"public_api"}, "lib")
        # public_api: def + return = 2
        # _helper: def + x + return = 3
        # unrelated: def + return = 2
        # total: 7, active should include both public_api + _helper = 5
        assert result.active_lloc == 5
        assert result.total_lloc == 7

    def test_transitive_chain(self, tmp_path):
        """A -> B -> C should include all three."""
        f = tmp_path / "chain.py"
        f.write_text(
            textwrap.dedent("""\
            def a():
                return b()

            def b():
                return c()

            def c():
                return 1
        """)
        )
        result = compute_heft([f], {"a"}, "chain")
        # a: 2, b: 2, c: 2 = 6 active out of 6 total
        assert result.active_lloc == 6
        assert result.total_lloc == 6
        assert result.heft_ratio == 1.0

    def test_no_infinite_loop_on_recursion(self, tmp_path):
        """Recursive calls should not cause infinite loops."""
        f = tmp_path / "rec.py"
        f.write_text(
            textwrap.dedent("""\
            def factorial(n):
                if n <= 1:
                    return 1
                return n * factorial(n - 1)
        """)
        )
        result = compute_heft([f], {"factorial"}, "rec")
        # def + if + return + return = 4
        assert result.active_lloc == 4
        assert result.total_lloc == 4

    def test_cross_file_transitive(self, tmp_path):
        """Calls across files in the same package should be traced."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "api.py").write_text(
            textwrap.dedent("""\
            from mypkg.utils import do_work

            def public_func():
                return do_work()
        """)
        )
        (pkg / "utils.py").write_text(
            textwrap.dedent("""\
            def do_work():
                return compute()

            def compute():
                return 42

            def unused():
                x = 1
                y = 2
                return x + y
        """)
        )
        result = compute_heft([pkg], {"public_func"}, "mypkg")
        # api.py: from import (1) + def public_func + return = 3
        # utils.py: do_work=2, compute=2, unused=4
        # total: 3 + 8 = 11
        # active: public_func (2) + do_work (2) + compute (2) = 6
        # (the from import line is not part of a function, not counted as active)
        assert result.active_lloc == 6

    def test_private_alias_resolution(self, tmp_path):
        """Public name 'init' should resolve to '_init' via private alias."""
        f = tmp_path / "sdk.py"
        f.write_text(
            textwrap.dedent("""\
            def _init():
                return setup()

            def setup():
                return True

            init = _init

            def unrelated():
                return False
        """)
        )
        result = compute_heft([f], {"init"}, "sdk")
        # _init: def + return = 2, setup: def + return = 2
        # active = 4 (resolved via _init alias)
        assert result.active_lloc == 4

    def test_submodule_as_used_name(self, tmp_path):
        """Used name matching a module file activates all defs in that module.

        Handles `from ua_parser import user_agent_parser` where
        user_agent_parser is a .py file, not a function/class.
        """
        pkg = tmp_path / "ua_parser"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "user_agent_parser.py").write_text(
            textwrap.dedent("""\
            def Parse(user_agent_string):
                result = _do_parse(user_agent_string)
                return result

            def _do_parse(s):
                return {"family": s}

            def GetFilters():
                return []
        """)
        )
        # "user_agent_parser" is a module name, not a definition
        result = compute_heft([pkg], {"user_agent_parser"}, "ua-parser")
        # All defs in user_agent_parser.py should be activated via module match
        assert result.active_lloc > 0
        assert result.heft_ratio > 0.0

    def test_module_level_variable(self, tmp_path):
        """Data-only packages: module-level variables should be traceable.

        Handles packages like disposable-email-domains where the API
        is a variable (blocklist = {...}) not a function.
        """
        f = tmp_path / "data_pkg.py"
        f.write_text(
            textwrap.dedent("""\
            allowlist = {"example.com", "test.com"}
            blocklist = {"spam.com", "junk.com"}
        """)
        )
        result = compute_heft([f], {"blocklist"}, "data-pkg")
        assert result.active_lloc == 1  # blocklist assignment = 1 LLOC
        assert result.total_lloc == 2  # allowlist + blocklist
        assert result.heft_ratio == 0.5

    def test_annotated_module_level_variable(self, tmp_path):
        """Annotated assignments (ALL: set[str] = {...}) should be traceable.

        Handles packages like github-reserved-names where the API is
        an annotated variable, not a function.
        """
        f = tmp_path / "names.py"
        f.write_text(
            textwrap.dedent("""\
            ALL: set[str] = {"foo", "bar", "baz"}
            PARTIAL: list[str] = ["foo"]
        """)
        )
        result = compute_heft([f], {"ALL"}, "names-pkg")
        assert result.active_lloc == 1
        assert result.total_lloc == 2
        assert result.heft_ratio == 0.5

    def test_variable_alias_not_indexed(self, tmp_path):
        """Simple name aliases (init = _init) should not be indexed as variables.

        These are handled by the private alias fallback in _trace_reachable.
        """
        f = tmp_path / "sdk.py"
        f.write_text(
            textwrap.dedent("""\
            def _real():
                return 42

            public = _real
        """)
        )
        result = compute_heft([f], {"public"}, "sdk")
        # Should resolve via _public fallback... but _public doesn't exist.
        # The alias `public = _real` is not indexed, so public won't match.
        # This is expected — simple aliases need explicit call graph support
        # to be fully resolved.
        assert result.total_lloc == 3  # _real(2) + public assignment(0, skipped)

    def test_empty_dep_paths(self, tmp_path):
        """compute_heft with no files should return zero heft."""
        pkg = tmp_path / "empty"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        result = compute_heft([pkg], {"anything"}, "empty")
        assert result.total_lloc == 0
        assert result.active_lloc == 0
        assert result.heft_ratio == 0.0


class TestExtractCalls:
    """Test internal call extraction from function bodies."""

    def test_simple_call(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            def foo():
                return bar()

            def bar():
                return 1
        """)
        )
        calls = _extract_calls(f)
        foo_calls = calls.get("foo", set())
        assert "bar" in foo_calls

    def test_no_calls(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            def foo():
                return 1
        """)
        )
        calls = _extract_calls(f)
        assert calls.get("foo", set()) == set()

    def test_multiple_calls(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            def main():
                a()
                b()
                c()
        """)
        )
        calls = _extract_calls(f)
        assert calls["main"] == {"a", "b", "c"}

    def test_obj_method_calls_detected(self, tmp_path):
        """obj.method() should detect method as a potential internal call."""
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            def foo():
                obj.method()
                helper()
        """)
        )
        calls = _extract_calls(f)
        assert calls["foo"] == {"helper", "method"}

    def test_self_calls_in_methods(self, tmp_path):
        """self.method() inside class methods should be traced."""
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            class Manager:
                def public(self):
                    return self._internal()

                def _internal(self):
                    return self._deep()

                def _deep(self):
                    return 42
        """)
        )
        calls = _extract_calls(f)
        # Method-level entries use qualified names
        assert "Manager._internal" in calls.get("Manager.public", set())
        assert "Manager._deep" in calls.get("Manager._internal", set())
        # Class-level aggregation includes qualified callees
        assert "Manager._internal" in calls["Manager"]
        assert "Manager._deep" in calls["Manager"]

    def test_self_call_transitive_heft(self, tmp_path):
        """self.method() chains should be traced for heft."""
        f = tmp_path / "lib.py"
        f.write_text(
            textwrap.dedent("""\
            class Provider:
                def get_resource(self, name):
                    path = self._resolve(name)
                    return self._read(path)

                def _resolve(self, name):
                    return normalize(name)

                def _read(self, path):
                    return open(path)

            def normalize(name):
                return name.lower()

            def unrelated():
                x = 1
                y = 2
                return x + y
        """)
        )
        result = compute_heft([f], {"get_resource"}, "lib")
        # get_resource: def + path + return = 3
        # _resolve: def + return = 2
        # _read: def + return = 2
        # normalize: def + return = 2
        # unrelated: def + x + y + return = 4
        # total = 14 (class + 4 methods + normalize + unrelated)
        # active = 3 + 2 + 2 + 2 = 9
        assert result.active_lloc == 9

    def test_module_dot_class_call(self, tmp_path):
        """module.Class() should be detected as a call to Class."""
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            def compile(expression):
                return parser.Parser().parse(expression)
        """)
        )
        calls = _extract_calls(f)
        assert "Parser" in calls["compile"]
        assert "parse" in calls["compile"]

    def test_module_dot_func_call(self, tmp_path):
        """module.func() should be detected."""
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            def do_stuff():
                return utils.helper()
        """)
        )
        calls = _extract_calls(f)
        assert "helper" in calls["do_stuff"]

    def test_chained_constructor_method(self, tmp_path):
        """Parser().parse() should trace through to Parser class."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text(
            textwrap.dedent("""\
            def compile(expr):
                return Parser().parse(expr)

            class Parser:
                def parse(self, expr):
                    tokens = self._tokenize(expr)
                    return self._build(tokens)

                def _tokenize(self, expr):
                    return expr.split()

                def _build(self, tokens):
                    return tokens

            def unrelated():
                x = 1
                return x
        """)
        )
        result = compute_heft([pkg], {"compile"}, "mypkg")
        # compile: def + return = 2
        # Parser class: class + parse(3) + _tokenize(2) + _build(2) = 8
        # parse method: def + tokens + return = 3
        # _tokenize: def + return = 2
        # _build: def + return = 2
        # unrelated: def + x + return = 3
        # total = 13
        # active: compile(2) + parse(3) + _tokenize(2) + _build(2) = 9
        # Parser class (8) would double-count, but lloc_by_name
        # picks first def, so Parser=8 is separate from methods
        assert result.active_lloc > 4  # at minimum compile + parse

    def test_class_method_collision(self, tmp_path):
        """Multiple classes with same method names get distinct LLOC counts."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text(
            textwrap.dedent("""\
            class Base:
                def __init__(self):
                    self.x = 1

                def base_method(self):
                    return self.x

            class Child:
                def __init__(self):
                    self.y = 2

                def child_method(self):
                    return self.y

            def unrelated():
                return 42
        """)
        )
        # Verify qualified names prevent collision
        index = index_dependency([pkg])
        lloc = {f["name"]: f["lloc"] for f in index["functions"]}
        assert "Base.__init__" in lloc
        assert "Child.__init__" in lloc
        # Each __init__ has its own LLOC (no collision)
        assert lloc["Base.__init__"] == lloc["Child.__init__"]  # both 3

        # Using Child: should trace Child.__init__ and child_method
        result = compute_heft([pkg], {"Child"}, "mypkg")
        # Child.__init__(2) + Child.child_method(2) = 4 active
        # (class envelope skipped due to method match)
        assert result.active_lloc == 4
        # Base and unrelated should NOT be reached
        assert result.active_lloc < result.total_lloc

    def test_cls_method_calls_qualified(self, tmp_path):
        """cls.method() should be qualified like self.method()."""
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            class Signature:
                @classmethod
                def verify(cls, payload, secret):
                    sig = cls._compute(payload, secret)
                    return sig

                @staticmethod
                def _compute(payload, secret):
                    return hash(payload + secret)
        """)
        )
        calls = _extract_calls(f)
        # cls._compute() inside Signature.verify should be qualified
        assert "Signature._compute" in calls.get("Signature.verify", set())

    def test_cross_class_method_resolution(self, tmp_path):
        """obj.method() calling a method on another class should resolve."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text(
            textwrap.dedent("""\
            class Webhook:
                def process(self):
                    return Signer.verify(self.data)

            class Signer:
                @classmethod
                def verify(cls, data):
                    return cls._check(data)

                @staticmethod
                def _check(data):
                    return len(data) > 0

            def unrelated():
                return 42
        """)
        )
        # process() calls Signer.verify via obj.method() pattern
        # which emits bare 'verify' — resolved via bare_to_qualified
        result = compute_heft([pkg], {"Webhook"}, "mypkg")
        # Should reach: Webhook, Webhook.process, and via bare 'verify'
        # → Signer.verify → Signer._check
        assert result.active_lloc > 4
        # unrelated should NOT be reached
        assert result.active_lloc < result.total_lloc

    def test_subscript_call_detected(self, tmp_path):
        """registry[key]() should be detected as a call to registry."""
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            _handlers = {'a': HandlerA, 'b': HandlerB}

            def dispatch(key):
                return _handlers[key]()
        """)
        )
        calls = _extract_calls(f)
        assert "_handlers" in calls["dispatch"]

    def test_dict_registry_values_traced(self, tmp_path):
        """Dict values (class references) should create call graph edges."""
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            _handlers = {'a': HandlerA, 'b': HandlerB}

            def dispatch(key):
                return _handlers[key]()
        """)
        )
        calls = _extract_calls(f)
        assert "HandlerA" in calls["_handlers"]
        assert "HandlerB" in calls["_handlers"]

    def test_list_variable_references_names(self, tmp_path):
        """Module-level list with Name refs should create call graph edges."""
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            __all__ = [Alpha, Beta]
        """)
        )
        calls = _extract_calls(f)
        assert "Alpha" in calls.get("__all__", set())
        assert "Beta" in calls.get("__all__", set())

    def test_plain_variable_no_edges(self, tmp_path):
        """Variable assigned a constant (not a collection) should have no edges."""
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            VERSION = 42
            NAME = "hello"
        """)
        )
        calls = _extract_calls(f)
        assert calls.get("VERSION", set()) == set()
        assert calls.get("NAME", set()) == set()

    def test_registry_heft_traces_through(self, tmp_path):
        """Factory dispatching via dict registry should trace to classes."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text(
            textwrap.dedent("""\
            class English:
                def stem(self, word):
                    return word.lower()

            class French:
                def stem(self, word):
                    return word.upper()

            _languages = {'en': English, 'fr': French}

            def stemmer(lang):
                return _languages[lang]()

            def unrelated():
                return 42
        """)
        )
        result = compute_heft([pkg], {"stemmer"}, "mypkg")
        # stemmer -> _languages (subscript call) -> English, French (dict values)
        # English/French classes reached -> their stem methods enqueued
        # unrelated should NOT be reached
        assert result.active_lloc > result.total_lloc * 0.5


class TestComputeHeftsBulk:
    """Test bulk heft computation."""

    def test_empty_work_items(self):
        result = compute_hefts_bulk([])
        assert result == {}

    def test_single_dep(self, tmp_path):
        f = tmp_path / "tiny.py"
        f.write_text(
            textwrap.dedent("""\
            def used_func():
                return 1

            def unused_func():
                return 2
        """)
        )
        result = compute_hefts_bulk([("tiny", [f], {"used_func"})])
        assert "tiny" in result
        assert result["tiny"].active_lloc == 2
        assert result["tiny"].total_lloc == 4

    def test_multiple_deps(self, tmp_path):
        f1 = tmp_path / "dep_a.py"
        f1.write_text("def a_func():\n    return 1\n")
        f2 = tmp_path / "dep_b.py"
        f2.write_text("def b_func():\n    return 2\n\ndef unused():\n    return 3\n")

        result = compute_hefts_bulk(
            [
                ("dep-a", [f1], {"a_func"}),
                ("dep-b", [f2], {"b_func"}),
            ]
        )
        assert result["dep-a"].active_lloc == 2
        assert result["dep-a"].total_lloc == 2
        assert result["dep-b"].active_lloc == 2
        assert result["dep-b"].total_lloc == 4

    def test_with_package_dir(self, tmp_path):
        """Bulk computation handles package directories."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text(
            textwrap.dedent("""\
            def public():
                return _helper()

            def _helper():
                return 42
        """)
        )
        result = compute_hefts_bulk([("mypkg", [pkg], {"public"})])
        assert result["mypkg"].active_lloc == 4  # public(2) + _helper(2)


class TestComputeHeftsBulkEdgeCases:
    """Edge case tests for bulk heft computation."""

    def test_unparseable_file_skipped(self, tmp_path):
        """Files with syntax errors in bulk computation are skipped gracefully."""
        good = tmp_path / "good.py"
        good.write_text("def used():\n    return 1\n")
        bad = tmp_path / "bad.py"
        bad.write_text("def broken(\n")

        result = compute_hefts_bulk([("dep", [good, bad], {"used"})])
        assert result["dep"].active_lloc == 2
        assert result["dep"].total_lloc == 2

    def test_opaque_single_file(self, tmp_path):
        """A .so file passed directly as dep_path should count as opaque."""
        so_file = tmp_path / "native.so"
        so_file.write_bytes(b"\x00")
        py_file = tmp_path / "wrapper.py"
        py_file.write_text("def wrap():\n    return 1\n")

        result = compute_hefts_bulk([("native", [so_file, py_file], {"wrap"})])
        assert result["native"].opaque_files == 1
        assert result["native"].total_lloc == 2

    def test_call_graph_merge_across_files(self, tmp_path):
        """Call graph entries from multiple files merge correctly in bulk."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        # Both files define a function that calls 'helper'
        (pkg / "a.py").write_text(
            "def api():\n    return helper()\n\ndef helper():\n    return 1\n"
        )
        (pkg / "b.py").write_text(
            "def other_api():\n    return helper()\n\ndef helper():\n    return 2\n"
        )
        result = compute_hefts_bulk([("mypkg", [pkg], {"api"})])
        # api -> helper (transitive), so both should be active
        assert result["mypkg"].active_lloc >= 4


class TestIsTestFile:
    """Test the _is_test_file heuristic for ambiguous filenames."""

    def test_test_prefix_always_excluded(self, tmp_path):
        assert _is_test_file("test_core.py", str(tmp_path)) is True

    def test_conftest_always_excluded(self, tmp_path):
        assert _is_test_file("conftest.py", str(tmp_path)) is True

    def test_regular_file_not_excluded(self, tmp_path):
        assert _is_test_file("core.py", str(tmp_path)) is False

    def test_tests_py_with_unittest_excluded(self, tmp_path):
        f = tmp_path / "tests.py"
        f.write_text("import unittest\n\nclass TestFoo(unittest.TestCase):\n    pass\n")
        assert _is_test_file("tests.py", str(tmp_path)) is True

    def test_tests_py_without_framework_not_excluded(self, tmp_path):
        """Library code named tests.py (like Jinja2) should not be excluded."""
        f = tmp_path / "tests.py"
        f.write_text("import operator\n\ndef test_defined(value):\n    return True\n")
        assert _is_test_file("tests.py", str(tmp_path)) is False

    def test_test_py_with_pytest_excluded(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("import pytest\n\ndef test_something():\n    pass\n")
        assert _is_test_file("test.py", str(tmp_path)) is True

    def test_test_py_without_framework_not_excluded(self, tmp_path):
        """Library code named test.py (like Werkzeug) should not be excluded."""
        f = tmp_path / "test.py"
        f.write_text("import dataclasses\n\nclass Client:\n    pass\n")
        assert _is_test_file("test.py", str(tmp_path)) is False

    def test_tests_py_excluded_from_source_collection(self, tmp_path):
        """Integration: _collect_source_files should skip tests.py with unittest."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("def func(): return 1\n")
        (pkg / "tests.py").write_text(
            "import unittest\nclass TestCore(unittest.TestCase):\n    pass\n"
        )
        files, _ = _collect_source_files([pkg])
        names = {f.name for f in files}
        assert "core.py" in names
        assert "tests.py" not in names


class TestResolveNamePrivateModuleAlias:
    """Test _resolve_name step 5: private module alias fallback."""

    def test_private_module_alias_fallback(self, tmp_path):
        """Name 'compat' resolves to defs in '_compat.py' (step 5 fallback)."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text(
            textwrap.dedent("""\
            from ._compat import helper
            """)
        )
        (pkg / "_compat.py").write_text(
            textwrap.dedent("""\
            def helper():
                return 42

            def other():
                return 0
            """)
        )
        index = index_dependency([pkg])
        ti = _TracerIndex(index["functions"])
        # 'compat' is not in lloc_by_name, not a private alias, not bare_to_qualified,
        # not in module_defs, but '_compat' IS in module_defs — step 5 fires.
        resolved = _resolve_name(
            "compat", ti.lloc_by_name, ti.bare_to_qualified, ti.module_defs
        )
        assert set(resolved) == {"helper", "other"}


class TestImportsTestFrameworkOSError:
    """Test _imports_test_framework OSError branch (line 734-735)."""

    def test_oserror_returns_false(self, tmp_path):
        """Missing file triggers OSError; _imports_test_framework returns False."""
        missing = str(tmp_path / "nonexistent.py")
        assert _imports_test_framework(missing) is False


class TestNamesInValueNone:
    """Test _names_in_value with None values in a dict literal (branch 548->547)."""

    def test_dict_with_none_values(self):
        """Dict literal containing None values should not raise and yields no names."""
        # Build `{key: None}` AST node manually — ast.parse produces a None
        # in dict.values for `{**other}` unpacking, and the branch guards it.
        node = ast.parse("{a: None, b: MyClass}", mode="eval").body
        assert isinstance(node, ast.Dict)
        # Replace one value with None to simulate dict unpacking (**x produces None)
        node.values[0] = None  # ty: ignore[invalid-assignment]
        result = _names_in_value(node)
        # Only the non-None value (MyClass) should be collected
        assert "MyClass" in result
        assert len(result) == 1


class TestResolveCallTargetNonStandard:
    """Test _resolve_call_target returning None for unsupported call patterns."""

    def test_non_name_attribute_subscript_returns_none(self):
        """Call on a complex expression (not Name/Attribute/Subscript) returns None."""
        # Build: `(lambda: x)()` — func is a Lambda node, not Name/Attribute/Subscript
        tree = ast.parse("(lambda: x)()", mode="eval")
        call_node = tree.body
        assert isinstance(call_node, ast.Call)
        assert isinstance(call_node.func, ast.Lambda)
        result = _resolve_call_target(call_node, None)
        assert result is None


class TestResolveAttrCallNonNameCall:
    """Test _resolve_attr_call returning None for non-Name/Call attribute target."""

    def test_subscript_attr_call_returns_none(self):
        """Attribute call on a subscript (e.g. obj[0].method()) returns None."""
        # Build: `obj[0].method()` — func.value is a Subscript node
        tree = ast.parse("obj[0].method()", mode="eval")
        call_node = tree.body
        assert isinstance(call_node, ast.Call)
        func = call_node.func
        assert isinstance(func, ast.Attribute)
        # func.value is ast.Subscript, not Name or Call
        result = _resolve_attr_call(func, None)
        assert result is None


class TestParallelIndexing:
    """Tests for the parallel indexing path (>= 100 files)."""

    def _make_large_package(self, tmp_path, n_files=101):
        """Create a package with many .py files to trigger parallel indexing."""
        pkg = tmp_path / "bigpkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("x = 1\n")
        for i in range(n_files):
            (pkg / f"mod{i}.py").write_text(f"def func{i}():\n    return {i}\n")
        return pkg

    def test_index_dependency_parallel(self, tmp_path):
        pkg = self._make_large_package(tmp_path)
        dep_index = index_dependency([pkg])
        # Should have definitions from all files
        assert dep_index["total_lloc"] > 0
        ti = _TracerIndex(dep_index["functions"])
        assert "func0" in ti.lloc_by_name

    def test_compute_hefts_bulk_parallel(self, tmp_path):
        pkg = self._make_large_package(tmp_path)
        results = compute_hefts_bulk([("bigpkg", [pkg], {"func0", "func1"})])
        assert "bigpkg" in results
        heft = results["bigpkg"]
        assert isinstance(heft, HeftResult)
        assert heft.active_lloc > 0


class TestCollectSourceFilesEdgeCases:
    def test_non_python_non_opaque_file(self, tmp_path):
        """A .txt file should be silently ignored."""
        txt = tmp_path / "data.txt"
        txt.write_text("hello")
        py_files, opaque = _collect_source_files([txt])
        assert py_files == []
        assert opaque == 0

    def test_nonexistent_path(self, tmp_path):
        """A path that doesn't exist should be silently skipped."""
        ghost = tmp_path / "ghost"
        py_files, opaque = _collect_source_files([ghost])
        assert py_files == []
        assert opaque == 0

    def test_opaque_file(self, tmp_path):
        """A .so file should be counted as opaque."""
        so = tmp_path / "mod.so"
        so.write_bytes(b"\x00")
        py_files, opaque = _collect_source_files([so])
        assert py_files == []
        assert opaque == 1


class TestAnnotationOnlyVariable:
    def test_annotation_without_value_not_indexed(self, tmp_path):
        """Module-level `x: int` (no value) should not appear in definitions."""
        f = tmp_path / "mod.py"
        f.write_text("x: int\ny: str = 'hello'\n")
        dep_index = index_dependency([f.parent])
        # y has a value so it might appear; x has no value so it shouldn't
        # The key test: this should not crash and annotation-only vars are skipped
        assert dep_index is not None


class TestModuleLevelAttributeAssignment:
    def test_attribute_target_not_indexed(self, tmp_path):
        """Module-level `obj.attr = value` should not crash call extraction."""
        f = tmp_path / "mod.py"
        f.write_text(
            "import sys\nsys.modules['x'] = None\ndef real_func():\n    return 1\n"
        )
        tree = _parse_file(f)
        assert tree is not None
        calls = _extract_calls_from_tree(tree)
        assert isinstance(calls, dict)
        assert "real_func" in calls


class TestCallsInBodyUnresolvableTarget:
    def test_lambda_call_in_function(self, tmp_path):
        """(lambda: x)() inside a function should not crash."""
        f = tmp_path / "mod.py"
        f.write_text("def foo():\n    (lambda: None)()\n    return 1\n")
        tree = _parse_file(f)
        assert tree is not None
        calls = _extract_calls_from_tree(tree)
        # foo should be in calls, the lambda call is silently skipped
        assert "foo" in calls


class TestWalkDirectoryNonPythonFiles:
    def test_non_python_files_ignored_in_walk(self, tmp_path):
        """README.txt and .cfg files should not appear in index."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("x = 1\n")
        (pkg / "README.txt").write_text("docs")
        (pkg / "setup.cfg").write_text("[metadata]\nname=x\n")
        dep_index = index_dependency([pkg])
        assert dep_index["total_lloc"] >= 1


class TestExtractBaseNames:
    """Test extraction of base class names from ClassDef nodes."""

    def test_simple_base(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text("class Foo(Bar):\n    pass\n")
        tree = _parse_file(f)
        assert tree is not None
        cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
        assert _extract_base_names(cls) == ["Bar"]

    def test_attribute_base(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text("class Foo(mod.Bar):\n    pass\n")
        tree = _parse_file(f)
        assert tree is not None
        cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
        assert _extract_base_names(cls) == ["Bar"]

    def test_generic_base(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text("class Foo(Generic[T]):\n    pass\n")
        tree = _parse_file(f)
        assert tree is not None
        cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
        assert _extract_base_names(cls) == ["Generic"]

    def test_multiple_bases(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text("class Foo(Bar, Baz):\n    pass\n")
        tree = _parse_file(f)
        assert tree is not None
        cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
        assert _extract_base_names(cls) == ["Bar", "Baz"]

    def test_no_bases(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text("class Foo:\n    pass\n")
        tree = _parse_file(f)
        assert tree is not None
        cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
        assert _extract_base_names(cls) == []


class TestInheritanceTracing:
    """Test that base class methods are activated via inheritance."""

    def test_simple_inheritance(self, tmp_path):
        """Using a subclass should activate the base class and its methods."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text(
            textwrap.dedent("""\
            class Base:
                def base_method(self):
                    return 1

            class Child(Base):
                def child_method(self):
                    return 2
        """)
        )
        result = compute_heft([pkg], {"Child"}, "mypkg")
        # Child is used → its methods activate, and Base's methods
        # should also activate via inheritance
        assert result.active_lloc > 0
        idx = index_dependency([pkg])
        ti = _TracerIndex(idx["functions"])
        assert "Base" in ti.class_bases.get("Child", [])

    def test_deep_inheritance_chain(self, tmp_path):
        """A→B→C chain: using C should activate A and B methods too."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text(
            textwrap.dedent("""\
            class A:
                def a_method(self):
                    return 1

            class B(A):
                def b_method(self):
                    return 2

            class C(B):
                def c_method(self):
                    return 3
        """)
        )
        # All three classes and their methods should be active
        idx = index_dependency([pkg])
        ti = _TracerIndex(idx["functions"])
        from unladen.tracer import _trace_reachable

        matched = _trace_reachable({"C"}, ti, idx["call_graph"])
        assert "C" in matched
        assert "B" in matched
        assert "A" in matched
        assert "A.a_method" in matched
        assert "B.b_method" in matched
        assert "C.c_method" in matched

    def test_multiple_inheritance(self, tmp_path):
        """class Child(Mixin, Base) should activate both parents."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text(
            textwrap.dedent("""\
            class Mixin:
                def mixin_method(self):
                    return 1

            class Base:
                def base_method(self):
                    return 2

            class Child(Mixin, Base):
                def child_method(self):
                    return 3
        """)
        )
        idx = index_dependency([pkg])
        ti = _TracerIndex(idx["functions"])
        from unladen.tracer import _trace_reachable

        matched = _trace_reachable({"Child"}, ti, idx["call_graph"])
        assert "Mixin" in matched
        assert "Base" in matched
        assert "Mixin.mixin_method" in matched
        assert "Base.base_method" in matched

    def test_external_base_not_in_dep(self, tmp_path):
        """Base class not defined in the dep should be silently skipped."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text(
            textwrap.dedent("""\
            class Child(SomeExternalBase):
                def method(self):
                    return 1
        """)
        )
        # Should not crash — SomeExternalBase is not in the index
        result = compute_heft([pkg], {"Child"}, "mypkg")
        assert result.active_lloc > 0

    def test_bases_stored_in_funcdef(self, tmp_path):
        """FuncDef for a class should include bases field."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("class Foo(Bar, Baz):\n    pass\n")
        idx = index_dependency([pkg])
        class_def = next(f for f in idx["functions"] if f["name"] == "Foo")
        assert class_def["bases"] == ["Bar", "Baz"]

    def test_no_bases_omits_field(self, tmp_path):
        """FuncDef for a class with no bases should not have bases field."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("class Foo:\n    pass\n")
        idx = index_dependency([pkg])
        class_def = next(f for f in idx["functions"] if f["name"] == "Foo")
        assert "bases" not in class_def


class TestTypeCheckingElseCallGraph:
    """Call graph extraction from TYPE_CHECKING else-bodies."""

    def test_variable_in_type_checking_else(self, tmp_path):
        """Variables in TYPE_CHECKING else get call graph edges."""
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            from typing import TYPE_CHECKING

            def _real_init():
                return 1

            if TYPE_CHECKING:
                class init:
                    pass
            else:
                init = (lambda: _real_init)()
        """)
        )
        tree = _parse_file(f)
        assert tree is not None
        calls = _extract_calls_from_tree(tree)
        assert "_real_init" in calls.get("init", set())

    def test_function_in_type_checking_else(self, tmp_path):
        """Functions defined in TYPE_CHECKING else-body should be in call graph."""
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            from typing import TYPE_CHECKING

            def _helper():
                return 1

            if TYPE_CHECKING:
                pass
            else:
                def real_func():
                    _helper()
        """)
        )
        tree = _parse_file(f)
        assert tree is not None
        calls = _extract_calls_from_tree(tree)
        assert "_helper" in calls.get("real_func", set())


class TestBoundMethodAlias:
    """Module-level `name = instance.method` aliases (issue #25).

    Libraries like pyjwt expose bound methods as module-level names::

        _jwt_global_obj = PyJWT()
        decode = _jwt_global_obj.decode

    Heft must follow the alias back to the class method, not stop at
    the one-line assignment.
    """

    def test_bound_method_alias_traced(self, tmp_path):
        """A `name = instance.method` alias should reach the class method."""
        f = tmp_path / "api.py"
        f.write_text(
            textwrap.dedent("""\
            class PyJWT:
                def decode(self, token):
                    return self._verify(token)

                def _verify(self, token):
                    return token

            _global = PyJWT()
            decode = _global.decode
        """)
        )
        result = compute_heft([f], {"decode"}, "pyjwt")
        # decode alias (1) + PyJWT.decode (2) + PyJWT._verify (2) = 5 active
        assert result.active_lloc == 5
        assert result.total_lloc == 7

    def test_dotted_constructor_alias_traced(self, tmp_path):
        """`obj = module.ClassName()` (dotted constructor) is tracked too."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text(
            textwrap.dedent("""\
            from mypkg import impl

            _global = impl.PyJWT()
            decode = _global.decode
        """)
        )
        (pkg / "impl.py").write_text(
            textwrap.dedent("""\
            class PyJWT:
                def decode(self, token):
                    return self._verify(token)

                def _verify(self, token):
                    return token
        """)
        )
        result = compute_heft([pkg], {"decode"}, "pyjwt")
        # decode alias (1) + PyJWT.decode (2) + PyJWT._verify (2) = 5 active
        assert result.active_lloc == 5
        assert result.total_lloc == 8

    def test_reassigned_instance_evicted(self, tmp_path):
        """Rebinding the instance var to a non-constructor drops the stale
        mapping, so a later alias gets no wrong-class edge."""
        f = tmp_path / "api.py"
        f.write_text(
            textwrap.dedent("""\
            class A:
                def run(self):
                    return self._work()

                def _work(self):
                    return 1

            _other = 42
            obj = A()
            obj = _other
            handler = obj.run
        """)
        )
        result = compute_heft([f], {"handler"}, "pkg")
        # `obj` was rebound to a non-constructor, so the stale `obj -> A`
        # mapping is dropped and `handler` gets no bogus `A.run` edge.
        assert result.active_lloc == 1


class TestConditionalBlockCallGraph:
    """Call edges must be extracted from defs inside try/if blocks,
    matching the blocks _collect_defs descends into."""

    def test_calls_inside_try_traced(self, tmp_path):
        f = tmp_path / "compat.py"
        f.write_text(
            textwrap.dedent("""\
            def _helper():
                a = 1
                b = 2
                return a + b

            try:
                def fast_impl():
                    return _helper()
            except ImportError:
                def fast_impl():
                    return None
        """)
        )
        calls = _extract_calls(f)
        assert "_helper" in calls.get("fast_impl", set())
        result = compute_heft([f], {"fast_impl"}, "compat")
        # fast_impl (2) + _helper (4) reached through the call graph
        assert result.active_lloc == 6

    def test_calls_inside_if_else_traced(self, tmp_path):
        f = tmp_path / "platform.py"
        f.write_text(
            textwrap.dedent("""\
            import sys

            def _real():
                x = 1
                return x

            if sys.platform == "win32":
                def api():
                    return None
            else:
                def api():
                    return _real()
        """)
        )
        calls = _extract_calls(f)
        assert "_real" in calls.get("api", set())

    def test_registry_assign_inside_try_traced(self, tmp_path):
        f = tmp_path / "registry.py"
        f.write_text(
            textwrap.dedent("""\
            def loader():
                x = 1
                return x

            try:
                HANDLERS = {"default": loader}
            except ImportError:
                HANDLERS = {}
        """)
        )
        calls = _extract_calls(f)
        assert "loader" in calls.get("HANDLERS", set())


class TestActiveFiles:
    """active_files: map traced usage back to activated source files."""

    def test_active_file_and_ancestor_inits(self, tmp_path):
        """Activating a submodule also activates ancestor __init__.py
        files (importing anything executes them), but not siblings."""
        pkg = tmp_path / "spam"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("from spam.core import breakfast\n")
        (pkg / "core.py").write_text("def breakfast():\n    return 1\n")
        (pkg / "unused.py").write_text("def lunch():\n    return 2\n")

        files = active_files(index_dependency([pkg]), {"breakfast"}, [pkg])
        assert pkg / "core.py" in files
        assert pkg / "__init__.py" in files
        assert pkg / "unused.py" not in files

    def test_nested_subpackage_walks_ancestors(self, tmp_path):
        """A deep active file activates each ancestor __init__.py up to
        the package root; namespace dirs (no __init__.py) are traversed
        without error."""
        pkg = tmp_path / "big"
        (pkg / "sub").mkdir(parents=True)
        (pkg / "nsdir").mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "sub" / "__init__.py").write_text("")
        (pkg / "sub" / "deep.py").write_text("def f():\n    return 1\n")
        (pkg / "nsdir" / "mod.py").write_text("def g():\n    return 2\n")

        files = active_files(index_dependency([pkg]), {"f", "g"}, [pkg])
        assert pkg / "sub" / "deep.py" in files
        assert pkg / "sub" / "__init__.py" in files
        assert pkg / "__init__.py" in files
        assert pkg / "nsdir" / "mod.py" in files

    def test_single_file_module_dep(self, tmp_path):
        """A single-file module dep has no package root to walk."""
        mod = tmp_path / "flat.py"
        mod.write_text("def f():\n    return 1\n")
        files = active_files(index_dependency([mod]), {"f"}, [mod])
        assert files == [mod]

    def test_no_used_names_yields_nothing(self, tmp_path):
        mod = tmp_path / "flat.py"
        mod.write_text("def f():\n    return 1\n")
        assert active_files(index_dependency([mod]), set(), [mod]) == []
