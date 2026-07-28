"""Tests for transitive dependency measurement."""

import json
from pathlib import Path

import pytest

from unladen.transitive import (
    ModuleOrigin,
    TransitiveDep,
    classify_module,
    trace_transitive,
)


def _make_dist(
    site_packages: Path,
    name: str,
    version: str,
    files: dict[str, str],
    requires: list[str] | None = None,
) -> None:
    """Create a fake installed distribution in *site_packages*."""
    dist_info = site_packages / f"{name}-{version}.dist-info"
    dist_info.mkdir(parents=True)
    metadata = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    for req in requires or []:
        metadata.append(f"Requires-Dist: {req}")
    (dist_info / "METADATA").write_text("\n".join(metadata) + "\n")
    (dist_info / "top_level.txt").write_text(name + "\n")
    for rel_path, content in files.items():
        target = site_packages / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)


@pytest.fixture
def chain_site_packages(tmp_path: Path) -> Path:
    """spam -> eggs -> bacon dependency chain.

    ``spam.breakfast`` (in spam/core.py) uses ``eggs.scramble``,
    which uses ``bacon.sizzle``.  ``spam/unused.py`` uses
    ``eggs.poach`` but is never activated by the test project.
    """
    sp = tmp_path / "site-packages"
    _make_dist(
        sp,
        "spam",
        "1.0",
        requires=["eggs"],
        files={
            "spam/__init__.py": "from spam.core import breakfast\n",
            "spam/core.py": (
                "from eggs import scramble\n\ndef breakfast():\n    return scramble()\n"
            ),
            "spam/unused.py": (
                "from eggs import poach\n\ndef lunch():\n    return poach()\n"
            ),
        },
    )
    _make_dist(
        sp,
        "eggs",
        "2.0",
        requires=["bacon"],
        files={
            "eggs/__init__.py": (
                "from bacon import sizzle\n"
                "\n"
                "def scramble():\n"
                "    return sizzle()\n"
                "\n"
                "def poach():\n"
                "    return 'poached'\n"
            ),
        },
    )
    _make_dist(
        sp,
        "bacon",
        "3.0",
        files={
            "bacon/__init__.py": (
                "def sizzle():\n"
                "    return 'sizzle'\n"
                "\n"
                "def smoke():\n"
                "    return 'smoke'\n"
            ),
        },
    )
    return sp


def _direct_dep_map(sp: Path, names: list[str]) -> dict:
    from unladen.collector import resolve_installed

    return resolve_installed(names, sp)


class TestClassifyModule:
    """ty-style ordered module classification."""

    def test_first_party(self):
        origin = classify_module("spam", {"spam"}, {"eggs"})
        assert origin is ModuleOrigin.FIRST_PARTY

    def test_third_party(self):
        origin = classify_module("eggs", {"spam"}, {"eggs"})
        assert origin is ModuleOrigin.THIRD_PARTY

    def test_stdlib(self):
        origin = classify_module("os", {"spam"}, {"eggs"})
        assert origin is ModuleOrigin.STDLIB

    def test_unknown(self):
        origin = classify_module("mystery", {"spam"}, {"eggs"})
        assert origin is ModuleOrigin.UNKNOWN

    def test_first_party_wins_over_stdlib(self):
        # A dep whose own import name shadows a stdlib name (e.g. legacy-cgi
        # providing "cgi") classifies as first-party, mirroring ty's
        # search-path priority (project source before stdlib).
        origin = classify_module("cgi", {"cgi"}, set())
        assert origin is ModuleOrigin.FIRST_PARTY

    def test_declared_wins_over_stdlib(self):
        # A declared dependency providing a stdlib-named module
        # (backport packages) counts as third-party.
        origin = classify_module("cgi", {"spam"}, {"cgi"})
        assert origin is ModuleOrigin.THIRD_PARTY


