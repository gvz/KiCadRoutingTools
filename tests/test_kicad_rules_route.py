"""End-to-end tests for `route.py --kicad-rules` (route per KiCad rule group).

Routes a small net subset of kicad_files/flat_hierarchy.kicad_pcb in a temp dir
with a generated .kicad_pro (net classes Default/WIDE) and .kicad_dru (one
pairwise WIDE<->Default clearance rule), then checks widths, the pairwise
copper gap, the output project files, explicit-flag overrides and that the
no-flag path still routes like the baseline.

Run: .venv/bin/python3 -m pytest tests/test_kicad_rules_route.py -q
"""
import json
import math
import os
import re
import shutil
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from kicad_parser import parse_kicad_pcb  # noqa: E402

BOARD = os.path.join(REPO, "kicad_files", "flat_hierarchy.kicad_pcb")
BOARD_PRO = os.path.join(REPO, "kicad_files", "flat_hierarchy.kicad_pro")
LIBDIR = "/nix/store/0ba5kj6r0zhfwd84l5vzccqvx7amlq2c-gcc-15.3.0-lib/lib"

WIDE_NETS = ["CLOCK-RB6", "DATA-RB7"]
OTHER_NETS = ["/pic_programmer/PC-CLOCK-OUT", "/pic_programmer/PC-DATA-OUT",
              "/pic_programmer/PC-DATA-IN", "/pic_programmer/VPP_ON",
              "Net-(Q2-C)", "VPP-MCLR"]
ROUTE_NETS = WIDE_NETS + OTHER_NETS

# Unmodified route.py (before --kicad-rules existed) on this board with the
# board's own project and `--nets ROUTE_NETS`: JSON_SUMMARY successful/failed.
BASELINE_COUNTS = (8, 0)

DRU_TEXT = """(version 1)
(rule "wide_to_default"
  (condition "A.NetClass == 'WIDE' && B.NetClass == 'Default'")
  (constraint clearance (min 0.3mm)))
"""


def _env():
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = LIBDIR + (":" + env["LD_LIBRARY_PATH"]
                                       if env.get("LD_LIBRARY_PATH") else "")
    return env


def _make_project(dst_pro):
    with open(BOARD_PRO, encoding="utf-8") as f:
        pro = json.load(f)
    default = next(c for c in pro["net_settings"]["classes"] if c["name"] == "Default")
    default = dict(default, clearance=0.15, track_width=0.15)
    wide = dict(default, name="WIDE", clearance=0.25, track_width=0.3, priority=0)
    pro["net_settings"] = {
        "classes": [default, wide],
        "meta": {"version": 4},
        "net_colors": None,
        "netclass_assignments": None,
        "netclass_patterns": [{"netclass": "WIDE", "pattern": n} for n in WIDE_NETS],
    }
    # The board's Setup minima (0.2) sit above the Default class; lower them so
    # the classes, not the floor, decide (KiCad would flag 0.15 tracks otherwise).
    pro["board"]["design_settings"]["rules"].update(
        min_clearance=0.1, min_track_width=0.1)
    with open(dst_pro, "w", encoding="utf-8") as f:
        json.dump(pro, f, indent=2)


def _setup(d, rules=True):
    os.makedirs(d, exist_ok=True)
    pcb = os.path.join(d, "board.kicad_pcb")
    shutil.copyfile(BOARD, pcb)
    if rules:
        _make_project(os.path.join(d, "board.kicad_pro"))
        with open(os.path.join(d, "board.kicad_dru"), "w", encoding="utf-8") as f:
            f.write(DRU_TEXT)
    else:
        shutil.copyfile(BOARD_PRO, os.path.join(d, "board.kicad_pro"))
    return pcb


def run_route(d, extra=(), rules=True):
    d = str(d)
    pcb = _setup(d, rules=rules)
    out = os.path.join(d, "out.kicad_pcb")
    cmd = [sys.executable, os.path.join(REPO, "route.py"), pcb, out,
           "--nets", *ROUTE_NETS]
    if rules:
        cmd.append("--kicad-rules")
    cmd += list(extra)
    proc = subprocess.run(cmd, cwd=REPO, env=_env(), capture_output=True,
                          text=True, timeout=600)
    proc.out_file = out
    return proc


def route_counts(tmp, flags):
    proc = run_route(tmp, extra=flags, rules=False)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    m = re.search(r"^JSON_SUMMARY: (.*)$", proc.stdout, re.M)
    s = json.loads(m.group(1))
    return (s["successful"], s["failed"])


def pro_of(pcb_path):
    return os.path.splitext(pcb_path)[0] + ".kicad_pro"


def _names(pcb):
    return {nid: n.name for nid, n in pcb.nets.items()}


def segs_of(pcb, names):
    names = set(names)
    nm = _names(pcb)
    return [s for s in pcb.segments if nm.get(s.net_id) in names]


