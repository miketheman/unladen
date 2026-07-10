"""Tests for Phase 2: The Flight — Call Graph Mapping."""

import textwrap
from pathlib import Path

import pytest

from unladen._parsing import parse_file
from unladen.inspector import (
    _walk_source_file,
    find_project_source,
    inspect_project,
    inspect_source_files,
    inspect_source_files_counted,
)


def extract_imports(file_path: Path):
    """Test helper: extract imports from a single file."""
    tree = parse_file(file_path)
    if tree is None:
        return []
    imports = []
    _walk_source_file(tree, file_path, set(), imports, {}, {})
    return imports


def extract_attribute_accesses(file_path: Path):
    """Test helper: extract attribute accesses from a single file."""
    tree = parse_file(file_path)
    if tree is None:
        return {}
    accesses = {}
    _walk_source_file(tree, file_path, set(), [], accesses, {})
    return accesses


def extract_string_references(file_path: Path, known_import_names: set[str]):
    """Test helper: extract string references from a single file."""
    tree = parse_file(file_path)
    if tree is None:
        return {}
    refs = {}
    _walk_source_file(tree, file_path, known_import_names, [], {}, refs)
    return refs


class TestExtractImports:
    """Test AST-based import extraction from a single file."""

    def test_simple_import(self, tmp_path):
        src = tmp_path / "mod.py"
        src.write_text("import requests\n")
        imports = extract_imports(src)
        assert any(i.module == "requests" and i.name is None for i in imports)

    def test_import_from(self, tmp_path):
        src = tmp_path / "mod.py"
        src.write_text("from click import echo\n")
        imports = extract_imports(src)
        assert any(i.module == "click" and i.name == "echo" for i in imports)

    def test_aliased_import(self, tmp_path):
        src = tmp_path / "mod.py"
        src.write_text("import pandas as pd\n")
        imports = extract_imports(src)
        assert any(i.module == "pandas" and i.alias == "pd" for i in imports)

    def test_aliased_from_import(self, tmp_path):
        src = tmp_path / "mod.py"
        src.write_text("from click import echo as say\n")
        imports = extract_imports(src)
        assert any(
            i.module == "click" and i.name == "echo" and i.alias == "say"
            for i in imports
        )

    def test_submodule_import(self, tmp_path):
        src = tmp_path / "mod.py"
        src.write_text("from os.path import join\n")
        imports = extract_imports(src)
        assert any(i.module == "os.path" and i.name == "join" for i in imports)

    def test_dotted_import(self, tmp_path):
        src = tmp_path / "mod.py"
        src.write_text("import xml.etree.ElementTree\n")
        imports = extract_imports(src)
        assert any(i.module == "xml.etree.ElementTree" for i in imports)

    def test_multiple_from_imports(self, tmp_path):
        src = tmp_path / "mod.py"
        src.write_text("from os import getcwd, listdir\n")
        imports = extract_imports(src)
        names = {i.name for i in imports if i.module == "os"}
        assert names == {"getcwd", "listdir"}

    def test_star_import(self, tmp_path):
        src = tmp_path / "mod.py"
        src.write_text("from os.path import *\n")
        imports = extract_imports(src)
        assert any(i.module == "os.path" and i.name == "*" for i in imports)

    def test_relative_imports_ignored(self, tmp_path):
        src = tmp_path / "mod.py"
        src.write_text("from . import sibling\nfrom .utils import helper\n")
        imports = extract_imports(src)
        assert len(imports) == 0

    def test_empty_file(self, tmp_path):
        src = tmp_path / "mod.py"
        src.write_text("")
        imports = extract_imports(src)
        assert imports == []

    def test_syntax_error_returns_empty(self, tmp_path):
        src = tmp_path / "mod.py"
        src.write_text("def broken(\n")
        imports = extract_imports(src)
        assert imports == []

    def test_multiple_imports_in_file(self, tmp_path):
        src = tmp_path / "mod.py"
        src.write_text(
            textwrap.dedent("""\
            import requests
            from click import echo
            import os
            from pathlib import Path
        """)
        )
        imports = extract_imports(src)
        modules = {i.module for i in imports}
        assert modules == {"requests", "click", "os", "pathlib"}

    def test_conditional_import(self, tmp_path):
        """Imports inside if/try blocks are still found by ast."""
        src = tmp_path / "mod.py"
        src.write_text(
            textwrap.dedent("""\
            try:
                import ujson as json
            except ImportError:
                import json
        """)
        )
        imports = extract_imports(src)
        modules = [i.module for i in imports]
        assert "ujson" in modules
        assert "json" in modules


