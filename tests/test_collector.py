"""Tests for Phase 1: The Nest — Dependency Discovery."""

import importlib.metadata

import pytest

from unladen.collector import (
    _get_import_names,
    _get_source_paths,
    _normalize_dep_name,
    collect_dependencies,
    collect_package_deps,
    discover_site_packages,
    parse_dependencies,
    resolve_installed,
    resolve_package_info,
)


class TestNormalizeName:
    @pytest.mark.parametrize(
        ("input_spec", "expected"),
        [
            ("requests", "requests"),
            ("requests>=2.28", "requests"),
            ("requests>=2.28,<3", "requests"),
            ("Jinja2[i18n]", "jinja2"),
            ("my.cool-package", "my-cool-package"),
            ("my_package>=1.0", "my-package"),
            ("Jinja2", "jinja2"),
            ("Django>=4.0,<5.0", "django"),
            ("zope.interface>=5", "zope-interface"),
        ],
        ids=[
            "simple",
            "version",
            "complex-version",
            "extras",
            "dashes-dots",
            "underscores",
            "uppercase",
            "django-version",
            "zope-dotted",
        ],
    )
    def test_normalize(self, input_spec, expected):
        assert _normalize_dep_name(input_spec) == expected


class TestParseDependencies:
    def test_reads_pyproject_toml(self, sample_project):
        deps = parse_dependencies(sample_project)
        assert "requests" in deps
        assert "click" in deps

    def test_raises_on_missing_both(self, empty_project):
        with pytest.raises(FileNotFoundError, match="No dependencies found"):
            parse_dependencies(empty_project)

    def test_returns_empty_when_no_deps(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'nodeps'\nversion = '0.1.0'\n")
        with pytest.raises(FileNotFoundError, match="No dependencies found"):
            parse_dependencies(tmp_path)

    def test_setup_py_literal_list(self, tmp_path):
        (tmp_path / "setup.py").write_text(
            "from setuptools import setup\n"
            "setup(\n"
            '    name="mypkg",\n'
            '    install_requires=["requests>=2.0", "click"],\n'
            ")\n"
        )
        deps = parse_dependencies(tmp_path)
        assert "requests" in deps
        assert "click" in deps

    def test_setup_py_variable_reference(self, tmp_path):
        (tmp_path / "setup.py").write_text(
            "from setuptools import setup\n"
            "install_requires = [\n"
            '    "requests",\n'
            '    "click>=8.0",\n'
            "]\n"
            'setup(name="mypkg", install_requires=install_requires)\n'
        )
        deps = parse_dependencies(tmp_path)
        assert "requests" in deps
        assert "click" in deps

    def test_setup_py_setuptools_dot_setup(self, tmp_path):
        (tmp_path / "setup.py").write_text(
            "import setuptools\n"
            "setuptools.setup(\n"
            '    name="mypkg",\n'
            '    install_requires=["flask"],\n'
            ")\n"
        )
        deps = parse_dependencies(tmp_path)
        assert "flask" in deps

    def test_setup_py_no_install_requires(self, tmp_path):
        (tmp_path / "setup.py").write_text(
            'from setuptools import setup\nsetup(name="mypkg")\n'
        )
        with pytest.raises(FileNotFoundError, match="No dependencies found"):
            parse_dependencies(tmp_path)

    def test_pyproject_takes_priority(self, tmp_path):
        """If pyproject.toml has deps, setup.py is not consulted."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "mypkg"\nversion = "0.1.0"\n'
            'dependencies = ["requests"]\n'
        )
        (tmp_path / "setup.py").write_text(
            "from setuptools import setup\n"
            'setup(name="mypkg", install_requires=["flask"])\n'
        )
        deps = parse_dependencies(tmp_path)
        assert "requests" in deps
        assert "flask" not in deps

    def test_falls_back_to_setup_py(self, tmp_path):
        """Empty pyproject.toml deps should fall back to setup.py."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "mypkg"\nversion = "0.1.0"\n')
        (tmp_path / "setup.py").write_text(
            "from setuptools import setup\n"
            'setup(name="mypkg", install_requires=["flask"])\n'
        )
        deps = parse_dependencies(tmp_path)
        assert "flask" in deps

    def test_setup_cfg_install_requires(self, tmp_path):
        (tmp_path / "setup.cfg").write_text(
            "[metadata]\nname = mypkg\n\n"
            "[options]\n"
            "install_requires =\n"
            "    aiohttp\n"
            "    packaging\n"
            "    setuptools\n"
        )
        deps = parse_dependencies(tmp_path)
        assert "aiohttp" in deps
        assert "packaging" in deps
        assert "setuptools" in deps

    def test_setup_cfg_with_version_specifiers(self, tmp_path):
        (tmp_path / "setup.cfg").write_text(
            "[options]\ninstall_requires =\n    requests>=2.0\n    click>=8.0,<9\n"
        )
        deps = parse_dependencies(tmp_path)
        assert "requests" in deps
        assert "click" in deps

    def test_setup_cfg_empty_install_requires(self, tmp_path):
        (tmp_path / "setup.cfg").write_text("[options]\ninstall_requires =\n")
        with pytest.raises(FileNotFoundError, match="No dependencies"):
            parse_dependencies(tmp_path)

    def test_setup_cfg_no_options_section(self, tmp_path):
        (tmp_path / "setup.cfg").write_text("[metadata]\nname = mypkg\n")
        with pytest.raises(FileNotFoundError, match="No dependencies"):
            parse_dependencies(tmp_path)

    def test_setup_py_takes_priority_over_setup_cfg(self, tmp_path):
        (tmp_path / "setup.py").write_text(
            "from setuptools import setup\n"
            'setup(name="mypkg", install_requires=["flask"])\n'
        )
        (tmp_path / "setup.cfg").write_text(
            "[options]\ninstall_requires =\n    aiohttp\n"
        )
        deps = parse_dependencies(tmp_path)
        assert "flask" in deps
        assert "aiohttp" not in deps

    def test_falls_back_to_setup_cfg(self, tmp_path):
        """Empty setup.py should fall back to setup.cfg."""
        (tmp_path / "setup.py").write_text(
            'from setuptools import setup\nsetup(name="mypkg")\n'
        )
        (tmp_path / "setup.cfg").write_text(
            "[options]\ninstall_requires =\n    aiohttp\n"
        )
        deps = parse_dependencies(tmp_path)
        assert "aiohttp" in deps

    def test_poetry_dependencies(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            "[tool.poetry]\n"
            'name = "myapp"\n'
            "[tool.poetry.dependencies]\n"
            'python = "^3.10"\n'
            'requests = "^2.31"\n'
            'click = {version = "^8.0", optional = true}\n'
        )
        deps = parse_dependencies(tmp_path)
        assert "requests" in deps
        assert "click" in deps
        assert "python" not in deps

    def test_poetry_skips_python(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[tool.poetry.dependencies]\npython = "^3.10"\n')
        with pytest.raises(FileNotFoundError, match="No dependencies"):
            parse_dependencies(tmp_path)

    def test_pep621_takes_priority_over_poetry(self, tmp_path):
        """PEP 621 [project] deps should be preferred over Poetry."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "mypkg"\nversion = "0.1.0"\n'
            'dependencies = ["flask"]\n'
            "[tool.poetry.dependencies]\n"
            'requests = "^2.31"\n'
        )
        deps = parse_dependencies(tmp_path)
        assert "flask" in deps
        assert "requests" not in deps

    def test_requirements_txt_basic(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("requests>=2.0\nclick\n")
        deps = parse_dependencies(tmp_path)
        assert "requests" in deps
        assert "click" in deps

    def test_requirements_txt_comments_and_blanks(self, tmp_path):
        (tmp_path / "requirements.txt").write_text(
            "# This is a comment\n\nrequests\n# Another comment\nclick\n"
        )
        deps = parse_dependencies(tmp_path)
        assert deps == ["requests", "click"]

    def test_requirements_txt_r_includes(self, tmp_path):
        (tmp_path / "requirements.txt").write_text(
            "requests\n-r requirements-dev.txt\n"
        )
        (tmp_path / "requirements-dev.txt").write_text("pytest\nruff\n")
        deps = parse_dependencies(tmp_path)
        assert "requests" in deps
        assert "pytest" in deps
        assert "ruff" in deps

    def test_requirements_txt_nested_includes(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("-r base.txt\nflask\n")
        (tmp_path / "base.txt").write_text("-r core.txt\nclick\n")
        (tmp_path / "core.txt").write_text("requests\n")
        deps = parse_dependencies(tmp_path)
        assert deps == ["requests", "click", "flask"]

    def test_requirements_txt_circular_include(self, tmp_path):
        """Circular -r includes should not loop forever."""
        (tmp_path / "a.txt").write_text("requests\n-r b.txt\n")
        (tmp_path / "b.txt").write_text("click\n-r a.txt\n")
        deps = parse_dependencies(tmp_path, requirements="a.txt")
        assert "requests" in deps
        assert "click" in deps

    def test_requirements_txt_skips_options(self, tmp_path):
        (tmp_path / "requirements.txt").write_text(
            "--index-url https://pypi.org/simple\n"
            "-i https://pypi.org/simple\n"
            "--find-links /path/to/wheels\n"
            "-e .\n"
            "requests\n"
        )
        deps = parse_dependencies(tmp_path)
        assert deps == ["requests"]

    def test_requirements_txt_skips_urls(self, tmp_path):
        (tmp_path / "requirements.txt").write_text(
            "requests\nhttps://example.com/pkg.whl\n"
        )
        deps = parse_dependencies(tmp_path)
        assert deps == ["requests"]

    def test_requirements_txt_inline_comments(self, tmp_path):
        (tmp_path / "requirements.txt").write_text(
            "requests>=2.0  # HTTP library\nclick  # CLI\n"
        )
        deps = parse_dependencies(tmp_path)
        assert deps == ["requests", "click"]

    def test_requirements_txt_is_last_fallback(self, tmp_path):
        """pyproject.toml takes priority over requirements.txt."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "mypkg"\nversion = "0.1.0"\ndependencies = ["flask"]\n'
        )
        (tmp_path / "requirements.txt").write_text("requests\n")
        deps = parse_dependencies(tmp_path)
        assert "flask" in deps
        assert "requests" not in deps

    def test_explicit_requirements_overrides_detection(self, tmp_path):
        """--requirements flag overrides auto-detection."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "mypkg"\nversion = "0.1.0"\ndependencies = ["flask"]\n'
        )
        (tmp_path / "reqs.txt").write_text("requests\n")
        deps = parse_dependencies(tmp_path, requirements="reqs.txt")
        assert "requests" in deps
        assert "flask" not in deps

    def test_explicit_requirements_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="Requirements file not found"):
            parse_dependencies(tmp_path, requirements="nonexistent.txt")

    def test_requirements_long_option(self, tmp_path):
        """--requirement should work the same as -r."""
        (tmp_path / "requirements.txt").write_text(
            "requests\n--requirement extra.txt\n"
        )
        (tmp_path / "extra.txt").write_text("click\n")
        deps = parse_dependencies(tmp_path)
        assert "requests" in deps
        assert "click" in deps

    def test_explicit_pyproject_toml(self, tmp_path):
        """Passing pyproject.toml to --requirements should parse it correctly."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "mypkg"\nversion = "0.1.0"\n'
            'dependencies = ["requests", "click"]\n'
        )
        deps = parse_dependencies(tmp_path, requirements="pyproject.toml")
        assert "requests" in deps
        assert "click" in deps

    def test_explicit_setup_cfg(self, tmp_path):
        (tmp_path / "setup.cfg").write_text(
            "[options]\ninstall_requires =\n    flask\n    jinja2\n"
        )
        deps = parse_dependencies(tmp_path, requirements="setup.cfg")
        assert "flask" in deps
        assert "jinja2" in deps

    def test_setup_py_syntax_error(self, tmp_path):
        """setup.py with syntax error returns no deps."""
        (tmp_path / "setup.py").write_text("def broken(\n")
        with pytest.raises(FileNotFoundError, match="No dependencies"):
            parse_dependencies(tmp_path)


