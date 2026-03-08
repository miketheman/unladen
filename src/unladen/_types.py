"""Shared data types used across multiple phases."""

from dataclasses import dataclass
from typing import NotRequired, TypedDict


@dataclass(frozen=True)
class HeftResult:
    """Result of heft computation for a single dependency."""

    dep_name: str
    total_lloc: int
    active_lloc: int
    heft_ratio: float
    opaque_files: int = 0


class FuncDef(TypedDict):
    """A single function, class, method, or variable definition."""

    name: str
    type: str  # "function" | "class" | "method" | "variable"
    lloc: int
    module: str
    owner: NotRequired[str]  # owning class name, present for type="method"
    bases: NotRequired[list[str]]  # base class names, present for type="class"


class DepIndex(TypedDict):
    """Indexed source code for a dependency."""

    total_lloc: int
    functions: list[FuncDef]
    opaque_files: int
    call_graph: dict[str, set[str]]