class TestExtractAttributeAccesses:
    """Test attribute access extraction from source files."""

    def test_simple_attribute(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text("import hupper\nhupper.is_active()\n")
        accesses = extract_attribute_accesses(f)
        assert "hupper" in accesses
        assert "is_active" in accesses["hupper"]

    def test_multiple_attributes(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            import hupper
            hupper.is_active()
            hupper.start_reloader(main)
            hupper.get_reloader()
        """)
        )
        accesses = extract_attribute_accesses(f)
        assert accesses["hupper"] == {"is_active", "start_reloader", "get_reloader"}

    def test_aliased_import(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text("import pandas as pd\npd.DataFrame()\n")
        accesses = extract_attribute_accesses(f)
        assert "pd" in accesses
        assert "DataFrame" in accesses["pd"]

    def test_no_attributes(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text("import os\nprint('hello')\n")
        accesses = extract_attribute_accesses(f)
        assert "os" not in accesses

    def test_chained_attribute(self, tmp_path):
        """pkg_resources.DefaultProvider.method() should detect DefaultProvider."""
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            import pkg_resources
            pkg_resources.DefaultProvider.get_resource_filename(self, mgr, name)
        """)
        )
        accesses = extract_attribute_accesses(f)
        assert "pkg_resources" in accesses
        assert "DefaultProvider" in accesses["pkg_resources"]

    def test_class_base(self, tmp_path):
        """class Foo(pkg_resources.DefaultProvider) should detect DefaultProvider."""
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            import pkg_resources
            class Override(pkg_resources.DefaultProvider):
                pass
        """)
        )
        accesses = extract_attribute_accesses(f)
        assert "pkg_resources" in accesses
        assert "DefaultProvider" in accesses["pkg_resources"]

    def test_self_module_attribute(self, tmp_path):
        """self.venusian.attach() should detect attach under venusian."""
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            import venusian
            class view_config:
                venusian = venusian
                def __call__(self, wrapped):
                    self.venusian.attach(wrapped, callback)
        """)
        )
        accesses = extract_attribute_accesses(f)
        assert "venusian" in accesses
        assert "attach" in accesses["venusian"]

    def test_self_module_multiple_attrs(self, tmp_path):
        """Multiple self.module.X calls should collect all attributes."""
        f = tmp_path / "mod.py"
        f.write_text(
            textwrap.dedent("""\
            import venusian
            class Configurator:
                venusian = venusian
                def scan(self):
                    scanner = self.venusian.Scanner()
                    scanner.scan(pkg)
        """)
        )
        accesses = extract_attribute_accesses(f)
        assert "venusian" in accesses
        assert "Scanner" in accesses["venusian"]

    def test_syntax_error(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text("def broken(\n")
        accesses = extract_attribute_accesses(f)
        assert accesses == {}


class TestFindProjectSource:
    """Test project source tree discovery."""

    def test_finds_src_layout(self, sample_project):
        sources = find_project_source(sample_project)
        filenames = {p.name for p in sources}
        assert "main.py" in filenames
        assert "__init__.py" in filenames

    def test_finds_flat_layout(self, tmp_path):
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("import requests\n")
        # Also create pyproject.toml declaring the package
        (tmp_path / "pyproject.toml").write_text(
            textwrap.dedent("""\
            [project]
            name = "mypkg"
            version = "0.1.0"
            dependencies = []
        """)
        )
        sources = find_project_source(tmp_path)
        filenames = {p.name for p in sources}
        assert "core.py" in filenames

    def test_excludes_non_python_files(self, tmp_path):
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "data.json").write_text("{}")
        (pkg / "README.md").write_text("hello")
        (tmp_path / "pyproject.toml").write_text(
            textwrap.dedent("""\
            [project]
            name = "mypkg"
            version = "0.1.0"
            dependencies = []
        """)
        )
        sources = find_project_source(tmp_path)
        assert all(p.suffix == ".py" for p in sources)

    def test_src_layout_scans_all_packages(self, tmp_path):
        """When src/ exists, find all packages under it, not just the project name."""
        src_dir = tmp_path / "src"
        # Package matching project name
        pkg_a = src_dir / "myapp"
        pkg_a.mkdir(parents=True)
        (pkg_a / "__init__.py").write_text("")
        (pkg_a / "core.py").write_text("import requests\n")
        # A second package under src/ (e.g. a shared lib)
        pkg_b = src_dir / "mylib"
        pkg_b.mkdir()
        (pkg_b / "__init__.py").write_text("")
        (pkg_b / "helpers.py").write_text("import click\n")
        (tmp_path / "pyproject.toml").write_text(
            textwrap.dedent("""\
            [project]
            name = "myapp"
            version = "0.1.0"
            dependencies = []
        """)
        )
        sources = find_project_source(tmp_path)
        filenames = {p.name for p in sources}
        assert "core.py" in filenames
        assert "helpers.py" in filenames

    def test_src_layout_package_name_differs_from_project(self, tmp_path):
        """Package dir name doesn't match normalized project name."""
        pkg = tmp_path / "src" / "my_cool_app"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "main.py").write_text("import requests\n")
        (tmp_path / "pyproject.toml").write_text(
            textwrap.dedent("""\
            [project]
            name = "totally-different-name"
            version = "0.1.0"
            dependencies = []
        """)
        )
        sources = find_project_source(tmp_path)
        filenames = {p.name for p in sources}
        assert "main.py" in filenames

    def test_flat_layout_scans_all_packages(self, tmp_path):
        """Flat layout with multiple packages at project root."""
        pkg_a = tmp_path / "myapp"
        pkg_a.mkdir()
        (pkg_a / "__init__.py").write_text("")
        (pkg_a / "core.py").write_text("import requests\n")
        pkg_b = tmp_path / "mylib"
        pkg_b.mkdir()
        (pkg_b / "__init__.py").write_text("")
        (pkg_b / "utils.py").write_text("import click\n")
        (tmp_path / "pyproject.toml").write_text(
            textwrap.dedent("""\
            [project]
            name = "myapp"
            version = "0.1.0"
            dependencies = []
        """)
        )
        sources = find_project_source(tmp_path)
        filenames = {p.name for p in sources}
        assert "core.py" in filenames
        assert "utils.py" in filenames

    def test_excludes_tests_and_venv_dirs(self, tmp_path):
        """Should not scan tests/, .venv/, or other non-source dirs."""
        pkg = tmp_path / "src" / "myapp"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("import requests\n")
        # These should be excluded
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "__init__.py").write_text("")
        (tests_dir / "test_core.py").write_text("import pytest\n")
        venv_dir = tmp_path / ".venv" / "lib" / "somepkg"
        venv_dir.mkdir(parents=True)
        (venv_dir / "__init__.py").write_text("")
        (venv_dir / "mod.py").write_text("import os\n")
        (tmp_path / "pyproject.toml").write_text(
            textwrap.dedent("""\
            [project]
            name = "myapp"
            version = "0.1.0"
            dependencies = []
        """)
        )
        sources = find_project_source(tmp_path)
        # Check that no returned path is under tests/ or .venv/
        for p in sources:
            rel = p.relative_to(tmp_path)
            assert not rel.parts[0] == "tests"
            assert not rel.parts[0] == ".venv"

    def test_empty_project_returns_empty(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            textwrap.dedent("""\
            [project]
            name = "empty"
            version = "0.1.0"
            dependencies = []
        """)
        )
        sources = find_project_source(tmp_path)
        assert sources == []

    def test_no_config_files_still_finds_packages(self, tmp_path):
        """Project without pyproject.toml or setup.py still discovers packages."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("x = 1\n")
        sources = find_project_source(tmp_path)
        names = {f.name for f in sources}
        assert "__init__.py" in names
        assert "core.py" in names

    def test_standalone_py_files_found(self, tmp_path):
        """Standalone .py files at project root should be discovered."""
        (tmp_path / "main.py").write_text("import requests\n")
        (tmp_path / "utils.py").write_text("x = 1\n")
        (tmp_path / "requirements.txt").write_text("requests\n")
        sources = find_project_source(tmp_path)
        names = {f.name for f in sources}
        assert "main.py" in names
        assert "utils.py" in names

    def test_setup_py_excluded_from_source(self, tmp_path):
        """setup.py should not be treated as project source."""
        (tmp_path / "setup.py").write_text("from setuptools import setup\n")
        (tmp_path / "main.py").write_text("import requests\n")
        sources = find_project_source(tmp_path)
        names = {f.name for f in sources}
        assert "main.py" in names
        assert "setup.py" not in names

    def test_hidden_dirs_excluded(self, tmp_path):
        """Directories starting with . should be excluded from scanning."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("x = 1\n")
        hidden = tmp_path / ".hidden"
        hidden.mkdir()
        (hidden / "__init__.py").write_text("")
        (hidden / "secret.py").write_text("y = 2\n")
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "mypkg"\nversion = "0.1.0"\ndependencies = []\n'
        )
        sources = find_project_source(tmp_path)
        filenames = {p.name for p in sources}
        assert "core.py" in filenames
        assert "secret.py" not in filenames


