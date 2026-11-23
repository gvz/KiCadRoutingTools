"""Project adapter interface.

A project adapter is a Python file defining ``make_adapter() -> ProjectAdapter``.
It tells the loop where the placement-input board lives, how to route it, which
parts may move, and how symmetric twins are derived.
"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Optional, Set, Tuple

Rect = Tuple[float, float, float, float]  # x0, y0, x1, y1 (mm)


@dataclass
class ProjectAdapter:
    # Board the router reads (the loop rewrites it with each candidate placement).
    placement_pcb: Path
    # Board the router writes (the loop grades it).
    routed_pcb: Path
    # Runs the full routing pipeline; must leave a board at ``routed_pcb``.
    # Receives a log file path. Return code != 0 counts as a failed route.
    route: Callable[[Path], int]
    # ref -> twin ref that must receive the same move (translated by twin_offset).
    twin_of: Callable[[str], Optional[str]] = lambda ref: None
    twin_offset: Tuple[float, float] = (0.0, 0.0)
    # ref -> region the part's courtyard must stay inside, or None for board bounds.
    region_of: Callable[[str], Optional[Rect]] = lambda ref: None
    # Parts that never move (connectors, holes, parts with pre-laid copper ...).
    locked: Set[str] = field(default_factory=set)
    # Predicate for parts the policy may choose as primary movers. Twins of
    # movable parts are moved implicitly, so they should NOT be movable themselves.
    is_movable: Callable[[str], bool] = lambda ref: True
    # Map any ref (e.g. a twin) to the ref the policy should move instead.
    canonical_ref: Callable[[str], str] = lambda ref: ref
    # Courtyard clearance between parts (mm).
    courtyard_gap: float = 0.10
    # Optional DRC: run kicad-cli on the routed board and add errors to the score.
    run_drc: bool = True
    # Extra free-text context handed to an LLM proposer.
    llm_context: str = ""
    # Called once after the baseline route, e.g. to auto-detect locked parts
    # from intermediate pipeline artifacts.
    after_baseline: Optional[Callable[["ProjectAdapter"], None]] = None


def load_adapter(path: Path) -> ProjectAdapter:
    spec = importlib.util.spec_from_file_location("placeloop_project", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load project adapter {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "make_adapter"):
        raise RuntimeError(f"{path} must define make_adapter()")
    return mod.make_adapter()
