"""`route.py` / `route_diff.py --kicad-rules`: route group by group with KiCad's own rules.

The board's sibling .kicad_pro / .kicad_dru (read by kicad_rules.BoardRules)
decide each group's track width, via size/drill and clearance. Groups are routed
one after another through a chain of intermediate boards; each group is one
batch_route call, so its obstacle map and per-net cache are built fresh with that
group's clearances. Earlier groups' copper is fixed (no rip-up across groups).
"""
import os
import shutil
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from kicad_rules import BoardRules

# route_kwargs key -> NetParams attribute (clearance comes from rules.clearance)
_RULE_KEYS = ('track_width', 'via_size', 'via_drill', 'clearance')


def chain_paths(output_file: str, n: int) -> List[str]:
    """Output paths for n chained groups: intermediates, then output_file last.

    Intermediates are `<output>.rules_step<i>.kicad_pcb` in the output directory.
    """
    d = os.path.dirname(os.path.abspath(output_file))
    base = os.path.splitext(os.path.basename(output_file))[0]
    steps = [os.path.join(d, f"{base}.rules_step{i}.kicad_pcb") for i in range(1, n)]
    return steps + [output_file]


def _net_names_by_id(pcb_file: str) -> Dict[int, str]:
    from kicad_parser import parse_kicad_pcb
    pcb = parse_kicad_pcb(pcb_file)
    return {nid: (net.name or "") for nid, net in pcb.nets.items()}


def explicit_flags_from_argv(parser, dests: Sequence[str],
                             argv: Optional[Sequence[str]] = None) -> Set[str]:
    """Dests in `dests` whose option the user typed on the command line.

    Reads argv (default sys.argv[1:]) rather than comparing parsed values to
    defaults, so typing a flag's default value still counts as explicit. A token
    counts when it equals one of the action's option strings, starts with
    `<option>=`, or (with parser.allow_abbrev) is a `--` prefix of exactly one
    long option, which is that action's. Scanning stops at a bare `--`.
    """
    import sys
    if argv is None:
        argv = sys.argv[1:]
    actions = {a.dest: a for a in parser._actions if a.option_strings}
    long_opts = [o for a in parser._actions for o in a.option_strings
                 if o.startswith('--')]
    found = set()
    for tok in argv:
        if tok == '--':
            break
        if not tok.startswith('-'):
            continue
        head = tok.split('=', 1)[0]
        for dest in dests:
            act = actions.get(dest)
            if act is None:
                continue
            if head in act.option_strings:
                found.add(dest)
            elif (getattr(parser, 'allow_abbrev', True) and head.startswith('--')
                  and head not in long_opts):
                hits = [o for o in long_opts if o.startswith(head)]
                if len(hits) == 1 and hits[0] in act.option_strings:
                    found.add(dest)
    return found


def make_clearance_fn(rules: BoardRules, rep: str, names_by_id: Dict[int, str],
                      partners: Sequence[str] = (), floor: float = 0.0):
    """Obstacle hook: clearance between group representative `rep` and net `nid`.

    With `partners` (a diff pair's N net), the hook returns the larger of the
    clearances of `rep` and each partner to `nid`, so a pair whose halves sit in
    different classes keeps the stricter one. Net 0 and ids not in the board
    resolve as name "" (the Default class). Memoised per net id; the hook runs
    once per stamped segment/via/pad. Each value is raised to `floor` (the fab
    clearance floor _group_kwargs applies to the group clearance).
    """
    reps = (rep,) + tuple(x for x in partners if x != rep)
    memo: Dict[int, float] = {}

    def fn(nid: int) -> float:
        v = memo.get(nid)
        if v is None:
            name = names_by_id.get(nid, "") if nid else ""
            v = memo[nid] = max([floor] + [rules.clearance(r, name) for r in reps])
        return v
    return fn


def _copy_project_files(input_file: str, output_file: str) -> None:
    """Give the output the input's .kicad_pro and .kicad_dru unchanged."""
    in_base = os.path.splitext(input_file)[0]
    out_base = os.path.splitext(output_file)[0]
    for ext in ('.kicad_pro', '.kicad_dru'):
        src, dst = in_base + ext, out_base + ext
        if os.path.isfile(src) and os.path.abspath(src) != os.path.abspath(dst):
            shutil.copyfile(src, dst)
            print(f"  Copied {src} -> {dst}")


