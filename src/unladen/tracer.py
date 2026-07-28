"""Phase 3: The Weight — Logic Computation.

Computes the Heft Ratio for each dependency by indexing dependency source code
and measuring how much of it is activated by the project's imports.
"""

import ast
import os
from concurrent.futures import InterpreterPoolExecutor
from pathlib import Path

from unladen._lloc import count_statements as _count_statements
from unladen._lloc import is_type_checking_block as _is_type_checking_block
from unladen._parsing import parse_file as _parse_file
from unladen._types import DepIndex, FuncDef, HeftResult


def compute_heft(
    dep_paths: list[Path],
    used_names: set[str],
    dep_name: str,
) -> HeftResult:
    """Compute the heft ratio for a dependency.

    Args:
        dep_paths: Paths to the dependency's source (package dirs or .py files).
        used_names: Names imported from this dependency (from Phase 2).
        dep_name: Distribution name (for reporting).

    Returns:
        A HeftResult with total/active LLOC and the heft ratio.
    """
    index = index_dependency(dep_paths)
    return _heft_from_index(index, used_names, dep_name)


def _heft_from_index(
    index: DepIndex, used_names: set[str], dep_name: str
) -> HeftResult:
    """Compute HeftResult from a pre-built dependency index."""
    total_lloc = index["total_lloc"]

    if total_lloc == 0 or not used_names:
        return HeftResult(
            dep_name=dep_name,
            total_lloc=total_lloc,
            active_lloc=0,
            heft_ratio=0.0,
            opaque_files=index["opaque_files"],
        )

    call_graph = index["call_graph"]
    ti = _TracerIndex(index["functions"])

    matched = _trace_reachable(used_names, ti, call_graph)
    active_lloc = _sum_active_lloc(matched, ti)

    heft_ratio = active_lloc / total_lloc

    return HeftResult(
        dep_name=dep_name,
        total_lloc=total_lloc,
        active_lloc=active_lloc,
        heft_ratio=heft_ratio,
        opaque_files=index["opaque_files"],
    )


def compute_hefts_bulk(
    work_items: list[tuple[str, list[Path], set[str]]],
) -> dict[str, HeftResult]:
    """Compute heft for multiple dependencies in parallel.

    Collects source files from all deps concurrently (I/O-bound),
    then indexes all files in a single process pool (CPU-bound).

    Args:
        work_items: List of (dep_name, dep_paths, used_names) tuples.

    Returns:
        Mapping of dep_name -> HeftResult.
    """
    if not work_items:
        return {}

    # Phase A: collect source files from all deps
    all_files: list[Path] = []
    file_dep: list[str] = []
    dep_opaque: dict[str, int] = {}
    dep_used_names: dict[str, set[str]] = {}

    for dep_name, dep_paths, used_names in work_items:
        py_files, opaque_files = _collect_source_files(dep_paths)
        dep_opaque[dep_name] = opaque_files
        dep_used_names[dep_name] = used_names
        for f in py_files:
            all_files.append(f)
            file_dep.append(dep_name)

    # Phase B: index all files in a single pool (CPU-bound)
    all_results = _index_files(all_files)

    # Phase C: reassemble per-dep indexes
    dep_indexes: dict[str, DepIndex] = {
        dep_name: {
            "total_lloc": 0,
            "functions": [],
            "opaque_files": dep_opaque[dep_name],
            "call_graph": {},
        }
        for dep_name in dep_opaque
    }

    for i, result in enumerate(all_results):
        if result is None:
            continue
        lloc, defs, calls = result
        idx = dep_indexes[file_dep[i]]
        idx["total_lloc"] += lloc
        idx["functions"].extend(defs)
        for name, callees in calls.items():
            try:
                idx["call_graph"][name].update(callees)
            except KeyError:
                idx["call_graph"][name] = callees

    # Phase D: compute heft from each assembled index
    return {
        dep_name: _heft_from_index(idx, dep_used_names[dep_name], dep_name)
        for dep_name, idx in dep_indexes.items()
    }


