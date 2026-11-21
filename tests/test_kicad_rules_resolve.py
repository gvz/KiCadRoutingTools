import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kicad_rules import BoardRules, NetParams  # noqa: E402
from kicad_rules_fixtures import write_pro  # noqa: E402

CLI = {"clearance": 0.2, "track_width": 0.25, "via_size": 0.6, "via_drill": 0.3,
       "diff_pair_gap": 0.101}

DRU = '''(version 1)
(rule "sgmii" (condition "A.NetClass == 'SGMII'")
  (constraint skew (max 0.1mm)) (constraint track_width (min 0.09mm) (opt 4mil)))
(rule "iso" (condition "A.NetClass == 'MDI' && B.NetClass == 'CHASSIS'")
  (constraint clearance (min 1.5)))
(rule "area" (condition "A.insideArea('X')") (constraint clearance (min 1mm)))
(rule "outer" (layer outer) (condition "A.NetName == '/SGMII_*'") (constraint track_width (opt 0.12mm)))
(rule "uncoupled" (condition "A.NetClass == 'SGMII'") (constraint diff_pair_uncoupled (max 5mm)))
'''


def make_rules(tmp_path, classes, patterns=None, assignments=None, rules=None,
               dru=None):
    pro = write_pro(tmp_path, classes=classes, patterns=patterns,
                    assignments=assignments, rules=rules)
    if dru is not None:
        with open(os.path.join(str(tmp_path), "board.kicad_dru"), "w") as f:
            f.write(dru)
    return BoardRules.load(pro[:-len(".kicad_pro")] + ".kicad_pcb", CLI)


@pytest.fixture
def rules(tmp_path):
    classes = [
        {"name": "Default", "clearance": 0.15, "track_width": 0.2,
         "via_diameter": 0.6, "via_drill": 0.3},
        {"name": "SGMII", "clearance": 0.15, "track_width": 0.1,
         "diff_pair_gap": 0.15, "diff_pair_width": 0.1},
        {"name": "PWR", "clearance": 0.2, "track_width": 0.4},
    ]
    patterns = [{"pattern": "/SGMII_*", "netclass": "SGMII"},
                {"pattern": "+*", "netclass": "PWR"}]
    return make_rules(tmp_path, classes, patterns, dru=DRU)


@pytest.fixture
def rules_mdi(tmp_path):
    classes = [
        {"name": "Default", "clearance": 0.15},
        {"name": "MDI", "clearance": 0.2},
        {"name": "CHASSIS"},
    ]
    patterns = [{"pattern": "ETH_*", "netclass": "MDI"},
                {"pattern": "SHIELD", "netclass": "CHASSIS"}]
    return make_rules(tmp_path, classes, patterns, dru=DRU)


def test_precedence_dru_over_class(rules):
    p = rules.net_params("/SGMII_TX_P")
    assert isinstance(p, NetParams)
    assert p.net_class == "SGMII"
    assert p.track_width == pytest.approx(0.1016)   # dru opt 4mil beats class 0.1
    assert p.skew_max == pytest.approx(0.1)
    assert p.diff_pair_gap == 0.15
    assert p.diff_pair_width == 0.1
    assert any("outer" in s for s in rules.report_items)   # layer rule skipped


def test_fallback_chain(rules):
    p = rules.net_params("uart0_tx")
    assert p.track_width == 0.2 and p.via_size == 0.6 and p.via_drill == 0.3
    assert p.diff_pair_gap == CLI["diff_pair_gap"]
    assert p.diff_pair_width == 0.2          # falls back to track width
    p = rules.net_params("+3V3")             # PWR lacks via -> Default class
    assert p.track_width == 0.4 and p.via_size == 0.6
    assert p.skew_max is None and p.length_min is None and p.length_max is None


def test_clearance_class_max_and_pair_rule(rules_mdi):
    assert rules_mdi.clearance("ETH_A_P", "uart0_tx") == pytest.approx(0.2)
    assert rules_mdi.clearance("SHIELD", "ETH_A_P") == pytest.approx(1.5)
    assert rules_mdi.clearance("ETH_A_P", "SHIELD") == pytest.approx(1.5)


def test_single_object_clearance_rule_either_net(tmp_path):
    r = make_rules(tmp_path, [{"name": "Default", "clearance": 0.1}],
                   dru='(version 1)(rule "r" (condition "A.NetName == \'HV*\'") '
                       '(constraint clearance (min 0.8)))')
    assert r.clearance("x", "HV1") == pytest.approx(0.8)
    assert r.clearance("HV1", "x") == pytest.approx(0.8)
    assert r.clearance("x", "y") == pytest.approx(0.1)