def _remove_intermediate(path: str) -> None:
    base = os.path.splitext(path)[0]
    for ext in ('.kicad_pcb', '.kicad_pro', '.kicad_prl', '.kicad_dru'):
        try:
            os.remove(base + ext)
        except OSError:
            pass


def _fmt(v) -> str:
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


# ---------------------------------------------------------------------------
# Shared machinery for route.py and route_diff.py
# ---------------------------------------------------------------------------

# Rule values the fab floor applies to (enforce_fab_floors' params).
_FLOOR_KEYS = ('track_width', 'via_size', 'via_drill', 'clearance')


def _load_rules(input_file: str, kwargs: dict, net_names: List[str]) -> BoardRules:
    """BoardRules from the input's sibling project files; prints the start report."""
    import routing_defaults as defaults
    cli_defaults = {k: kwargs.get(k) for k in _RULE_KEYS}
    cli_defaults['diff_pair_gap'] = kwargs.get('diff_pair_gap', defaults.DIFF_PAIR_GAP)
    rules = BoardRules.load(input_file, cli_defaults)
    rules.print_report(net_names)
    return rules


def _group_kwargs(base_kwargs: dict, rule_vals: dict, explicit_flags: Set[str],
                  n_layers: int) -> dict:
    """base_kwargs with rule_vals applied; explicit flags keep their CLI value.

    Rule geometry below the fab floor is pinned up like typed values (warned);
    each explicit override is printed.
    """
    from fab_tiers import enforce_fab_floors
    rule_vals = dict(rule_vals)
    for k, floor in enforce_fab_floors(
            n_layers, **{k: v for k, v in rule_vals.items()
                         if k in _FLOOR_KEYS and k not in explicit_flags}).items():
        rule_vals[k] = floor
    kw = dict(base_kwargs)
    for k, v in rule_vals.items():
        if k in explicit_flags:
            print(f"override: {k}={_fmt(base_kwargs[k])} (rules say {_fmt(v)})")
        else:
            kw[k] = v
    return kw


def _clearance_floor(n_layers: int) -> float:
    from fab_tiers import fab_floor_for_param
    return fab_floor_for_param('clearance', n_layers) or 0.0


def board_rule_vals(rules: BoardRules) -> dict:
    """batch kwargs taken from Board Setup minima: copper-to-edge and hole-to-hole."""
    bm = rules.project.board_min
    out = {}
    for key, kw in (('min_copper_edge_clearance', 'board_edge_clearance'),
                    ('min_hole_to_hole', 'hole_to_hole_clearance')):
        if bm.get(key, 0) > 0:
            out[kw] = bm[key]
    return out


def _hook_for(rules: BoardRules, rep: str, pcb_file: str, explicit_flags: Set[str],
              n_layers: int, partners: Sequence[str] = ()):
    if 'clearance' in explicit_flags:
        return None  # an explicit --clearance applies to every net pair
    return make_clearance_fn(rules, rep, _net_names_by_id(pcb_file), partners,
                             floor=_clearance_floor(n_layers))


