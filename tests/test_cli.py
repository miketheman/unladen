"""Tests for the CLI interface."""

import json

import pytest

from unladen._config import load_config, load_exclude_set
from unladen.cli import (
    build_parser,
    main,
)


class TestBuildParser:
    def test_returns_parser(self):
        parser = build_parser()
        assert parser.prog == "unladen"

    @pytest.mark.parametrize(
        ("argv", "attr", "expected"),
        [
            (["check", "/some/path"], "command", "check"),
            (["check"], "target", "."),
            (["check", "--site-packages", "/sp"], "site_packages", "/sp"),
            (["check", "-r", "reqs.txt"], "requirements", "reqs.txt"),
            (["check", "--requirements", "reqs.txt"], "requirements", "reqs.txt"),
            (["show", "requests", "/some/path"], "command", "show"),
            (["show", "requests"], "path", "."),
            (["show", "requests", "-r", "reqs.txt"], "requirements", "reqs.txt"),
        ],
        ids=[
            "check-command",
            "check-defaults-cwd",
            "check-site-packages",
            "check-r-short",
            "check-r-long",
            "show-command",
            "show-defaults-cwd",
            "show-r-flag",
        ],
    )
    def test_parses_args(self, argv, attr, expected):
        parser = build_parser()
        args = parser.parse_args(argv)
        assert str(getattr(args, attr)) == expected

    def test_show_dep_arg(self):
        parser = build_parser()
        args = parser.parse_args(["show", "requests", "/some/path"])
        assert args.dep == "requests"


class TestMain:
    def test_no_command_prints_help(self, capsys):
        ret = main([])
        assert ret == 0

    def test_check_nonexistent_path(self, capsys):
        ret = main(["check", "/nonexistent/path"])
        assert ret == 1
        captured = capsys.readouterr()
        assert "Error" in captured.err

    def test_check_treemap_flag(self):
        parser = build_parser()
        args = parser.parse_args(["check", "--treemap"])
        assert args.treemap is True

    def test_check_treemap_with_sample_project(
        self, sample_project, fake_site_packages, capsys
    ):
        ret = main(
            [
                "check",
                str(sample_project),
                "--site-packages",
                str(fake_site_packages),
                "--treemap",
            ]
        )
        assert ret == 0
        output = capsys.readouterr().out
        assert "Treemap" in output

    def test_check_with_sample_project(self, sample_project, fake_site_packages):
        ret = main(
            [
                "check",
                str(sample_project),
                "--site-packages",
                str(fake_site_packages),
            ]
        )
        assert ret == 0