class _TracerIndex:
    """Pre-computed lookup tables for heft tracing.

    Names are *qualified*: methods are stored as ``ClassName.method``
    to avoid collisions between same-named methods in different classes.
    Top-level functions and classes use their bare name.

    Built in a single pass over the functions list, replacing the
    separate ``_build_lloc_index`` and ``_build_method_maps`` calls.
    """

    __slots__ = (
        "lloc_by_name",
        "classes",
        "class_bases",
        "method_of",
        "module_defs",
        "class_methods",
        "bare_to_qualified",
    )

    def __init__(self, functions: list[FuncDef]) -> None:
        self.lloc_by_name: dict[str, int] = {}
        self.classes: set[str] = set()
        self.class_bases: dict[str, list[str]] = {}
        self.method_of: dict[str, str] = {}
        self.module_defs: dict[str, set[str]] = {}
        self.class_methods: dict[str, list[str]] = {}
        self.bare_to_qualified: dict[str, list[str]] = {}

        for func in functions:
            name = func["name"]
            if name not in self.lloc_by_name:
                self.lloc_by_name[name] = func["lloc"]
            ftype = func["type"]
            if ftype == "class":
                self.classes.add(name)
                if "bases" in func:
                    self.class_bases[name] = func["bases"]
            elif ftype == "method" and "owner" in func:
                owner = func["owner"]
                self.method_of[name] = owner
                self.class_methods.setdefault(owner, []).append(name)
                bare = name.split(".", 1)[1] if "." in name else name
                self.bare_to_qualified.setdefault(bare, []).append(name)
            self.module_defs.setdefault(func["module"], set()).add(name)


def _resolve_name(
    name: str,
    lloc_by_name: dict[str, int],
    bare_to_qualified: dict[str, list[str]],
    module_defs: dict[str, set[str]],
) -> list[str]:
    """Resolve a used name to one or more definition names.

    Five-step fallback chain:
    1. Direct match — ``get`` -> ``get`` (top-level function).
    2. Private alias — ``init`` -> ``_init`` (public API wrapping private impl).
    3. Bare-to-qualified — ``parse`` -> ``[Parser.parse, Lexer.parse]`` (method name).
    4. Module defs — ``utils`` -> all defs in ``utils.py`` (submodule import).
    5. Private module alias — ``compat`` -> all defs in ``_compat.py``.
    """
    if name in lloc_by_name:
        return [name]
    if f"_{name}" in lloc_by_name:
        return [f"_{name}"]
    if name in bare_to_qualified:
        return bare_to_qualified[name]
    if name in module_defs:
        return list(module_defs[name])
    if f"_{name}" in module_defs:
        return list(module_defs[f"_{name}"])
    return []


def _resolve_callee(name: str, ti: _TracerIndex) -> list[str]:
    """Resolve a call graph callee to definition names.

    Simpler than ``_resolve_name`` — used during BFS traversal of the
    internal call graph where names are already within the dependency.
    Only needs direct and bare-to-qualified lookup (no module or
    private-alias fallbacks).
    """
    if name in ti.lloc_by_name:
        return [name]
    if name in ti.bare_to_qualified:
        return ti.bare_to_qualified[name]
    return []


def _trace_reachable(
    used_names: set[str],
    ti: _TracerIndex,
    call_graph: dict[str, set[str]],
) -> set[str]:
    """BFS from used names through the call graph.

    When a class is reached, all its methods (qualified as
    ``ClassName.method``) are enqueued, and its base classes
    are transitively followed so inherited methods are counted.
    """
    matched: set[str] = set()
    worklist: list[str] = []
    for name in used_names:
        worklist.extend(
            _resolve_name(name, ti.lloc_by_name, ti.bare_to_qualified, ti.module_defs)
        )

    while worklist:
        name = worklist.pop()
        if name in matched:
            continue
        matched.add(name)
        # Class reached → enqueue its own methods
        for method in ti.class_methods.get(name, ()):
            if method not in matched and method in ti.lloc_by_name:
                worklist.append(method)
        # Follow inheritance: enqueue base classes (transitively)
        for base in ti.class_bases.get(name, ()):
            if base not in matched:
                worklist.extend(_resolve_callee(base, ti))
        for callee in call_graph.get(name, ()):
            if callee not in matched:
                worklist.extend(_resolve_callee(callee, ti))
    return matched


