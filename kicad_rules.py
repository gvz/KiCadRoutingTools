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


# ---------------------------------------------------------------------------
# .kicad_dru (custom design rules)
# ---------------------------------------------------------------------------

ROUTING_KINDS = {"clearance", "track_width", "via_diameter", "hole_size", "diff_pair_gap"}
MATCH_KINDS = {"skew", "length"}

_UNIT_SCALE = {'mm': 1.0, 'mil': 0.0254, 'in': 25.4, 'um': 0.001}
_VALUE_RE = re.compile(r'^([+-]?(?:\d+\.?\d*|\.\d+))\s*(mm|mil|in|um)?$')


@dataclass
class DruConstraint:
    kind: str
    min: Optional[float] = None
    opt: Optional[float] = None
    max: Optional[float] = None

    def value(self) -> Optional[float]:
        """Value the router uses: opt, else min."""
        return self.opt if self.opt is not None else self.min


@dataclass
class ItemInfo:
    net_name: str
    net_class: str


@dataclass
class DruRule:
    name: str
    condition: str = ""
    constraints: Dict[str, DruConstraint] = field(default_factory=dict)
    layer: Optional[str] = None  # layer name, "outer", "inner" or None
    supported: bool = True
    reason: str = ""  # why unsupported
    uses_b: bool = False
    uses_netname: bool = False
    _ast: object = field(default=None, repr=False, compare=False)


class _DruError(Exception):
    pass


def _read_sexpr(text: str) -> list:
    """Read all top-level forms. Lenient: unbalanced input never raises."""
    stack: list = [[]]
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
        elif ch == '#':
            while i < n and text[i] != '\n':
                i += 1
        elif ch == '(':
            stack.append([])
            i += 1
        elif ch == ')':
            if len(stack) > 1:
                done = stack.pop()
                stack[-1].append(done)
            i += 1
        elif ch == '"':
            i += 1
            buf = []
            while i < n and text[i] != '"':
                if text[i] == '\\' and i + 1 < n:
                    i += 1
                buf.append(text[i])
                i += 1
            i += 1
            stack[-1].append(''.join(buf))
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '()"':
                j += 1
            stack[-1].append(text[i:j])
            i = j
    while len(stack) > 1:
        done = stack.pop()
        stack[-1].append(done)
    return stack[0]


def _parse_length(tok) -> float:
    m = _VALUE_RE.match(tok.strip()) if isinstance(tok, str) else None
    if not m:
        raise _DruError(f"bad value {tok!r}")
    return float(m.group(1)) * _UNIT_SCALE[m.group(2) or 'mm']


_COND_TOKEN_RE = re.compile(
    r"""\s*(?:(?P<str>'[^']*'|"[^"]*")|(?P<op>==|!=|&&|\|\||[!(),])|(?P<id>[A-Za-z_][A-Za-z0-9_.]*)|(?P<bad>\S))""")


def _tokenize_condition(cond: str):
    toks = []
    pos = 0
    while pos < len(cond):
        m = _COND_TOKEN_RE.match(cond, pos)
        if not m:
            break  # only trailing whitespace remains
        pos = m.end()
        if m.group('str') is not None:
            toks.append(('str', m.group('str')[1:-1]))
        elif m.group('op') is not None:
            toks.append(('op', m.group('op')))
        elif m.group('id') is not None:
            toks.append(('id', m.group('id')))
        else:
            raise _DruError(f"unsupported token '{m.group('bad')}'")
    return toks


