"""Read routing constraints from KiCad's own project files.

Currently: net classes, pattern/explicit net assignments and Board Setup
minima from a .kicad_pro. All lengths are in mm.
"""
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


def kicad_wildcard_match(name: str, pattern: str) -> bool:
    """KiCad wildcard match: only '*' (any run) and '?' (one char) are special.

    Case-sensitive, full-string; unlike fnmatch, '[...]' is literal.
    """
    regex = ''.join('.*' if c == '*' else '.' if c == '?' else re.escape(c)
                    for c in pattern)
    return re.fullmatch(regex, name, re.DOTALL) is not None


def _pattern_matches(pattern: str, name: str) -> bool:
    if kicad_wildcard_match(name, pattern):
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


_MAX_COND_DEPTH = 200


class _CondParser:
    def __init__(self, toks):
        self.toks = toks
        self.i = 0
        self.uses_b = False
        self.uses_netname = False
        self.depth = 0

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
        self.depth += 1
        if self.depth > _MAX_COND_DEPTH:
            raise _DruError("condition too deeply nested")
        try:
            if self.peek() == ('op', '!'):
                self.i += 1
                return ('not', self.not_())
            return self.primary()
        finally:
            self.depth -= 1

    def primary(self):
        if self.peek() == ('op', '('):
            self.i += 1
            self.depth += 1
            if self.depth > _MAX_COND_DEPTH:
                raise _DruError("condition too deeply nested")
            node = self.or_()
            self.depth -= 1
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
    parser = None
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
    except (_DruError, RecursionError) as e:
        rule.supported = False
        rule.reason = (str(e) if isinstance(e, _DruError)
                       else "condition too deeply nested")
        rule._ast = None
        if parser is not None:
            rule.uses_b = parser.uses_b
            rule.uses_netname = parser.uses_netname
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
    return kicad_wildcard_match(value, pattern)


def eval_condition(rule: DruRule, a: ItemInfo, b: Optional[ItemInfo]) -> bool:
    """Whether the rule's condition holds for items a and b (b may be None).

    No condition matches everything; an unsupported rule matches nothing.
    """
    if not rule.supported:
        return False
    if rule._ast is None:
        return True
    try:
        return _eval_node(rule._ast, {'A': a, 'B': b if b is not None else ItemInfo("", "")})
    except RecursionError:
        return False


# ---------------------------------------------------------------------------
# Resolution: per-net parameters, pairwise clearance, routing groups
# ---------------------------------------------------------------------------

_CLI_KEYS = ('clearance', 'track_width', 'via_size', 'via_drill', 'diff_pair_gap')


@dataclass
class NetParams:
    net_class: str
    track_width: float
    via_size: float
    via_drill: float
    diff_pair_width: float
    diff_pair_gap: float
    skew_max: Optional[float] = None
    length_min: Optional[float] = None
    length_max: Optional[float] = None


def _netname_patterns(node, out):
    """Collect (side, pattern) of every A/B.NetName comparison in an AST."""
    if node is None:
        return out
    if node[0] in ('or', 'and'):
        _netname_patterns(node[1], out)
        _netname_patterns(node[2], out)
    elif node[0] == 'not':
        _netname_patterns(node[1], out)
    elif node[2] == 'NetName':
        out.append((node[1], node[3]))
    return out


