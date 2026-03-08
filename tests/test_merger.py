"""Tests for the merger module — Phase 2.5 usage aggregation."""

from pathlib import Path

import pytest

from unladen.inspector import ImportInfo, StringRef
from unladen.merger import (
    _matches_owned,
    _owned_module_prefixes,
    detect_dynamic_dispatch,
    merge_dep_usage,
)


class TestMergeDepUsage:
    def test_empty_usage(self):
        summary = merge_dep_usage(["requests"], {})
        assert not summary.is_used
        assert summary.used_names == set()
        assert summary.imports == []
        assert summary.file_count == 0

    def test_merges_imports(self):
        usage = {
            "requests": {
                "imports": [
                    ImportInfo(
                        module="requests",
                        name="get",
                        source_file=Path("a.py"),
                        lineno=1,
                    )
                ],
                "used_names": {"get"},
                "files": {Path("a.py")},
                "string_refs": [],
            }
        }
        summary = merge_dep_usage(["requests"], usage)
        assert summary.is_used
        assert summary.has_imports
        assert "get" in summary.used_names
        assert summary.import_count == 1
        assert summary.file_count == 1
        assert summary.matched_import_names == ["requests"]

    def test_merges_multiple_import_names(self):
        usage = {
            "cattr": {
                "imports": [
                    ImportInfo(module="cattr", source_file=Path("a.py"), lineno=1)
                ],
                "used_names": {"Converter"},
                "files": {Path("a.py")},
                "string_refs": [],
            },
            "cattrs": {
                "imports": [],
                "used_names": set(),
                "files": set(),
                "string_refs": [],
            },
        }
        summary = merge_dep_usage(["cattr", "cattrs"], usage)
        assert summary.matched_import_names == ["cattr"]
        assert "Converter" in summary.used_names

    def test_string_refs_extract_leaf_names(self):
        ref = StringRef(
            value="whitenoise.middleware.WhiteNoiseMiddleware",
            source_file=Path("settings.py"),
            lineno=10,
        )
        usage = {
            "whitenoise": {
                "imports": [],
                "used_names": set(),
                "files": set(),
                "string_refs": [ref],
            }
        }
        summary = merge_dep_usage(["whitenoise"], usage)
        assert summary.is_used
        assert not summary.has_imports
        assert "WhiteNoiseMiddleware" in summary.used_names
        assert Path("settings.py") in summary.files

    def test_bare_string_ref_no_leaf(self):
        """A bare string ref (no dot) should not extract a leaf name."""
        ref = StringRef(
            value="template_partials", source_file=Path("settings.py"), lineno=5
        )
        usage = {
            "template_partials": {
                "imports": [],
                "used_names": set(),
                "files": set(),
                "string_refs": [ref],
            }
        }
        summary = merge_dep_usage(["template_partials"], usage)
        assert summary.is_used
        assert summary.used_names == set()


class TestOwnedModulePrefixes:
    """Tests for _owned_module_prefixes namespace disambiguation."""

    def test_regular_package_returns_none(self, tmp_path):
        """Non-namespace packages need no filtering."""
        pkg = tmp_path / "requests"
        pkg.mkdir()
        result = _owned_module_prefixes(["requests"], [pkg])
        assert result is None

    def test_namespace_subpackage(self, tmp_path):
        """Namespace subpackage like zope/sqlalchemy returns dotted prefix."""
        zope_dir = tmp_path / "zope"
        zope_dir.mkdir()
        sa_dir = zope_dir / "sqlalchemy"
        sa_dir.mkdir()
        result = _owned_module_prefixes(["zope"], [sa_dir])
        assert result == {"zope.sqlalchemy"}

    def test_mixed_regular_and_namespace(self, tmp_path):
        """Mix of regular and namespace paths — returns prefixes."""
        zope_dir = tmp_path / "zope"
        zope_dir.mkdir()
        sa_dir = zope_dir / "sqlalchemy"
        sa_dir.mkdir()
        result = _owned_module_prefixes(["zope"], [zope_dir, sa_dir])
        assert result == {"zope", "zope.sqlalchemy"}

    def test_empty_paths_returns_none(self):
        result = _owned_module_prefixes(["zope"], [])
        assert result is None

    def test_file_path_uses_stem(self, tmp_path):
        """Single .py file uses stem for matching."""
        f = tmp_path / "mymod.py"
        f.write_text("")
        result = _owned_module_prefixes(["mymod"], [f])
        assert result is None


class TestMatchesOwned:
    """Tests for _matches_owned prefix matching."""

    @pytest.mark.parametrize(
        ("module", "prefixes", "expected"),
        [
            ("zope.sqlalchemy", {"zope.sqlalchemy"}, True),
            ("zope.sqlalchemy.datamanager", {"zope.sqlalchemy"}, True),
            ("zope.interface", {"zope.sqlalchemy"}, False),
            ("zope.sql", {"zope.sqlalchemy"}, False),
        ],
        ids=["exact", "submodule", "sibling", "partial-name"],
    )
    def test_matching(self, module, prefixes, expected):
        assert _matches_owned(module, prefixes) is expected


