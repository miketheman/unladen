"""Performance benchmarks for unladen's critical paths.

Exercises the hot paths identified in the architecture:
- LLOC counting (AST statement traversal)
- Source file walking (single-pass import/access/string-ref extraction)
- Tracer index construction and BFS reachability
- Dependency name normalization
- Call graph extraction from AST
"""

import ast
import textwrap
from pathlib import Path

import pytest

from unladen._lloc import count_statements
from unladen._parsing import parse_file
from unladen.collector import _normalize_dep_name
from unladen.inspector import _walk_source_file
from unladen.tracer import (
    _extract_calls_from_tree,
    _index_single_file,
    _trace_reachable,
    _TracerIndex,
)

# ---------------------------------------------------------------------------
# Fixtures — synthetic source code and data structures
# ---------------------------------------------------------------------------

# A realistic Python module (~80 LLOC) representing the kind of
# dependency source code unladen typically analyzes.
_SAMPLE_SOURCE = textwrap.dedent("""\
    \"\"\"Sample module for benchmarking.\"\"\"

    import os
    import sys
    from pathlib import Path
    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from collections.abc import Sequence

    DEFAULT_TIMEOUT = 30
    _CACHE: dict[str, object] = {}

    class Config:
        \"\"\"Configuration container.\"\"\"

        def __init__(self, path: Path, timeout: int = DEFAULT_TIMEOUT):
            self.path = path
            self.timeout = timeout
            self._loaded = False

        def load(self) -> dict:
            if self._loaded:
                return self._get_cached()
            data = self._read_file()
            self._loaded = True
            return data

        def _read_file(self) -> dict:
            text = self.path.read_text(encoding="utf-8")
            return self._parse(text)

        def _parse(self, text: str) -> dict:
            result = {}
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                key, _, value = line.partition("=")
                result[key.strip()] = value.strip()
            return result

        def _get_cached(self) -> dict:
            key = str(self.path)
            if key in _CACHE:
                return _CACHE[key]
            return {}

    class AdvancedConfig(Config):
        \"\"\"Extended configuration with validation.\"\"\"

        def __init__(self, path: Path, strict: bool = False):
            super().__init__(path)
            self.strict = strict

        def validate(self) -> list[str]:
            data = self.load()
            errors = []
            for key, value in data.items():
                if not value:
                    errors.append(f"Empty value for {key}")
            return errors

    def create_config(path: str, strict: bool = False) -> Config:
        p = Path(path)
        if strict:
            return AdvancedConfig(p, strict=True)
        return Config(p)

    def find_config_files(root: str) -> list[Path]:
        base = Path(root)
        results = []
        for dirpath, _dirnames, filenames in os.walk(str(base)):
            for fname in filenames:
                if fname.endswith(".cfg") or fname.endswith(".ini"):
                    results.append(Path(dirpath) / fname)
        return results

    def load_all(root: str) -> dict[str, dict]:
        files = find_config_files(root)
        merged = {}
        for f in files:
            cfg = create_config(str(f))
            merged[str(f)] = cfg.load()
        return merged

    def _normalize(name: str) -> str:
        return name.lower().replace("-", "_").replace(".", "_")
""")


@pytest.fixture
def sample_tree():
    """Parse the sample source into an AST tree."""
    return ast.parse(_SAMPLE_SOURCE)