class TestSourceRootDetection:
    """Test detection of source root from config files."""

    def test_pyproject_packages_find_where(self, tmp_path):
        """[tool.setuptools.packages.find] where = ["lib"]"""
        lib_dir = tmp_path / "lib" / "mypkg"
        lib_dir.mkdir(parents=True)
        (lib_dir / "__init__.py").write_text("")
        (lib_dir / "core.py").write_text("import requests\n")
        (tmp_path / "pyproject.toml").write_text(
            textwrap.dedent("""\
            [project]
            name = "mypkg"
            version = "0.1.0"
            dependencies = []

            [tool.setuptools.packages.find]
            where = ["lib"]
        """)
        )
        sources = find_project_source(tmp_path)
        filenames = {p.name for p in sources}
        assert "core.py" in filenames

    def test_pyproject_package_dir(self, tmp_path):
        """[tool.setuptools.package-dir] '' = 'lib'"""
        lib_dir = tmp_path / "lib" / "mypkg"
        lib_dir.mkdir(parents=True)
        (lib_dir / "__init__.py").write_text("")
        (lib_dir / "core.py").write_text("import requests\n")
        (tmp_path / "pyproject.toml").write_text(
            textwrap.dedent("""\
            [project]
            name = "mypkg"
            version = "0.1.0"
            dependencies = []

            [tool.setuptools.package-dir]
            "" = "lib"
        """)
        )
        sources = find_project_source(tmp_path)
        filenames = {p.name for p in sources}
        assert "core.py" in filenames

    def test_setup_py_package_dir(self, tmp_path):
        """setup(package_dir={"": "lib"})"""
        lib_dir = tmp_path / "lib" / "mypkg"
        lib_dir.mkdir(parents=True)
        (lib_dir / "__init__.py").write_text("")
        (lib_dir / "core.py").write_text("import requests\n")
        (tmp_path / "setup.py").write_text(
            "from setuptools import setup\n"
            'setup(name="mypkg", package_dir={"": "lib"})\n'
        )
        sources = find_project_source(tmp_path)
        filenames = {p.name for p in sources}
        assert "core.py" in filenames

    def test_setup_py_package_dir_variable(self, tmp_path):
        """package_dir stored in variable then passed to setup()."""
        lib_dir = tmp_path / "lib" / "mypkg"
        lib_dir.mkdir(parents=True)
        (lib_dir / "__init__.py").write_text("")
        (lib_dir / "core.py").write_text("x = 1\n")
        (tmp_path / "setup.py").write_text(
            "from setuptools import setup\n"
            'pkg_dir = {"": "lib"}\n'
            'setup(name="mypkg", package_dir=pkg_dir)\n'
        )
        sources = find_project_source(tmp_path)
        filenames = {p.name for p in sources}
        assert "core.py" in filenames

    def test_src_still_works_without_config(self, tmp_path):
        """src/ layout still works as fallback when no config specifies root."""
        pkg = tmp_path / "src" / "mypkg"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("x = 1\n")
        (tmp_path / "pyproject.toml").write_text(
            textwrap.dedent("""\
            [project]
            name = "mypkg"
            version = "0.1.0"
            dependencies = []
        """)
        )
        sources = find_project_source(tmp_path)
        filenames = {p.name for p in sources}
        assert "core.py" in filenames

    def test_packages_as_list_does_not_crash(self, tmp_path):
        """[tool.setuptools] packages = [...] should not raise."""
        pkg = tmp_path / "myapp"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("x = 1\n")
        (tmp_path / "pyproject.toml").write_text(
            textwrap.dedent("""\
            [project]
            name = "myapp"
            version = "0.1.0"
            dependencies = []

            [tool.setuptools]
            packages = ["myapp"]
        """)
        )
        sources = find_project_source(tmp_path)
        filenames = {p.name for p in sources}
        assert "core.py" in filenames


