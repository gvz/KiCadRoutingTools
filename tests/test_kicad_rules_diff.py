"""End-to-end tests for `route_diff.py --kicad-rules` (diff pairs per KiCad rules).

Routes one LVDS pair (/fpga_adc/lvds_rx1_12) of kicad_files/routed_output.kicad_pcb
(the board of tests/test_diff_layer_costs_response.py, which routes this pair 1/1)
in a temp dir with a generated .kicad_pro (net classes Default and LVDS) and
.kicad_dru (skew max 0.05mm, length max 1mm on LVDS). Checks the pair's width and
gap follow the LVDS class, P/N skew is matched, the length table flags the
impossible 1mm maximum, mixed-class pairs warn, explicit flags win, and the
no-flag path still routes like the baseline.

Run: .venv/bin/python3 -m pytest tests/test_kicad_rules_diff.py -q
"""
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from kicad_parser import parse_kicad_pcb  # noqa: E402

BOARD = os.path.join(REPO, "kicad_files", "routed_output.kicad_pcb")
BOARD_PRO = os.path.join(REPO, "kicad_files", "routed_output.kicad_pro")
LIBDIR = "/nix/store/0ba5kj6r0zhfwd84l5vzccqvx7amlq2c-gcc-15.3.0-lib/lib"

P_NET = "/fpga_adc/lvds_rx1_12_P"
N_NET = "/fpga_adc/lvds_rx1_12_N"
PAIR_NETS = {"LVDS": (P_NET, N_NET)}
NET_ARGS = ["--nets", "*lvds_rx1_12*"]
LAYER_ARGS = ["--layers", "F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "B.Cu",
              "--proximity-heuristic-factor", "0.0"]
# The no-flag path uses test_diff_layer_costs_response.py's geometry (no --impedance).
PLAIN_GEOM = ["--track-width", "0.1", "--clearance", "0.1",
              "--via-size", "0.3", "--via-drill", "0.2"]

# Unmodified route_diff.py (before --kicad-rules existed) on this board with the
# board's own project, NET_ARGS + LAYER_ARGS + PLAIN_GEOM: JSON_SUMMARY counts.
BASELINE_COUNTS = (1, 0)

DRU_TEXT = """(version 1)
(rule "lvds_match"
  (condition "A.NetClass == 'LVDS'")
  (constraint skew (max 0.05mm))
  (constraint length (max 1mm)))
"""


def _env():
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = LIBDIR + (":" + env["LD_LIBRARY_PATH"]
                                       if env.get("LD_LIBRARY_PATH") else "")
    return env


def _make_project(dst_pro, pattern_only_on_p=False):
    with open(BOARD_PRO, encoding="utf-8") as f:
        pro = json.load(f)
    default = next(c for c in pro["net_settings"]["classes"] if c["name"] == "Default")
    default = dict(default, clearance=0.1, track_width=0.1, via_diameter=0.3,
                   via_drill=0.2, diff_pair_width=0.1, diff_pair_gap=0.1)
    lvds = dict(default, name="LVDS", track_width=0.2, diff_pair_width=0.2,
                diff_pair_gap=0.15, priority=0)
    nets = [P_NET] if pattern_only_on_p else [P_NET, N_NET]
    pro["net_settings"] = {
        "classes": [default, lvds],
        "meta": {"version": 4},
        "net_colors": None,
        "netclass_assignments": None,
        "netclass_patterns": [{"netclass": "LVDS", "pattern": n} for n in nets],
    }
    with open(dst_pro, "w", encoding="utf-8") as f:
        json.dump(pro, f, indent=2)


def _setup(d, rules=True, pattern_only_on_p=False):
    os.makedirs(d, exist_ok=True)
    pcb = os.path.join(d, "board.kicad_pcb")
    shutil.copyfile(BOARD, pcb)
    if rules:
        _make_project(os.path.join(d, "board.kicad_pro"), pattern_only_on_p)
        with open(os.path.join(d, "board.kicad_dru"), "w", encoding="utf-8") as f:
            f.write(DRU_TEXT)
    else:
        shutil.copyfile(BOARD_PRO, os.path.join(d, "board.kicad_pro"))
    return pcb