class TestTraceTransitive:
    def test_chain_is_discovered(self, chain_site_packages):
        dep_map = _direct_dep_map(chain_site_packages, ["spam"])
        result = trace_transitive(dep_map, {"spam": {"breakfast"}}, chain_site_packages)
        names = {td.name for td in result}
        assert names == {"eggs", "bacon"}

    def test_usage_driven_propagation(self, chain_site_packages):
        """Only imports in *active* modules propagate.

        spam/unused.py imports eggs.poach but is never activated,
        so poach must not appear in eggs' used names.
        """
        dep_map = _direct_dep_map(chain_site_packages, ["spam"])
        result = trace_transitive(dep_map, {"spam": {"breakfast"}}, chain_site_packages)
        eggs = next(td for td in result if td.name == "eggs")
        assert "scramble" in eggs.used_names
        assert "poach" not in eggs.used_names

    def test_via_and_depth(self, chain_site_packages):
        dep_map = _direct_dep_map(chain_site_packages, ["spam"])
        result = trace_transitive(dep_map, {"spam": {"breakfast"}}, chain_site_packages)
        eggs = next(td for td in result if td.name == "eggs")
        bacon = next(td for td in result if td.name == "bacon")
        assert eggs.via == {"spam"}
        assert eggs.depth == 1
        assert bacon.via == {"eggs"}
        assert bacon.depth == 2

    def test_heft_computed(self, chain_site_packages):
        dep_map = _direct_dep_map(chain_site_packages, ["spam"])
        result = trace_transitive(dep_map, {"spam": {"breakfast"}}, chain_site_packages)
        eggs = next(td for td in result if td.name == "eggs")
        assert eggs.heft is not None
        # eggs/__init__.py: import(1) + scramble(2) + poach(2) = 5 LLOC,
        # only scramble(2) is active.
        assert eggs.heft.total_lloc == 5
        assert eggs.heft.active_lloc == 2

    def test_diamond_unions_usage(self, chain_site_packages, tmp_path):
        """Two direct deps feeding the same transitive dep union their names."""
        _make_dist(
            chain_site_packages,
            "ham",
            "1.0",
            requires=["eggs"],
            files={
                "ham/__init__.py": (
                    "from eggs import poach\n\ndef fry():\n    return poach()\n"
                ),
            },
        )
        dep_map = _direct_dep_map(chain_site_packages, ["spam", "ham"])
        result = trace_transitive(
            dep_map,
            {"spam": {"breakfast"}, "ham": {"fry"}},
            chain_site_packages,
        )
        eggs = next(td for td in result if td.name == "eggs")
        assert {"scramble", "poach"} <= eggs.used_names
        assert eggs.via == {"spam", "ham"}

    def test_cross_level_union(self, chain_site_packages):
        """A dep found at depth 1 still accumulates names from deeper parents.

        toast (direct) uses bacon.smoke at depth 1; spam -> eggs
        contributes bacon.sizzle at depth 2.  Both must land in
        bacon's used names, and its depth stays at first discovery.
        """
        _make_dist(
            chain_site_packages,
            "toast",
            "1.0",
            requires=["bacon"],
            files={
                "toast/__init__.py": (
                    "from bacon import smoke\n\ndef crunch():\n    return smoke()\n"
                ),
            },
        )
        dep_map = _direct_dep_map(chain_site_packages, ["spam", "toast"])
        result = trace_transitive(
            dep_map,
            {"spam": {"breakfast"}, "toast": {"crunch"}},
            chain_site_packages,
        )
        bacon = next(td for td in result if td.name == "bacon")
        assert bacon.depth == 1
        assert bacon.via == {"toast", "eggs"}
        assert {"smoke", "sizzle"} <= bacon.used_names

    def test_parent_without_dist_info_is_skipped(self, chain_site_packages):
        """A dep_map entry with no dist-info contributes nothing."""
        dep_map = _direct_dep_map(chain_site_packages, ["spam"])
        dep_map["phantom"] = {
            "version": "0",
            "import_names": ["phantom"],
            "paths": [chain_site_packages / "spam"],
            "installed": True,
        }
        result = trace_transitive(
            dep_map, {"phantom": {"anything"}}, chain_site_packages
        )
        assert result == []

    def test_no_active_modules_yields_nothing(self, chain_site_packages):
        """Used names matching no definitions activate no files."""
        dep_map = _direct_dep_map(chain_site_packages, ["spam"])
        result = trace_transitive(
            dep_map, {"spam": {"does_not_exist"}}, chain_site_packages
        )
        assert result == []

    def test_direct_deps_not_reported_as_transitive(self, chain_site_packages):
        """A dep that is both direct and transitive stays in the main report."""
        dep_map = _direct_dep_map(chain_site_packages, ["spam", "eggs"])
        result = trace_transitive(
            dep_map,
            {"spam": {"breakfast"}, "eggs": {"poach"}},
            chain_site_packages,
        )
        names = {td.name for td in result}
        assert "eggs" not in names
        assert "bacon" in names

    def test_max_depth_limits_traversal(self, chain_site_packages):
        dep_map = _direct_dep_map(chain_site_packages, ["spam"])
        result = trace_transitive(
            dep_map,
            {"spam": {"breakfast"}},
            chain_site_packages,
            max_depth=1,
        )
        names = {td.name for td in result}
        assert names == {"eggs"}

    def test_no_children_yields_empty(self, chain_site_packages):
        dep_map = _direct_dep_map(chain_site_packages, ["bacon"])
        result = trace_transitive(dep_map, {"bacon": {"sizzle"}}, chain_site_packages)
        assert result == []

    def test_missing_child_is_skipped(self, tmp_path):
        """A declared but uninstalled transitive dep is skipped."""
        sp = tmp_path / "site-packages"
        _make_dist(
            sp,
            "spam",
            "1.0",
            requires=["ghost"],
            files={
                "spam/__init__.py": ("def breakfast():\n    return 1\n"),
            },
        )
        dep_map = _direct_dep_map(sp, ["spam"])
        result = trace_transitive(dep_map, {"spam": {"breakfast"}}, sp)
        assert result == []

    def test_to_dict(self, chain_site_packages):
        dep_map = _direct_dep_map(chain_site_packages, ["spam"])
        result = trace_transitive(dep_map, {"spam": {"breakfast"}}, chain_site_packages)
        eggs = next(td for td in result if td.name == "eggs")
        d = eggs.to_dict()
        assert d["name"] == "eggs"
        assert d["via"] == ["spam"]
        assert d["depth"] == 1
        assert d["used_names"] == sorted(eggs.used_names)
        assert d["heft"]["total_lloc"] == 5