def _sum_active_lloc(matched: set[str], ti: _TracerIndex) -> int:
    """Sum LLOC for matched names, avoiding class/method double-counting.

    A class definition's LLOC includes all its methods' LLOC.  If any
    method of a class is matched, the class-level LLOC is skipped to
    avoid counting the same lines twice.
    """
    classes_with_matched_methods: set[str] = set()
    for name in matched:
        if name in ti.method_of:
            classes_with_matched_methods.add(ti.method_of[name])

    total = 0
    for name in matched:
        if name in ti.classes and name in classes_with_matched_methods:
            continue
        total += ti.lloc_by_name[name]
    return total


def active_modules(index: DepIndex, used_names: set[str]) -> set[str]:
    """Return names of modules containing definitions reached from *used_names*.

    Runs the same BFS as heft computation and maps the matched
    definitions back to their modules.  Used by transitive tracing to
    decide which of a dependency's files count as activated.
    """
    if not used_names:
        return set()
    ti = _TracerIndex(index["functions"])
    matched = _trace_reachable(used_names, ti, index["call_graph"])
    return {module for module, defs in ti.module_defs.items() if defs & matched}


def index_dependency(dep_paths: list[Path]) -> DepIndex:
    """Index a dependency's source code.

    Walks all .py files under the given paths, parses them with ast,
    and catalogs all functions and classes with their LLOC counts.

    Uses parallel workers when the file count is large enough to
    benefit from the overhead.
    """
    py_files, opaque_files = _collect_source_files(dep_paths)
    file_results = _index_files(py_files)
    idx = _assemble_index(file_results, opaque_files)
    return idx


# Minimum file count to justify subinterpreter startup overhead.
# Below this, serial indexing is faster.  Empirically tuned: 100 files
# takes ~5ms serially vs ~15ms with pool startup on a typical machine.
_PARALLEL_THRESHOLD = 100


def _index_files(
    py_files: list[Path],
) -> list[tuple[int, list[FuncDef], dict[str, set[str]]] | None]:
    """Index files serially or in parallel based on count."""
    if len(py_files) >= _PARALLEL_THRESHOLD:
        return _index_files_parallel(py_files)
    return [_index_single_file(f) for f in py_files]


def _assemble_index(
    file_results: list[tuple[int, list[FuncDef], dict[str, set[str]]] | None],
    opaque_files: int = 0,
) -> DepIndex:
    """Assemble a DepIndex from per-file indexing results."""
    total_lloc = 0
    functions: list[FuncDef] = []
    call_graph: dict[str, set[str]] = {}
    for result in file_results:
        if result is None:
            continue
        lloc, defs, calls = result
        total_lloc += lloc
        functions.extend(defs)
        for name, callees in calls.items():
            try:
                call_graph[name].update(callees)
            except KeyError:
                call_graph[name] = callees
    return {
        "total_lloc": total_lloc,
        "functions": functions,
        "opaque_files": opaque_files,
        "call_graph": call_graph,
    }


def _index_single_file(
    py_file: Path,
) -> tuple[int, list[FuncDef], dict[str, set[str]]] | None:
    """Parse, count LLOC, extract defs and calls from one file."""
    tree = _parse_file(py_file)
    if tree is None:
        return None
    lloc = _count_statements(tree)
    # Use package name for __init__.py so module_defs keys match
    # subpackage names (e.g. "applehelp" not "__init__").
    module_name = py_file.parent.name if py_file.stem == "__init__" else py_file.stem
    defs: list[FuncDef] = []
    _collect_defs(tree, module_name, defs)
    calls = _extract_calls_from_tree(tree)
    return lloc, defs, calls


def _index_files_parallel(
    py_files: list[Path],
) -> list[tuple[int, list[FuncDef], dict[str, set[str]]] | None]:
    """Index files using parallel interpreter workers.

    Uses InterpreterPoolExecutor (Python 3.14+) for true parallelism.
    Capped at 6 workers to avoid diminishing returns from context
    switching.  Chunksize tuned to ~4 batches per worker.
    """
    workers = min(os.cpu_count() or 1, 6)
    chunksize = max(1, len(py_files) // (workers * 4))
    with InterpreterPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_index_single_file, py_files, chunksize=chunksize))


_OPAQUE_SUFFIXES = frozenset((".so", ".pyd", ".dylib"))


