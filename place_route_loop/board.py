"""pcbnew-backed placement state: read positions, apply moves, check legality."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple

import pcbnew

from .project import ProjectAdapter, Rect

NM = 1e6  # nm per mm


def mm(v: float) -> int:
    return int(round(v * NM))


@dataclass(frozen=True)
class Move:
    ref: str
    dx: float = 0.0      # mm
    dy: float = 0.0      # mm
    drot: float = 0.0    # degrees CCW
    reason: str = ""

    def key(self) -> Tuple[str, float, float, float]:
        return (self.ref, round(self.dx, 2), round(self.dy, 2), round(self.drot % 360, 1))

    def to_dict(self) -> Dict:
        return asdict(self)


class Placement:
    """A loaded board plus the adapter's constraints."""

    def __init__(self, path: Path, adapter: ProjectAdapter):
        self.path = Path(path)
        self.adapter = adapter
        self.board = pcbnew.LoadBoard(str(path))
        self._rule_areas = [
            (z.GetZoneName() or f"zone{i}", z)
            for i, z in enumerate(self.board.Zones()) if z.GetIsRuleArea()
        ]
        e = self.board.GetBoardEdgesBoundingBox()
        self.board_rect: Rect = (e.GetLeft() / NM, e.GetTop() / NM, e.GetRight() / NM, e.GetBottom() / NM)

    # ---------------------------------------------------------------- queries
    def fp(self, ref: str):
        f = self.board.FindFootprintByReference(ref)
        if f is None:
            raise KeyError(ref)
        return f

    def refs(self) -> List[str]:
        return [f.GetReference() for f in self.board.GetFootprints()]

    def pose(self, ref: str) -> Tuple[float, float, float]:
        f = self.fp(ref)
        p = f.GetPosition()
        return (p.x / NM, p.y / NM, f.GetOrientationDegrees())

    def courtyard(self, ref: str) -> Rect:
        f = self.fp(ref)
        layer = pcbnew.F_CrtYd if f.GetLayer() == pcbnew.F_Cu else pcbnew.B_CrtYd
        poly = f.GetCourtyard(layer)
        if poly.OutlineCount() > 0:
            bb = poly.BBox()
        else:  # no courtyard drawn: fall back to the body bbox without text
            bb = f.GetBoundingBox(False)
        return (bb.GetLeft() / NM, bb.GetTop() / NM, bb.GetRight() / NM, bb.GetBottom() / NM)

    def side(self, ref: str) -> int:
        return 0 if self.fp(ref).GetLayer() == pcbnew.F_Cu else 1

    def pads(self, ref: str) -> List[Tuple[str, str, float, float]]:
        """(pad number, net name, x, y) for every pad."""
        out = []
        for p in self.fp(ref).Pads():
            c = p.GetPosition()
            out.append((p.GetNumber(), p.GetNetname(), c.x / NM, c.y / NM))
        return out

    def rule_area_signature(self, ref: str) -> FrozenSet[Tuple[str, str]]:
        """Set of (pad, rule-area name) memberships; moves must preserve it."""
        sig = set()
        for p in self.fp(ref).Pads():
            c = p.GetPosition()
            for name, z in self._rule_areas:
                if z.Outline().Contains(c):
                    sig.add((p.GetNumber(), name))
        return frozenset(sig)

    # ---------------------------------------------------------------- moves
    def _apply_one(self, ref: str, dx: float, dy: float, drot: float) -> None:
        f = self.fp(ref)
        p = f.GetPosition()
        f.SetPosition(pcbnew.VECTOR2I(p.x + mm(dx), p.y + mm(dy)))
        if drot:
            f.Rotate(f.GetPosition(), pcbnew.EDA_ANGLE(drot, pcbnew.DEGREES_T))

    def group(self, ref: str) -> List[str]:
        """The ref plus its twin (if any): everything one Move changes."""
        twin = self.adapter.twin_of(ref)
        return [ref] + ([twin] if twin else [])

    def apply(self, move: Move) -> None:
        for r in self.group(move.ref):
            self._apply_one(r, move.dx, move.dy, move.drot)

    def undo(self, move: Move) -> None:
        for r in self.group(move.ref):
            if move.drot:
                self._apply_one(r, 0, 0, -move.drot)
            self._apply_one(r, -move.dx, -move.dy, 0)

    def legality(self, move: Move) -> Optional[str]:
        """None if the move is legal, else a short reason. Leaves the board unchanged."""
        a = self.adapter
        group = self.group(move.ref)
        if any(r in a.locked for r in group):
            return "locked"
        before_sig = {r: self.rule_area_signature(r) for r in group}
        self.apply(move)
        try:
            gap = a.courtyard_gap
            moved = set(group)
            for r in group:
                cy = self.courtyard(r)
                region = a.region_of(r) or self.board_rect
                if cy[0] < region[0] or cy[1] < region[1] or cy[2] > region[2] or cy[3] > region[3]:
                    return f"{r} leaves region"
                if self.rule_area_signature(r) != before_sig[r]:
                    return f"{r} changes rule-area membership"
                s = self.side(r)
                for other in self.refs():
                    if other in moved or self.side(other) != s:
                        continue
                    o = self.courtyard(other)
                    if cy[0] < o[2] + gap and o[0] < cy[2] + gap and cy[1] < o[3] + gap and o[1] < cy[3] + gap:
                        return f"{r} overlaps {other}"
            return None
        finally:
            self.undo(move)

    def save(self, path: Path) -> None:
        self.board.Save(str(path))

    def snapshot(self) -> Dict[str, Tuple[float, float, float]]:
        return {r: self.pose(r) for r in self.refs()}