class TestDiscoverSitePackages:
    def test_returns_none_without_venv(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        result = discover_site_packages(tmp_path)
        assert result is None

    def test_finds_dot_venv(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        # Create a fake .venv structure
        sp = tmp_path / ".venv" / "lib" / "python3.14" / "site-packages"
        sp.mkdir(parents=True)
        result = discover_site_packages(tmp_path)
        assert result == sp

    def test_dot_venv_takes_priority_over_virtual_env(self, tmp_path, monkeypatch):
        # .venv in the project root should win over VIRTUAL_ENV
        # (VIRTUAL_ENV may point to the tool's venv, not the project's)
        venv_sp = tmp_path / "myvenv" / "lib" / "python3.14" / "site-packages"
        venv_sp.mkdir(parents=True)
        monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "myvenv"))

        dot_sp = tmp_path / ".venv" / "lib" / "python3.14" / "site-packages"
        dot_sp.mkdir(parents=True)

        result = discover_site_packages(tmp_path)
        assert result == dot_sp

    def test_falls_back_to_virtual_env(self, tmp_path, monkeypatch):
        # No .venv in project, but VIRTUAL_ENV is set
        venv_sp = tmp_path / "myvenv" / "lib" / "python3.14" / "site-packages"
        venv_sp.mkdir(parents=True)
        monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "myvenv"))

        result = discover_site_packages(tmp_path)
        assert result == venv_sp