def _collect_source_files(dep_paths: list[Path]) -> tuple[list[Path], int]:
    """Walk dependency paths and collect .py files and opaque extension count."""
    py_files: list[Path] = []
    opaque_files = 0
    for path in dep_paths:
        if path.is_file():
            if path.suffix == ".py":
                py_files.append(path)
            elif path.suffix in _OPAQUE_SUFFIXES:
                opaque_files += 1
        elif path.is_dir():
            pys, opaques = _walk_directory(path)
            py_files.extend(pys)
            opaque_files += opaques
    return py_files, opaque_files


def _walk_directory(root: Path) -> tuple[list[Path], int]:
    """Walk a directory tree, collecting .py and opaque files.

    Uses ``dirnames[:] =`` in-place mutation to prune excluded
    directories from ``os.walk`` traversal before they are descended
    into (the standard os.walk pruning idiom).
    """
    py_files: list[Path] = []
    opaque_files = 0
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]
        for fname in filenames:
            if fname.endswith(".py"):
                if not _is_test_file(fname, dirpath):
                    py_files.append(Path(dirpath, fname))
            elif os.path.splitext(fname)[1] in _OPAQUE_SUFFIXES:
                opaque_files += 1
    return py_files, opaque_files


def _extract_calls_from_tree(tree: ast.Module) -> dict[str, set[str]]:
    """Extract function calls from an already-parsed AST.

    Uses qualified names (``ClassName.method``) for methods so that
    same-named methods in different classes don't collide.
    """
    calls: dict[str, set[str]] = {}
    # Maps a local name to the class it was instantiated from
    # (``obj = ClassName()``), so bound-method aliases can be resolved.
    instances: dict[str, str] = {}
    _extract_calls_from_children(tree, calls, instances)
    return calls