class TestSourceRootEdgeCases:
    """Coverage for source root detection edge cases."""

    def test_setup_py_syntax_error_returns_no_root(self, tmp_path):
        """setup.py with syntax error should not crash source root detection."""
        pkg = tmp_path / "src" / "mypkg"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("x = 1\n")
        (tmp_path / "setup.py").write_text("def broken(\n")
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "mypkg"\nversion = "0.1.0"\ndependencies = []\n'
        )
        sources = find_project_source(tmp_path)
        # Falls back to src/ layout
        filenames = {p.name for p in sources}
        assert "core.py" in filenames

    def test_setup_py_setuptools_dot_setup_package_dir(self, tmp_path):
        """setuptools.setup(package_dir=...) should also be detected."""
        lib_dir = tmp_path / "lib" / "mypkg"
        lib_dir.mkdir(parents=True)
        (lib_dir / "__init__.py").write_text("")
        (lib_dir / "core.py").write_text("x = 1\n")
        (tmp_path / "setup.py").write_text(
            "import setuptools\n"
            'setuptools.setup(name="mypkg", package_dir={"": "lib"})\n'
        )
        sources = find_project_source(tmp_path)
        filenames = {p.name for p in sources}
        assert "core.py" in filenames

    def test_setup_py_non_string_dict_in_package_dir(self, tmp_path):
        """package_dir with non-string values should be skipped gracefully."""
        pkg = tmp_path / "src" / "mypkg"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("x = 1\n")
        (tmp_path / "setup.py").write_text(
            "from setuptools import setup\n"
            "import os\n"
            'setup(name="mypkg", package_dir={"": os.path.join("src")})\n'
        )
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "mypkg"\nversion = "0.1.0"\ndependencies = []\n'
        )
        # Non-string value in dict -> _extract_string_dict returns None
        # Falls back to src/ layout
        sources = find_project_source(tmp_path)
        filenames = {p.name for p in sources}
        assert "core.py" in filenames

    def test_pyproject_toml_parse_error(self, tmp_path):
        """Malformed pyproject.toml shouldn't crash source root detection."""
        pkg = tmp_path / "src" / "mypkg"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("x = 1\n")
        (tmp_path / "pyproject.toml").write_text("this is not valid toml {{{\n")
        (tmp_path / "setup.py").write_text(
            "from setuptools import setup\nsetup(name='mypkg')\n"
        )
        # _source_root_from_pyproject catches Exception -> None
        # No setup.py root either, falls back to src/ layout
        sources = find_project_source(tmp_path)
        filenames = {p.name for p in sources}
        assert "core.py" in filenames