class TestResolveInstalled:
    def test_resolves_known_packages(self, fake_site_packages):
        result = resolve_installed(["requests", "click"], fake_site_packages)

        assert "requests" in result
        assert result["requests"]["installed"] is True
        assert result["requests"]["version"] == "2.31.0"
        assert "requests" in result["requests"]["import_names"]

        assert "click" in result
        assert result["click"]["installed"] is True
        assert result["click"]["version"] == "8.1.7"

    def test_handles_missing_package(self, fake_site_packages):
        result = resolve_installed(["nonexistent"], fake_site_packages)
        assert result["nonexistent"]["installed"] is False

    def test_paths_point_to_real_dirs(self, fake_site_packages):
        result = resolve_installed(["requests"], fake_site_packages)
        for p in result["requests"]["paths"]:
            assert p.exists()


class TestGetImportNames:
    def test_fallback_to_record(self, tmp_path):
        """When top_level.txt is absent, fall back to RECORD."""

        dist_info = tmp_path / "mypkg-1.0.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: mypkg\nVersion: 1.0\n"
        )
        (dist_info / "RECORD").write_text(
            "mypkg/__init__.py,sha256=abc,100\n"
            "mypkg/core.py,sha256=def,200\n"
            "mypkg-1.0.dist-info/METADATA,sha256=ghi,50\n"
        )
        # Also create the package dir so it's realistic
        (tmp_path / "mypkg").mkdir()
        (tmp_path / "mypkg" / "__init__.py").write_text("")

        dist = importlib.metadata.Distribution.at(dist_info)
        names = _get_import_names(dist, "mypkg", tmp_path)
        assert "mypkg" in names

    def test_fallback_to_dist_name(self, tmp_path):
        """When both top_level.txt and RECORD are absent, guess from name."""

        dist_info = tmp_path / "my_pkg-1.0.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: my-pkg\nVersion: 1.0\n"
        )

        dist = importlib.metadata.Distribution.at(dist_info)
        names = _get_import_names(dist, "my-pkg", tmp_path)
        assert names == ["my_pkg"]

    def test_record_skips_underscore_dirs(self, tmp_path):
        """RECORD entries starting with _ should be skipped."""

        dist_info = tmp_path / "pkg-1.0.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n"
        )
        (dist_info / "RECORD").write_text(
            "_internal/__init__.py,sha256=abc,10\n"
            "pkg-1.0.dist-info/METADATA,sha256=ghi,50\n"
        )
        dist = importlib.metadata.Distribution.at(dist_info)
        names = _get_import_names(dist, "pkg", tmp_path)
        # Underscore dirs are filtered → falls back to dist name
        assert names == ["pkg"]

    def test_record_with_no_usable_entries(self, tmp_path):
        """RECORD with only dist-info entries → fall back to dist name."""

        dist_info = tmp_path / "pkg-1.0.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n"
        )
        (dist_info / "RECORD").write_text(
            "pkg-1.0.dist-info/METADATA,sha256=ghi,50\npkg-1.0.dist-info/RECORD,,\n"
        )
        dist = importlib.metadata.Distribution.at(dist_info)
        names = _get_import_names(dist, "pkg", tmp_path)
        assert names == ["pkg"]

    def test_record_toplevel_py_module(self, tmp_path):
        """RECORD with top-level .py files (e.g. legacy-cgi → cgi.py)."""
        dist_info = tmp_path / "legacy_cgi-2.6.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: legacy-cgi\nVersion: 2.6\n"
        )
        (dist_info / "RECORD").write_text(
            "cgi.py,sha256=abc,100\n"
            "cgitb.py,sha256=def,200\n"
            "__pycache__/cgi.cpython-314.pyc,,\n"
            "legacy_cgi-2.6.dist-info/METADATA,sha256=ghi,50\n"
        )
        dist = importlib.metadata.Distribution.at(dist_info)
        names = _get_import_names(dist, "legacy-cgi", tmp_path)
        assert "cgi" in names
        assert "cgitb" in names