def _extract_calls_from_children(
    parent: ast.AST,
    calls: dict[str, set[str]],
    instances: dict[str, str],
) -> None:
    """Extract calls from *parent*'s children, descending into if/try blocks.

    Descends into the same conditional/try blocks as ``_collect_defs``
    so definitions found there also get call-graph edges.
    """
    for node in ast.iter_child_nodes(parent):
        if _is_type_checking_block(node):
            # Skip type-stub body but process the else-body
            # which contains runtime code (e.g. sentry_sdk's
            # ``init = (lambda: _init)()`` pattern).
            assert isinstance(node, ast.If)
            if node.orelse:
                wrapper = ast.Module(body=node.orelse, type_ignores=[])
                _extract_calls_from_children(wrapper, calls, instances)
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_calls = _calls_in_body(node)
            func_calls.discard(node.name)
            calls.setdefault(node.name, set()).update(func_calls)
        elif isinstance(node, ast.ClassDef):
            class_name = node.name
            for class_child in ast.iter_child_nodes(node):
                if isinstance(class_child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    method_calls = _calls_in_body(class_child, class_name)
                    qname = f"{class_name}.{class_child.name}"
                    method_calls.discard(qname)
                    calls.setdefault(qname, set()).update(method_calls)
                    calls.setdefault(class_name, set()).update(method_calls)
        elif isinstance(node, (ast.If, ast.Try, ast.TryStar, ast.ExceptHandler)):
            # Mirror _collect_defs: definitions inside conditional/try
            # blocks (compat-library pattern) need call edges too.
            _extract_calls_from_children(node, calls, instances)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            # Module-level variable: extract referenced names from the
            # value so registry dicts (e.g. ``_languages = {'en': English}``)
            # create edges to the classes/functions they hold.
            target = node.targets[0]
            if isinstance(target, ast.Name):
                # Track ``obj = ClassName()`` so a later
                # ``alias = obj.method`` resolves to ``ClassName.method``.
                # Rebinding the name to anything else drops the mapping
                # so a stale class is never used.
                class_name = _constructor_class_name(node.value)
                if class_name is not None:
                    instances[target.id] = class_name
                else:
                    instances.pop(target.id, None)
                refs = _names_in_value(node.value)
                bound = _resolve_bound_method_alias(node.value, instances)
                if bound:
                    refs = refs | {bound}
                if refs:
                    calls.setdefault(target.id, set()).update(refs)


def _names_in_value(node: ast.expr) -> set[str]:
    """Extract bare Name references from an assignment value.

    Handles dict values, list/set/tuple elements, call arguments,
    lambda bodies, and nested combinations.  Collects ``ast.Name``
    nodes that likely reference functions/classes in the dependency.
    """
    names: set[str] = set()
    if isinstance(node, ast.Name):
        names.add(node.id)
    elif isinstance(node, ast.Dict):
        for v in node.values:
            if v is not None:
                names.update(_names_in_value(v))
    elif isinstance(node, (ast.List, ast.Set, ast.Tuple)):
        for elt in node.elts:
            names.update(_names_in_value(elt))
    elif isinstance(node, ast.Call):
        # ``name = SomeCallable(arg)`` or ``name = (lambda: _func)()``
        names.update(_names_in_value(node.func))
        for arg in node.args:
            names.update(_names_in_value(arg))
    elif isinstance(node, ast.Lambda):
        # ``name = (lambda: _func)()`` → extract _func from body
        names.update(_names_in_value(node.body))
    return names


def _resolve_bound_method_alias(
    value: ast.expr, instances: dict[str, str]
) -> str | None:
    """Resolve a bound-method alias to its qualified ``ClassName.method`` name.

    Handles the module-level ``alias = obj.method`` pattern where ``obj``
    is a known instance of a class (recorded from ``obj = ClassName()``).
    Libraries like pyjwt expose their public API this way::

        _jwt_global_obj = PyJWT()
        decode = _jwt_global_obj.decode

    Returns ``None`` when *value* is not an attribute access on a
    tracked instance.
    """
    if (
        isinstance(value, ast.Attribute)
        and isinstance(value.value, ast.Name)
        and value.value.id in instances
    ):
        return f"{instances[value.value.id]}.{value.attr}"
    return None


def _constructor_class_name(value: ast.expr) -> str | None:
    """Return the class name for a constructor call, or ``None``.

    Handles bare ``ClassName()`` and dotted ``module.ClassName()``
    instantiations.  The dotted form takes the final attribute,
    mirroring ``_extract_base_names``.
    """
    if not isinstance(value, ast.Call):
        return None
    func = value.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _calls_in_body(node: ast.AST, class_name: str | None = None) -> set[str]:
    """Extract all called names from a function/method body.

    Detects direct calls, self/cls calls (qualified), attribute calls,
    and subscript calls (``registry[key]()``).
    """
    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = _resolve_call_target(child, class_name)
            if name:
                found.add(name)
    return found


def _resolve_call_target(call: ast.Call, class_name: str | None) -> str | None:
    """Determine the callee name from a Call node.

    Handles three call patterns:
    - ``func()`` — bare Name node.
    - ``obj.method()`` — Attribute node, delegated to ``_resolve_attr_call``.
    - ``registry[key]()`` — Subscript node (dict/list dispatch).
    """
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return _resolve_attr_call(func, class_name)
    if isinstance(func, ast.Subscript) and isinstance(func.value, ast.Name):
        return func.value.id
    return None


def _resolve_attr_call(func: ast.Attribute, class_name: str | None) -> str | None:
    """Resolve an attribute call like ``obj.method()``.

    - ``self.method()`` / ``cls.method()`` -> ``ClassName.method`` (qualified).
    - ``other_obj.method()`` -> ``method`` (bare, resolved later by BFS).
    - ``SomeClass().method()`` -> ``method`` (chained constructor call).
    """
    if isinstance(func.value, ast.Name):
        # self.method() or cls.method()
        if func.value.id in ("self", "cls") and class_name:
            return f"{class_name}.{func.attr}"
        return func.attr
    if isinstance(func.value, ast.Call):
        # expr().method() e.g. Parser().parse()
        return func.attr
    return None


def _is_private_alias(target_name: str, value: ast.expr) -> bool:
    """Check if an assignment is a private alias (``init = _init``).

    Only skips when the value is the underscore-prefixed version
    of the target name — the actual alias pattern handled by the
    private-name fallback in ``_trace_reachable``.
    Assignments like ``PlatformDirs = _Result`` are NOT aliases
    (different names) and should be indexed.
    """
    if not isinstance(value, ast.Name):
        return False
    return value.id == f"_{target_name}"


def _maybe_variable_def(node: ast.AST, module_name: str) -> FuncDef | None:
    """Check if a node is a module-level variable assignment worth indexing.

    Variables like ``DEFAULT_TIMEOUT = 30`` or ``_languages = {...}`` are
    indexed so they can be matched as used names and traced through
    the call graph (e.g. a registry dict referencing class names).
    """
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        if isinstance(target, ast.Name) and not _is_private_alias(
            target.id, node.value
        ):
            return {
                "name": target.id,
                "type": "variable",
                "lloc": 1,
                "module": module_name,
            }
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        if node.value is not None and not _is_private_alias(node.target.id, node.value):
            return {
                "name": node.target.id,
                "type": "variable",
                "lloc": 1,
                "module": module_name,
            }
    return None


def _extract_base_names(node: ast.ClassDef) -> list[str]:
    """Extract base class names from a ClassDef node.

    Handles bare names (``class Foo(Bar)``), attribute access
    (``class Foo(mod.Bar)`` → ``Bar``), and generic subscripts
    (``class Foo(Bar[T])`` → ``Bar``).
    """
    names: list[str] = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.append(base.id)
        elif isinstance(base, ast.Attribute):
            names.append(base.attr)
        elif isinstance(base, ast.Subscript):
            # Generic base: Bar[T] → extract Bar
            if isinstance(base.value, ast.Name):
                names.append(base.value.id)
            elif isinstance(base.value, ast.Attribute):
                names.append(base.value.attr)
    return names


def _collect_defs(parent: ast.AST, module_name: str, defs: list[FuncDef]) -> None:
    """Collect function/class definitions, descending into if/try blocks."""
    for node in ast.iter_child_nodes(parent):
        if _is_type_checking_block(node):
            # Skip the if-body (type stubs) but process the
            # else-body which contains runtime code.
            assert isinstance(node, ast.If)
            if node.orelse:
                wrapper = ast.Module(body=node.orelse, type_ignores=[])
                _collect_defs(wrapper, module_name, defs)
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lloc = 1 + _count_statements(node)
            defs.append(
                {
                    "name": node.name,
                    "type": "function",
                    "lloc": lloc,
                    "module": module_name,
                }
            )
        elif (var := _maybe_variable_def(node, module_name)) is not None:
            defs.append(var)
        elif isinstance(node, ast.ClassDef):
            lloc = 1 + _count_statements(node)
            bases = _extract_base_names(node)
            class_def: FuncDef = {
                "name": node.name,
                "type": "class",
                "lloc": lloc,
                "module": module_name,
            }
            if bases:
                class_def["bases"] = bases
            defs.append(class_def)
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    method_lloc = 1 + _count_statements(child)
                    defs.append(
                        {
                            "name": f"{node.name}.{child.name}",
                            "type": "method",
                            "lloc": method_lloc,
                            "module": module_name,
                            "owner": node.name,
                        }
                    )
        elif isinstance(node, (ast.If, ast.Try, ast.TryStar, ast.ExceptHandler)):
            # Descend into conditional/try blocks to find definitions
            # (common in compatibility libraries like typing_extensions)
            _collect_defs(node, module_name, defs)


# Directory names to exclude when indexing dependency source
_EXCLUDED_DIRS = {"tests", "test", "testing", "conftest"}


_TEST_FRAMEWORK_MARKERS = (
    b"import unittest",
    b"from unittest",
    b"import pytest",
    b"from pytest",
)


def _is_test_file(fname: str, dirpath: str) -> bool:
    """Check if a Python file is a test file that should be excluded.

    Obvious patterns (``test_*.py``, ``conftest.py``) are excluded by name.
    Ambiguous names (``tests.py``, ``test.py``) are only excluded if they
    import a test framework — libraries like Jinja2 and Werkzeug use
    these names for non-test modules.
    """
    if fname.startswith("test_") or fname == "conftest.py":
        return True
    if fname in ("tests.py", "test.py"):
        return _imports_test_framework(os.path.join(dirpath, fname))
    return False


def _imports_test_framework(filepath: str) -> bool:
    """Quick byte scan for test framework imports in the first 8KB."""
    try:
        with open(filepath, "rb") as f:
            head = f.read(8192)
        return any(marker in head for marker in _TEST_FRAMEWORK_MARKERS)
    except OSError:
        return False