def _run_chain(input_file: str, output_file: str, n_groups: int,
               route_group: Callable[[int, str, str], Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Run `route_group(index, in_file, out_file)` for each group through a chain
    of intermediate boards ending at output_file. Intermediates are removed and
    the input's .kicad_pro/.kicad_dru are copied beside the output."""
    if n_groups == 0:
        return []
    paths = chain_paths(output_file, n_groups)
    results = []
    in_i = input_file
    try:
        for gi, out_i in enumerate(paths):
            results.append(route_group(gi, in_i, out_i))
            in_i = out_i
    finally:
        for path in paths[:-1]:
            if os.path.abspath(path) != os.path.abspath(output_file):
                _remove_intermediate(path)
    if os.path.isfile(output_file):
        _copy_project_files(input_file, output_file)
    return results


def _print_summary(labels: List[Tuple[str, int]], results: List[Tuple[int, int]],
                   unit: str) -> Tuple[int, int]:
    print("\nKiCad rules routing summary:")
    n = len(labels)
    for gi, ((cname, count), (ok, fail)) in enumerate(zip(labels, results), 1):
        print(f"  group {gi}/{n} ({cname}, {count} {unit}): routed {ok}, failed {fail}")
    total_ok = sum(r[0] for r in results)
    total_fail = sum(r[1] for r in results)
    print(f"  total: routed {total_ok}, failed {total_fail}")
    return total_ok, total_fail


# ---------------------------------------------------------------------------
# route.py --kicad-rules
# ---------------------------------------------------------------------------

def run_route_with_rules(input_file: str, output_file: str, net_names: List[str],
                         route_kwargs: dict, explicit_flags: Set[str],
                         batch_route_fn=None) -> Tuple[int, int]:
    """Route `net_names` per KiCad rule group. Returns (total routed, total failed).

    `route_kwargs` are batch_route's keyword arguments (without input/output/nets).
    Keys in `explicit_flags` (argparse dests the user typed) keep their
    route_kwargs value over the rules; each such override is printed.
    `batch_route_fn` defaults to route.batch_route (route.py passes its own, so
    running as __main__ does not import route.py a second time).
    """
    if batch_route_fn is None:
        from route import batch_route as batch_route_fn
    from fab_tiers import count_copper_layers_in_file

    rules = _load_rules(input_file, route_kwargs, net_names)
    if route_kwargs.get('rip_existing_nets'):
        print("  note: --rip-existing-nets is ignored with --kicad-rules "
              "(rip-up stays inside each rule group)")

    groups = rules.group_nets(net_names)
    n_layers = count_copper_layers_in_file(input_file)
    labels = []

    def route_group(gi, in_i, out_i):
        group = groups[gi]
        rep = group[0]
        p = rules.net_params(rep)
        labels.append((p.net_class, len(group)))
        kw = _group_kwargs(route_kwargs,
                           {'track_width': p.track_width, 'via_size': p.via_size,
                            'via_drill': p.via_drill, 'clearance': rules.clearance(rep, rep),
                            **board_rule_vals(rules)},
                           explicit_flags, n_layers)
        kw['obstacle_clearance_fn'] = _hook_for(rules, rep, in_i, explicit_flags, n_layers)
        kw['rip_existing_nets'] = None
        print(f"\n=== KiCad rules group {gi + 1}/{len(groups)}: class {p.net_class}, "
              f"{len(group)} nets, track {_fmt(kw['track_width'])}, "
              f"via {_fmt(kw['via_size'])}/{_fmt(kw['via_drill'])}, "
              f"clearance {_fmt(kw['clearance'])} ===")
        res = batch_route_fn(in_i, out_i, group, **kw)
        return res[0], res[1]

    results = _run_chain(input_file, output_file, len(groups), route_group)
    totals = _print_summary(labels, results, "nets")
    windows = []
    for n in net_names:
        p = rules.net_params(n)
        if p.length_min is not None or p.length_max is not None:
            windows.append((n, p.length_min, p.length_max))
    _print_length_table(rules, output_file, windows, [], net_names)
    return totals


# ---------------------------------------------------------------------------
# route_diff.py --kicad-rules
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PairParams:
    """A diff pair's routing values: the stricter of its P and N nets."""
    label: str           # class name, "P/N" classes when they differ
    width: float
    gap: float
    via_size: float
    via_drill: float
    clearance: float
    skew_max: Optional[float]
    length_min: Optional[float]
    length_max: Optional[float]


def _opt(fn, values):
    vals = [v for v in values if v is not None]
    return fn(vals) if vals else None


def pair_params(rules: BoardRules, p_net: str, n_net: str, base: str = "") -> PairParams:
    """Stricter of P and N: max width/gap/via/clearance, min skew, the tighter
    length window (larger min, smaller max). Warns when the classes differ."""
    a, b = rules.net_params(p_net), rules.net_params(n_net)
    name = base or f"{p_net}/{n_net}"
    if a.net_class != b.net_class:
        print(f"  warning: pair {name}: P in class '{a.net_class}', N in class "
              f"'{b.net_class}' (different classes); using the stricter values of both")
    widths = []
    for x in (a, b):
        if x.diff_pair_width > 0:
            widths.append(x.diff_pair_width)
        else:
            print(f"  note: class '{x.net_class}' has no diff pair width; "
                  f"using its track width {x.track_width:g}")
            widths.append(x.track_width)
    gaps = []
    for x in (a, b):
        if x.diff_pair_gap > 0:
            gaps.append(x.diff_pair_gap)
        else:
            print(f"  note: class '{x.net_class}' has no diff pair gap; "
                  f"using --diff-pair-gap {rules.cli['diff_pair_gap']:g}")
            gaps.append(rules.cli['diff_pair_gap'])
    label = a.net_class if a.net_class == b.net_class else f"{a.net_class}/{b.net_class}"
    return PairParams(
        label=label, width=max(widths), gap=max(gaps),
        via_size=max(a.via_size, b.via_size), via_drill=max(a.via_drill, b.via_drill),
        clearance=max(rules.clearance(p_net, p_net), rules.clearance(n_net, n_net)),
        skew_max=_opt(min, (a.skew_max, b.skew_max)),
        length_min=_opt(max, (a.length_min, b.length_min)),
        length_max=_opt(min, (a.length_max, b.length_max)))


def warn_gap_below_clearance(rules: BoardRules, base: str, p_net: str, n_net: str,
                             gap: float) -> bool:
    """Warn (and return True) when the pair gap is below the P<->N clearance the
    rules require: KiCad's DRC then flags the coupled run as a clearance error."""
    clr = rules.clearance(p_net, n_net)
    if gap < clr - 1e-9:
        print(f"  warning: pair {base}: diff pair gap {gap:g} is below the "
              f"{p_net}<->{n_net} clearance {clr:g}; DRC will flag the coupled run. "
              f"Fix: raise the gap to at least {clr:g}, or add a clearance rule "
              f"for the pair (e.g. A.NetName/B.NetName on {p_net} and {n_net}).")
        return True
    return False


def _print_length_table(rules: BoardRules, output_file: str,
                        windows: List[Tuple[str, Optional[float], Optional[float]]],
                        pairs: List[Tuple[str, str, str, Optional[float]]],
                        routed_nets: Sequence[str]) -> None:
    """Measured on the final board: per-net routed length vs its length window,
    intra-pair skew per pair, the group skew of every skew rule without
    within_diff_pairs across the routed nets it matches; then the DRC-only list.

    `windows` is (net, length_min, length_max); `pairs` is (base, P, N, skew_max).
    Lengths count all copper on the net (stubs and via barrels included).
    """
    from kicad_parser import parse_kicad_pcb
    from net_queries import calculate_route_length
    print(f"\nLength check (KiCad length/skew rules) on {output_file}:")
    if not os.path.isfile(output_file):
        print("  (no output board; skipped)")
    else:
        pcb = parse_kicad_pcb(output_file)
        ids = {n.name: nid for nid, n in pcb.nets.items() if n.name}
        cache: Dict[str, float] = {}

        def length(name):
            if name not in cache:
                nid = ids.get(name)
                cache[name] = 0.0 if nid is None else calculate_route_length(
                    [s for s in pcb.segments if s.net_id == nid],
                    [v for v in pcb.vias if v.net_id == nid], pcb)
            return cache[name]

        def lim(v):
            return '-' if v is None else _fmt(v)

        if windows:
            w = max([len(n) for n, _, _ in windows] + [3])
            print(f"  {'net':<{w}} {'length':>9} {'min':>6} {'max':>6} result")
            for name, lmin, lmax in windows:
                L = length(name)
                ok = ((lmin is None or L >= lmin - 1e-6)
                      and (lmax is None or L <= lmax + 1e-6))
                print(f"  {name:<{w}} {L:9.3f} {lim(lmin):>6} "
                      f"{lim(lmax):>6} {'PASS' if ok else 'FAIL'}")
        else:
            print("  no length rules match the routed nets")
        for base, p_net, n_net, smax in pairs:
            if smax is not None:
                d = abs(length(p_net) - length(n_net))
                res = 'PASS' if d <= smax + 1e-6 else 'FAIL'
                print(f"  pair {base}: intra-pair skew {d:.3f} (max {_fmt(smax)}) {res}")
        for rule in rules.group_skew_rules():
            matched = [n for n in routed_nets if rules.rule_matches(rule, n)]
            if not matched:
                continue
            lens = [length(n) for n in matched]
            d = max(lens) - min(lens)
            smax = rule.constraints['skew'].max
            res = 'PASS' if d <= smax + 1e-6 else 'FAIL'
            print(f"  rule '{rule.name}': group skew across all matched nets "
                  f"({len(matched)} nets): {d:.3f} vs {_fmt(smax)} {res}")
    if rules.drc_only:
        print("  DRC-only rules (not used for routing): " + ", ".join(rules.drc_only))
    else:
        print("  no DRC-only rules")


def run_diff_with_rules(input_file: str, output_file: str, net_names: List[str],
                        diff_kwargs: dict, explicit_flags: Set[str],
                        batch_route_fn=None) -> Tuple[int, int]:
    """Route the diff pairs among `net_names` per KiCad rule group.

    Returns (total routed, total failed). Pairs are grouped by the rule
    signatures of their P and N nets; each group is one batch_route_diff_pairs
    call with the pair's stricter-of-P/N width, gap, vias and clearance, plus
    intra-pair matching to the skew limit when a skew rule applies. Keys in
    `explicit_flags` keep their diff_kwargs value (printed); an explicit
    --impedance keeps its computed width. After routing, prints a per-net length
    table against the length rules and the DRC-only rule list.
    """
    if batch_route_fn is None:
        from route_diff import batch_route_diff_pairs as batch_route_fn
    from fab_tiers import count_copper_layers_in_file
    from kicad_parser import parse_kicad_pcb
    from net_queries import find_differential_pairs

    rules = _load_rules(input_file, diff_kwargs, net_names)
    found = find_differential_pairs(parse_kicad_pcb(input_file), net_names)
    pairs = []  # (base, P, N, PairParams)
    for base, dp in found.items():
        if dp.is_complete:
            pairs.append((base, dp.p_net_name, dp.n_net_name,
                          pair_params(rules, dp.p_net_name, dp.n_net_name, base)))
    paired = {n for _, p, nn, _ in pairs for n in (p, nn)}
    for base, p_net, n_net, pp in pairs:
        gap = (diff_kwargs.get('diff_pair_gap') if 'diff_pair_gap' in explicit_flags
               else pp.gap)
        if gap is not None:
            warn_gap_below_clearance(rules, base, p_net, n_net, gap)
    loose = [n for n in net_names if n not in paired]
    if loose:
        print(f"  note: {len(loose)} net(s) are not in a P/N pair and are not "
              f"routed with --kicad-rules: {', '.join(loose[:5])}"
              f"{'...' if len(loose) > 5 else ''}")

    # Group by (signature(P), signature(N)); order groups like group_nets orders P.
    by_sig: Dict[Tuple, list] = {}
    for item in pairs:
        by_sig.setdefault((rules.signature(item[1]), rules.signature(item[2])), []).append(item)
    p_order = rules.group_nets([item[1] for item in pairs])
    rank = {n: i for i, g in enumerate(p_order) for n in g}
    groups = sorted(by_sig.values(), key=lambda g: rank[g[0][1]])

    n_layers = count_copper_layers_in_file(input_file)
    impedance = diff_kwargs.get('impedance')
    labels = []

    def route_group(gi, in_i, out_i):
        group = groups[gi]
        _, rep_p, rep_n, pp = group[0]
        labels.append((pp.label, len(group)))
        rule_vals = {'track_width': pp.width, 'diff_pair_gap': pp.gap,
                     'via_size': pp.via_size, 'via_drill': pp.via_drill,
                     'clearance': pp.clearance, **board_rule_vals(rules)}
        if pp.skew_max is not None:
            rule_vals['diff_pair_intra_match'] = True
            rule_vals['length_match_tolerance'] = pp.skew_max
        if impedance is not None:
            print(f"override: impedance={_fmt(impedance)} keeps its computed width "
                  f"(rules say {_fmt(pp.width)})")
        kw = _group_kwargs(diff_kwargs, rule_vals, explicit_flags, n_layers)
        kw['obstacle_clearance_fn'] = _hook_for(rules, rep_p, in_i, explicit_flags,
                                                n_layers, partners=(rep_n,))
        extra = ''
        if kw.get('diff_pair_intra_match'):
            extra = (f", diff_pair_intra_match=True, "
                     f"length_match_tolerance={_fmt(kw['length_match_tolerance'])}")
        print(f"\n=== KiCad rules group {gi + 1}/{len(groups)}: class {pp.label}, "
              f"{len(group)} pairs, track {_fmt(kw['track_width'])}, "
              f"gap {_fmt(kw['diff_pair_gap'])}, "
              f"via {_fmt(kw['via_size'])}/{_fmt(kw['via_drill'])}, "
              f"clearance {_fmt(kw['clearance'])}{extra} ===")
        nets = [n for _, p, nn, _ in group for n in (p, nn)]
        res = batch_route_fn(in_i, out_i, nets, **kw)
        return res[0], res[1]

    if not groups:
        print("  no complete diff pairs among the given nets; nothing to route")
        return 0, 0
    results = _run_chain(input_file, output_file, len(groups), route_group)
    totals = _print_summary(labels, results, "pairs")
    windows = [(n, pp.length_min, pp.length_max)
               for _, p, nn, pp in pairs for n in (p, nn)]
    _print_length_table(rules, output_file, windows,
                        [(base, p, nn, pp.skew_max) for base, p, nn, pp in pairs],
                        sorted(paired))
    return totals