@pytest.fixture
def sample_func_defs():
    """Build a realistic list of FuncDef dicts for TracerIndex construction."""
    return [
        {
            "name": "Config",
            "type": "class",
            "lloc": 35,
            "module": "config",
            "bases": [],
        },
        {
            "name": "Config.__init__",
            "type": "method",
            "lloc": 4,
            "module": "config",
            "owner": "Config",
        },
        {
            "name": "Config.load",
            "type": "method",
            "lloc": 5,
            "module": "config",
            "owner": "Config",
        },
        {
            "name": "Config._read_file",
            "type": "method",
            "lloc": 3,
            "module": "config",
            "owner": "Config",
        },
        {
            "name": "Config._parse",
            "type": "method",
            "lloc": 8,
            "module": "config",
            "owner": "Config",
        },
        {
            "name": "Config._get_cached",
            "type": "method",
            "lloc": 4,
            "module": "config",
            "owner": "Config",
        },
        {
            "name": "AdvancedConfig",
            "type": "class",
            "lloc": 12,
            "module": "config",
            "bases": ["Config"],
        },
        {
            "name": "AdvancedConfig.__init__",
            "type": "method",
            "lloc": 3,
            "module": "config",
            "owner": "AdvancedConfig",
        },
        {
            "name": "AdvancedConfig.validate",
            "type": "method",
            "lloc": 6,
            "module": "config",
            "owner": "AdvancedConfig",
        },
        {"name": "create_config", "type": "function", "lloc": 4, "module": "config"},
        {
            "name": "find_config_files",
            "type": "function",
            "lloc": 7,
            "module": "config",
        },
        {"name": "load_all", "type": "function", "lloc": 6, "module": "config"},
        {"name": "_normalize", "type": "function", "lloc": 2, "module": "config"},
        {"name": "DEFAULT_TIMEOUT", "type": "variable", "lloc": 1, "module": "config"},
        {"name": "_CACHE", "type": "variable", "lloc": 1, "module": "config"},
        # Add a second module to make the index more realistic
        {
            "name": "Registry",
            "type": "class",
            "lloc": 20,
            "module": "registry",
            "bases": [],
        },
        {
            "name": "Registry.__init__",
            "type": "method",
            "lloc": 3,
            "module": "registry",
            "owner": "Registry",
        },
        {
            "name": "Registry.register",
            "type": "method",
            "lloc": 4,
            "module": "registry",
            "owner": "Registry",
        },
        {
            "name": "Registry.get",
            "type": "method",
            "lloc": 5,
            "module": "registry",
            "owner": "Registry",
        },
        {
            "name": "get_default_registry",
            "type": "function",
            "lloc": 3,
            "module": "registry",
        },
    ]


@pytest.fixture
def sample_call_graph():
    """Build a call graph matching the sample func defs."""
    return {
        "Config.load": {"Config._get_cached", "Config._read_file"},
        "Config._read_file": {"Config._parse"},
        "AdvancedConfig.validate": {"Config.load"},
        "create_config": {"AdvancedConfig", "Config"},
        "find_config_files": set(),
        "load_all": {"find_config_files", "create_config", "Config.load"},
        "Registry.register": set(),
        "Registry.get": set(),
        "get_default_registry": {"Registry"},
    }


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------


def test_bench_count_statements(benchmark, sample_tree):
    """Benchmark LLOC counting on a realistic AST tree."""
    benchmark(count_statements, sample_tree)


def test_bench_walk_source_file(benchmark, sample_tree):
    """Benchmark single-pass AST walking for imports, accesses, and string refs."""
    known_names = {"os", "sys", "pathlib", "config", "registry"}
    file_path = Path("sample.py")

    def run():
        imports = []
        accesses = {}
        string_refs = {}
        _walk_source_file(
            sample_tree, file_path, known_names, imports, accesses, string_refs
        )

    benchmark(run)


def test_bench_tracer_index_construction(benchmark, sample_func_defs):
    """Benchmark _TracerIndex construction from function definitions."""
    benchmark(_TracerIndex, sample_func_defs)


def test_bench_trace_reachable(benchmark, sample_func_defs, sample_call_graph):
    """Benchmark BFS reachability tracing through the call graph."""
    ti = _TracerIndex(sample_func_defs)
    used_names = {"create_config", "load_all", "Registry"}
    benchmark(_trace_reachable, used_names, ti, sample_call_graph)


def test_bench_extract_calls_from_tree(benchmark, sample_tree):
    """Benchmark call graph extraction from an AST tree."""
    benchmark(_extract_calls_from_tree, sample_tree)


def test_bench_index_single_file(benchmark, tmp_path):
    """Benchmark full single-file indexing (parse + LLOC + defs + calls)."""
    py_file = tmp_path / "sample.py"
    py_file.write_text(_SAMPLE_SOURCE, encoding="utf-8")
    benchmark(_index_single_file, py_file)


def test_bench_normalize_dep_name(benchmark):
    """Benchmark PEP 503 dependency name normalization."""
    specs = [
        "requests>=2.28,<3",
        "Jinja2[i18n]",
        "zope.interface",
        "typing-extensions>=4.0",
        "google-cloud-storage",
        "PyYAML",
        "setuptools",
        "urllib3",
    ]

    def run():
        for spec in specs:
            _normalize_dep_name(spec)

    benchmark(run)


def test_bench_parse_file(benchmark, tmp_path):
    """Benchmark Python file parsing via ast.parse."""
    py_file = tmp_path / "sample.py"
    py_file.write_text(_SAMPLE_SOURCE, encoding="utf-8")
    benchmark(parse_file, py_file)