class TestGetSourcePaths:
    def test_finds_package_directory(self, fake_site_packages):

        paths = _get_source_paths(["requests"], fake_site_packages)
        assert len(paths) == 1
        assert paths[0].name == "requests"

    def test_finds_single_file_module(self, tmp_path):

        (tmp_path / "simple.py").write_text("x = 1\n")
        paths = _get_source_paths(["simple"], tmp_path)
        assert len(paths) == 1
        assert paths[0].name == "simple.py"

    def test_returns_empty_for_missing(self, tmp_path):

        paths = _get_source_paths(["nope"], tmp_path)
        assert paths == []


class TestNamespacePackages:
    def test_narrows_to_owned_subpackages(self, tmp_path):
        """Namespace packages should be narrowed via RECORD."""

        # Create a namespace package with two subpackages
        zope = tmp_path / "zope"
        zope.mkdir()
        # No __init__.py — namespace package
        dep_dir = zope / "deprecation"
        dep_dir.mkdir()
        (dep_dir / "__init__.py").write_text("x = 1\n")
        iface_dir = zope / "interface"
        iface_dir.mkdir()
        (iface_dir / "__init__.py").write_text("y = 1\n")

        # Create a fake dist with RECORD listing only deprecation
        dist_info = tmp_path / "zope_deprecation-6.0.dist-info"
        dist_info.mkdir()
        (dist_info / "RECORD").write_text(
            "zope/deprecation/__init__.py,sha256=abc,10\n"
            "zope/deprecation/fixture.py,sha256=def,20\n"
        )
        (dist_info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: zope-deprecation\nVersion: 6.0\n"
        )
        dist = importlib.metadata.Distribution.at(dist_info)

        paths = _get_source_paths(["zope"], tmp_path, dist)
        path_names = [p.name for p in paths]
        assert "deprecation" in path_names
        assert "interface" not in path_names

    def test_regular_package_not_affected(self, fake_site_packages):
        """Regular packages with __init__.py should not be narrowed."""

        paths = _get_source_paths(["requests"], fake_site_packages)
        assert len(paths) == 1
        assert paths[0].name == "requests"

    def test_falls_back_if_no_record(self, tmp_path):
        """Namespace package without RECORD should include full directory."""

        zope = tmp_path / "zope"
        zope.mkdir()
        dep_dir = zope / "deprecation"
        dep_dir.mkdir()
        (dep_dir / "__init__.py").write_text("x = 1\n")

        # Dist with no RECORD
        dist_info = tmp_path / "zope_deprecation-6.0.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: zope-deprecation\nVersion: 6.0\n"
        )
        dist = importlib.metadata.Distribution.at(dist_info)

        paths = _get_source_paths(["zope"], tmp_path, dist)
        assert len(paths) == 1
        assert paths[0].name == "zope"


