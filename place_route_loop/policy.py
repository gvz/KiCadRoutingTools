"""Classical move policy: turn connectivity failures into ranked, legal moves.

Three move families per broken net:
  pull   - move a part owning a disconnected pad toward the rest of its net
  push   - move a part sitting in the corridor between a disconnected pad and
           its nearest same-net pad sideways, out of the way
  rotate - rotate a small part owning a disconnected pad

Candidates are ranked by the change in half-perimeter wirelength (HPWL) of the
nets the move touches, then filtered for legality until ``k`` remain.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Dict, Iterable, List, Set, Tuple

from .board import Move, Placement

PULL_STEPS = (0.5, 1.0, 2.0)
PUSH_STEPS = (0.75, 1.5)
GRID = 0.25


def _snap(v: float) -> float:
    return round(v / GRID) * GRID


def net_index(pl: Placement) -> Dict[str, List[Tuple[str, float, float]]]:
    idx: Dict[str, List[Tuple[str, float, float]]] = defaultdict(list)
    for ref in pl.refs():
        for _num, net, x, y in pl.pads(ref):
            if net and not net.startswith("unconnected-"):
                idx[net].append((ref, x, y))
    return idx


def hpwl(points: Iterable[Tuple[float, float]]) -> float:
    pts = list(points)
    if len(pts) < 2:
        return 0.0
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (max(xs) - min(xs)) + (max(ys) - min(ys))


# Nets with this many pads or more (GND, rails) are ignored for HPWL ranking:
# they are planes/pours and dominate the metric without meaning much.
BIG_NET = 25


def move_cost(pl: Placement, move: Move) -> float:
    """HPWL delta over all non-plane nets touched by the move's group."""
    group = pl.group(move.ref)
    nets: Set[str] = set()
    for r in group:
        nets |= {n for _, n, _, _ in pl.pads(r) if n}

    def total() -> float:
        idx = net_index_subset(pl, nets)
        return sum(hpwl((x, y) for _, x, y in pts) for n, pts in idx.items() if len(pts) < BIG_NET)

    before = total()
    pl.apply(move)
    try:
        after = total()
    finally:
        pl.undo(move)
    return after - before


def net_index_subset(pl: Placement, nets: Set[str]) -> Dict[str, List[Tuple[str, float, float]]]:
    idx: Dict[str, List[Tuple[str, float, float]]] = defaultdict(list)
    for ref in pl.refs():
        for _num, net, x, y in pl.pads(ref):
            if net in nets:
                idx[net].append((ref, x, y))
    return idx


def _seg_hits_rect(p: Tuple[float, float], q: Tuple[float, float], r, pad: float) -> bool:
    """Coarse test: does segment pq pass through rect r (expanded by pad)? Sampled."""
    x0, y0, x1, y1 = r[0] - pad, r[1] - pad, r[2] + pad, r[3] + pad
    n = max(2, int(math.dist(p, q) / 0.2))
    for i in range(n + 1):
        t = i / n
        x = p[0] + (q[0] - p[0]) * t
        y = p[1] + (q[1] - p[1]) * t
        if x0 <= x <= x1 and y0 <= y <= y1:
            return True
    return False


def candidates(pl: Placement, issues: List[Dict]) -> List[Move]:
    a = pl.adapter
    idx = net_index(pl)
    out: Dict[Tuple, Move] = {}

    def add(m: Move) -> None:
        ref = a.canonical_ref(m.ref)
        if ref != m.ref:
            # Canonicalize: a twin's failure moves the primary by the same vector.
            m = Move(ref, m.dx, m.dy, m.drot, m.reason)
        if ref in a.locked or not a.is_movable(ref):
            return
        if abs(m.dx) < 1e-6 and abs(m.dy) < 1e-6 and not m.drot:
            return
        out.setdefault(m.key(), m)

    courtyards = {r: pl.courtyard(r) for r in pl.refs()}

    for iss in issues:
        net = iss["net_name"]
        pts = idx.get(net, [])
        if len(pts) < 2:
            continue
        disc = iss["disconnected_pads"] or [(x, y, "", r) for r, x, y in pts]
        for (dxp, dyp, _layer, dref) in disc:
            others = [(r, x, y) for r, x, y in pts if r != dref]
            if not others:
                continue
            # ---- pull toward the centroid of the rest of the net
            cx = sum(x for _, x, _ in others) / len(others)
            cy = sum(y for _, _, y in others) / len(others)
            vx, vy = cx - dxp, cy - dyp
            d = math.hypot(vx, vy)
            if d > 1e-6:
                for s in PULL_STEPS:
                    s = min(s, d)
                    add(Move(dref, _snap(vx / d * s), _snap(vy / d * s), 0.0, f"pull {net}"))
            # ---- rotate small parts
            if len(pl.pads(dref)) <= 4:
                for rot in (90.0, 180.0, 270.0):
                    add(Move(dref, 0.0, 0.0, rot, f"rotate {net}"))
            # ---- push corridor blockers sideways
            near = min(others, key=lambda o: math.dist((o[1], o[2]), (dxp, dyp)))
            p, q = (dxp, dyp), (near[1], near[2])
            L = math.dist(p, q)
            if L < 1e-6:
                continue
            nx, ny = -(q[1] - p[1]) / L, (q[0] - p[0]) / L  # unit normal
            for ref, cyd in courtyards.items():
                if ref in (dref, near[0]):
                    continue
                if not _seg_hits_rect(p, q, cyd, 0.3):
                    continue
                # push away from the corridor: pick the normal side the part is on
                mx, my = (cyd[0] + cyd[2]) / 2, (cyd[1] + cyd[3]) / 2
                sgn = 1.0 if (mx - p[0]) * nx + (my - p[1]) * ny >= 0 else -1.0
                for s in PUSH_STEPS:
                    add(Move(ref, _snap(sgn * nx * s), _snap(sgn * ny * s), 0.0, f"push from {net}"))
    return list(out.values())


def ranked_legal(pl: Placement, cands: List[Move], tabu: Set[Tuple], k: int) -> List[Tuple[float, Move]]:
    scored = []
    for m in cands:
        if m.key() in tabu:
            continue
        scored.append((move_cost(pl, m), m))
    # pull/push first (they target the failure directly), cheaper HPWL first
    scored.sort(key=lambda cm: (cm[1].reason.startswith("rotate"), cm[0]))
    legal: List[Tuple[float, Move]] = []
    for c, m in scored:
        if pl.legality(m) is None:
            legal.append((c, m))
            if len(legal) >= k:
                break
    return legal
