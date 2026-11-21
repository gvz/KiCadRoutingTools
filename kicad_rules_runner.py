"""`route.py --kicad-rules`: route nets group by group with KiCad's own rules.

The board's sibling .kicad_pro / .kicad_dru (read by kicad_rules.BoardRules)
decide each group's track width, via size/drill and clearance. Groups are routed
one after another through a chain of intermediate boards; each group is one
batch_route call, so its obstacle map and per-net cache are built fresh with that
group's clearances. Earlier groups' copper is fixed (no rip-up across groups).
"""
import os
import shutil
from typing import Dict, List, Set, Tuple

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


def make_clearance_fn(rules: BoardRules, rep: str, names_by_id: Dict[int, str]):
    """Obstacle hook: clearance between group representative `rep` and net `nid`.

    Net 0 and ids not in the board resolve as name "" (the Default class).
    Memoised per net id; the hook runs once per stamped segment/via/pad.
    """
    memo: Dict[int, float] = {}

    def fn(nid: int) -> float:
        v = memo.get(nid)
        if v is None:
            name = names_by_id.get(nid, "") if nid else ""
            v = memo[nid] = rules.clearance(rep, name)
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
    from fab_tiers import enforce_fab_floors, count_copper_layers_in_file
    import routing_defaults as defaults

    cli_defaults = {k: route_kwargs.get(k) for k in _RULE_KEYS}
    cli_defaults['diff_pair_gap'] = route_kwargs.get('diff_pair_gap', defaults.DIFF_PAIR_GAP)
    rules = BoardRules.load(input_file, cli_defaults)
    rules.print_report(net_names)

    if route_kwargs.get('rip_existing_nets'):
        print("  note: --rip-existing-nets is ignored with --kicad-rules "
              "(rip-up stays inside each rule group)")

    groups = rules.group_nets(net_names)
    paths = chain_paths(output_file, len(groups))
    n_layers = count_copper_layers_in_file(input_file)
    summary = []
    total_ok = total_fail = 0
    in_i = input_file
    try:
        for gi, (group, out_i) in enumerate(zip(groups, paths), 1):
            rep = group[0]
            p = rules.net_params(rep)
            rule_vals = {'track_width': p.track_width, 'via_size': p.via_size,
                         'via_drill': p.via_drill, 'clearance': rules.clearance(rep, rep)}
            # The fab can't make sub-floor geometry; pin rule values like CLI ones.
            for k, floor in enforce_fab_floors(
                    n_layers, **{k: v for k, v in rule_vals.items()
                                 if k not in explicit_flags}).items():
                rule_vals[k] = floor
            kw = dict(route_kwargs)
            for k, v in rule_vals.items():
                if k in explicit_flags:
                    print(f"override: {k}={_fmt(route_kwargs[k])} (rules say {_fmt(v)})")
                else:
                    kw[k] = v
            if 'clearance' in explicit_flags:
                hook = None  # an explicit --clearance applies to every net pair
            else:
                hook = make_clearance_fn(rules, rep, _net_names_by_id(in_i))
            kw['obstacle_clearance_fn'] = hook
            kw['rip_existing_nets'] = None
            print(f"\n=== KiCad rules group {gi}/{len(groups)}: class {p.net_class}, "
                  f"{len(group)} nets, track {_fmt(kw['track_width'])}, "
                  f"via {_fmt(kw['via_size'])}/{_fmt(kw['via_drill'])}, "
                  f"clearance {_fmt(kw['clearance'])} ===")
            res = batch_route_fn(in_i, out_i, group, **kw)
            ok, fail = res[0], res[1]
            total_ok += ok
            total_fail += fail
            summary.append((p.net_class, len(group), ok, fail))
            in_i = out_i
    finally:
        for path in paths[:-1]:
            if os.path.abspath(path) != os.path.abspath(output_file):
                _remove_intermediate(path)

    if os.path.isfile(output_file):
        _copy_project_files(input_file, output_file)

    print("\nKiCad rules routing summary:")
    for gi, (cname, n, ok, fail) in enumerate(summary, 1):
        print(f"  group {gi}/{len(groups)} ({cname}, {n} nets): routed {ok}, failed {fail}")
    print(f"  total: routed {total_ok}, failed {total_fail}")
    return total_ok, total_fail