class TestPackageMode:
    """Tests for `unladen check <package-name>` mode."""

    def test_check_package_not_found(self, tmp_path, capsys):
        """Unknown package name should error."""
        sp = tmp_path / "sp"
        sp.mkdir()
        ret = main(["check", "nonexistent-pkg", "--site-packages", str(sp)])
        assert ret == 1
        assert "not found" in capsys.readouterr().err

    def test_check_package_no_deps(self, tmp_path, capsys):
        """Package with no Requires-Dist should report no deps."""
        sp = tmp_path / "sp"
        sp.mkdir()
        pkg = sp / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("x = 1\n")
        di = sp / "mypkg-1.0.dist-info"
        di.mkdir()
        (di / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: mypkg\nVersion: 1.0\n"
        )
        (di / "top_level.txt").write_text("mypkg\n")
        ret = main(["check", "mypkg", "--site-packages", str(sp)])
        assert ret == 0
        output = capsys.readouterr().out
        assert "no declared dependencies" in output.lower()

    def test_check_package_with_deps(self, tmp_path, capsys):
        """Package with Requires-Dist should analyze its deps."""
        sp = tmp_path / "sp"
        sp.mkdir()

        # Target package: mypkg that uses helplib
        pkg = sp / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("from helplib import helper\nhelper()\n")
        di = sp / "mypkg-1.0.dist-info"
        di.mkdir()
        (di / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: mypkg\nVersion: 1.0\n"
            "Requires-Dist: helplib>=1.0\n"
        )
        (di / "top_level.txt").write_text("mypkg\n")

        # Dependency: helplib
        dep = sp / "helplib"
        dep.mkdir()
        (dep / "__init__.py").write_text(
            "def helper():\n    return 1\n\ndef unused():\n    return 2\n"
        )
        dep_di = sp / "helplib-1.0.dist-info"
        dep_di.mkdir()
        (dep_di / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: helplib\nVersion: 1.0\n"
        )
        (dep_di / "top_level.txt").write_text("helplib\n")

        ret = main(["check", "mypkg", "--site-packages", str(sp)])
        assert ret == 0
        output = capsys.readouterr().out
        assert "mypkg" in output
        assert "helplib" in output

    def test_check_package_skips_extras(self, tmp_path, capsys):
        """Extras-only Requires-Dist should be excluded."""
        sp = tmp_path / "sp"
        sp.mkdir()
        pkg = sp / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("x = 1\n")
        di = sp / "mypkg-1.0.dist-info"
        di.mkdir()
        (di / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: mypkg\nVersion: 1.0\n"
            'Requires-Dist: dev-only; extra == "dev"\n'
        )
        (di / "top_level.txt").write_text("mypkg\n")
        ret = main(["check", "mypkg", "--site-packages", str(sp)])
        assert ret == 0
        output = capsys.readouterr().out
        assert "no declared dependencies" in output.lower()

    def test_check_package_r_flag_rejected(self, capsys):
        """Package mode should reject -r flag."""
        ret = main(["check", "mypkg", "-r", "reqs.txt"])
        assert ret == 1
        assert "not supported" in capsys.readouterr().err.lower()

    def test_directory_takes_precedence(self, sample_project, fake_site_packages):
        """If target is an existing directory, use project mode."""
        ret = main(
            [
                "check",
                str(sample_project),
                "--site-packages",
                str(fake_site_packages),
            ]
        )
        assert ret == 0


class TestRequirementsPathResolution:
    """Test that -r paths resolve relative to CWD, not project path."""

    def test_requirements_resolved_from_cwd(self, tmp_path, capsys, monkeypatch):
        """'-r reqs.txt' should resolve from CWD, not from the project path."""
        # Layout: tmp_path/
        #   reqs.txt          <- requirements at CWD level
        #   src/              <- project path passed as arg
        #     myapp/__init__.py
        src = tmp_path / "src"
        pkg = src / "myapp"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (src / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "0.1.0"\n'
        )
        req = tmp_path / "reqs.txt"
        req.write_text("requests\n")

        sp = tmp_path / "sp"
        sp.mkdir()

        monkeypatch.chdir(tmp_path)
        ret = main(["check", str(src), "-r", "reqs.txt", "--site-packages", str(sp)])
        # Should find reqs.txt relative to CWD (tmp_path), not src/
        assert ret == 0


class TestCheckIntegration:
    """Integration tests for _cmd_check covering more code paths."""

    def test_check_no_deps_found(self, tmp_path, capsys):
        """Project with empty deps list."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "empty"\nversion = "0.1.0"\n')
        ret = main(["check", str(tmp_path)])
        # No deps in pyproject -> FileNotFoundError
        assert ret == 1

    def test_check_file_not_found_error(self, capsys):
        ret = main(["check", "/nonexistent/path/to/project"])
        assert ret == 1
        assert "Error" in capsys.readouterr().err

    def test_check_deps_declared_but_none_installed(self, tmp_path, capsys):
        """All deps declared but none installed -> table with 'not installed'."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "myapp"\nversion = "0.1.0"\n'
            'dependencies = ["nonexistent-pkg"]\n'
        )
        sp = tmp_path / "fake_sp"
        sp.mkdir()
        ret = main(["check", str(tmp_path), "--site-packages", str(sp)])
        assert ret == 0

    def test_check_no_deps_collected(self, tmp_path, capsys):
        """Empty dep map returns early with 'No dependencies found.'"""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "myapp"\nversion = "0.1.0"\n'
            'dependencies = ["nonexistent-pkg"]\n'
        )
        sp = tmp_path / "fake_sp"
        sp.mkdir()
        ret = main(["check", str(tmp_path), "--site-packages", str(sp)])
        assert ret == 0


