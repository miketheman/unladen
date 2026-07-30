"""Tests for transitive dependency measurement."""

import json
from pathlib import Path

import pytest

from unladen.transitive import TransitiveDep, trace_transitive


def _make_dist(
    site_packages: Path,
    name: str,
    version: str,
    files: dict[str, str],
    requires: list[str] | None = None,
    top_level: str | None = None,
) -> None:
    """Create a fake installed distribution in *site_packages*.

    *top_level* overrides the import name written to top_level.txt
    (for namespace packages whose import name differs from the
    distribution name).  A RECORD file listing the distribution's
    files is always written so namespace ownership can be resolved.
    """
    dist_info = site_packages / f"{name}-{version}.dist-info"
    dist_info.mkdir(parents=True)
    metadata = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    for req in requires or []:
        metadata.append(f"Requires-Dist: {req}")
    (dist_info / "METADATA").write_text("\n".join(metadata) + "\n")
    (dist_info / "top_level.txt").write_text((top_level or name) + "\n")
    (dist_info / "RECORD").write_text(
        "\n".join(f"{rel_path},," for rel_path in files) + "\n"
    )
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

    def test_namespace_shared_top_level_traced(self, tmp_path):
        """Namespace children sharing the parent's top-level import name
        must still be traced (zope.component -> zope.interface style);
        merge_dep_usage narrows attribution to owned subpackages."""
        sp = tmp_path / "sp"
        _make_dist(
            sp,
            "ns-component",
            "1.0",
            requires=["ns-interface"],
            top_level="ns",
            files={
                "ns/component/__init__.py": (
                    "from ns.interface import Iface\n"
                    "\n"
                    "def register():\n"
                    "    return Iface()\n"
                ),
            },
        )
        _make_dist(
            sp,
            "ns-interface",
            "2.0",
            top_level="ns",
            files={
                "ns/interface/__init__.py": (
                    "class Iface:\n    def provided_by(self):\n        return True\n"
                ),
            },
        )
        dep_map = _direct_dep_map(sp, ["ns-component"])
        result = trace_transitive(dep_map, {"ns-component": {"register"}}, sp)
        interface = next((td for td in result if td.name == "ns-interface"), None)
        assert interface is not None
        assert "Iface" in interface.used_names

    def test_excluded_dep_not_reported_or_traversed(self, chain_site_packages):
        """A [tool.unladen]-excluded dep must not reappear transitively,
        and usage must not propagate through it."""
        dep_map = _direct_dep_map(chain_site_packages, ["spam"])
        result = trace_transitive(
            dep_map,
            {"spam": {"breakfast"}},
            chain_site_packages,
            exclude={"eggs"},
        )
        assert result == []

    def test_excluded_leaf_only(self, chain_site_packages):
        dep_map = _direct_dep_map(chain_site_packages, ["spam"])
        result = trace_transitive(
            dep_map,
            {"spam": {"breakfast"}},
            chain_site_packages,
            exclude={"bacon"},
        )
        names = {td.name for td in result}
        assert names == {"eggs"}

    def test_cycle_back_to_excluded_target(self, tmp_path):
        """Package mode passes the target as an exclusion so a dependency
        cycle can't report the analyzed package as its own transitive dep."""
        sp = tmp_path / "sp"
        _make_dist(
            sp,
            "applehelp",
            "1.0",
            requires=["bigdep"],
            files={
                "applehelp/__init__.py": (
                    "from bigdep import build\n\ndef setup():\n    return build()\n"
                ),
            },
        )
        _make_dist(
            sp,
            "bigdep",
            "2.0",
            requires=["applehelp"],
            files={
                "bigdep/__init__.py": (
                    "from applehelp import setup\n\ndef build():\n    return setup()\n"
                ),
            },
        )
        from unladen.collector import collect_package_deps

        dep_map = collect_package_deps("applehelp", sp)
        result = trace_transitive(
            dep_map, {"bigdep": {"build"}}, sp, exclude={"applehelp"}
        )
        assert {td.name for td in result} == set()

    def test_unused_direct_dep_still_propagates(self, chain_site_packages):
        """A declared-but-unused direct dep is not reported transitively,
        but usage flowing through it must still reach its children."""
        dep_map = _direct_dep_map(chain_site_packages, ["spam", "eggs"])
        result = trace_transitive(dep_map, {"spam": {"breakfast"}}, chain_site_packages)
        names = {td.name for td in result}
        assert "eggs" not in names  # direct: stays in the main report
        assert "bacon" in names  # reached through the unused direct dep
        bacon = next(td for td in result if td.name == "bacon")
        assert bacon.via == {"eggs"}
        assert bacon.depth == 2

    def test_late_contribution_recomputes_heft(self, chain_site_packages):
        """When a later-processed parent adds names to an already-traced
        dep, heft must reflect the union (stale trace cache is bypassed)."""
        _make_dist(
            chain_site_packages,
            "crumpet",
            "1.0",
            requires=["muffin"],
            files={
                "crumpet/__init__.py": (
                    "from muffin import warm\n\ndef tea():\n    return warm()\n"
                ),
            },
        )
        _make_dist(
            chain_site_packages,
            "muffin",
            "1.0",
            requires=["eggs"],
            files={
                "muffin/__init__.py": (
                    "from eggs import poach\n\ndef warm():\n    return poach()\n"
                ),
            },
        )
        dep_map = _direct_dep_map(chain_site_packages, ["spam", "crumpet"])
        result = trace_transitive(
            dep_map,
            {"spam": {"breakfast"}, "crumpet": {"tea"}},
            chain_site_packages,
        )
        eggs = next(td for td in result if td.name == "eggs")
        # eggs was processed at depth 1 with {scramble}; muffin (depth 2)
        # added {poach} afterwards.  Heft must count both: scramble(2) +
        # poach(2) of total 5.
        assert {"scramble", "poach"} <= eggs.used_names
        assert eggs.heft is not None
        assert eggs.heft.active_lloc == 4

    def test_used_direct_dep_accumulates_and_repropagates(self, tmp_path):
        """Fixpoint: names contributed into a USED direct dep re-propagate.

        The project uses eggs.poach directly; spam's active code uses
        eggs.scramble, whose file imports bacon.  eggs must be
        re-processed with the union so bacon is discovered.
        """
        sp = tmp_path / "sp"
        _make_dist(
            sp,
            "spam",
            "1.0",
            requires=["eggs"],
            files={
                "spam/__init__.py": (
                    "from eggs import scramble\n"
                    "\n"
                    "def breakfast():\n"
                    "    return scramble()\n"
                ),
            },
        )
        _make_dist(
            sp,
            "eggs",
            "2.0",
            requires=["bacon"],
            files={
                "eggs/__init__.py": "",
                "eggs/poach_mod.py": "def poach():\n    return 1\n",
                "eggs/scramble_mod.py": (
                    "from bacon import sizzle\n\ndef scramble():\n    return sizzle()\n"
                ),
            },
        )
        _make_dist(
            sp,
            "bacon",
            "3.0",
            files={"bacon/__init__.py": "def sizzle():\n    return 1\n"},
        )
        dep_map = _direct_dep_map(sp, ["spam", "eggs"])
        result = trace_transitive(
            dep_map, {"spam": {"breakfast"}, "eggs": {"poach"}}, sp
        )
        names = {td.name for td in result}
        assert "bacon" in names
        assert "eggs" not in names  # direct: stays in the main report

    def test_late_contribution_unlocks_grandchild(self, chain_site_packages):
        """Fixpoint: a name contributed by a later parent re-propagates.

        eggs is first traced with {scramble}; muffin later adds {poach},
        and poach's file imports salt — salt must still be discovered.
        """
        _make_dist(
            chain_site_packages,
            "crumpet",
            "1.0",
            requires=["muffin"],
            files={
                "crumpet/__init__.py": (
                    "from muffin import warm\n\ndef tea():\n    return warm()\n"
                ),
            },
        )
        _make_dist(
            chain_site_packages,
            "muffin",
            "1.0",
            requires=["eggs2"],
            files={
                "muffin/__init__.py": (
                    "from eggs2 import poach\n\ndef warm():\n    return poach()\n"
                ),
            },
        )
        _make_dist(
            chain_site_packages,
            "eggs2",
            "1.0",
            requires=["salt"],
            files={
                "eggs2/__init__.py": "",
                "eggs2/scramble_mod.py": "def scramble():\n    return 1\n",
                "eggs2/poach_mod.py": (
                    "from salt import pinch\n\ndef poach():\n    return pinch()\n"
                ),
            },
        )
        _make_dist(
            chain_site_packages,
            "salt",
            "1.0",
            files={"salt/__init__.py": "def pinch():\n    return 1\n"},
        )
        _make_dist(
            chain_site_packages,
            "toastr",
            "1.0",
            requires=["eggs2"],
            files={
                "toastr/__init__.py": (
                    "from eggs2 import scramble\n"
                    "\n"
                    "def crunch():\n"
                    "    return scramble()\n"
                ),
            },
        )
        dep_map = _direct_dep_map(chain_site_packages, ["toastr", "crumpet"])
        result = trace_transitive(
            dep_map,
            {"toastr": {"crunch"}, "crumpet": {"tea"}},
            chain_site_packages,
        )
        salt = next((td for td in result if td.name == "salt"), None)
        assert salt is not None
        assert "pinch" in salt.used_names

    def test_namespace_self_import_does_not_activate_sibling(self, tmp_path):
        """A parent importing its OWN namespace subpackages must not
        mark a declared namespace sibling as used."""
        sp = tmp_path / "sp"
        _make_dist(
            sp,
            "ns-component",
            "1.0",
            requires=["ns-interface"],
            top_level="ns",
            files={
                "ns/component/__init__.py": (
                    "from ns.component.helpers import x\n"
                    "\n"
                    "def register():\n"
                    "    return x\n"
                ),
                "ns/component/helpers.py": "x = 1\n",
            },
        )
        _make_dist(
            sp,
            "ns-interface",
            "2.0",
            top_level="ns",
            files={
                "ns/interface/__init__.py": (
                    "class Iface:\n    def provided_by(self):\n        return True\n"
                ),
            },
        )
        dep_map = _direct_dep_map(sp, ["ns-component"])
        result = trace_transitive(dep_map, {"ns-component": {"register"}}, sp)
        assert result == []

    def test_pathless_child_has_no_heft(self, tmp_path):
        """An installed child whose sources can't be resolved is reported
        without heft (rendered '-'), even when it declares children —
        never as a misleading 0/0 'Barely used' row."""
        sp = tmp_path / "sp"
        _make_dist(
            sp,
            "par",
            "1.0",
            requires=["weird"],
            files={
                "par/__init__.py": (
                    "from weird import thing\n\ndef go():\n    return thing\n"
                ),
            },
        )
        _make_dist(
            sp, "weird", "1.0", requires=["leafy"], files={"weird_data.txt": "x\n"}
        )
        _make_dist(
            sp,
            "leafy",
            "1.0",
            files={"leafy/__init__.py": "def leaf():\n    return 1\n"},
        )
        dep_map = _direct_dep_map(sp, ["par"])
        result = trace_transitive(dep_map, {"par": {"go"}}, sp)
        weird = next(td for td in result if td.name == "weird")
        assert weird.heft is None

    def test_type_checking_import_not_propagated(self, tmp_path):
        """A typing-only import in a parent's active file must not count
        as runtime activation of the child."""
        sp = tmp_path / "sp"
        _make_dist(
            sp,
            "typedpar",
            "1.0",
            requires=["numpyish"],
            files={
                "typedpar/__init__.py": (
                    "from typing import TYPE_CHECKING\n"
                    "\n"
                    "if TYPE_CHECKING:\n"
                    "    from numpyish import ndarray\n"
                    "\n"
                    "def go():\n"
                    "    return 1\n"
                ),
            },
        )
        _make_dist(
            sp,
            "numpyish",
            "1.0",
            files={
                "numpyish/__init__.py": (
                    "class ndarray:\n    def sum(self):\n        return 0\n"
                )
            },
        )
        dep_map = _direct_dep_map(sp, ["typedpar"])
        result = trace_transitive(dep_map, {"typedpar": {"go"}}, sp)
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
        used_names={"f"},
        via={"parent"},
        depth=1,
    )
    assert td.heft is None
    assert td.to_dict()["heft"] is None