class TestInspectProject:
    """Test the full inspection pipeline — map imports to known dependencies."""

    def test_maps_imports_to_dependencies(self, sample_project):
        known_import_names = {"requests", "click"}
        usage = inspect_project(sample_project, known_import_names)

        assert "requests" in usage
        assert "click" in usage

    def test_includes_used_names(self, sample_project):
        known_import_names = {"requests", "click"}
        usage = inspect_project(sample_project, known_import_names)

        # main.py: import requests; requests.get(...) / from click import echo
        assert "requests" in usage
        # bare import + attribute access: requests.get() -> "get"
        assert "get" in usage["requests"]["used_names"]
        # click has specific name imported
        assert "echo" in usage["click"]["used_names"]

    def test_bare_import_resolves_attribute_accesses(self, tmp_path):
        """import hupper; hupper.is_active() should resolve to 'is_active'."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text(
            textwrap.dedent("""\
            import hupper
            if not hupper.is_active():
                hupper.start_reloader(main)
        """)
        )
        (tmp_path / "pyproject.toml").write_text(
            textwrap.dedent("""\
            [project]
            name = "mypkg"
            version = "0.1.0"
            dependencies = []
        """)
        )

        usage = inspect_project(tmp_path, {"hupper"})
        assert "hupper" in usage
        assert "is_active" in usage["hupper"]["used_names"]
        assert "start_reloader" in usage["hupper"]["used_names"]
        # The module name itself should NOT be in used_names
        assert "hupper" not in usage["hupper"]["used_names"]

    def test_aliased_bare_import_resolves_attributes(self, tmp_path):
        """import pandas as pd; pd.DataFrame() should resolve to 'DataFrame'."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text(
            textwrap.dedent("""\
            import pandas as pd
            df = pd.DataFrame()
        """)
        )
        (tmp_path / "pyproject.toml").write_text(
            textwrap.dedent("""\
            [project]
            name = "mypkg"
            version = "0.1.0"
            dependencies = []
        """)
        )

        usage = inspect_project(tmp_path, {"pandas"})
        assert "pandas" in usage
        assert "DataFrame" in usage["pandas"]["used_names"]

    def test_ignores_stdlib_and_unknown(self, sample_project):
        known_import_names = {"requests", "click"}
        usage = inspect_project(sample_project, known_import_names)

        # os and pathlib are stdlib, should not appear
        assert "os" not in usage
        assert "pathlib" not in usage

    def test_tracks_source_files(self, sample_project):
        known_import_names = {"requests", "click"}
        usage = inspect_project(sample_project, known_import_names)

        # Both requests and click are imported from main.py
        for dep in ("requests", "click"):
            files = usage[dep]["files"]
            assert any("main.py" in str(f) for f in files)

    def test_no_matches_returns_empty(self, sample_project):
        known_import_names = {"numpy", "scipy"}
        usage = inspect_project(sample_project, known_import_names)
        assert usage == {}

    def test_submodule_maps_to_top_level(self, tmp_path):
        """'from requests.auth import HTTPBasicAuth' maps to 'requests'."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("from requests.auth import HTTPBasicAuth\n")
        (tmp_path / "pyproject.toml").write_text(
            textwrap.dedent("""\
            [project]
            name = "mypkg"
            version = "0.1.0"
            dependencies = []
        """)
        )

        known_import_names = {"requests"}
        usage = inspect_project(tmp_path, known_import_names)
        assert "requests" in usage
        assert "HTTPBasicAuth" in usage["requests"]["used_names"]

    def test_bare_submodule_import_resolves_attributes(self, tmp_path):
        """import pygments.lexers; pygments.lexers.get_lexer_by_name() resolves."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text(
            textwrap.dedent("""\
            import pygments.lexers
            lexer = pygments.lexers.get_lexer_by_name("python")
        """)
        )
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "mypkg"\nversion = "0.1.0"\ndependencies = []\n'
        )
        usage = inspect_project(tmp_path, {"pygments"})
        assert "pygments" in usage
        # Should resolve the submodule attribute accesses
        assert "get_lexer_by_name" in usage["pygments"]["used_names"]

    def test_from_import_submodule_resolves_attributes(self, tmp_path):
        """from ua_parser import user_agent_parser; user_agent_parser.Parse()."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text(
            textwrap.dedent("""\
            from ua_parser import user_agent_parser
            result = user_agent_parser.Parse("Mozilla/5.0")
        """)
        )
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "mypkg"\nversion = "0.1.0"\ndependencies = []\n'
        )
        usage = inspect_project(tmp_path, {"ua_parser"})
        assert "ua_parser" in usage
        # Should include both the submodule name and the attribute access
        assert "user_agent_parser" in usage["ua_parser"]["used_names"]
        assert "Parse" in usage["ua_parser"]["used_names"]

    def test_bare_import_no_attrs_uses_module_name(self, tmp_path):
        """import foo with no attribute access should use 'foo' as used name."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("import hupper\n")
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "mypkg"\nversion = "0.1.0"\ndependencies = []\n'
        )
        usage = inspect_project(tmp_path, {"hupper"})
        assert "hupper" in usage
        # No attribute accesses → module name itself is used
        assert "hupper" in usage["hupper"]["used_names"]