class TestRequirementsTxtEdgeCases:
    """Coverage for requirements.txt parsing edge cases."""

    def test_inline_comment_only_after_strip(self, tmp_path):
        """A line that becomes empty after stripping inline comment."""
        (tmp_path / "requirements.txt").write_text("# comment\nrequests  # lib\n")
        deps = parse_dependencies(tmp_path)
        assert deps == ["requests"]

    def test_requirements_with_path_specifiers(self, tmp_path):
        """Lines starting with . or / should be skipped."""
        (tmp_path / "requirements.txt").write_text(
            "requests\n./local_pkg\n/abs/path/pkg\nclick\n"
        )
        deps = parse_dependencies(tmp_path)
        assert deps == ["requests", "click"]

    def test_explicit_setup_py_requirements(self, tmp_path):
        """Passing setup.py to --requirements should dispatch correctly."""
        (tmp_path / "setup.py").write_text(
            "from setuptools import setup\n"
            'setup(name="mypkg", install_requires=["flask"])\n'
        )
        deps = parse_dependencies(tmp_path, requirements="setup.py")
        assert "flask" in deps

    def test_explicit_requirements_absolute_path(self, tmp_path):
        """Absolute path to requirements file."""
        reqs = tmp_path / "custom" / "reqs.txt"
        reqs.parent.mkdir()
        reqs.write_text("requests\n")
        deps = parse_dependencies(tmp_path, requirements=str(reqs))
        assert "requests" in deps

    def test_empty_explicit_requirements_raises(self, tmp_path):
        """Explicit requirements file with no deps should raise."""
        reqs = tmp_path / "empty.txt"
        reqs.write_text("# only comments\n\n")
        with pytest.raises(FileNotFoundError, match="No dependencies found"):
            parse_dependencies(tmp_path, requirements="empty.txt")


class TestNormalizeEdgeCases:
    """Edge cases in dependency name normalization."""

    def test_single_char_name(self):
        assert _normalize_dep_name("a") == "a"

    def test_unmatchable_string(self):
        """Strings that don't match the regex fall back to strip+lower."""
        assert _normalize_dep_name("!invalid") == "!invalid"