class BoardRules:
    """Routing constraints resolved from a board's .kicad_pro and .kicad_dru."""

    def __init__(self, project: ProjectRules, dru_rules: List[DruRule],
                 cli_defaults: Dict[str, float]):
        self.project = project
        self.dru_rules = dru_rules
        self.cli = dict(cli_defaults)
        self._warn: List[str] = []
        self._warned = set()
        self.report_items: List[str] = []
        self.unsupported: List[Tuple[str, str]] = []
        self.drc_only: List[str] = []
        self._params_memo: Dict[str, NetParams] = {}
        self._sig_memo: Dict[str, Tuple] = {}
        self._clr_rules = []
        for rule in dru_rules:
            if not rule.supported:
                self.unsupported.append((rule.name, rule.reason))
                continue
            if any(k not in ROUTING_KINDS and k not in MATCH_KINDS
                   for k in rule.constraints):
                self.drc_only.append(rule.name)
            if rule.layer is None and rule.constraints.get('clearance') is not None \
                    and rule.constraints['clearance'].value() is not None:
                self._clr_rules.append(rule)
        if 'Default' not in project.classes:
            self._warn_once('default', "no 'Default' net class in project; "
                            "using command-line defaults for unset values")

    @property
    def warnings(self) -> List[str]:
        return self.project.warnings + self._warn

    def _warn_once(self, key, msg):
        if key not in self._warned:
            self._warned.add(key)
            self._warn.append(msg)

    @classmethod
    def load(cls, pcb_path: str, cli_defaults: Dict[str, float]) -> 'BoardRules':
        base = pcb_path.rsplit('.', 1)[0] if '.' in os.path.basename(pcb_path) else pcb_path
        return cls(load_project_rules(base + '.kicad_pro'),
                   load_dru(base + '.kicad_dru'), cli_defaults)

    # -- helpers ----------------------------------------------------------

    def _class_value(self, cls_name: str, attr: str) -> Optional[float]:
        classes = self.project.classes
        c = classes.get(cls_name)
        if c is None and cls_name != 'Default':
            self._warn_once('cls:' + cls_name,
                            f"net class '{cls_name}' is not defined in the project; "
                            "using Default values")
        v = getattr(c, attr, None) if c is not None else None
        if v is None:
            d = classes.get('Default')
            v = getattr(d, attr, None) if d is not None else None
        return v

    def _item(self, net: str) -> ItemInfo:
        return ItemInfo(net, self.project.net_class(net))

    # -- per-net parameters ----------------------------------------------

    def net_params(self, net_name: str) -> NetParams:
        if net_name in self._params_memo:
            return self._params_memo[net_name]
        item = self._item(net_name)
        cname = item.net_class

        found: Dict[str, Optional[float]] = {}
        found_con: Dict[str, DruConstraint] = {}
        for rule in self.dru_rules:
            if not rule.supported or rule.uses_b:
                continue
            if not any(k in ROUTING_KINDS or k in MATCH_KINDS for k in rule.constraints):
                continue
            if not eval_condition(rule, item, None):
                continue
            if rule.layer is not None:
                msg = (f"rule '{rule.name}' is restricted to layer '{rule.layer}'; "
                       "not applied to routing values")
                if msg not in self.report_items:
                    self.report_items.append(msg)
                continue
            for kind, con in rule.constraints.items():
                if kind in ROUTING_KINDS and con.value() is not None:
                    found[kind] = con.value()
                elif kind in MATCH_KINDS:
                    found_con[kind] = con

        def pick(kind, attr, cli_key):
            if kind in found:
                return found[kind]
            v = self._class_value(cname, attr)
            return v if v is not None else self.cli[cli_key]

        track = pick('track_width', 'track_width', 'track_width')
        dpw = self._class_value(cname, 'diff_pair_width')
        skew = found_con.get('skew')
        length = found_con.get('length')
        p = NetParams(
            net_class=cname,
            track_width=track,
            via_size=pick('via_diameter', 'via_diameter', 'via_size'),
            via_drill=pick('hole_size', 'via_drill', 'via_drill'),
            diff_pair_width=dpw if dpw is not None else track,
            diff_pair_gap=pick('diff_pair_gap', 'diff_pair_gap', 'diff_pair_gap'),
            skew_max=skew.max if skew is not None else None,
            length_min=length.min if length is not None else None,
            length_max=length.max if length is not None else None,
        )
        self._params_memo[net_name] = p
        return p

    # -- clearance --------------------------------------------------------

    def _class_clearance(self, cname: str) -> float:
        v = self._class_value(cname, 'clearance')
        return v if v is not None else self.cli['clearance']

    def clearance(self, net_a: str, net_b: str) -> float:
        a, b = self._item(net_a), self._item(net_b)
        result = max(self._class_clearance(a.net_class),
                     self._class_clearance(b.net_class))
        for rule in self._clr_rules:
            if rule.uses_b:
                hit = eval_condition(rule, a, b) or eval_condition(rule, b, a)
            else:
                hit = eval_condition(rule, a, None) or eval_condition(rule, b, None)
            if hit:
                result = rule.constraints['clearance'].value()
        return max(result, self.project.board_min.get('min_clearance', 0.0))

    # -- grouping ---------------------------------------------------------

    def signature(self, net_name: str) -> Tuple:
        """Nets with equal signatures resolve identically against any other net.

        Entries are (rule index, matched (side, pattern) pairs) for each supported
        rule whose condition names this net via A.NetName/B.NetName.
        """
        if net_name in self._sig_memo:
            return self._sig_memo[net_name]
        entries = []
        for i, rule in enumerate(self.dru_rules):
            if not rule.supported or not rule.uses_netname:
                continue
            hits = tuple(sorted(
                (side, pat) for side, pat in _netname_patterns(rule._ast, [])
                if kicad_wildcard_match(net_name, pat)))
            if hits:
                entries.append((i, hits))
        sig = (self.project.net_class(net_name), tuple(entries))
        self._sig_memo[net_name] = sig
        return sig

    def group_nets(self, net_names: List[str]) -> List[List[str]]:
        groups: Dict[Tuple, List[str]] = {}
        for n in net_names:
            groups.setdefault(self.signature(n), []).append(n)
        order = self.project.class_order

        def rank(sig):
            c = sig[0]
            if c == 'Default':
                return len(order) + 1
            return order.index(c) if c in order else len(order)

        # sorted() is stable: ties keep first-appearance order
        return [g for _, g in sorted(groups.items(), key=lambda kv: rank(kv[0]))]

    # -- report -----------------------------------------------------------

    def print_report(self, net_names: List[str]) -> None:
        print("KiCad rules:")
        by_class: Dict[str, List[str]] = {}
        for n in net_names:
            by_class.setdefault(self.project.net_class(n), []).append(n)
        for cname, nets in by_class.items():
            p = self.net_params(nets[0])
            print(f"  class {cname}: {len(nets)} nets, track {p.track_width:g}, "
                  f"via {p.via_size:g}/{p.via_drill:g}, "
                  f"diff pair {p.diff_pair_width:g}/{p.diff_pair_gap:g}, "
                  f"clearance {self._class_clearance(cname):g}")
        applied = [r.name for r in self.dru_rules if r.supported and any(
            k in ROUTING_KINDS or k in MATCH_KINDS for k in r.constraints)]
        if applied:
            print("  applied rules: " + ", ".join(applied))
        for item in self.report_items:
            print(f"  note: {item}")
        for name, reason in self.unsupported:
            print(f"  unsupported rule '{name}': {reason}")
        if self.drc_only:
            print("  DRC-only rules (not used for routing): " + ", ".join(self.drc_only))
        for w in self.warnings:
            print(f"  warning: {w}")
