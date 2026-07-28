"""Shared data types used across multiple phases."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, NotRequired, TypedDict


@dataclass(frozen=True)
class HeftResult:
    """Result of heft computation for a single dependency."""

    dep_name: str
    total_lloc: int
    active_lloc: int
    heft_ratio: float
    opaque_files: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the JSON shape shared by all reports."""
        return {
            "ratio": self.heft_ratio,
            "active_lloc": self.active_lloc,
            "total_lloc": self.total_lloc,
            "opaque_files": self.opaque_files,
        }


class FuncDef(TypedDict):
    """A single function, class, method, or variable definition."""

    name: str
    type: str  # "function" | "class" | "method" | "variable"
    lloc: int
    module: str
    owner: NotRequired[str]  # owning class name, present for type="method"
    bases: NotRequired[list[str]]  # base class names, present for type="class"
    # Source file the definition came from.  Annotated during index
    # assembly (not in workers, to keep the pickled payload small) so
    # file-level activation is exact, not module-name-based.
    path: NotRequired[Path]


class DepIndex(TypedDict):
    """Indexed source code for a dependency."""

    total_lloc: int
    functions: list[FuncDef]
    opaque_files: int
    call_graph: dict[str, set[str]]
    # All source files the index was built from.  Lets consumers check
    # file membership (e.g. ancestor __init__.py lookups) without
    # re-walking the tree or issuing stat calls.
    files: list[Path]
