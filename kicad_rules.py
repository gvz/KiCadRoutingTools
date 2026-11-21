"""Read routing constraints from KiCad's own project files.

Currently: net classes, pattern/explicit net assignments and Board Setup
minima from a .kicad_pro. All lengths are in mm.
"""
import fnmatch
import json
import os
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
    _memo: Dict[str, str] = field(default_factory=dict, repr=False, compare=False)

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
    except (OSError, json.JSONDecodeError) as e:
        rules.warnings.append(f"could not read project file '{pro_path}': {e}")
        return rules

    ns = pro.get('net_settings') or {}
    for c in ns.get('classes') or []:
        name = c.get('name', 'Default')
        vals = {k: c[k] for k in _CLASS_FIELDS if isinstance(c.get(k), (int, float))}
        rules.classes[name] = NetClass(name=name, **vals)
        if name not in rules.class_order:
            rules.class_order.append(name)

    for p in ns.get('netclass_patterns') or []:
        if p.get('pattern') is not None and p.get('netclass'):
            rules.patterns.append((p['pattern'], p['netclass']))

    na = ns.get('netclass_assignments') or {}
    if isinstance(na, dict):
        rules.assignments = {k: v for k, v in na.items() if isinstance(v, str)}

    board = ((pro.get('board') or {}).get('design_settings') or {}).get('rules') or {}
    rules.board_min = {k: v for k, v in board.items()
                       if isinstance(v, (int, float)) and not isinstance(v, bool)}
    return rules