class _CondParser:
    def __init__(self, toks):
        self.toks = toks
        self.i = 0
        self.uses_b = False
        self.uses_netname = False

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def take(self):
        tok = self.peek()
        if tok[0] is None:
            raise _DruError("unexpected end of condition")
        self.i += 1
        return tok

    def expect(self, op):
        tok = self.take()
        if tok != ('op', op):
            raise _DruError(f"expected '{op}' but found '{tok[1]}'")

    def parse(self):
        node = self.or_()
        if self.peek()[0] is not None:
            raise _DruError(f"unexpected token '{self.peek()[1]}'")
        return node

    def or_(self):
        node = self.and_()
        while self.peek() == ('op', '||'):
            self.i += 1
            node = ('or', node, self.and_())
        return node

    def and_(self):
        node = self.not_()
        while self.peek() == ('op', '&&'):
            self.i += 1
            node = ('and', node, self.not_())
        return node

    def not_(self):
        if self.peek() == ('op', '!'):
            self.i += 1
            return ('not', self.not_())
        return self.primary()

    def primary(self):
        if self.peek() == ('op', '('):
            self.i += 1
            node = self.or_()
            self.expect(')')
            return node
        kind, val = self.take()
        if kind != 'id':
            raise _DruError(f"unsupported token '{val}'")
        parts = val.split('.')
        if len(parts) != 2 or parts[0] not in ('A', 'B'):
            raise _DruError(f"unsupported token '{val}'")
        side, attr = parts
        if side == 'B':
            self.uses_b = True
        if attr in ('NetClass', 'NetName'):
            op = self.take()
            if op not in (('op', '=='), ('op', '!=')):
                raise _DruError(f"unsupported token '{op[1]}' after '{val}'")
            s = self.take()
            if s[0] != 'str':
                raise _DruError(f"unsupported token '{s[1]}' (expected a quoted string)")
            if attr == 'NetName':
                self.uses_netname = True
            node = ('cmp', side, attr, s[1])
            return node if op[1] == '==' else ('not', node)
        if attr == 'hasNetclass':
            self.expect('(')
            s = self.take()
            if s[0] != 'str':
                raise _DruError(f"unsupported token '{s[1]}' (expected a quoted string)")
            self.expect(')')
            return ('cmp', side, 'NetClass', s[1])
        raise _DruError(f"unsupported token '{val}'")


def _parse_rule(form: list) -> DruRule:
    name = form[1] if len(form) > 1 and isinstance(form[1], str) else ""
    rule = DruRule(name=name)
    try:
        if not name:
            raise _DruError("rule has no name")
        for item in form[2:]:
            if not isinstance(item, list) or not item or not isinstance(item[0], str):
                continue
            head = item[0]
            if head == 'condition':
                if len(item) < 2 or not isinstance(item[1], str):
                    raise _DruError("malformed condition")
                rule.condition = item[1]
            elif head == 'layer':
                if len(item) < 2 or not isinstance(item[1], str):
                    raise _DruError("malformed layer clause")
                rule.layer = item[1]
            elif head == 'constraint':
                if len(item) < 2 or not isinstance(item[1], str):
                    raise _DruError("malformed constraint")
                con = DruConstraint(kind=item[1])
                for sub in item[2:]:
                    if (isinstance(sub, list) and len(sub) == 2
                            and sub[0] in ('min', 'opt', 'max')
                            and isinstance(sub[1], str)):
                        setattr(con, sub[0], _parse_length(sub[1]))
                rule.constraints[con.kind] = con
        if rule.condition.strip():
            parser = _CondParser(_tokenize_condition(rule.condition))
            rule._ast = parser.parse()
            rule.uses_b = parser.uses_b
            rule.uses_netname = parser.uses_netname
    except _DruError as e:
        rule.supported = False
        rule.reason = str(e)
        rule._ast = None
    return rule


def parse_dru(text: str) -> List[DruRule]:
    """Parse .kicad_dru text into rules, in file order. Never raises."""
    try:
        forms = _read_sexpr(text)
    except Exception:  # defensive: parser must never take the router down
        return []
    rules = []
    for form in forms:
        if isinstance(form, list) and form and form[0] == 'rule':
            rules.append(_parse_rule(form))
    return rules


def load_dru(path: str) -> List[DruRule]:
    """Load rules from a .kicad_dru file; unreadable or missing file gives []."""
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except (OSError, ValueError):
        return []
    return parse_dru(text)


def _eval_node(node, items) -> bool:
    op = node[0]
    if op == 'or':
        return _eval_node(node[1], items) or _eval_node(node[2], items)
    if op == 'and':
        return _eval_node(node[1], items) and _eval_node(node[2], items)
    if op == 'not':
        return not _eval_node(node[1], items)
    _, side, attr, pattern = node
    item = items[side]
    value = item.net_class if attr == 'NetClass' else item.net_name
    return fnmatch.fnmatchcase(value, pattern)


def eval_condition(rule: DruRule, a: ItemInfo, b: Optional[ItemInfo]) -> bool:
    """Whether the rule's condition holds for items a and b (b may be None).

    No condition matches everything; an unsupported rule matches nothing.
    """
    if not rule.supported:
        return False
    if rule._ast is None:
        return True
    return _eval_node(rule._ast, {'A': a, 'B': b if b is not None else ItemInfo("", "")})
