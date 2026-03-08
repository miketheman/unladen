# CLAUDE.md

## Project

`unladen` is a CLI tool that measures the "logical mass" of Python dependencies
via static analysis.
Python 3.14+, `uv` with src layout, single runtime dependency (`rich`).

## Quick Reference

```bash
just test           # run tests
just cov            # tests with coverage
just lint           # ruff check + format check
just fmt            # auto-format
just run check /path/to/project  # run the tool
```

## Architecture

Four phases, each in its own module under `src/unladen/`:

| Module | Phase | What it does |
| ------ | ----- | ------------ |
| `collector.py` | 1. Collect (Nest) | Parse dependency declarations, locate installed source via `importlib.metadata` |
| `inspector.py` | 2. Inspect (Flight) | Single-pass AST walk for imports, attribute accesses, string references |
| `merger.py` | 2.5. Merge | Aggregate per-import-name usage into per-distribution summaries; namespace filtering |
| `tracer.py` | 3. Trace (Weight) | Index dependency source, build call graph, BFS from entry points, compute LLOC heft ratio |
| `reporter.py` | 4. Report (Coconut) | Format results as a `rich` table with recommendations |
| `treemap.py` | 4. Visualize | Squarified treemap rendering via Rich |
| `cli.py` | Orchestration | `argparse` CLI, wires phases together for `check`/`show` commands |
| `_parsing.py` | Shared | `parse_file()`, `is_setup_call()` — shared AST utilities |
| `_types.py` | Shared | `HeftResult`, `FuncDef`, `DepIndex` — cross-phase type definitions |

## Key Concepts

- **Heft Ratio**: `active_lloc / total_lloc` — percentage of dependency code activated by the project.
- **LLOC**: Logical Lines of Code — executable `ast` statement nodes, excluding docstrings, comments, and `if TYPE_CHECKING:` blocks.
- **Used names**: Names imported or accessed from a dependency (e.g. `get` from `requests.get()`), traced transitively through the dependency's internal call graph.
- **String references**: Django-style activation via dotted path strings in settings (e.g. `INSTALLED_APPS`, `MIDDLEWARE`). Leaf names are extracted for heft computation.

## Code Conventions

- All AST parsing uses `except SyntaxError, UnicodeDecodeError:` (PEP 758, Python 3.14+).
- Lazy imports inside command functions (`_cmd_check`, `_cmd_show`) to keep CLI startup fast.
- `_normalize_dep_name()` handles PEP 503 normalization with regex (no external `packaging` dependency).
- Private alias resolution: if used name `X` not found in definitions but `_X` is, treat as alias.
- **Qualified method names**: methods are indexed as `ClassName.method` in the tracer
  to avoid collisions when multiple classes define `__init__`, `__str__`, etc.
  `self.method()` calls are also qualified. The BFS falls back from qualified
  to bare names for inherited methods.
- `_PARALLEL_THRESHOLD = 100` — file count below which serial indexing is used
  (avoids subinterpreter startup overhead for small deps).

## Performance

- **Single-pass AST walk**: `_walk_source_file()` extracts imports,
  attribute accesses, and string references in one `ast.walk` pass
  (was three separate walks; 44% faster on sphinx).
- **Parse-once**: each file is parsed once and the AST tree is shared
  across LLOC counting, definition extraction, and call graph extraction.
- **File-level parallelism**: `InterpreterPoolExecutor` (Python 3.14 subinterpreters)
  for indexing large dependency trees (>100 files).
  Lighter than `ProcessPoolExecutor`, same throughput.
- **`os.walk`** instead of `pathlib.rglob` for 2x faster file collection
  with in-place directory pruning.
- **Bulk heft**: `compute_hefts_bulk()` flattens files from all deps
  into a single worker pool, avoiding per-dep overhead.
- **`_TracerIndex`**: single-pass construction of all BFS lookup tables
  (lloc_by_name, classes, method_of, module_defs, class_methods, bare_to_qualified).

## Testing

- `pytest` with `pytest-cov`, `pytest-randomly`.
- Tests live in `tests/`, one file per module (`test_collector.py`, `test_inspector.py`, etc.).
- Fixtures in `tests/conftest.py` — `sample_project`, `fake_site_packages`, etc.
- Run `just test` or `just cov` — minimum 95% coverage (configured in `pyproject.toml`).
- Always run tests and lint after changes: `just test && just lint`.
- Use `@pytest.mark.parametrize` for repetitive test patterns
  (e.g. normalization, recommendations, dynamic dispatch detection).

## Check Modes

The `check` command supports two modes:

- **Project mode**: `unladen check /path/to/project` —
  reads dependency declarations (pyproject.toml, requirements.txt, etc.)
  and inspects the project's own source code.
- **Package mode**: `unladen check requests` —
  reads `Requires-Dist` from the installed package's METADATA
  and inspects the package's own source for dependency usage.
  Useful for exploring a package's dependency tree without a project.

Detection: if the target resolves to an existing directory, project mode;
otherwise package mode.

## Namespace Package Handling

Namespace packages (e.g. `zope.*`) where multiple distributions share
the same top-level import name are disambiguated via:

- `_owned_module_prefixes()` / `_matches_owned()` in `merger.py`
- Imports and string refs are filtered to the distribution's owned subpackages
- `used_names` are passed through unfiltered (extra names from sibling
  packages are harmless — the tracer only matches names in the dep's own definitions)

## Dependency Source Priority

Auto-detection order (first match wins):

1. `pyproject.toml` — PEP 621 `[project] dependencies`
2. `pyproject.toml` — Poetry `[tool.poetry.dependencies]`
3. `setup.py` — `install_requires` via AST (never executed)
4. `setup.cfg` — `[options] install_requires`
5. `requirements.txt` — with `-r`/`--requirement` include traversal

The `--requirements` / `-r` flag overrides all auto-detection.

## Configuration

Project-level configuration lives in `pyproject.toml` under `[tool.unladen]`.

```toml
[tool.unladen]
exclude = ["setuptools", "pip", "wheel"]
```

Supported keys:

| Key | Type | Description |
| --- | ---- | ----------- |
| `exclude` | `list[str]` | Dependency names to skip during analysis. PEP 503 normalized for matching. |

Exclusions are applied after dependency collection but before inspection.
When deps are excluded, a footnote appears on the check table,
the treemap title, and the show command header.

Config is loaded via `load_config()` in `cli.py`.
The normalized exclude set is built by `_load_exclude_set()`,
which reuses `_normalize_dep_name()` from `collector.py`.

## Style

- Linting: `ruff` with `E`, `F`, `I`, `UP` rules, target Python 3.14.
- Documentation: use semantic line breaks per [sembr.org](https://sembr.org).
- No emojis in code or output unless requested.
- Keep changes minimal — don't refactor surrounding code when fixing a bug.
- Never commit or push unless explicitly asked.