def run_diff(d, extra=(), rules=True, pattern_only_on_p=False):
    d = str(d)
    pcb = _setup(d, rules=rules, pattern_only_on_p=pattern_only_on_p)
    out = os.path.join(d, "out.kicad_pcb")
    cmd = [sys.executable, os.path.join(REPO, "route_diff.py"), pcb, out,
           *NET_ARGS, *LAYER_ARGS]
    if rules:
        cmd.append("--kicad-rules")
    cmd += list(extra)
    proc = subprocess.run(cmd, cwd=REPO, env=_env(), capture_output=True,
                          text=True, timeout=600)
    proc.out_file = out
    return proc


def _seg_key(s):
    return (s.layer, round(s.start_x, 4), round(s.start_y, 4),
            round(s.end_x, 4), round(s.end_y, 4))


def _new_body_segments(routed, net):
    """Segments the run added to `net`, minus the stub-to-pair connectors.

    The board carries pre-existing 0.1mm fan-out stubs at 0.2mm pitch; the
    engine joins them to the coupled pair with short connector segments that it
    necks to clear the partner (#318). Those touch a stub endpoint and are not
    the coupled body whose width/gap the rules set.
    """
    before = parse_kicad_pcb(BOARD)
    after = parse_kicad_pcb(routed)
    bid = next(i for i, n in before.nets.items() if n.name == net)
    aid = next(i for i, n in after.nets.items() if n.name == net)
    old = [s for s in before.segments if s.net_id == bid]
    old_keys = {_seg_key(s) for s in old}
    stub_pts = {(round(x, 3), round(y, 3)) for s in old
                for x, y in ((s.start_x, s.start_y), (s.end_x, s.end_y))}
    body = []
    for s in after.segments:
        if s.net_id != aid or _seg_key(s) in old_keys:
            continue
        ends = {(round(s.start_x, 3), round(s.start_y, 3)),
                (round(s.end_x, 3), round(s.end_y, 3))}
        if ends & stub_pts:
            continue
        body.append(s)
    return body


def pair_widths(routed, cls):
    p, n = PAIR_NETS[cls]
    segs = _new_body_segments(routed, p) + _new_body_segments(routed, n)
    assert segs
    return {round(s.width, 4) for s in segs}


def pair_gap(routed, cls):
    """Median edge-to-edge gap between parallel, overlapping P and N body segments."""
    p, n = PAIR_NETS[cls]
    ps, ns = _new_body_segments(routed, p), _new_body_segments(routed, n)
    gaps = []
    for a in ps:
        ax, ay = a.end_x - a.start_x, a.end_y - a.start_y
        la = math.hypot(ax, ay)
        if la < 0.2:
            continue
        ux, uy = ax / la, ay / la
        best = None
        for b in ns:
            if b.layer != a.layer:
                continue
            bx, by = b.end_x - b.start_x, b.end_y - b.start_y
            lb = math.hypot(bx, by)
            if lb < 1e-6 or abs(ux * by - uy * bx) / lb > 1e-3:
                continue  # not parallel
            t0 = (b.start_x - a.start_x) * ux + (b.start_y - a.start_y) * uy
            t1 = (b.end_x - a.start_x) * ux + (b.end_y - a.start_y) * uy
            if min(max(t0, t1), la) - max(min(t0, t1), 0.0) < 0.1:
                continue  # no overlap along the run
            perp = abs((b.start_x - a.start_x) * uy - (b.start_y - a.start_y) * ux)
            gap = perp - (a.width + b.width) / 2
            if best is None or gap < best:
                best = gap
        if best is not None and best < 1.0:
            gaps.append(best)
    assert gaps, "no coupled P/N segments found"
    return statistics.median(gaps)


def pair_lengths(routed, cls):
    from net_queries import calculate_route_length
    pcb = parse_kicad_pcb(routed)
    out = []
    for name in PAIR_NETS[cls]:
        nid = next(i for i, n in pcb.nets.items() if n.name == name)
        segs = [s for s in pcb.segments if s.net_id == nid]
        vias = [v for v in pcb.vias if v.net_id == nid]
        out.append(calculate_route_length(segs, vias, pcb))
    return tuple(out)


def _counts(proc):
    m = re.search(r"^JSON_SUMMARY: (.*)$", proc.stdout, re.M)
    s = json.loads(m.group(1))
    return (s["successful"], s["failed"])


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    d = tmp_path_factory.mktemp("rules_diff")
    proc = run_diff(d)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    proc.dir = str(d)
    return proc


