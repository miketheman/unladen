"""Tests for the ``unladen show`` command."""

import textwrap

from unladen.cli import main


class TestShowBasic:
    """Basic show command integration tests."""

    def test_show_file_not_found_error(self, capsys):
        ret = main(["show", "requests", "/nonexistent/path"])
        assert ret == 1
        assert "Error" in capsys.readouterr().err

    def test_show_dep_not_declared(self, sample_project, fake_site_packages, capsys):
        ret = main(
            [
                "show",
                "nonexistent",
                str(sample_project),
                "--site-packages",
                str(fake_site_packages),
            ]
        )
        assert ret == 1
        assert "not a declared dependency" in capsys.readouterr().err

    def test_show_dep_with_output(self, sample_project, fake_site_packages, capsys):
        ret = main(
            [
                "show",
                "requests",
                str(sample_project),
                "--site-packages",
                str(fake_site_packages),
            ]
        )
        assert ret == 0
        output = capsys.readouterr().out
        assert "requests" in output
        assert "Declared in" in output

    def test_show_dep_not_installed(self, tmp_path, capsys):
        """Show a dep that is declared but not installed."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "myapp"\nversion = "0.1.0"\n'
            'dependencies = ["nonexistent-pkg"]\n'
        )
        sp = tmp_path / "fake_sp"
        sp.mkdir()
        ret = main(
            ["show", "nonexistent-pkg", str(tmp_path), "--site-packages", str(sp)]
        )
        assert ret == 0
        output = capsys.readouterr().out
        assert "not installed" in output.lower()

    def test_show_dep_not_used(self, tmp_path, fake_site_packages, capsys):
        """Show a dep that is installed but not imported by the project."""
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
                "show",
                "requests",
                str(tmp_path),
                "--site-packages",
                str(fake_site_packages),
            ]
        )
        assert ret == 0
        output = capsys.readouterr().out
        assert "Not imported" in output

    def test_show_dep_with_heft(self, sample_project, fake_site_packages, capsys):
        """Show should display heft info for an imported dep."""
        ret = main(
            [
                "show",
                "requests",
                str(sample_project),
                "--site-packages",
                str(fake_site_packages),
            ]
        )
        assert ret == 0
        output = capsys.readouterr().out
        assert "Heft" in output
        assert "LLOC" in output


class TestShowStringRefs:
    """Tests for show command string reference handling."""

    def test_show_string_ref_only_dep(self, tmp_path, capsys):
        """Show a dep activated only via string references."""
        pkg = tmp_path / "src" / "myapp"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "settings.py").write_text(
            textwrap.dedent("""\
            MIDDLEWARE = [
                "whitenoise.middleware.WhiteNoiseMiddleware",
            ]
        """)
        )
        sp = tmp_path / "site-packages"
        sp.mkdir()
        wn_pkg = sp / "whitenoise"
        wn_pkg.mkdir()
        (wn_pkg / "__init__.py").write_text("x = 1\n")
        (wn_pkg / "middleware.py").write_text(
            textwrap.dedent("""\
            class WhiteNoiseMiddleware:
                def __init__(self):
                    self.active = True
        """)
        )
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
        ret = main(["show", "whitenoise", str(tmp_path), "--site-packages", str(sp)])
        assert ret == 0
        output = capsys.readouterr().out
        assert "String References" in output
        assert "string references only" in output.lower()

    def test_show_dep_with_imports_and_string_refs(self, tmp_path, capsys):
        """Show dep activated via both imports and string references."""
        pkg = tmp_path / "src" / "myapp"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "views.py").write_text("from whitenoise import middleware\n")
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
        ret = main(["show", "whitenoise", str(tmp_path), "--site-packages", str(sp)])
        assert ret == 0
        output = capsys.readouterr().out
        assert "Import Statements" in output
        assert "String References" in output
        assert "string references only" not in output.lower()


class TestShowDynamicDispatch:
    """Tests for dynamic dispatch detection in show output."""

    def test_show_dynamic_dispatch_warning(self, tmp_path, capsys):
        """Show should display dynamic dispatch warning when patterns are found."""
        pkg = tmp_path / "src" / "myapp"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text(
            textwrap.dedent("""\
            from pygments.lexers import get_lexer_by_name
            lexer = get_lexer_by_name("python")
        """)
        )
        sp = tmp_path / "site-packages"
        sp.mkdir()
        pyg_pkg = sp / "pygments"
        pyg_pkg.mkdir()
        (pyg_pkg / "__init__.py").write_text("x = 1\n")
        lex_pkg = pyg_pkg / "lexers"
        lex_pkg.mkdir()
        (lex_pkg / "__init__.py").write_text(
            "def get_lexer_by_name(name):\n    return name\n"
        )
        dist_info = sp / "pygments-2.18.0.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: Pygments\nVersion: 2.18.0\n"
        )
        (dist_info / "top_level.txt").write_text("pygments\n")
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "0.1.0"\n'
            'dependencies = ["pygments"]\n'
        )
        ret = main(["show", "pygments", str(tmp_path), "--site-packages", str(sp)])
        assert ret == 0
        output = capsys.readouterr().out
        assert "Dynamic dispatch" in output
        assert "get_lexer_by_name" in output


class TestShowNativeExtension:
    """Tests for binary extension handling in show output."""

    def test_show_native_extension_note(self, tmp_path, capsys):
        """Show should note binary extensions when present."""
        pkg = tmp_path / "src" / "myapp"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("from nh3 import clean\n")
        sp = tmp_path / "site-packages"
        sp.mkdir()
        nh3_pkg = sp / "nh3"
        nh3_pkg.mkdir()
        (nh3_pkg / "__init__.py").write_text("def clean(x): return x\n")
        (nh3_pkg / "_nh3.so").write_bytes(b"\x00")
        dist_info = sp / "nh3-0.3.2.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: nh3\nVersion: 0.3.2\n"
        )
        (dist_info / "top_level.txt").write_text("nh3\n")
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "0.1.0"\ndependencies = ["nh3"]\n'
        )
        ret = main(["show", "nh3", str(tmp_path), "--site-packages", str(sp)])
        assert ret == 0
        output = capsys.readouterr().out
        assert "extension" in output.lower()


class TestShowAliasedImports:
    """Tests for aliased import display in show output."""

    def test_show_aliased_import(self, tmp_path, capsys):
        """Show should display aliased imports correctly."""
        pkg = tmp_path / "src" / "myapp"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text(
            "from requests.auth import HTTPBasicAuth as Auth\n"
        )
        sp = tmp_path / "site-packages"
        sp.mkdir()
        req_pkg = sp / "requests"
        req_pkg.mkdir()
        (req_pkg / "__init__.py").write_text("x = 1\n")
        auth_pkg = req_pkg / "auth"
        auth_pkg.mkdir()
        (auth_pkg / "__init__.py").write_text("class HTTPBasicAuth:\n    pass\n")
        dist_info = sp / "requests-2.31.0.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: requests\nVersion: 2.31.0\n"
        )
        (dist_info / "top_level.txt").write_text("requests\n")
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "0.1.0"\n'
            'dependencies = ["requests"]\n'
        )
        ret = main(["show", "requests", str(tmp_path), "--site-packages", str(sp)])
        assert ret == 0
        output = capsys.readouterr().out
        assert "as Auth" in output

    def test_show_bare_import_with_alias(self, tmp_path, capsys):
        """'import pandas as pd' should show 'as pd' in output."""
        pkg = tmp_path / "src" / "myapp"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("import pandas as pd\ndf = pd.DataFrame()\n")
        sp = tmp_path / "site-packages"
        sp.mkdir()
        pd_pkg = sp / "pandas"
        pd_pkg.mkdir()
        (pd_pkg / "__init__.py").write_text("class DataFrame:\n    pass\n")
        dist_info = sp / "pandas-2.0.0.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: pandas\nVersion: 2.0.0\n"
        )
        (dist_info / "top_level.txt").write_text("pandas\n")
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "0.1.0"\ndependencies = ["pandas"]\n'
        )
        ret = main(["show", "pandas", str(tmp_path), "--site-packages", str(sp)])
        assert ret == 0
        output = capsys.readouterr().out
        assert "as pd" in output


class TestShowHeaderEdgeCases:
    """Tests for _show_header edge cases."""

    def test_show_header_dep_source_unknown_on_missing_file(
        self, tmp_path, fake_site_packages, capsys, monkeypatch
    ):
        """When dependency_source raises FileNotFoundError, falls back to 'unknown'."""
        from unladen import _cmd_show

        monkeypatch.setattr(
            _cmd_show,
            "dependency_source",
            lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError("no file")),
        )
        pkg = tmp_path / "src" / "myapp"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "0.1.0"\n'
            'dependencies = ["requests"]\n'
        )
        ret = main(
            [
                "show",
                "requests",
                str(tmp_path),
                "--site-packages",
                str(fake_site_packages),
            ]
        )
        assert ret == 0
        output = capsys.readouterr().out
        assert "unknown" in output


class TestShowExcluded:
    """Tests for show command with excluded deps."""

    def test_show_excluded_dep_note(self, tmp_path, fake_site_packages, capsys):
        """Show for an excluded dep should display a note."""
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
                "show",
                "requests",
                str(tmp_path),
                "--site-packages",
                str(fake_site_packages),
            ]
        )
        assert ret == 0
        output = capsys.readouterr().out
        assert "Excluded" in output


class TestShowNameNormalization:
    """The show argument accepts any PEP 503-equivalent spelling."""

    def test_show_accepts_unnormalized_name(
        self, sample_project, fake_site_packages, capsys
    ):
        ret = main(
            [
                "show",
                "Requests",
                str(sample_project),
                "--site-packages",
                str(fake_site_packages),
            ]
        )
        assert ret == 0
        assert "Declared in" in capsys.readouterr().out
