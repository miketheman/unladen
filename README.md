# 🥥 unladen

> [!WARNING]
> This project is in **early alpha**.
> Results are approximate and should be treated as starting points for investigation,
> not concrete recommendations.
> Static analysis has inherent limitations —
> it cannot detect dynamic imports,
> plugin systems,
> or runtime-only usage patterns.

**"What is the airspeed velocity of an unladen codebase?"**

`unladen` measures the "logical mass" of your Python dependencies.
It doesn't just tell you _that_ you use a library —
it tells you _how much_ of it you actually use.
By mapping your call graphs to a dependency's internal structure,
it provides data-driven recommendations to keep your codebase lean
and avoid carrying unnecessary "coconuts" in your project.

## The Problem

Modern Python applications often pull in large libraries
for a handful of functions.
This "dependency bloat" increases:

* **Attack surface** — unused code can still contain vulnerabilities.
* **Cold boot times** — more code to find and load into memory.
* **Deployment size** — heavier containers and slower CI/CD pipelines.

## How It Works

`unladen` performs a four-phase static analysis:

1. **Collect** — Discovers declared dependencies and locates their installed
   source code via `importlib.metadata` and `site-packages`.
2. **Inspect** — Parses your project's source with `ast` to find import
   statements, attribute accesses, and string references (e.g. Django settings).
3. **Trace** — Indexes each dependency's source,
   builds a call graph,
   and walks it transitively from your entry points to compute activated LLOC.
4. **Report** — Renders a table of results with per-dependency recommendations.

## The Heft Metric

The core metric is the **Heft Ratio**:
the percentage of a dependency's Logical Lines of Code (LLOC)
that your project actually activates.

$$Heft = \frac{\text{Active LLOC}}{\text{Total LLOC}} \times 100\%$$

LLOC counts executable statements (via `ast`),
excluding comments, blank lines, and docstrings.
`if TYPE_CHECKING:` blocks are excluded
since they never execute at runtime.

## Recommendations

| Heft    | Mass   | Recommendation |
|---------|--------|----------------|
| > 25%   | Any    | **Keep** — you use a significant portion |
| 5–25%   | Any    | **Review** — moderate usage, worth examining |
| 1–5%    | Low    | **Vendor** — consider copying the code you need |
| < 1%    | High   | **Rewrite** — drop the coconut |
| < 1%    | Low    | **Vendor** — small enough to inline |
| Unused  | —      | **Remove?** — declared but never imported |
| Native  | —      | **Keep (native)** — compiled extensions, LLOC analysis doesn't apply |

_Mass threshold: Low < 500 LLOC, High >= 500 LLOC._

## Usage

```bash
# Analyze all dependencies in the current project
unladen check

# Analyze a specific project path
unladen check /path/to/project

# Drill into a single dependency with file:line detail
unladen show requests

# Use an explicit requirements file
unladen check -r requirements/prod.txt

# Use an explicit site-packages directory
unladen check --site-packages /path/to/site-packages
```

### Configuration

Project-level settings live in `pyproject.toml`
under `[tool.unladen]`:

```toml
[tool.unladen]
exclude = [
    "setuptools",
    "pip",
    "wheel",
]
```

| Key | Type | Description |
|-----|------|-------------|
| `exclude` | `list[str]` | Dependencies to skip during analysis. Names are PEP 503 normalized, so `"Jinja2"`, `"jinja2"`, and `"jinja-2"` all match. |

When dependencies are excluded,
a footnote appears on the check table and treemap
noting the count,
and the `show` command marks excluded deps in the header.

### Example Output

```console
                       unladen - Dependency Heft Report
┏━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━┳━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┓
┃              ┃         ┃ Names ┃       ┃        ┃           LLOC ┃                ┃
┃ Dependency   ┃ Version ┃  Used ┃ Files ┃ Heft % ┃ (active/total) ┃ Recommendation ┃
┡━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━╇━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━┩
│ click        │ 8.1.7   │     5 │     3 │  32.1% │       210/654  │ Keep           │
├──────────────┼─────────┼───────┼───────┼────────┼────────────────┼────────────────┤
│ requests     │ 2.31.0  │     2 │     1 │   5.0% │       50/1000  │ Review         │
├──────────────┼─────────┼───────┼───────┼────────┼────────────────┼────────────────┤
│ nh3          │ 0.3.2   │     1 │     1 │    n/a │    1 extension │ Keep (native)  │
└──────────────┴─────────┴───────┴───────┴────────┴────────────────┴────────────────┘
```