class TestActiveFileSelection:
    def test_nested_subpackage_walks_ancestors(self, tmp_path):
        """An active file deep in a subpackage activates each ancestor
        __init__.py up to the package root; namespace dirs (no
        __init__.py) are traversed without error."""
        from unladen.transitive import _active_files

        pkg = tmp_path / "big"
        (pkg / "sub").mkdir(parents=True)
        (pkg / "nsdir").mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "sub" / "__init__.py").write_text("")
        (pkg / "sub" / "deep.py").write_text("def f():\n    return 1\n")
        (pkg / "nsdir" / "mod.py").write_text("def g():\n    return 2\n")

        files = _active_files([pkg], {"deep", "mod"})
        assert pkg / "sub" / "deep.py" in files
        assert pkg / "sub" / "__init__.py" in files
        assert pkg / "__init__.py" in files
        assert pkg / "nsdir" / "mod.py" in files

    def test_single_file_module_dep(self, tmp_path):
        """A single-file module dep has no package root to walk."""
        from unladen.transitive import _active_files

        mod = tmp_path / "flat.py"
        mod.write_text("def f():\n    return 1\n")
        files = _active_files([mod], {"flat"})
        assert files == [mod]

    def test_ancestor_init_files_included(self, chain_site_packages):
        """Activating spam/core.py must also treat spam/__init__.py as
        active — importing anything from a package executes its
        ancestor __init__ modules."""
        from unladen.transitive import _active_files

        spam_pkg = chain_site_packages / "spam"
        files = _active_files([spam_pkg], {"core"})
        assert spam_pkg / "core.py" in files
        assert spam_pkg / "__init__.py" in files
        assert spam_pkg / "unused.py" not in files


class TestCLIIntegration:
    @pytest.fixture
    def chain_project(self, tmp_path, chain_site_packages) -> Path:
        proj = tmp_path / "proj"
        (proj / "src" / "myapp").mkdir(parents=True)
        (proj / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "0"\ndependencies = ["spam"]\n'
        )
        (proj / "src" / "myapp" / "__init__.py").write_text(
            "from spam import breakfast\n\nbreakfast()\n"
        )
        return proj

    def test_check_transitive_table(self, chain_project, chain_site_packages, capsys):
        from unladen.cli import main

        rc = main(
            [
                "check",
                str(chain_project),
                "--site-packages",
                str(chain_site_packages),
                "--transitive",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "Transitive" in out
        assert "eggs" in out
        assert "bacon" in out

    def test_check_transitive_json(self, chain_project, chain_site_packages, capsys):
        from unladen.cli import main

        rc = main(
            [
                "check",
                str(chain_project),
                "--site-packages",
                str(chain_site_packages),
                "--transitive",
                "--format",
                "json",
            ]
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert "transitive" in payload
        names = {t["name"] for t in payload["transitive"]}
        assert names == {"eggs", "bacon"}

    def test_check_without_flag_has_no_transitive(
        self, chain_project, chain_site_packages, capsys
    ):
        from unladen.cli import main

        rc = main(
            [
                "check",
                str(chain_project),
                "--site-packages",
                str(chain_site_packages),
                "--format",
                "json",
            ]
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert "transitive" not in payload

    def test_transitive_none_found_message(self, tmp_path, chain_site_packages, capsys):
        from unladen.cli import main

        proj = tmp_path / "leafproj"
        (proj / "src" / "leafapp").mkdir(parents=True)
        (proj / "pyproject.toml").write_text(
            '[project]\nname = "leafapp"\nversion = "0"\ndependencies = ["bacon"]\n'
        )
        (proj / "src" / "leafapp" / "__init__.py").write_text(
            "from bacon import sizzle\n\nsizzle()\n"
        )
        rc = main(
            [
                "check",
                str(proj),
                "--site-packages",
                str(chain_site_packages),
                "--transitive",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "No transitive dependency usage" in out


def test_transitive_dep_dataclass_defaults():
    td = TransitiveDep(
        name="x",
        version="1.0",
        import_names=["x"],
        used_names={"f"},
        via={"parent"},
        depth=1,
    )
    assert td.heft is None
    assert td.to_dict()["heft"] is None