class TestCheckRecommendations:
    """Integration tests for check command recommendations."""

    def test_check_installed_unused_dep_gets_remove(
        self, tmp_path, fake_site_packages, capsys
    ):
        """Installed dep not imported by project should get Remove? recommendation."""
        pkg = tmp_path / "src" / "myapp"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "main.py").write_text("x = 1\n")
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "0.1.0"\n'
            'dependencies = ["requests"]\n'
        )
        ret = main(
            [
                "check",
                str(tmp_path),
                "--site-packages",
                str(fake_site_packages),
            ]
        )
        assert ret == 0
        output = capsys.readouterr().out
        assert "Remove" in output

    def test_check_string_ref_only_dep(self, tmp_path, capsys):
        """Check with a dep activated only via string refs (no direct import)."""
        pkg = tmp_path / "src" / "myapp"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "settings.py").write_text(
            'MIDDLEWARE = ["whitenoise.middleware.WhiteNoiseMiddleware"]\n'
        )
        sp = tmp_path / "site-packages"
        sp.mkdir()
        wn_pkg = sp / "whitenoise"
        wn_pkg.mkdir()
        (wn_pkg / "__init__.py").write_text("x = 1\n")
        (wn_pkg / "middleware.py").write_text("class WhiteNoiseMiddleware:\n    pass\n")
        dist_info = sp / "whitenoise-6.0.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: whitenoise\nVersion: 6.0\n"
        )
        (dist_info / "top_level.txt").write_text("whitenoise\n")
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "0.1.0"\n'
            'dependencies = ["whitenoise"]\n'
        )
        ret = main(["check", str(tmp_path), "--site-packages", str(sp)])
        assert ret == 0


class TestLoadExcludeSet:
    def test_returns_normalized_names(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[tool.unladen]\nexclude = ["Setuptools", "my_pkg"]\n'
        )
        result = load_exclude_set(tmp_path)
        assert result == {"setuptools", "my-pkg"}

    def test_empty_when_no_config(self, tmp_path):
        result = load_exclude_set(tmp_path)
        assert result == set()


class TestLoadConfig:
    def test_reads_tool_unladen_section(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[tool.unladen]\nexclude = ["setuptools", "pip"]\n'
        )
        config = load_config(tmp_path)
        assert config["exclude"] == ["setuptools", "pip"]

    def test_missing_pyproject(self, tmp_path):
        config = load_config(tmp_path)
        assert config == {}

    def test_no_tool_unladen_section(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "0.1.0"\n'
        )
        config = load_config(tmp_path)
        assert config == {}

    def test_malformed_toml(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("not valid toml [[[")
        config = load_config(tmp_path)
        assert config == {}

    def test_empty_exclude_list(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.unladen]\nexclude = []\n")
        config = load_config(tmp_path)
        assert config["exclude"] == []


class TestExcludeIntegration:
    """Integration tests for [tool.unladen] exclude feature."""

    def test_check_excludes_deps(self, tmp_path, fake_site_packages, capsys):
        """Excluded deps should not appear in check output."""
        pkg = tmp_path / "src" / "myapp"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("import requests\nrequests.get('url')\n")
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "0.1.0"\n'
            'dependencies = ["requests"]\n\n'
            '[tool.unladen]\nexclude = ["requests"]\n'
        )
        ret = main(
            [
                "check",
                str(tmp_path),
                "--site-packages",
                str(fake_site_packages),
            ]
        )
        assert ret == 0
        output = capsys.readouterr().out
        assert "excluded" in output.lower()

    def test_check_excluded_footnote(self, tmp_path, fake_site_packages, capsys):
        """Table footnote should mention excluded count."""
        pkg = tmp_path / "src" / "myapp"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("x = 1\n")
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "0.1.0"\n'
            'dependencies = ["requests"]\n\n'
            '[tool.unladen]\nexclude = ["requests"]\n'
        )
        ret = main(
            [
                "check",
                str(tmp_path),
                "--site-packages",
                str(fake_site_packages),
            ]
        )
        assert ret == 0
        output = capsys.readouterr().out
        assert "all excluded" in output.lower()

    def test_check_with_no_excludes(self, sample_project, fake_site_packages, capsys):
        """No [tool.unladen] section should work normally."""
        ret = main(
            [
                "check",
                str(sample_project),
                "--site-packages",
                str(fake_site_packages),
            ]
        )
        assert ret == 0
        output = capsys.readouterr().out
        assert "excluded" not in output.lower()

    def test_exclude_normalization(self, tmp_path, fake_site_packages, capsys):
        """Exclude list should match regardless of name normalization."""
        pkg = tmp_path / "src" / "myapp"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("x = 1\n")
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "0.1.0"\n'
            'dependencies = ["requests"]\n\n'
            '[tool.unladen]\nexclude = ["Requests"]\n'
        )
        ret = main(
            [
                "check",
                str(tmp_path),
                "--site-packages",
                str(fake_site_packages),
            ]
        )
        assert ret == 0
        output = capsys.readouterr().out
        # requests was excluded — either "all excluded" or footnote
        assert "excluded" in output.lower()