### `unladen show`

The `show` command provides detailed per-dependency analysis:

* Where the dependency is declared
  (pyproject.toml, setup.py, setup.cfg)
* A table of every import statement with file:line locations
* String references
  (e.g. Django `INSTALLED_APPS`, `MIDDLEWARE`)
* Heft computation with LLOC breakdown
* Warnings for dynamic dispatch patterns
  that may underestimate actual usage

## Dependency Source Support

`unladen` discovers declared dependencies from
(in priority order):

1. `pyproject.toml` — PEP 621 `[project] dependencies`
2. `pyproject.toml` — Poetry `[tool.poetry.dependencies]`
3. `setup.py` — `install_requires` via AST (never executed)
4. `setup.cfg` — `[options] install_requires`
5. `requirements.txt` — with `-r`/`--requirement` include traversal

Use `-r`/`--requirements` to point at a specific file,
bypassing auto-detection.
This is useful for projects with split requirements
(e.g. `requirements/prod.txt`, `requirements/dev.txt`).

## What It Detects

* **Direct imports**:
  `import requests`, `from click import echo`
* **Bare import attribute access**:
  `requests.get()` resolves `get` as a used name
* **Submodule imports**:
  `import pygments.lexers; pygments.lexers.get_lexer_by_name()`
* **Aliased imports**:
  `import pandas as pd; pd.DataFrame()`
* **String references**:
  Django-style activation via `INSTALLED_APPS`, `MIDDLEWARE`,
  and dotted path strings in settings files
* **Compiled extensions**:
  `.so`, `.pyd`, `.dylib` files counted as opaque mass
* **Re-export shims**:
  packages like `cattrs`/`cattr`
  where the import name differs from the implementation package
* **Private aliases**:
  `def _init(); init = _init` patterns
  (common in `sentry-sdk` and similar)

## Performance

`unladen` is designed to be fast on real-world projects:

* **Parse-once** — each source file is parsed once
  and the AST tree is shared across all analysis passes.
* **Parallel indexing** — large dependency trees (100+ files)
  are indexed using Python 3.14's `InterpreterPoolExecutor`
  (lightweight subinterpreters, no process overhead).
* **Bulk computation** — heft for all dependencies is computed
  in a single worker pool rather than per-dependency.
* **Fast file discovery** — uses `os.walk` with directory pruning
  instead of `pathlib.rglob`.

## Limitations

* **Static analysis only** —
  cannot detect `importlib.import_module()`, `__import__()`,
  or other dynamic imports at runtime.
* **Direct dependencies only** —
  does not analyze transitive dependency chains.
* **Python 3.14+ required** —
  uses modern syntax features.

## Requirements

* Python 3.14+
* `rich` (the only runtime dependency)

## Installation

Run directly, like:

```shell
pipx run unladen check
uvx unladen check
```

Or install it:

```shell
# With pip
pip install unladen

# With uv
uv tool install unladen
```

## Architecture

| Phase | Module | Responsibility |
|-------|--------|----------------|
| 1. Collect (Nest) | `collector.py` | Discover dependencies and locate installed source |
| 2. Inspect (Flight) | `inspector.py` | Parse project code to find imports and usage |
| 3. Trace (Weight) | `tracer.py` | Index dependency source, build call graph, compute heft |
| 4. Report (Coconut) | `reporter.py` | Format findings as a rich CLI table |

## Security

`unladen` is a read-only analysis tool.
It **never executes** third-party code —
all source analysis uses `ast.parse()`,
which parses without compiling or running.
There are no `eval()`, `exec()`, `subprocess`,
or shell invocations anywhere in the codebase.

### Known Limitations

- **Metadata trust**:
  `unladen` trusts `top_level.txt` and `RECORD` from installed distributions,
  mirroring pip's own behavior.
  A malicious package could ship metadata
  claiming import names belonging to another package,
  which would affect analysis accuracy but not execute code.
- **Deeply nested files**:
  Some AST traversal functions use recursion.
  A crafted Python file with extreme nesting (1000+ levels)
  could hit Python's recursion limit and crash the tool.
  This cannot cause data loss or code execution.

## Authors

* [Mike Fiedler](https://github.com/miketheman)

## License

MIT License.
See [LICENSE](LICENSE) for details.
