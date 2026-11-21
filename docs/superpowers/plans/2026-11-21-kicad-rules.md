# KiCad Rules Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `route.py --kicad-rules` and `route_diff.py --kicad-rules` route every net with the width, via, diff-pair geometry and exact pairwise clearance defined in the board's `.kicad_pro` and `.kicad_dru`.

**Architecture:** A new pure-Python `kicad_rules.py` loads and resolves the rules. The engine gains one hook, `GridRouteConfig.obstacle_clearance_fn(net_id) -> clearance`, used wherever obstacle copper is stamped, which gives exact pairwise clearance with no Rust change. A new `kicad_rules_runner.py` groups nets that see identical rules and calls the existing `batch_route` / `batch_route_diff_pairs` once per group, chaining output files, so rip-up stays inside a group for free (earlier groups' copper is pre-existing and unrippable by default).

**Tech Stack:** Python 3.13, pytest, existing KiCadRoutingTools engine, `kicad-cli` 10.0.4 for end-to-end DRC.

**Spec:** `docs/superpowers/specs/2026-11-21-kicad-rules-design.md`

## Global Constraints

- No changes under `rust_router/`.
- Default behaviour without `--kicad-rules` must be byte-for-byte the same routing (existing tests pass unchanged).
- Explicit CLI flags win over rule files; every override is printed.
- `--kicad-rules` implies `--no-fix-drc-settings`; the output `.kicad_pro` gets the input project's rules unchanged.
- `min/opt/max` constraints: router uses `opt`, else `min`. `max`-only constraints are not used for routing values.
- KiCad precedence: in `.kicad_dru` a later matching rule overrides an earlier one; net-class clearance between two nets is the max of both classes; nothing below Board Setup `min_clearance`.
- Every unsupported rule or fallback is printed, never silently ignored.
- CLI only; the KiCad plugin is not touched.
- Work on branch `feat/kicad-rules`; invoke Python as `python3`.

## Review Focus

1. A net matching several `netclass_patterns` (KiCad 9 can merge classes): expect first match in file order and a printed warning naming the net and all matching classes. → test in Task 1.
2. `.kicad_dru` missing, empty, or containing only `(version 1)`: expect project-only rules, no crash. → test in Task 2.
3. Values with units (`0.2mm`, `8mil`, `0.01in`, bare `0.2`): expect conversion to mm. → test in Task 2.
4. A `.kicad_pro` with no `net_settings` (exactly `pcb_design_test` today: `"net_settings": {}`): expect Default class from Board Setup / CLI defaults plus a printed warning, routing still runs. → test in Task 3.
5. A pair whose P and N resolve to different classes: expect the pair routed with the stricter (larger) gap/width/clearance and a warning. → test in Task 6.

---

### Task 1: Load net classes and assignments from `.kicad_pro`

**Files:**
- Create: `kicad_rules.py`
- Test: `tests/test_kicad_rules_project.py`

**Interfaces:**
- Produces:
  - `@dataclass NetClass: name: str; clearance: Optional[float]; track_width: Optional[float]; via_diameter: Optional[float]; via_drill: Optional[float]; diff_pair_width: Optional[float]; diff_pair_gap: Optional[float]` (mm)
  - `@dataclass ProjectRules: classes: Dict[str, NetClass]; class_order: List[str]; patterns: List[Tuple[str, str]]  # (pattern, class) in file order; assignments: Dict[str, str]; board_min: Dict[str, float]  # design_settings.rules, e.g. "min_clearance"; warnings: List[str]`
  - `load_project_rules(pro_path: str) -> ProjectRules` (missing file → empty ProjectRules plus warning)
  - `ProjectRules.net_class(net_name: str) -> str`: explicit assignment → first pattern match → `"Default"`. Pattern match = `fnmatch.fnmatchcase(name, pattern)` or, if the pattern compiles as a regex, `re.fullmatch(pattern, name)`.

- [ ] **Step 0: Set up environment and branch**

```bash
cd KiCadRoutingTools && git switch -c feat/kicad-rules
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt pytest
python3 build_router.py
python3 -m pytest tests -q --ignore=tests/stress -p no:randomly 2>&1 | tail -40 > /tmp/claude-1000/-home-oni-work-project-s-autorouter/dd5c0a2f-dd8f-467e-937e-f0b13f048805/scratchpad/kicad_rules_baseline.txt
```
Expected: router builds or downloads; the full suite runs. Its pass/fail set in `/tmp/claude-1000/-home-oni-work-project-s-autorouter/dd5c0a2f-dd8f-467e-937e-f0b13f048805/scratchpad/kicad_rules_baseline.txt` is the regression baseline for Tasks 4 and 7 (pre-existing failures are not this work's to fix).

- [ ] **Step 1: Write failing tests** (write fixture `.kicad_pro` JSON into `tmp_path`)

```python
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

def test_missing_file(tmp_path):
    r = load_project_rules(str(tmp_path / "none.kicad_pro"))
    assert r.classes == {} and r.warnings
```

- [ ] **Step 2: Run, expect FAIL** — `python3 -m pytest tests/test_kicad_rules_project.py -v` → `ModuleNotFoundError: kicad_rules`.
- [ ] **Step 3: Implement `NetClass`, `ProjectRules`, `load_project_rules`, `net_class`** in `kicad_rules.py`. Warn once per net in `net_class` (memoise). Leave `list_nets.read_design_rules` as is.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat: load net classes and patterns from .kicad_pro`

---

### Task 2: Parse `.kicad_dru` and evaluate the condition subset

**Files:**
- Modify: `kicad_rules.py`
- Test: `tests/test_kicad_rules_dru.py`

**Interfaces:**
- Produces:
  - `@dataclass DruConstraint: kind: str; min: Optional[float]; opt: Optional[float]; max: Optional[float]` with `def value(self) -> Optional[float]` returning opt, else min.
  - `@dataclass DruRule: name: str; condition: str; constraints: Dict[str, DruConstraint]; layer: Optional[str]  # layer name, "outer", "inner" or None; supported: bool; reason: str  # why unsupported; uses_b: bool; uses_netname: bool`
  - `parse_dru(text: str) -> List[DruRule]` (file order kept)
  - `load_dru(path: str) -> List[DruRule]` (missing file → `[]`)
  - `eval_condition(rule: DruRule, a: ItemInfo, b: Optional[ItemInfo]) -> bool` where `@dataclass ItemInfo: net_name: str; net_class: str`
  - Module constants `ROUTING_KINDS = {"clearance", "track_width", "via_diameter", "hole_size", "diff_pair_gap"}`, `MATCH_KINDS = {"skew", "length"}`.

Grammar: a tiny s-expression reader (strings in `"..."`, `#` line comments). Units: `mm` (default), `mil` (×0.0254), `in` (×25.4), `um` (×0.001). Condition subset: `A.NetClass == 'x'`, `A.NetName == 'x'` (string `==`/`!=` with KiCad wildcards `*`/`?`, via `fnmatchcase`), `A.hasNetclass('x')`, same with `B.`, plus `&&`, `||`, `!`, parentheses. Any other token makes `supported=False` with `reason` naming the token. A rule with no condition matches everything.

- [ ] **Step 1: Write failing tests**

```python
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
```

- [ ] **Step 2: Run, expect FAIL** (`ImportError: parse_dru`).
- [ ] **Step 3: Implement** reader, `parse_dru`, `load_dru`, `eval_condition` (recursive-descent over a tokenised condition; never `eval()`).
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat: parse .kicad_dru rule subset`

---

### Task 3: Resolve per-net parameters, pairwise clearance and routing groups

**Files:**
- Modify: `kicad_rules.py`
- Test: `tests/test_kicad_rules_resolve.py`

**Interfaces:**
- Consumes: Task 1 `ProjectRules`, Task 2 `DruRule`, `eval_condition`, `ItemInfo`.
- Produces:
  - `@dataclass NetParams: net_class: str; track_width: float; via_size: float; via_drill: float; diff_pair_width: float; diff_pair_gap: float; skew_max: Optional[float]; length_min: Optional[float]; length_max: Optional[float]`
  - `class BoardRules` with:
    - `BoardRules.load(pcb_path: str, cli_defaults: Dict[str, float]) -> BoardRules`: reads sibling `.kicad_pro` and `.kicad_dru`. `cli_defaults` keys: `clearance, track_width, via_size, via_drill, diff_pair_gap`, used where neither file sets a value.
    - `net_params(net_name: str) -> NetParams`: precedence: last matching single-object (`not uses_b`) supported dru rule → net class → Default class → `cli_defaults`. Layer-restricted rules are skipped for routing values and added to `report_items`.
    - `clearance(net_a: str, net_b: str) -> float`: `max(class_clr(a), class_clr(b))`; then the last supported dru `clearance` rule matching (a,b) or (b,a) overrides; result `max(.., board_min.get("min_clearance", 0))`.
    - `signature(net_name: str) -> Tuple`: `(net_class, tuple of indices of supported dru rules whose condition mentions A.NetName and matches this net as A or B)`. Nets with equal signatures resolve identically against every other net.
    - `group_nets(net_names: List[str]) -> List[List[str]]`: groups by `signature`, ordered by the class's position in `class_order` with `Default` last; within a class, by first appearance. (Plan decision: the spec's "most constrained first" is realised by the order classes appear in `.kicad_pro`, which `constraints.py` controls; diff pairs are routed by `route_diff.py` before `route.py` anyway.)
    - `unsupported: List[Tuple[str, str]]` (rule name, reason); `drc_only: List[str]` (rule names with `diff_pair_uncoupled`, `via_count`, `edge_clearance`, `hole_clearance`, or other non-routing kinds); `warnings: List[str]`.
    - `print_report(net_names: List[str]) -> None`: classes, net count per class, resolved values per class, applied/unsupported/DRC-only rules, warnings.

- [ ] **Step 1: Write failing tests** (reuse Task 1 `write_pro` helper; write `.kicad_dru` next to a dummy `board.kicad_pcb` path)

```python
def test_precedence_dru_over_class(rules):      # fixture: classes from Task 1 test + DRU from Task 2
    p = rules.net_params("/SGMII_TX_P")
    assert p.net_class == "SGMII"
    assert p.track_width == pytest.approx(0.1016)   # dru opt 4mil beats class 0.1
    assert p.skew_max == pytest.approx(0.1)
    assert p.diff_pair_gap == 0.15

def test_clearance_class_max_and_pair_rule(rules_mdi):
    assert rules_mdi.clearance("ETH_A_P", "uart0_tx") == pytest.approx(0.2)   # max(MDI 0.2, Default 0.15)
    assert rules_mdi.clearance("SHIELD", "ETH_A_P") == pytest.approx(1.5)     # pair rule, either order

def test_board_min_floor(tmp_path):
    r = make_rules(tmp_path, classes=[{"name": "Default", "clearance": 0.05}], rules={"min_clearance": 0.1})
    assert r.clearance("a", "b") == pytest.approx(0.1)

def test_empty_net_settings_uses_cli_defaults(tmp_path):
    r = make_rules(tmp_path, classes=None)        # "net_settings": {}
    p = r.net_params("x")
    assert p.net_class == "Default" and p.track_width == CLI["track_width"]
    assert r.warnings

def test_groups_split_on_netname_rule(rules):
    groups = rules.group_nets(["/SGMII_TX_P", "/SGMII_TX_N", "uart0_tx", "+3V3"])
    assert groups[0] == ["/SGMII_TX_P", "/SGMII_TX_N"] and groups[-1] == ["uart0_tx"]

def test_unsupported_and_drc_only_listed(rules):
    assert [n for n, _ in rules.unsupported] == ["area"]
    assert rules.drc_only == []   # add a diff_pair_uncoupled rule to the fixture and assert its name appears
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement `NetParams`, `BoardRules`** as specified.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat: resolve per-net params and pairwise clearance`

---

### Task 4: Engine hook for pairwise clearance

**Files:**
- Modify: `routing_config.py` (`GridRouteConfig`, near line 131)
- Modify: `obstacle_map.py:34-153` (`build_base_obstacle_map`) and `obstacle_map.py:~1785` (`build_base_obstacle_map_with_vis`)
- Modify: `obstacle_cache.py:252` (`precompute_net_obstacles`)
- Modify: `route.py:241` (`batch_route`), `route_diff.py:102` (`batch_route_diff_pairs`): new kwarg `obstacle_clearance_fn=None`, stored on the config.
- Test: `tests/test_obstacle_clearance_fn.py`

**Interfaces:**
- Produces:
  - `GridRouteConfig.obstacle_clearance_fn: Optional[Callable[[int], float]] = None`
  - `GridRouteConfig.clearance_for_net(net_id: int) -> float`: `obstacle_clearance_fn(net_id)` if set, else `self.clearance`.
  - `batch_route(..., obstacle_clearance_fn=None)` and `batch_route_diff_pairs(..., obstacle_clearance_fn=None)`.

Change: in the three builders, every place that stamps copper belonging to net `nid` (segment, via, pad expansions and via-block radii) uses `config.clearance_for_net(nid)` in place of `config.clearance` / `effective_clearance`. When `obstacle_clearance_fn` is set, the `net_clearances` max-approximation is bypassed. Board-edge, drill-hole and keepout code is unchanged.

- [ ] **Step 1: Write failing tests** on a synthetic `PCBData` with two horizontal segments of nets 1 and 2 (width 0.2) and grid 0.05:

```python
def test_default_unchanged(pcb, config):
    a = build_base_obstacle_map(pcb, config, [])
    config.obstacle_clearance_fn = lambda nid: config.clearance
    b = build_base_obstacle_map(pcb, config, [])
    assert blocked_set(a) == blocked_set(b)

def test_per_net_expansion(pcb, config):
    config.obstacle_clearance_fn = lambda nid: {1: 0.1, 2: 0.5}[nid]
    m = build_base_obstacle_map(pcb, config, [])
    # keep-out half-width = seg_w/2 + clr + routed_w/2
    assert keepout_halfwidth_cells(m, net=1) == cells(0.1 + 0.1 + config.track_width / 2)
    assert keepout_halfwidth_cells(m, net=2) == cells(0.1 + 0.5 + config.track_width / 2)

def test_cache_uses_hook(pcb, config):
    config.obstacle_clearance_fn = lambda nid: 0.5
    wide = precompute_net_obstacles(pcb, 1, config)
    config.obstacle_clearance_fn = None
    assert len(wide.blocked_cells) > len(precompute_net_obstacles(pcb, 1, config).blocked_cells)
```

- [ ] **Step 2: Run, expect FAIL** (`AttributeError: obstacle_clearance_fn`).
- [ ] **Step 3: Implement** the field, `clearance_for_net`, the builder changes, and the `batch_route*` kwarg.
- [ ] **Step 4: Run new tests, expect PASS; then regression** — `python3 -m pytest tests -q -x --ignore=tests/stress --ignore=tests/gui_parity` → same pass/fail set as the Task 1 Step 0 baseline.
- [ ] **Step 5: Commit** `feat: per-net obstacle clearance hook`

---

### Task 5: `route.py --kicad-rules`

**Files:**
- Create: `kicad_rules_runner.py`
- Modify: `route.py` main (around line 2016): build the `batch_route` keyword arguments into a dict `route_kwargs`, then either `batch_route(**route_kwargs)` (unchanged path) or the runner; add `--kicad-rules` argument.
- Test: `tests/test_kicad_rules_route.py`

**Interfaces:**
- Consumes: `BoardRules` (Task 3), `batch_route(..., obstacle_clearance_fn=)` (Task 4).
- Produces:
  - `run_route_with_rules(input_file: str, output_file: str, net_names: List[str], route_kwargs: dict, explicit_flags: Set[str]) -> Tuple[int, int]` (total routed, total failed).
  - `explicit_flags` = argparse dests the user actually typed (detect by comparing against `parser.get_default`); those keys in `route_kwargs` are never overridden, and a line `override: <flag>=<value> (rules say <rule value>)` is printed.
  - Helper `chain_paths(output_file: str, n: int) -> List[str]`: intermediate files `<output>.rules_step<i>.kicad_pcb` in the output directory, last one = `output_file`; intermediates deleted at the end.

Behaviour per group `g` from `rules.group_nets(net_names)`: representative `r = g[0]`; `p = rules.net_params(r)`; call `batch_route(in_i, out_i, g, **route_kwargs | {track_width: p.track_width, via_size: p.via_size, via_drill: p.via_drill, clearance: rules.clearance(r, r), obstacle_clearance_fn: lambda nid: rules.clearance(r, net_name(nid)), rip_existing_nets: None})`. `route.py` then skips the DRC-settings writeback (implied `--no-fix-drc-settings`) and copies the input `.kicad_pro` to the output project path. Print `rules.print_report(net_names)` before routing and a per-group routed/failed summary after.

- [ ] **Step 1: Write failing tests** using `kicad_files/flat_hierarchy.kicad_pcb` copied to `tmp_path`, with a generated `.kicad_pro` (copy of the board's, `net_settings` replaced) defining classes `Default` (0.15/0.15), `WIDE` (track 0.3, clearance 0.25, pattern matching two chosen nets), and a `.kicad_dru` with one pairwise clearance rule `WIDE`↔`Default` of 0.3:

```python
def test_widths_follow_classes(routed):           # fixture runs route.py --kicad-rules via subprocess
    pcb = parse_kicad_pcb(routed)
    assert all(s.width == pytest.approx(0.3) for s in segs_of(pcb, WIDE_NETS))
    assert all(s.width == pytest.approx(0.15) for s in segs_of(pcb, OTHER_NETS))

def test_pairwise_clearance(routed):
    pcb = parse_kicad_pcb(routed)
    assert min_edge_gap(pcb, WIDE_NETS, OTHER_NETS) >= 0.3 - 0.01

def test_project_not_loosened(routed, input_pro):
    assert json.load(open(pro_of(routed)))["net_settings"] == json.load(open(input_pro))["net_settings"]

def test_explicit_flag_overrides(tmp_path):
    out = run_route(tmp_path, extra=["--track-width", "0.2"])
    assert "override: track_width=0.2" in out.stdout

def test_default_path_untouched():
    # without --kicad-rules route.py output counts equal a baseline run
    assert route_counts(flags=[]) == BASELINE_COUNTS
```

- [ ] **Step 2: Run, expect FAIL** (`unrecognized arguments: --kicad-rules`).
- [ ] **Step 3: Implement** `kicad_rules_runner.run_route_with_rules`, `chain_paths`, the `route.py` kwargs refactor and flag.
- [ ] **Step 4: Run, expect PASS; then** `python3 check_connected.py <routed>` and `kicad-cli pcb drc --refill-zones --format json -o drc.json <routed>` → no `clearance` or `track_width` violations involving routed nets.
- [ ] **Step 5: Commit** `feat: route.py --kicad-rules runs per rule group`

---

### Task 6: `route_diff.py --kicad-rules`

**Files:**
- Modify: `kicad_rules_runner.py`, `route_diff.py` main (around line 1392, same kwargs refactor) and flag.
- Test: `tests/test_kicad_rules_diff.py`

**Interfaces:**
- Consumes: Task 3, Task 4, `net_queries.find_differential_pairs(pcb_data, patterns) -> Dict[str, DiffPairNet]`, `net_queries.calculate_route_length(segments, vias, pcb_data)`.
- Produces: `run_diff_with_rules(input_file: str, output_file: str, net_names: List[str], diff_kwargs: dict, explicit_flags: Set[str]) -> Tuple[int, int]`.

Behaviour: find pairs from `net_names`; a pair's params = stricter of P and N (`max` of gap, width, clearance; warn when P and N classes differ). Group pairs by `(signature(P), signature(N))`. Per group call `batch_route_diff_pairs` with `diff_pair_gap=p.diff_pair_gap`, `track_width=p.diff_pair_width`, via sizes, `clearance`, `obstacle_clearance_fn` as in Task 5; if `p.skew_max` is set: `diff_pair_intra_match=True`, `length_match_tolerance=p.skew_max`. An explicit `--impedance` keeps its computed width; print the rule width it replaced. After all groups, print a length table per net with `length_min`/`length_max` and `PASS`/`FAIL`, then the DRC-only list.

- [ ] **Step 1: Write failing tests** on `kicad_files/esp_prog.kicad_pcb` (has a USB pair, see `tests/test_esp_prog_diff.py`), generated rules: class `USB` (dp gap 0.15, dp width 0.2, pattern on the pair), dru `skew (max 0.05mm)` and `length (max 1mm)`:

```python
def test_pair_geometry(routed):
    assert pair_gap(routed, "USB") == pytest.approx(0.15, abs=0.01)
    assert pair_widths(routed, "USB") == {pytest.approx(0.2)}

def test_skew_matched(routed):
    p, n = pair_lengths(routed, "USB")
    assert abs(p - n) <= 0.05 + 0.01

def test_length_report_fails_impossible_max(run):
    assert "FAIL" in run.stdout and "length" in run.stdout

def test_mixed_class_pair_warns(tmp_path):
    out = run_diff(tmp_path, pattern_only_on_p=True)
    assert "different classes" in out.stdout
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** `run_diff_with_rules` and the `route_diff.py` changes.
- [ ] **Step 4: Run, expect PASS; then** `kicad-cli pcb drc` on the output → no clearance/width/diff-pair-gap violations on the pair.
- [ ] **Step 5: Commit** `feat: route_diff.py --kicad-rules with skew and length report`

---

### Task 7: Parity gate, docs, full regression

**Files:**
- Modify: `tests/gui_parity/test_manifest_plan_parity.py` and `tests/gui_parity/test_cli_postpass_coverage.py` (register `--kicad-rules` as CLI-only, with a comment pointing at the spec's non-goals)
- Modify: `docs/configuration.md` (new section "Routing from KiCad rule files": flag, supported rule subset, precedence, what is DRC-only)
- Modify: `README.md` Command Reference (one line per script)

- [ ] **Step 1: Run parity tests, expect FAIL** naming `--kicad-rules`: `python3 -m pytest tests/gui_parity/test_manifest_plan_parity.py tests/gui_parity/test_cli_postpass_coverage.py -q`
- [ ] **Step 2: Register the flag as CLI-only; rerun, expect PASS.**
- [ ] **Step 3: Write the docs section.** `python3 tests/run_doc_examples.py` → PASS.
- [ ] **Step 4: Full regression** — `python3 -m pytest tests -q --ignore=tests/stress` → same pass/fail set as the Task 1 Step 0 baseline plus the new tests passing.
- [ ] **Step 5: Commit** `docs: kicad rules mode; register CLI-only flag`