class TestCollectDependencies:
    def test_end_to_end(self, sample_project, fake_site_packages):
        result = collect_dependencies(
            sample_project,
            site_packages=fake_site_packages,
        )
        assert "requests" in result
        assert "click" in result

    def test_raises_without_venv(self, sample_project, monkeypatch):
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        # Ensure no .venv exists under sample_project
        with pytest.raises(FileNotFoundError, match="Could not discover"):
            collect_dependencies(sample_project)

    def test_empty_deps_raises(self, tmp_path, fake_site_packages):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'nodeps'\nversion = '0.1.0'\n")
        with pytest.raises(FileNotFoundError, match="No dependencies found"):
            collect_dependencies(tmp_path, site_packages=fake_site_packages)


class TestResolvePackageInfo:
    def test_returns_version_and_paths(self, fake_site_packages):
        version, import_names, paths = resolve_package_info(
            "requests", fake_site_packages
        )
        assert version is not None
        assert "requests" in import_names
        assert len(paths) >= 1

    def test_not_installed_raises(self, tmp_path):
        sp = tmp_path / "sp"
        sp.mkdir()
        with pytest.raises(FileNotFoundError):
            resolve_package_info("nonexistent", sp)


class TestCollectPackageDeps:
    def test_reads_requires_dist(self, tmp_path):
        sp = tmp_path / "sp"
        sp.mkdir()
        di = sp / "mypkg-1.0.dist-info"
        di.mkdir()
        (di / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: mypkg\nVersion: 1.0\n"
            "Requires-Dist: helplib>=1.0\n"
            "Requires-Dist: otherlib\n"
        )
        # Create helplib and otherlib dist-infos (not installed)
        result = collect_package_deps("mypkg", sp)
        assert "helplib" in result
        assert "otherlib" in result
        # Both should be marked not installed (no dist-info for them)
        assert not result["helplib"]["installed"]

    def test_skips_extras(self, tmp_path):
        sp = tmp_path / "sp"
        sp.mkdir()
        di = sp / "mypkg-1.0.dist-info"
        di.mkdir()
        (di / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: mypkg\nVersion: 1.0\n"
            "Requires-Dist: real-dep\n"
            'Requires-Dist: dev-dep; extra == "dev"\n'
        )
        result = collect_package_deps("mypkg", sp)
        assert "real-dep" in result
        assert "dev-dep" not in result

    def test_no_requires(self, tmp_path):
        sp = tmp_path / "sp"
        sp.mkdir()
        di = sp / "mypkg-1.0.dist-info"
        di.mkdir()
        (di / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: mypkg\nVersion: 1.0\n"
        )
        result = collect_package_deps("mypkg", sp)
        assert result == {}


class TestCollectDependenciesEmptyDeps:
    """collect_dependencies returns {} when no dependency specs are found."""

    def test_returns_empty_dict_when_no_deps_declared(
        self, tmp_path, fake_site_packages, monkeypatch
    ):
        # _find_dependencies normally raises when empty, so mock it to return []
        import unladen.collector as collector_mod

        monkeypatch.setattr(
            collector_mod, "_find_dependencies", lambda *a, **kw: ([], "none")
        )
        result = collect_dependencies(tmp_path, site_packages=fake_site_packages)
        assert result == {}


class TestRequirementsTxtReadError:
    """Lines 277-278: _parse_requirements_txt returns [] on read error."""

    def test_raises_when_file_has_bad_encoding(self, tmp_path):
        # Write bytes that are not valid UTF-8 to trigger UnicodeDecodeError
        req = tmp_path / "requirements.txt"
        req.write_bytes(b"requests\n\xff\xfe invalid utf-8\n")
        # _parse_requirements_txt returns [] → _find_dependencies raises
        with pytest.raises(FileNotFoundError, match="No dependencies found"):
            parse_dependencies(tmp_path)


class TestExtractStringListNonString:
    """_extract_string_list returns None for non-string elements."""

    def test_setup_py_with_non_string_in_install_requires(self, tmp_path):
        # install_requires=[1, "requests"] — integer element causes bail-out
        (tmp_path / "setup.py").write_text(
            "from setuptools import setup\n"
            'setup(name="mypkg", install_requires=[1, "requests"])\n'
        )
        # _extract_string_list returns None → _parse_setup_py returns []
        # → _find_dependencies raises FileNotFoundError
        with pytest.raises(FileNotFoundError, match="No dependencies found"):
            parse_dependencies(tmp_path)