def _copper(pcb, names):
    """(layer, x1, y1, x2, y2, half_width) for segments and vias of `names`."""
    out = []
    for s in segs_of(pcb, names):
        out.append((s.layer, s.start_x, s.start_y, s.end_x, s.end_y, s.width / 2))
    names = set(names)
    nm = _names(pcb)
    for v in pcb.vias:
        if nm.get(v.net_id) in names:
            for layer in pcb.board_info.copper_layers:
                out.append((layer, v.x, v.y, v.x, v.y, v.size / 2))
    return out


def _pt_seg(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _seg_seg(a, b):
    (ax1, ay1, ax2, ay2), (bx1, by1, bx2, by2) = a, b

    def orient(px, py, qx, qy, rx, ry):
        return (qx - px) * (ry - py) - (qy - py) * (rx - px)
    d1 = orient(bx1, by1, bx2, by2, ax1, ay1)
    d2 = orient(bx1, by1, bx2, by2, ax2, ay2)
    d3 = orient(ax1, ay1, ax2, ay2, bx1, by1)
    d4 = orient(ax1, ay1, ax2, ay2, bx2, by2)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 and d2 and d3 and d4:
        return 0.0
    return min(_pt_seg(ax1, ay1, bx1, by1, bx2, by2), _pt_seg(ax2, ay2, bx1, by1, bx2, by2),
               _pt_seg(bx1, by1, ax1, ay1, ax2, ay2), _pt_seg(bx2, by2, ax1, ay1, ax2, ay2))


def min_edge_gap(pcb, nets_a, nets_b):
    """Smallest copper edge-to-edge gap between the two net sets, same layer."""
    best = math.inf
    for la, *ga, ha in _copper(pcb, nets_a):
        for lb, *gb, hb in _copper(pcb, nets_b):
            if la == lb:
                best = min(best, _seg_seg(ga, gb) - ha - hb)
    return best


@pytest.fixture(scope="module")
def routed_run(tmp_path_factory):
    d = tmp_path_factory.mktemp("rules")
    proc = run_route(d)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    return proc, str(d)


@pytest.fixture(scope="module")
def routed(routed_run):
    return routed_run[0].out_file


@pytest.fixture(scope="module")
def input_pro(routed_run):
    return os.path.join(routed_run[1], "board.kicad_pro")


def test_widths_follow_classes(routed):
    pcb = parse_kicad_pcb(routed)
    wide, other = segs_of(pcb, WIDE_NETS), segs_of(pcb, OTHER_NETS)
    assert wide and other
    assert all(s.width == pytest.approx(0.3) for s in wide)
    assert all(s.width == pytest.approx(0.15) for s in other)


def test_pairwise_clearance(routed):
    pcb = parse_kicad_pcb(routed)
    assert min_edge_gap(pcb, WIDE_NETS, OTHER_NETS) >= 0.3 - 0.01


def test_project_not_loosened(routed, input_pro):
    out_pro = json.load(open(pro_of(routed)))
    in_pro = json.load(open(input_pro))
    assert out_pro["net_settings"] == in_pro["net_settings"]
    assert out_pro["board"]["design_settings"] == in_pro["board"]["design_settings"]
    dru = os.path.splitext(routed)[0] + ".kicad_dru"
    assert open(dru).read() == DRU_TEXT


def test_report_and_group_summary(routed_run):
    out = routed_run[0].stdout
    assert "KiCad rules:" in out
    assert "class WIDE: 2 nets" in out
    assert re.search(r"group 1/2 \(WIDE.*routed 2, failed 0", out)


def test_intermediates_removed(routed_run):
    left = [f for f in os.listdir(routed_run[1]) if "rules_step" in f]
    assert left == []


def test_chain_paths():
    from kicad_rules_runner import chain_paths
    p = chain_paths("/x/out.kicad_pcb", 3)
    assert p == ["/x/out.rules_step1.kicad_pcb", "/x/out.rules_step2.kicad_pcb",
                 "/x/out.kicad_pcb"]
    assert chain_paths("/x/out.kicad_pcb", 1) == ["/x/out.kicad_pcb"]


def test_explicit_flag_overrides(tmp_path):
    out = run_route(tmp_path, extra=["--track-width", "0.2"])
    assert out.returncode == 0, out.stdout[-3000:] + out.stderr[-3000:]
    assert "override: track_width=0.2" in out.stdout
    assert "override: track_width=0.2 (rules say 0.3)" in out.stdout
    pcb = parse_kicad_pcb(out.out_file)
    segs = segs_of(pcb, ROUTE_NETS)
    assert segs and all(s.width == pytest.approx(0.2) for s in segs)


def test_default_path_untouched(tmp_path):
    # without --kicad-rules route.py output counts equal a baseline run
    assert route_counts(tmp_path, flags=[]) == BASELINE_COUNTS


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
