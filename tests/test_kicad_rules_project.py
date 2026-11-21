import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kicad_rules import load_project_rules
from kicad_rules_fixtures import write_pro


def test_classes_and_patterns(tmp_path):
    pro = write_pro(tmp_path, classes=[
        {"name": "Default", "clearance": 0.15, "track_width": 0.15},
        {"name": "SGMII", "clearance": 0.2, "track_width": 0.1, "diff_pair_gap": 0.15, "diff_pair_width": 0.1},
        {"name": "PWR", "clearance": 0.2, "track_width": 0.5}],
        patterns=[{"netclass": "SGMII", "pattern": "/SGMII_*"}, {"netclass": "PWR", "pattern": "+*"}],
        assignments={"GND": "PWR"},
        rules={"min_clearance": 0.1})
    r = load_project_rules(pro)
    assert r.net_class("/SGMII_TX_P") == "SGMII"
    assert r.net_class("+3V3") == "PWR"
    assert r.net_class("GND") == "PWR"          # explicit assignment
    assert r.net_class("uart0_tx") == "Default"
    assert r.classes["SGMII"].diff_pair_gap == 0.15
    assert r.board_min["min_clearance"] == 0.1
    assert r.class_order == ["Default", "SGMII", "PWR"]


def test_regex_pattern(tmp_path):
    r = load_project_rules(write_pro(tmp_path, classes=[{"name": "Default"}, {"name": "MDI"}],
                                     patterns=[{"netclass": "MDI", "pattern": "P[0-3]_MDI_.*"}]))
    assert r.net_class("P2_MDI_A_P") == "MDI"


def test_multi_pattern_match_warns(tmp_path):
    r = load_project_rules(write_pro(tmp_path, classes=[{"name": "Default"}, {"name": "A"}, {"name": "B"}],
                                     patterns=[{"netclass": "A", "pattern": "X*"}, {"netclass": "B", "pattern": "XY*"}]))
    assert r.net_class("XY1") == "A"
    assert any("XY1" in w and "A" in w and "B" in w for w in r.warnings)
    n = len(r.warnings)
    r.net_class("XY1")
    assert len(r.warnings) == n  # warn once per net


def test_missing_file(tmp_path):
    r = load_project_rules(str(tmp_path / "none.kicad_pro"))
    assert r.classes == {} and r.warnings
    assert r.net_class("anything") == "Default"


def test_garbled_list_json(tmp_path):
    p = tmp_path / "x.kicad_pro"
    p.write_text("[]")
    r = load_project_rules(str(p))
    assert r.classes == {} and r.warnings
    assert r.net_class("a") == "Default"


def test_binary_file(tmp_path):
    p = tmp_path / "x.kicad_pro"
    p.write_bytes(b"\xff\xfe\x00\x80\x81binary")
    r = load_project_rules(str(p))
    assert r.classes == {} and r.warnings


def test_malformed_entries_warn(tmp_path):
    import json
    p = tmp_path / "x.kicad_pro"
    p.write_text(json.dumps({"net_settings": {
        "classes": ["bad", {"name": "Default", "clearance": True, "track_width": 0.2}],
        "netclass_patterns": [{"netclass": "X"}, "junk"],
        "netclass_assignments": ["nope"]}}))
    r = load_project_rules(str(p))
    assert r.classes["Default"].track_width == 0.2
    assert r.classes["Default"].clearance is None
    assert r.patterns == []
    assert len(r.warnings) >= 4
    p.write_text(json.dumps({"net_settings": 5, "board": []}))
    r = load_project_rules(str(p))
    assert r.warnings


def test_bad_class_names_warn(tmp_path):
    import json
    p = tmp_path / "x.kicad_pro"
    p.write_text(json.dumps({"net_settings": {
        "classes": [{"name": ["x"]}, {"name": {}}, {"name": None},
                    {"name": "Good", "track_width": 0.2}],
        "netclass_patterns": [{"pattern": "A*", "netclass": ["y"]},
                              {"pattern": 5, "netclass": "Good"}]}}))
    r = load_project_rules(str(p))
    assert list(r.classes) == ["Good"]
    assert r.patterns == []
    assert len(r.warnings) == 5