def test_board_min_floor(tmp_path):
    r = make_rules(tmp_path, classes=[{"name": "Default", "clearance": 0.05}],
                   rules={"min_clearance": 0.1})
    assert r.clearance("a", "b") == pytest.approx(0.1)


def test_empty_net_settings_uses_cli_defaults(tmp_path):
    r = make_rules(tmp_path, classes=None)
    p = r.net_params("x")
    assert p.net_class == "Default" and p.track_width == CLI["track_width"]
    assert p.via_size == CLI["via_size"] and p.via_drill == CLI["via_drill"]
    assert r.clearance("x", "y") == CLI["clearance"]
    assert r.warnings


def test_missing_class_never_crashes(tmp_path):
    r = make_rules(tmp_path, [{"name": "Default", "track_width": 0.3}],
                   assignments={"n1": "Ghost"})
    p = r.net_params("n1")
    assert p.net_class == "Ghost" and p.track_width == 0.3
    assert any("Ghost" in w for w in r.warnings)
    assert r.clearance("n1", "n2") == CLI["clearance"]


def test_groups_split_on_netname_rule(rules):
    groups = rules.group_nets(["/SGMII_TX_P", "/SGMII_TX_N", "uart0_tx", "+3V3"])
    assert groups[0] == ["/SGMII_TX_P", "/SGMII_TX_N"] and groups[-1] == ["uart0_tx"]
    assert ["+3V3"] in groups


def test_groups_split_same_class_by_netname(tmp_path):
    r = make_rules(tmp_path, [{"name": "Default"}],
                   dru='(version 1)(rule "r" (condition "A.NetName == \'HV*\'") '
                       '(constraint clearance (min 0.8)))')
    groups = r.group_nets(["a", "HV1", "b", "HV2"])
    assert groups == [["a", "b"], ["HV1", "HV2"]]


def test_unsupported_and_drc_only_listed(rules):
    assert [n for n, _ in rules.unsupported] == ["area"]
    assert rules.drc_only == ["uncoupled"]


def test_print_report(rules, tmp_path, capsys):
    rules.print_report(["/SGMII_TX_P", "uart0_tx", "+3V3"])
    out = capsys.readouterr().out
    for s in ("SGMII", "PWR", "Default", "area", "uncoupled"):
        assert s in out
    r2 = make_rules(tmp_path, classes=None)
    r2.print_report(["x"])
    assert "warning" in capsys.readouterr().out.lower()



SKIP_DRU = '''(version 1)
(rule "lay2" (layer outer) (condition "A.NetClass == 'MDI' && B.NetClass == 'CHASSIS'")
  (constraint clearance (min 2)))
(rule "tw2" (condition "A.NetClass == 'MDI' && B.NetClass == 'CHASSIS'")
  (constraint track_width (opt 0.5)))
(rule "maxonly" (condition "A.NetClass == 'MDI'") (constraint clearance (max 3)))
(rule "good" (condition "A.NetClass == 'MDI'") (constraint clearance (min 0.4)))
'''


def test_skipped_rules_reported(tmp_path, capsys):
    r = make_rules(tmp_path, [{"name": "Default"}, {"name": "MDI"}],
                   dru=SKIP_DRU)
    for name in ("lay2", "tw2", "maxonly"):
        assert any(f"'{name}'" in i for i in r.report_items), name
    assert not any("'good'" in i for i in r.report_items)
    r.print_report([])
    out = capsys.readouterr().out
    for name in ("lay2", "tw2", "maxonly"):
        assert name in out
    applied = [l for l in out.splitlines() if "applied rules" in l][0]
    assert "good" in applied and "lay2" not in applied and "tw2" not in applied


def test_clearance_cached_by_signature(rules_mdi, monkeypatch):
    import kicad_rules
    first = rules_mdi.clearance("ETH_A_P", "SHIELD")
    calls = []
    real = kicad_rules.eval_condition
    monkeypatch.setattr(kicad_rules, "eval_condition",
                        lambda *a: calls.append(1) or real(*a))
    assert rules_mdi.clearance("ETH_A_P", "SHIELD") == first
    assert rules_mdi.clearance("SHIELD", "ETH_A_P") == first
    assert rules_mdi.clearance("ETH_B_N", "SHIELD") == first   # equal signature
    assert calls == []


def test_netparams_frozen(rules):
    with pytest.raises(Exception):
        rules.net_params("x").track_width = 1.0


def test_missing_cli_keys_filled(tmp_path):
    pro = write_pro(tmp_path, classes=None)
    r = BoardRules.load(pro[:-len(".kicad_pro")] + ".kicad_pcb", {})
    p = r.net_params("x")
    assert p.track_width > 0 and r.clearance("a", "b") > 0
    assert any("cli_defaults" in w for w in r.warnings)