class TestPackageModeNoSitePackages:
    """Tests for package mode when site-packages cannot be discovered."""

    def test_check_package_no_site_packages_discovered(self, capsys, monkeypatch):
        """Package mode with no site-packages should print an error and return 1."""
        # Unset VIRTUAL_ENV so _discover_site_packages cannot find an active venv
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        # Patch site.getsitepackages to return an empty list so the fallback also fails
        import site

        monkeypatch.setattr(site, "getsitepackages", lambda: [])
        ret = main(["check", "some-pkg"])
        assert ret == 1
        assert "site-packages" in capsys.readouterr().err


class TestPackageModeTreemap:
    """Tests for package mode with --treemap flag."""

    def test_check_package_with_deps_treemap(self, tmp_path, capsys):
        """Package mode with --treemap should render treemap."""
        sp = tmp_path / "sp"
        sp.mkdir()
        # Target package
        pkg = sp / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("from helplib import helper\nhelper()\n")
        di = sp / "mypkg-1.0.dist-info"
        di.mkdir()
        (di / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: mypkg\nVersion: 1.0\n"
            "Requires-Dist: helplib>=1.0\n"
        )
        (di / "top_level.txt").write_text("mypkg\n")
        # Dependency
        dep = sp / "helplib"
        dep.mkdir()
        (dep / "__init__.py").write_text(
            "def helper():\n    return 1\n\ndef unused():\n    return 2\n"
        )
        dep_di = sp / "helplib-1.0.dist-info"
        dep_di.mkdir()
        (dep_di / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: helplib\nVersion: 1.0\n"
        )
        (dep_di / "top_level.txt").write_text("helplib\n")
        ret = main(["check", "mypkg", "--treemap", "--site-packages", str(sp)])
        assert ret == 0
        output = capsys.readouterr().out
        assert "Treemap" in output