class TestDiscoverSitePackagesWindowsLayout:
    """Lines 465-468: Windows-style venv layout discovery."""

    def test_finds_windows_layout_venv(self, tmp_path, monkeypatch):
        """Windows-style .venv/Lib/site-packages should be discovered."""
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        sp = tmp_path / ".venv" / "Lib" / "site-packages"
        sp.mkdir(parents=True)
        result = discover_site_packages(tmp_path)
        assert result == sp

    def test_returns_none_for_broken_venv(self, tmp_path, monkeypatch):
        """A .venv with no recognizable site-packages layout returns None."""
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        broken = tmp_path / ".venv" / "lib" / "notpython"
        broken.mkdir(parents=True)
        result = discover_site_packages(tmp_path)
        assert result is None


class TestGetImportNamesRecordEdgeCases:
    """Line 502 and branch 511→499: RECORD parsing edge cases in _get_import_names."""

    def test_record_empty_path_entry(self, tmp_path):
        """RECORD line starting with comma should be skipped."""
        dist_info = tmp_path / "pkg-1.0.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n"
        )
        (dist_info / "RECORD").write_text(
            ",sha256=abc,100\npkg/__init__.py,sha256=def,200\n"
        )
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "__init__.py").write_text("")
        dist = importlib.metadata.Distribution.at(dist_info)
        names = _get_import_names(dist, "pkg", tmp_path)
        assert "pkg" in names

    def test_record_non_python_toplevel_file(self, tmp_path):
        """RECORD with LICENSE (no / and not .py) should be skipped."""
        dist_info = tmp_path / "pkg-1.0.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n"
        )
        (dist_info / "RECORD").write_text(
            "LICENSE,sha256=abc,100\npkg/__init__.py,sha256=def,200\n"
        )
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "__init__.py").write_text("")
        dist = importlib.metadata.Distribution.at(dist_info)
        names = _get_import_names(dist, "pkg", tmp_path)
        assert "pkg" in names
        assert "LICENSE" not in names


class TestOwnedSubpackagesEdgeCases:
    """Branches 602→596 and 604→596: namespace package RECORD edge cases."""

    def test_namespace_record_toplevel_file_skipped(self, tmp_path):
        """zope/__init__.py (no subdir) should not add a subpackage."""
        zope = tmp_path / "zope"
        zope.mkdir()
        dep_dir = zope / "deprecation"
        dep_dir.mkdir()
        (dep_dir / "__init__.py").write_text("x = 1\n")
        dist_info = tmp_path / "zope_deprecation-6.0.dist-info"
        dist_info.mkdir()
        (dist_info / "RECORD").write_text(
            "zope/__init__.py,sha256=abc,10\n"
            "zope/deprecation/__init__.py,sha256=def,20\n"
        )
        (dist_info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: zope-deprecation\nVersion: 6.0\n"
        )
        dist = importlib.metadata.Distribution.at(dist_info)
        paths = _get_source_paths(["zope"], tmp_path, dist)
        path_names = [p.name for p in paths]
        assert "deprecation" in path_names

    def test_namespace_record_dist_info_entry_skipped(self, tmp_path):
        """RECORD entry like zope/weird.dist-info/ should be filtered out."""
        zope = tmp_path / "zope"
        zope.mkdir()
        dep_dir = zope / "deprecation"
        dep_dir.mkdir()
        (dep_dir / "__init__.py").write_text("x = 1\n")
        dist_info = tmp_path / "zope_deprecation-6.0.dist-info"
        dist_info.mkdir()
        (dist_info / "RECORD").write_text(
            "zope/weird.dist-info/METADATA,sha256=abc,10\n"
            "zope/deprecation/__init__.py,sha256=def,20\n"
        )
        (dist_info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: zope-deprecation\nVersion: 6.0\n"
        )
        dist = importlib.metadata.Distribution.at(dist_info)
        paths = _get_source_paths(["zope"], tmp_path, dist)
        path_names = [p.name for p in paths]
        assert "deprecation" in path_names
        assert "weird.dist-info" not in path_names


class TestCollectListVariablesEdgeCases:
    """Branches 206→203, 208→203: _collect_list_variables edge cases."""

    def test_setup_py_tuple_assignment_ignored(self, tmp_path):
        """Tuple unpacking at module level should not crash setup.py parsing."""
        (tmp_path / "setup.py").write_text(
            "from setuptools import setup\n"
            "a, b = ['x'], ['y']\n"
            'setup(name="mypkg", install_requires=["flask"])\n'
        )
        deps = parse_dependencies(tmp_path)
        assert "flask" in deps