class TestNamespaceFiltering:
    """Tests for namespace package filtering in merge_dep_usage."""

    def test_filters_imports_to_owned_subpackage(self, tmp_path):
        """Only imports matching owned subpackage should be included."""
        zope_dir = tmp_path / "zope"
        zope_dir.mkdir()
        sa_dir = zope_dir / "sqlalchemy"
        sa_dir.mkdir()

        usage = {
            "zope": {
                "imports": [
                    ImportInfo(
                        module="zope.sqlalchemy",
                        name="register",
                        source_file=Path("app.py"),
                        lineno=1,
                    ),
                    ImportInfo(
                        module="zope.interface",
                        name="implementer",
                        source_file=Path("app.py"),
                        lineno=2,
                    ),
                ],
                "used_names": {"register", "implementer"},
                "files": {Path("app.py")},
                "string_refs": [],
            }
        }
        summary = merge_dep_usage(["zope"], usage, dep_paths=[sa_dir])
        assert summary.import_count == 1
        assert summary.imports[0].name == "register"
        assert "register" in summary.used_names
        assert "implementer" in summary.used_names

    def test_bare_import_preserves_used_names(self, tmp_path):
        """Bare 'import zope.sqlalchemy' should preserve inspector's used_names."""
        zope_dir = tmp_path / "zope"
        zope_dir.mkdir()
        sa_dir = zope_dir / "sqlalchemy"
        sa_dir.mkdir()

        usage = {
            "zope": {
                "imports": [
                    ImportInfo(
                        module="zope.sqlalchemy",
                        name=None,
                        source_file=Path("app.py"),
                        lineno=1,
                    ),
                ],
                "used_names": {"sqlalchemy", "register"},
                "files": {Path("app.py")},
                "string_refs": [],
            }
        }
        summary = merge_dep_usage(["zope"], usage, dep_paths=[sa_dir])
        assert "sqlalchemy" in summary.used_names
        assert "register" in summary.used_names
        assert summary.import_count == 1

    def test_no_dep_paths_skips_filtering(self):
        """Without dep_paths, all imports are included."""
        usage = {
            "zope": {
                "imports": [
                    ImportInfo(
                        module="zope.interface",
                        name="implementer",
                        source_file=Path("a.py"),
                        lineno=1,
                    ),
                ],
                "used_names": {"implementer"},
                "files": {Path("a.py")},
                "string_refs": [],
            }
        }
        summary = merge_dep_usage(["zope"], usage, dep_paths=None)
        assert "implementer" in summary.used_names
        assert summary.import_count == 1

    def test_regular_package_paths_skip_filtering(self, tmp_path):
        """Regular (non-namespace) package paths don't trigger filtering."""
        pkg = tmp_path / "requests"
        pkg.mkdir()

        usage = {
            "requests": {
                "imports": [
                    ImportInfo(
                        module="requests",
                        name="get",
                        source_file=Path("a.py"),
                        lineno=1,
                    ),
                ],
                "used_names": {"get"},
                "files": {Path("a.py")},
                "string_refs": [],
            }
        }
        summary = merge_dep_usage(["requests"], usage, dep_paths=[pkg])
        assert "get" in summary.used_names

    def test_string_refs_filtered_for_namespace(self, tmp_path):
        """String refs should be filtered to owned subpackage."""
        sc_dir = tmp_path / "sphinxcontrib"
        sc_dir.mkdir()
        apple_dir = sc_dir / "applehelp"
        apple_dir.mkdir()

        ref_apple = StringRef(
            value="sphinxcontrib.applehelp",
            source_file=Path("app.py"),
            lineno=1,
        )
        ref_jsmath = StringRef(
            value="sphinxcontrib.jsmath",
            source_file=Path("app.py"),
            lineno=2,
        )
        usage = {
            "sphinxcontrib": {
                "imports": [],
                "used_names": set(),
                "files": set(),
                "string_refs": [ref_apple, ref_jsmath],
            }
        }
        summary = merge_dep_usage(["sphinxcontrib"], usage, dep_paths=[apple_dir])
        assert len(summary.string_refs) == 1
        assert summary.string_refs[0].value == "sphinxcontrib.applehelp"
        assert "applehelp" in summary.used_names
        assert "jsmath" not in summary.used_names


class TestDetectDynamicDispatch:
    @pytest.mark.parametrize(
        ("name", "expected_reason"),
        [
            ("get_lexer_by_name", "registry lookup"),
            ("find_module", "dynamic finder"),
            ("load_plugin", "dynamic loader"),
            ("create_engine_from_config", "dynamic factory"),
            ("import_module", "dynamic import"),
            ("__import__", "dynamic import"),
            ("entry_points", "plugin registry"),
            ("iter_entry_points", "plugin registry"),
            ("resolve", "plugin resolution"),
        ],
    )
    def test_detects_pattern(self, name, expected_reason):
        result = detect_dynamic_dispatch({name})
        assert name in result
        assert expected_reason in result[name]

    def test_no_match(self):
        result = detect_dynamic_dispatch({"highlight", "clean"})
        assert result == {}

    def test_mixed(self):
        names = {"highlight", "get_lexer_by_name", "TextLexer"}
        result = detect_dynamic_dispatch(names)
        assert len(result) == 1
        assert "get_lexer_by_name" in result