class TestExtractStringReferences:
    """Test detection of string references to dependencies."""

    def test_dotted_path_matches(self, tmp_path):
        f = tmp_path / "settings.py"
        f.write_text(
            textwrap.dedent("""\
            MIDDLEWARE = [
                "whitenoise.middleware.WhiteNoiseMiddleware",
            ]
        """)
        )
        refs = extract_string_references(f, {"whitenoise"})
        assert "whitenoise" in refs
        values = {r.value for r in refs["whitenoise"]}
        assert "whitenoise.middleware.WhiteNoiseMiddleware" in values
        assert refs["whitenoise"][0].lineno == 2
        assert refs["whitenoise"][0].source_file == f

    def test_bare_name_matches(self, tmp_path):
        f = tmp_path / "settings.py"
        f.write_text('INSTALLED_APPS = ["allauth"]\n')
        refs = extract_string_references(f, {"allauth"})
        assert "allauth" in refs
        assert refs["allauth"][0].value == "allauth"

    @pytest.mark.parametrize(
        ("content", "dep"),
        [
            ('LANGUAGE_CODE = "en-us"\nSECRET_KEY = "abc123"\n', "whitenoise"),
            ('MSG = "whitenoise is great"\n', "whitenoise"),
            ('STATIC = "/whitenoise/static"\n', "whitenoise"),
        ],
        ids=["non-matching", "spaces", "path-string"],
    )
    def test_ignores_non_ref_strings(self, tmp_path, content, dep):
        f = tmp_path / "settings.py"
        f.write_text(content)
        refs = extract_string_references(f, {dep})
        assert refs == {}

    def test_multiple_refs_same_dep(self, tmp_path):
        f = tmp_path / "settings.py"
        f.write_text(
            textwrap.dedent("""\
            INSTALLED_APPS = ["allauth", "allauth.account"]
            MIDDLEWARE = [
                "allauth.account.middleware.AccountMiddleware",
            ]
        """)
        )
        refs = extract_string_references(f, {"allauth"})
        assert len(refs["allauth"]) == 3

    def test_multiple_deps(self, tmp_path):
        f = tmp_path / "settings.py"
        f.write_text(
            textwrap.dedent("""\
            INSTALLED_APPS = ["allauth", "anymail"]
        """)
        )
        refs = extract_string_references(f, {"allauth", "anymail"})
        assert "allauth" in refs
        assert "anymail" in refs

    def test_syntax_error_returns_empty(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text("def broken(\n")
        refs = extract_string_references(f, {"whitenoise"})
        assert refs == {}


class TestInspectProjectStringRefs:
    """Test string reference integration in inspect_project."""

    def test_string_ref_only_dep_appears_in_usage(self, tmp_path):
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "settings.py").write_text(
            textwrap.dedent("""\
            MIDDLEWARE = [
                "whitenoise.middleware.WhiteNoiseMiddleware",
            ]
        """)
        )
        (tmp_path / "pyproject.toml").write_text(
            textwrap.dedent("""\
            [project]
            name = "mypkg"
            version = "0.1.0"
            dependencies = []
        """)
        )
        usage = inspect_project(tmp_path, {"whitenoise"})
        assert "whitenoise" in usage
        ref_values = {r.value for r in usage["whitenoise"]["string_refs"]}
        assert "whitenoise.middleware.WhiteNoiseMiddleware" in ref_values

    def test_dep_with_both_imports_and_string_refs(self, tmp_path):
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "views.py").write_text(
            textwrap.dedent("""\
            from allauth.account.models import EmailAddress
            INSTALLED_APPS = ["allauth"]
        """)
        )
        (tmp_path / "pyproject.toml").write_text(
            textwrap.dedent("""\
            [project]
            name = "mypkg"
            version = "0.1.0"
            dependencies = []
        """)
        )
        usage = inspect_project(tmp_path, {"allauth"})
        assert "allauth" in usage
        assert "EmailAddress" in usage["allauth"]["used_names"]
        ref_values = {r.value for r in usage["allauth"]["string_refs"]}
        assert "allauth" in ref_values


