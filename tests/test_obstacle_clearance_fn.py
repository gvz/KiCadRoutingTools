"""Per-net obstacle clearance hook (GridRouteConfig.obstacle_clearance_fn).

The obstacle builders stamp each obstacle net's copper with
config.clearance_for_net(net_id): the hook's value when set, else the global
config.clearance (so default behaviour is unchanged).

Run:  python3 -m pytest tests/test_obstacle_clearance_fn.py -q
"""
import math
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from kicad_parser import PCBData, BoardInfo, Segment, Via
from routing_config import GridRouteConfig, GridCoord
from obstacle_map import (build_base_obstacle_map, build_base_obstacle_map_with_vis,
                          add_segments_list_as_obstacles, remove_segments_list_from_obstacles,
                          add_vias_list_as_obstacles, remove_vias_list_from_obstacles,
                          GridObstacleMap)
from obstacle_cache import precompute_net_obstacles

LAYERS = ["F.Cu", "B.Cu"]
GRID = 0.05
SEG_Y = {1: 5.0, 2: 15.0}
X0, X1 = 5.0, 10.0
# Scan window (mm) covering both segments plus generous keep-out margin.
WIN = (3.0, 2.0, 12.0, 18.0)


@pytest.fixture
def pcb():
    segs = [Segment(start_x=X0, start_y=y, end_x=X1, end_y=y, width=0.2,
                    layer="F.Cu", net_id=nid) for nid, y in SEG_Y.items()]
    info = BoardInfo(layers={0: "F.Cu", 31: "B.Cu"}, copper_layers=list(LAYERS))
    return PCBData(board_info=info, nets={}, footprints={}, vias=[],
                   segments=segs, pads_by_net={})


@pytest.fixture
def config():
    # track_width 0.15 puts both expected keep-out half-widths mid-cell
    # (5.5 and 13.5 cells), away from any rounding boundary.
    return GridRouteConfig(layers=list(LAYERS), grid_step=GRID, track_width=0.15,
                           clearance=0.1, via_size=0.3, via_drill=0.2)


def cells(mm):
    return int(math.floor(mm / GRID + 1e-9))


def blocked_set(m):
    coord = GridCoord(GRID)
    lo = coord.to_grid(WIN[0], WIN[1])
    hi = coord.to_grid(WIN[2], WIN[3])
    out = set()
    for gx in range(lo[0], hi[0] + 1):
        for gy in range(lo[1], hi[1] + 1):
            if m.is_via_blocked(gx, gy):
                out.add((gx, gy, "via"))
            for li in range(len(LAYERS)):
                if m.is_blocked(gx, gy, li):
                    out.add((gx, gy, li))
    return out


def keepout_halfwidth_cells(m, net):
    """Track keep-out half-width (cells) on F.Cu at the segment's midpoint."""
    coord = GridCoord(GRID)
    gx, gy0 = coord.to_grid((X0 + X1) / 2, SEG_Y[net])
    k = 0
    while m.is_blocked(gx, gy0 + k + 1, 0):
        k += 1
    return k


def test_default_unchanged(pcb, config):
    a = build_base_obstacle_map(pcb, config, [])
    config.obstacle_clearance_fn = lambda nid: config.clearance
    b = build_base_obstacle_map(pcb, config, [])
    assert blocked_set(a) == blocked_set(b)


def test_clearance_for_net(config):
    assert config.obstacle_clearance_fn is None
    assert config.clearance_for_net(7) == config.clearance
    config.obstacle_clearance_fn = lambda nid: 0.3 + nid
    assert config.clearance_for_net(1) == pytest.approx(1.3)


def test_per_net_expansion(pcb, config):
    config.obstacle_clearance_fn = lambda nid: {1: 0.1, 2: 0.5}[nid]
    m = build_base_obstacle_map(pcb, config, [])
    # keep-out half-width = seg_w/2 + clr + routed_w/2
    assert keepout_halfwidth_cells(m, net=1) == cells(0.1 + 0.1 + config.track_width / 2)
    assert keepout_halfwidth_cells(m, net=2) == cells(0.1 + 0.5 + config.track_width / 2)


def test_per_net_expansion_with_vis(pcb, config):
    config.obstacle_clearance_fn = lambda nid: {1: 0.1, 2: 0.5}[nid]
    m, _vis = build_base_obstacle_map_with_vis(pcb, config, [])
    assert keepout_halfwidth_cells(m, net=1) == cells(0.1 + 0.1 + config.track_width / 2)
    assert keepout_halfwidth_cells(m, net=2) == cells(0.1 + 0.5 + config.track_width / 2)


def test_hook_bypasses_net_clearances(pcb, config):
    # Without the hook, net_clearances max-approximation inflates every net.
    m0 = build_base_obstacle_map(pcb, config, [], net_clearances={1: 0.5, 2: 0.5})
    assert keepout_halfwidth_cells(m0, net=1) == cells(0.1 + 0.5 + config.track_width / 2)
    # With the hook, the hook wins and net_clearances is ignored.
    config.obstacle_clearance_fn = lambda nid: 0.1
    m = build_base_obstacle_map(pcb, config, [], net_clearances={1: 0.5, 2: 0.5})
    assert keepout_halfwidth_cells(m, net=1) == cells(0.1 + 0.1 + config.track_width / 2)


def test_cache_uses_hook(pcb, config):
    config.obstacle_clearance_fn = lambda nid: 0.5
    wide = precompute_net_obstacles(pcb, 1, config)
    config.obstacle_clearance_fn = None
    assert len(wide.blocked_cells) > len(precompute_net_obstacles(pcb, 1, config).blocked_cells)


def test_cache_default_unchanged(pcb, config):
    a = precompute_net_obstacles(pcb, 2, config)
    config.obstacle_clearance_fn = lambda nid: config.clearance
    b = precompute_net_obstacles(pcb, 2, config)
    assert (a.blocked_cells == b.blocked_cells).all()
    assert (a.blocked_vias == b.blocked_vias).all()


def test_list_helpers_use_hook_and_stay_symmetric(pcb, config):
    via = Via(x=8.0, y=10.0, size=0.3, drill=0.2, layers=["F.Cu", "B.Cu"], net_id=2)
    config.obstacle_clearance_fn = lambda nid: {1: 0.1, 2: 0.5}[nid]
    m = GridObstacleMap(len(LAYERS))
    add_segments_list_as_obstacles(m, pcb.segments, config)
    add_vias_list_as_obstacles(m, [via], config)
    assert keepout_halfwidth_cells(m, net=2) == cells(0.1 + 0.5 + config.track_width / 2)
    remove_segments_list_from_obstacles(m, pcb.segments, config)
    remove_vias_list_from_obstacles(m, [via], config)
    assert m.get_stats()[0] == 0 and m.get_stats()[1] == 0