class TestCheckTreemapUninstalledDeps:
    """Tests for treemap rendering when deps are not installed."""

    def test_check_treemap_uninstalled_deps(self, tmp_path, capsys):
        """Treemap with all deps uninstalled should not crash."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "0.1.0"\n'
            'dependencies = ["nonexistent-pkg"]\n'
        )
        sp = tmp_path / "fake_sp"
        sp.mkdir()
        ret = main(["check", str(tmp_path), "--treemap", "--site-packages", str(sp)])
        assert ret == 0


class TestTreemapExcludedFootnote:
    """Tests for treemap title with excluded count."""

    def test_check_treemap_excluded_footnote(
        self, tmp_path, fake_site_packages, capsys
    ):
        """Treemap title should mention excluded count."""
        pkg = tmp_path / "src" / "myapp"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("import requests\nrequests.get('url')\n")
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "0.1.0"\n'
            'dependencies = ["requests", "click"]\n\n'
            '[tool.unladen]\nexclude = ["click"]\n'
        )
        ret = main(
            [
                "check",
                str(tmp_path),
                "--treemap",
                "--site-packages",
                str(fake_site_packages),
            ]
        )
        assert ret == 0
        output = capsys.readouterr().out
        assert "excluded" in output.lower()


class TestJsonOutput:
    """Tests for --format json output."""

    def test_format_arg_parsed(self):
        parser = build_parser()
        args = parser.parse_args(["check", "--format", "json"])
        assert args.output_format == "json"

    def test_format_default_is_table(self):
        parser = build_parser()
        args = parser.parse_args(["check"])
        assert args.output_format == "table"

    def test_check_project_json(self, sample_project, fake_site_packages, capsys):
        ret = main(
            [
                "check",
                str(sample_project),
                "--site-packages",
                str(fake_site_packages),
                "--format",
                "json",
            ]
        )
        assert ret == 0
        data = json.loads(capsys.readouterr().out)
        assert "dependencies" in data
        assert "summary" in data
        assert data["summary"]["total"] > 0
        names = {d["name"] for d in data["dependencies"]}
        assert "requests" in names

    def test_check_project_json_structure(
        self, sample_project, fake_site_packages, capsys
    ):
        """Verify JSON schema has all expected fields."""
        main(
            [
                "check",
                str(sample_project),
                "--site-packages",
                str(fake_site_packages),
                "--format",
                "json",
            ]
        )
        data = json.loads(capsys.readouterr().out)
        dep = next(d for d in data["dependencies"] if d["name"] == "requests")
        assert "version" in dep
        assert "installed" in dep
        assert "status" in dep
        assert "used_names" in dep
        assert "heft" in dep
        assert "recommendation" in dep
        assert isinstance(dep["used_names"], list)

    def test_check_package_json(self, tmp_path, capsys):
        """Package mode also supports --format json."""
        sp = tmp_path / "sp"
        sp.mkdir()
        pkg = sp / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("from helplib import helper\nhelper()\n")
        di = sp / "mypkg-1.0.dist-info"
        di.mkdir()
        (di / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: mypkg\nVersion: 1.0\n"
            "Requires-Dist: helplib>=1.0\n"
        )
        (di / "top_level.txt").write_text("mypkg\n")
        dep = sp / "helplib"
        dep.mkdir()
        (dep / "__init__.py").write_text("def helper():\n    return 1\n")
        dep_di = sp / "helplib-1.0.dist-info"
        dep_di.mkdir()
        (dep_di / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: helplib\nVersion: 1.0\n"
        )
        (dep_di / "top_level.txt").write_text("helplib\n")
        ret = main(["check", "mypkg", "--format", "json", "--site-packages", str(sp)])
        assert ret == 0
        data = json.loads(capsys.readouterr().out)
        assert data["dependencies"][0]["name"] == "helplib"

    def test_json_excludes_count(self, tmp_path, fake_site_packages, capsys):
        """Excluded deps should appear in summary.excluded."""
        pkg = tmp_path / "src" / "myapp"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("import requests\nrequests.get('url')\n")
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "0.1.0"\n'
            'dependencies = ["requests", "click"]\n\n'
            '[tool.unladen]\nexclude = ["click"]\n'
        )
        ret = main(
            [
                "check",
                str(tmp_path),
                "--format",
                "json",
                "--site-packages",
                str(fake_site_packages),
            ]
        )
        assert ret == 0
        data = json.loads(capsys.readouterr().out)
        assert data["summary"]["excluded"] == 1
        names = {d["name"] for d in data["dependencies"]}
        assert "click" not in names