class TestInspectSourceFilesUnparseable:
    """Test that inspect_source_files skips unparseable files (line 331)."""

    def test_skips_unparseable_files(self, tmp_path):
        """Mix of valid and unparseable .py files — valid ones are processed."""
        valid = tmp_path / "valid.py"
        valid.write_text("import requests\n")
        broken = tmp_path / "broken.py"
        broken.write_text("def broken(\n")

        with pytest.warns(UserWarning, match="Could not parse"):
            usage = inspect_source_files([valid, broken], {"requests"})
        assert "requests" in usage


class TestCollectPackagesExcludedDirs:
    """Test that excluded directories are pruned in _collect_packages (line 188)."""

    def test_excluded_dir_at_base_level_is_pruned(self, tmp_path):
        """Excluded dir sitting directly under the scanned base is skipped."""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("x = 1\n")
        # tests/ is in _EXCLUDED_DIRS and has an __init__.py
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "__init__.py").write_text("")
        (tests_dir / "test_core.py").write_text("import pytest\n")

        # Flat layout: _collect_packages runs on tmp_path directly,
        # so tests/ is a child and the continue on line 188 is hit.
        sources = find_project_source(tmp_path)
        filenames = {p.name for p in sources}
        assert "core.py" in filenames
        assert "test_core.py" not in filenames


