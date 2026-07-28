# Future Work

Ideas, deferred features, and improvements
that are worth revisiting but not urgent.

## Configuration

### Configurable thresholds

The heft recommendation thresholds (Keep >25%, Review >5%, etc.)
and the mass threshold (500 LLOC) are hard-coded in `reporter.py`.
Allow overriding via `[tool.unladen]` in `pyproject.toml` or CLI flags.

## Analysis accuracy

### Platform-conditional dependencies

Dependencies declared with environment markers
(e.g. `colorama; sys_platform == 'win32'`)
show as "not installed" on non-matching platforms.
The import may also live inside a platform guard
(`if sys.platform == 'win32': import colorama`).
Options:

- Preserve the marker through the collector pipeline
  and display "conditional (win32)" instead of "not installed."
- Detect `sys.platform` / `os.name` guards in the inspector
  and annotate those imports as platform-specific.

### Dynamic dispatch: further improvements

Registry dicts (`_languages[key]()`) and subscript calls
are now traced.
Remaining blind spots:

- `getattr(obj, name)()` — dynamic attribute access.
- `__init_subclass__` hooks — implicit class registration.
- Plugin entry points (`[project.entry-points]`) —
  could parse pyproject.toml to discover which dependency code
  is activated at runtime.
- Alias indirection: `f = registry[key]; f()` —
  the call goes through a local variable,
  not a direct subscript call.

### Call graph: aliasing and indirection

The current hand-rolled call graph
is name-based and does not track aliasing
(`f = some_module.func; f()`).
Research from PyCG (assignment-graph approach, ICSE 2021)
and Pyan3 (lexical scoping) could improve precision.
Revisit when false negatives become a pain point.

### Bound-method alias tracking

`unladen` resolves the module-level `alias = obj.method` pattern
where `obj = ClassName()` or `obj = module.ClassName()`
(added in [#25](https://github.com/miketheman/unladen/issues/25)
for the pyjwt `decode = _jwt_global_obj.decode` case).
A review surfaced gaps that are real but not yet observed in the wild:

- Factory functions — `obj = make_session(); get = obj.get`.
  Resolving this needs return-type inference
  to know what `make_session()` constructs.
  The edge is currently recorded against a non-existent
  `make_session.get` and dead-ends harmlessly.
- Guarded or chained instantiation —
  `obj` created inside a `try:`/`if` block,
  or via chained assignment (`a = b = ClassName()`).
  Instance tracking only inspects module-top-level assignments.
- Intermediate aliases — `b = a; c = b.method`.
  This is the general name-aliasing case noted above.
- Inherited methods — `obj = Sub(); alias = obj.method`
  where `method` is defined on a base class.
  The edge resolves to `Sub.method`, which is not indexed;
  `_resolve_callee` would need to walk the base chain.
- `TYPE_CHECKING` else-bodies —
  an instance and its alias split across the
  `if TYPE_CHECKING:`/`else:` boundary are not connected,
  since the else-body is analyzed by a separate recursive pass.

### `if TYPE_CHECKING` re-exports

Some libraries re-export types from dependencies
inside `if TYPE_CHECKING:` blocks.
These are currently invisible to the inspector.
Detecting this pattern could improve
heft accuracy for typing-heavy projects.

### Transitive measurement: follow-ups

`check --transitive` (experimental) ships with deliberate
simplifications worth revisiting:

- **Fixpoint iteration.**
  Each dep is processed once with the used names known when dequeued;
  names contributed by later-discovered parents count toward heft
  but do not re-propagate to grandchildren.
- **Transitive usage into direct deps.**
  When a dependency's active code uses a dep the project also declares
  directly, that extra usage is dropped rather than raising the
  direct dep's heft.  Showing "direct + transitive" heft per dep
  would give a truer total-activation picture.
- **Index reuse.**
  Parent deps are re-indexed for active-module tracing even though
  the bulk heft pass indexes them again.  Sharing the `DepIndex`
  would roughly halve `--transitive` runtime.
- **Undeclared imports.**
  `classify_module` returns `UNKNOWN` for imports that are neither
  first-party, declared third-party, nor stdlib —
  surfacing those would flag missing `Requires-Dist` entries.
- **Treemap integration.**
  Transitive deps could render as nested tiles under their parent.

## CLI and UX

### `--include-tests` flag

`find_project_source()` excludes `tests/` by default.
A flag to include test files would let users measure
how much of a dependency their test suite exercises
vs. their production code.

### Diff mode

Compare heft reports across two commits or branches
to show how dependency usage changed.
Useful for PR review ("this change increases heft of X by 5%").
Currently can be manually done by running `unladen check --output=json`
and comparing reports, but a built-in diff mode would be more convenient.

### Richer "not installed" reporting

When a dependency is declared but not installed,
distinguish between "genuinely missing"
and "conditional on another platform"
(has environment markers like `sys_platform == 'win32'`).
Currently both show the same "not installed" row.
This might be the same as the platform-conditional dependencies improvement
in the analysis section,

## Packaging and distribution

### `packaging` library for name normalization

`_normalize_dep_name()` uses a hand-rolled regex
for PEP 503 normalization.
The `packaging` library's `canonicalize_name()`
and `Requirement` parser are more correct
for edge cases (URL requirements, nested extras).
Revisit when the regex becomes a maintenance burden.
This dependency is pretty stable overall,
but would need to decide if the added dependency is worth the heft.

### Caching

Dependency indexing (Phase 3) is the most expensive step.
A persistent cache keyed on
`(dep_name, version, file content hashes)` could skip re-indexing
unchanged dependencies across runs.