@pytest.fixture(scope="module")
def routed(run):
    return run.out_file


def test_pair_geometry(routed):
    assert pair_gap(routed, "LVDS") == pytest.approx(0.15, abs=0.01)
    # (the brief's `== {pytest.approx(0.2)}` cannot run: approx is unhashable)
    widths = pair_widths(routed, "LVDS")
    assert len(widths) == 1 and widths.pop() == pytest.approx(0.2)


def test_skew_matched(routed):
    p, n = pair_lengths(routed, "LVDS")
    assert abs(p - n) <= 0.05 + 0.01


def test_length_report_fails_impossible_max(run):
    assert "FAIL" in run.stdout and "length" in run.stdout
    for net in (P_NET, N_NET):
        assert re.search(re.escape(net) + r"\s+[\d.]+\s+-\s+1\s+FAIL", run.stdout), net
    tail = run.stdout[run.stdout.index("Length check"):]
    assert "DRC-only rules" in tail or "no DRC-only rules" in tail


def test_rules_applied_and_summary(run):
    out = run.stdout
    assert "KiCad rules:" in out
    assert "class LVDS: 2 nets" in out
    assert re.search(r"group 1/1 \(LVDS, 1 pairs\): routed 1, failed 0", out)
    assert "diff_pair_intra_match=True" in out and "length_match_tolerance=0.05" in out


def test_project_not_loosened(run):
    out_pro = json.load(open(os.path.join(run.dir, "out.kicad_pro")))
    in_pro = json.load(open(os.path.join(run.dir, "board.kicad_pro")))
    assert out_pro["net_settings"] == in_pro["net_settings"]
    assert out_pro["board"]["design_settings"] == in_pro["board"]["design_settings"]
    assert open(os.path.join(run.dir, "out.kicad_dru")).read() == DRU_TEXT
    assert [f for f in os.listdir(run.dir) if "rules_step" in f] == []


def test_mixed_class_pair_warns(tmp_path):
    out = run_diff(tmp_path, pattern_only_on_p=True)
    assert out.returncode == 0, out.stdout[-3000:] + out.stderr[-3000:]
    assert "different classes" in out.stdout
    # stricter of P (LVDS) and N (Default): LVDS width and gap
    assert "track 0.2" in out.stdout and "gap 0.15" in out.stdout


def test_explicit_flags_override(tmp_path):
    out = run_diff(tmp_path, extra=["--length-match-tolerance", "0.2",
                                    "--impedance", "100"])
    assert out.returncode == 0, out.stdout[-3000:] + out.stderr[-3000:]
    assert "override: length_match_tolerance=0.2 (rules say 0.05)" in out.stdout
    assert "override: impedance=100 keeps its computed width (rules say 0.2)" in out.stdout


def test_default_path_untouched(tmp_path):
    proc = run_diff(tmp_path, extra=PLAIN_GEOM, rules=False)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    assert "KiCad rules" not in proc.stdout
    assert _counts(proc) == BASELINE_COUNTS


def test_gap_below_pair_clearance_warns(tmp_path, capsys):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from kicad_rules import BoardRules
    from kicad_rules_fixtures import write_pro
    from kicad_rules_runner import warn_gap_below_clearance
    pro = write_pro(tmp_path, classes=[{"name": "Default", "clearance": 0.15,
                                        "diff_pair_gap": 0.12}])
    r = BoardRules.load(pro[:-len(".kicad_pro")] + ".kicad_pcb", {})
    assert warn_gap_below_clearance(r, "D", "D_P", "D_N", 0.12) is True
    out = capsys.readouterr().out
    assert "0.12" in out and "0.15" in out and "D_P" in out and "D_N" in out
    assert "raise the gap" in out and "clearance rule" in out
    assert warn_gap_below_clearance(r, "D", "D_P", "D_N", 0.15) is False
    assert capsys.readouterr().out == ""


def test_intra_pair_skew_labelled(run):
    assert re.search(r"pair /fpga_adc/lvds_rx1_12: intra-pair skew [\d.]+ "
                     r"\(max 0\.05\) (PASS|FAIL)", run.stdout)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
