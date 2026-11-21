import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from kicad_rules import parse_dru, load_dru, eval_condition, ItemInfo  # noqa: E402

DRU = '''(version 1)
# comment
(rule "sgmii" (condition "A.NetClass == 'SGMII'")
  (constraint skew (max 0.1mm)) (constraint track_width (min 0.09mm) (opt 4mil)))
(rule "iso" (condition "A.NetClass == 'MDI' && B.NetClass == 'CHASSIS'")
  (constraint clearance (min 1.5)))
(rule "area" (condition "A.insideArea('X')") (constraint clearance (min 1mm)))
(rule "outer" (layer outer) (condition "A.NetName == '/SGMII_*'") (constraint track_width (opt 0.12mm)))'''


def test_parse_values_and_units():
    rules = parse_dru(DRU)
    assert [r.name for r in rules] == ["sgmii", "iso", "area", "outer"]
    assert rules[0].constraints["skew"].max == pytest.approx(0.1)
    assert rules[0].constraints["track_width"].value() == pytest.approx(0.1016)
    assert rules[3].layer == "outer"


def test_unsupported_condition():
    r = parse_dru(DRU)[2]
    assert not r.supported and "insideArea" in r.reason


def test_eval_pairwise():
    iso = parse_dru(DRU)[1]
    assert iso.uses_b
    assert eval_condition(iso, ItemInfo("ETH_A_P", "MDI"), ItemInfo("SHIELD", "CHASSIS"))
    assert not eval_condition(iso, ItemInfo("ETH_A_P", "MDI"), ItemInfo("x", "Default"))


def test_eval_wildcard_and_not():
    r = parse_dru('(version 1)(rule "r" (condition "!(A.NetName == \'+*\') || A.hasNetclass(\'PWR\')") (constraint clearance (min 0.3)))')[0]
    assert eval_condition(r, ItemInfo("sig", "Default"), None)
    assert not eval_condition(r, ItemInfo("+3V3", "Default"), None)
    assert eval_condition(r, ItemInfo("+3V3", "PWR"), None)


def test_empty_and_missing(tmp_path):
    assert parse_dru("(version 1)") == []
    assert load_dru(str(tmp_path / "x.kicad_dru")) == []


def test_malformed_never_raises():
    for text in ['(rule', '(rule "a" (condition "A.NetClass ==") (constraint clearance (min 1)))',
                 '(rule "b" (constraint clearance (min abc)))', ')))', '"unterminated',
                 '(rule (condition 3))']:
        for r in parse_dru(text):
            assert isinstance(r.supported, bool)
    r = parse_dru('(rule "a" (condition "A.NetClass ==") (constraint clearance (min 1)))')[0]
    assert not r.supported and r.reason
    r = parse_dru('(rule "b" (constraint clearance (min abc)))')[0]
    assert not r.supported and "abc" in r.reason


def test_no_condition_matches_all_and_double_quotes():
    r = parse_dru('(rule "n" (constraint clearance (min 0.2)))')[0]
    assert r.supported and eval_condition(r, ItemInfo("a", "b"), None)
    r = parse_dru('(rule "d" (condition "A.NetClass != \\"X\\"") (constraint clearance (min 0.2)))')[0]
    assert eval_condition(r, ItemInfo("a", "Y"), None)
    assert not eval_condition(r, ItemInfo("a", "X"), None)


def test_units():
    r = parse_dru('(rule "u" (constraint clearance (min 10mil) (opt 1in) (max 500um)))')[0]
    c = r.constraints["clearance"]
    assert (c.min, c.opt, c.max) == pytest.approx((0.254, 25.4, 0.5))