class TestSourceRootFromSetupPyMalformedPackageDir:
    """Test that malformed package_dir in setup.py falls back gracefully (line 279)."""

    def test_package_dir_function_call_is_skipped(self, tmp_path):
        """package_dir=some_function() is neither ast.Dict nor ast.Name — skipped."""
        pkg = tmp_path / "src" / "mypkg"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("x = 1\n")
        (tmp_path / "setup.py").write_text(
            textwrap.dedent("""\
            from setuptools import setup
            setup(name="mypkg", package_dir=get_package_dir())
        """)
        )
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "mypkg"\nversion = "0.1.0"\ndependencies = []\n'
        )
        # package_dir value is a Call node — the else: continue branch fires.
        # Detection falls back to src/ layout.
        sources = find_project_source(tmp_path)
        filenames = {p.name for p in sources}
        assert "core.py" in filenames


class TestBareStringRefScoping:
    """Bare-name strings only count inside settings-style assignments."""

    @pytest.mark.parametrize(
        "content",
        [
            'errors = {"click": "boom"}\n',
            'label = "click"\n',
            'plugins = ["click"]\n',  # lowercase target: not a settings list
            'def f():\n    return "click"\n',
        ],
        ids=["dict-key", "plain-string", "lowercase-list", "return-value"],
    )
    def test_bare_name_outside_settings_ignored(self, tmp_path, content):
        f = tmp_path / "mod.py"
        f.write_text(content)
        refs = extract_string_references(f, {"click"})
        assert refs == {}

    def test_bare_name_in_augassign_settings(self, tmp_path):
        f = tmp_path / "settings.py"
        f.write_text('INSTALLED_APPS = []\nINSTALLED_APPS += ["allauth"]\n')
        refs = extract_string_references(f, {"allauth"})
        assert refs["allauth"][0].value == "allauth"

    def test_bare_name_in_annotated_settings(self, tmp_path):
        f = tmp_path / "settings.py"
        f.write_text('INSTALLED_APPS: list[str] = ["allauth"]\n')
        refs = extract_string_references(f, {"allauth"})
        assert refs["allauth"][0].value == "allauth"

    def test_bare_name_in_settings_tuple(self, tmp_path):
        f = tmp_path / "settings.py"
        f.write_text('MIDDLEWARE = ("allauth",)\n')
        refs = extract_string_references(f, {"allauth"})
        assert refs["allauth"][0].value == "allauth"

    def test_dotted_path_outside_settings_still_counts(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text('handler = "whitenoise.middleware.WhiteNoiseMiddleware"\n')
        refs = extract_string_references(f, {"whitenoise"})
        assert "whitenoise" in refs


class TestNamespacePackageDiscovery:
    """Project packages without __init__.py (PEP 420) must be discovered."""

    def test_namespace_package_collected(self, tmp_path):
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "core.py").write_text("import requests\n")
        sub = pkg / "sub"
        sub.mkdir()
        (sub / "util.py").write_text("x = 1\n")
        files = find_project_source(tmp_path)
        names = {f.name for f in files}
        assert "core.py" in names
        assert "util.py" in names

    def test_namespace_recursion_skips_excluded_dirs(self, tmp_path):
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "core.py").write_text("x = 1\n")
        tests_dir = pkg / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_core.py").write_text("x = 1\n")
        files = find_project_source(tmp_path)
        names = {f.name for f in files}
        assert "core.py" in names
        assert "test_core.py" not in names


class TestInspectSourceFilesCounted:
    """inspect_source_files_counted returns usage and LLOC in one pass."""

    def test_returns_usage_and_lloc(self, tmp_path):
        f = tmp_path / "app.py"
        f.write_text('import requests\nx = requests.get("u")\n')
        usage, lloc = inspect_source_files_counted([f], {"requests"})
        assert "requests" in usage
        assert lloc == 2

    def test_empty_input(self):
        usage, lloc = inspect_source_files_counted([], {"requests"})
        assert usage == {}
        assert lloc == 0
