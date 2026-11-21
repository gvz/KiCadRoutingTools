"""Read routing constraints from KiCad's own project files.

Currently: net classes, pattern/explicit net assignments and Board Setup
minima from a .kicad_pro. All lengths are in mm.
"""
import fnmatch
import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

_CLASS_FIELDS = ('clearance', 'track_width', 'via_diameter', 'via_drill',
                 'diff_pair_width', 'diff_pair_gap')


@dataclass
class NetClass:
    name: str
    clearance: Optional[float] = None
    track_width: Optional[float] = None
    via_diameter: Optional[float] = None
    via_drill: Optional[float] = None
    diff_pair_width: Optional[float] = None
    diff_pair_gap: Optional[float] = None


def _pattern_matches(pattern: str, name: str) -> bool:
    if fnmatch.fnmatchcase(name, pattern):
        return True
    try:
        return re.fullmatch(pattern, name) is not None
    except re.error:
        return False


@dataclass
class ProjectRules:
    classes: Dict[str, NetClass] = field(default_factory=dict)
    class_order: List[str] = field(default_factory=list)
    patterns: List[Tuple[str, str]] = field(default_factory=list)  # (pattern, class)
    assignments: Dict[str, str] = field(default_factory=dict)
    board_min: Dict[str, float] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    _memo: Dict[str, str] = field(default_factory=dict, init=False, repr=False, compare=False)

    def net_class(self, net_name: str) -> str:
        """Explicit assignment, else first matching pattern, else "Default"."""
        if net_name in self._memo:
            return self._memo[net_name]
        if net_name in self.assignments:
            result = self.assignments[net_name]
        else:
            matches = [cls for pat, cls in self.patterns if _pattern_matches(pat, net_name)]
            result = matches[0] if matches else "Default"
            distinct = list(dict.fromkeys(matches))
            if len(distinct) > 1:
                self.warnings.append(
                    f"net '{net_name}' matches multiple netclass patterns "
                    f"({', '.join(distinct)}); using '{result}'")
        self._memo[net_name] = result
        return result


def load_project_rules(pro_path: str) -> ProjectRules:
    """Load net classes, patterns, assignments and board minima from a .kicad_pro."""
    rules = ProjectRules()
    try:
        with open(pro_path, encoding='utf-8') as f:
            pro = json.load(f)
    except (OSError, ValueError) as e:
        rules.warnings.append(f"could not read project file '{pro_path}': {e}")
        return rules

    def num(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    def section(parent, key, kind):
        v = parent.get(key)
        if v is None:
            return kind()
        if not isinstance(v, kind):
            rules.warnings.append(
                f"'{key}' in '{pro_path}' is not a {kind.__name__}; skipped")
            return kind()
        return v

    if not isinstance(pro, dict):
        rules.warnings.append(
            f"project file '{pro_path}' is not a JSON object; ignored")
        return rules

    ns = section(pro, 'net_settings', dict)
    for c in section(ns, 'classes', list):
        if not isinstance(c, dict):
            rules.warnings.append(f"skipped malformed netclass entry {c!r}")
            continue
        name = c.get('name', 'Default')
        if not isinstance(name, str):
            rules.warnings.append(f"skipped netclass with invalid name {name!r}")
            continue
        vals = {k: c[k] for k in _CLASS_FIELDS if num(c.get(k))}
        rules.classes[name] = NetClass(name=name, **vals)
        if name not in rules.class_order:
            rules.class_order.append(name)

    for p in section(ns, 'netclass_patterns', list):
        if (isinstance(p, dict) and isinstance(p.get('pattern'), str)
                and isinstance(p.get('netclass'), str) and p['netclass']):
            rules.patterns.append((p['pattern'], p['netclass']))
        else:
            rules.warnings.append(f"skipped malformed netclass pattern entry {p!r}")

    na = section(ns, 'netclass_assignments', dict)
    for k, v in na.items():
        if isinstance(v, str):
            rules.assignments[k] = v
        else:
            rules.warnings.append(f"skipped malformed netclass assignment for '{k}'")

    board = section(pro, 'board', dict)
    ds = section(board, 'design_settings', dict)
    brules = section(ds, 'rules', dict)
    rules.board_min = {k: v for k, v in brules.items() if num(v)}
    return rules