class TestInlineCommentOrdering:
    """Inline comments must be stripped before URL/option skip checks."""

    def test_inline_comment_with_url_kept(self, tmp_path):
        """A URL inside an inline comment must not skip the dependency."""
        (tmp_path / "requirements.txt").write_text(
            "requests  # docs: https://example.com/requests\n"
        )
        deps = parse_dependencies(tmp_path)
        assert deps == ["requests"]

    def test_include_with_inline_comment_followed(self, tmp_path):
        """-r includes with trailing comments are still followed."""
        (tmp_path / "base.txt").write_text("click\n")
        (tmp_path / "requirements.txt").write_text("-r base.txt  # shared deps\n")
        deps = parse_dependencies(tmp_path)
        assert deps == ["click"]

    def test_url_fragment_not_treated_as_comment(self, tmp_path):
        """'#egg=' with no preceding whitespace is a URL fragment;
        the line is still skipped as a URL."""
        (tmp_path / "requirements.txt").write_text(
            "https://example.com/pkg.whl#egg=pkg\nrequests\n"
        )
        deps = parse_dependencies(tmp_path)
        assert deps == ["requests"]


class TestFindDistInfoDottedNames:
    """Metadata dirs that keep dots in the name must still match."""

    def test_egg_info_with_dots_matches(self, tmp_path):
        egg = tmp_path / "zope.interface-6.0.egg-info"
        egg.mkdir()
        (egg / "PKG-INFO").write_text("Name: zope.interface\nVersion: 6.0\n")
        result = resolve_installed(["zope-interface"], tmp_path)
        assert result["zope-interface"]["installed"] is True

    def test_missing_dist_info_reports_not_installed(self, tmp_path):
        result = resolve_installed(["nonexistent"], tmp_path)
        assert result["nonexistent"]["installed"] is False


class TestMalformedConfigFiles:
    """Malformed config files raise a clean ValueError, not a traceback."""

    def test_malformed_pyproject_raises_value_error(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project\ndependencies = [\n")
        with pytest.raises(ValueError, match="Invalid TOML"):
            parse_dependencies(tmp_path)

    def test_malformed_setup_cfg_raises_value_error(self, tmp_path):
        (tmp_path / "setup.cfg").write_text("[options]\n[options]\n")
        with pytest.raises(ValueError, match="Invalid setup.cfg"):
            parse_dependencies(tmp_path)


class TestEnvironmentMarkers:
    """Marker-gated deps carry their marker so absence isn't a false alarm."""

    def test_marker_captured_from_pep621(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "p"\nversion = "0"\n'
            "dependencies = [\n"
            '  "requests>=2.0",\n'
            "  'colorama ; sys_platform == \"win32\"',\n"
            "]\n"
        )
        sp = tmp_path / "sp"
        sp.mkdir()
        result = collect_dependencies(tmp_path, site_packages=sp)
        assert result["colorama"]["marker"] == 'sys_platform == "win32"'
        assert result["colorama"]["installed"] is False
        assert result["requests"]["marker"] is None

    def test_marker_captured_from_requirements(self, tmp_path):
        (tmp_path / "requirements.txt").write_text(
            'requests\ncolorama; sys_platform == "win32"\n'
        )
        sp = tmp_path / "sp"
        sp.mkdir()
        result = collect_dependencies(tmp_path, site_packages=sp)
        assert result["colorama"]["marker"] == 'sys_platform == "win32"'

    def test_marker_captured_from_poetry_dict(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[tool.poetry.dependencies]\n"
            'python = "^3.14"\n'
            'colorama = { version = "*", markers = "sys_platform == \'win32\'" }\n'
            'requests = "*"\n'
        )
        sp = tmp_path / "sp"
        sp.mkdir()
        result = collect_dependencies(tmp_path, site_packages=sp)
        assert result["colorama"]["marker"] == "sys_platform == 'win32'"
        assert result["requests"]["marker"] is None
        assert "python" not in result

    def test_marker_captured_from_requires_dist(self, tmp_path):
        sp = tmp_path / "sp"
        sp.mkdir()
        di = sp / "mypkg-1.0.dist-info"
        di.mkdir()
        (di / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: mypkg\nVersion: 1.0\n"
            "Requires-Dist: real-dep\n"
            'Requires-Dist: win-dep ; sys_platform == "win32"\n'
            'Requires-Dist: dev-dep ; extra == "dev"\n'
        )
        result = collect_package_deps("mypkg", sp)
        assert result["win-dep"]["marker"] == 'sys_platform == "win32"'
        assert result["real-dep"]["marker"] is None
        assert "dev-dep" not in result

    def test_parse_dependencies_still_normalizes_markered_specs(self, tmp_path):
        (tmp_path / "requirements.txt").write_text(
            'Foo.Bar>=1.0 ; python_version < "3.10"\n'
        )
        deps = parse_dependencies(tmp_path)
        assert deps == ["foo-bar"]
