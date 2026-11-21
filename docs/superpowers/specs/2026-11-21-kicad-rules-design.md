# Design: route from the board's own KiCad constraint files

Date: 2026-11-21 · Status: draft, awaiting approval

## Intent

The user generates a board's constraints from one Python source
(`constraints.py` in the circuit-synth project) into KiCad's own files:
net classes and class assignment patterns in `.kicad_pro`, advanced rules in
`.kicad_dru`. Today KiCadRoutingTools ignores both and takes every value from
CLI flags, so the constraints would have to be written twice and could drift.
Goal: `route.py` / `route_diff.py` read the KiCad files and route each net with
the width, clearance, via and diff-pair geometry KiCad's DRC will later check
it against, so a routed board passes `kicad-cli pcb drc` without hand-tuned
flags.

## Goals

1. Read `.kicad_pro` net classes (clearance, track width, via diameter/drill,
   diff-pair width/gap), `netclass_patterns` (wildcard and regex), explicit
   `netclass_assignments`, and Board Setup minima.
2. Read the common subset of `.kicad_dru` (see "Rule subset").
3. Resolve, per net: track width, via size/drill, diff-pair width/gap; and per
   pair of nets: required clearance, with KiCad precedence.
4. **Exact clearance per net pair**, not the current "largest clearance applies
   to everyone" approximation (`obstacle_map.py:58`).
5. Map skew / length rules onto the existing length-matching machinery.
6. List every rule the router does not enforce, so the user knows DRC is the
   only check for it.
7. In rules mode, never loosen the project's rules after routing.

## Non-goals

- KiCad plugin (GUI) support. CLI only; this is a local fork. (Repo
  `CLAUDE.md` asks for GUI parity — knowingly skipped; see Risks.)
- Rust changes. Not needed (see Approach).
- The full KiCad rule expression language (`insideArea()`, `memberOf()`,
  footprint/pad conditions, `A.Type`, …). Unsupported rules are reported,
  not guessed.
- Region/area-specific rules, uncoupled-length limits, via-count limits as
  routing constraints (reported as DRC-only).
- Declaring diff pairs from rules. Pairs are still found by net name.

## Anti-goals

- Silently routing a net with a value different from what the rules say.
  Every fallback (unsupported condition, missing class) must be printed.
- Making the default (no flag) behaviour change in any way.

## Approaches considered

1. **Rules reader + per-class passes, approximate clearance** — Python only,
   clearance between nets of different classes approximated.
2. **Rules reader + exact per-net-pair clearance** — *chosen by the user.*
   Originally assumed to need a Rust core change. Code reading showed it does
   not: the Rust grid stores only blocked cells; the clearance is baked in on
   the Python side when each net's copper is stamped
   (`precompute_net_obstacles`, `obstacle_cache.py:252`, expansion =
   `obstacle_width/2 + clearance + routed_width/2`). Making that expansion use
   `clearance(routed_net, obstacle_net)` gives exact pairwise clearance with no
   Rust change, no crate bump and no new prebuilt binaries.
3. **No router change; `constraints.py` emits CLI flags.** Rejected: duplicates
   the constraints, and still can't express pairwise clearance.

## Design

### New module `kicad_rules.py`

- `load_board_rules(pcb_path) -> BoardRules`: reads sibling `.kicad_pro` and
  `.kicad_dru`. Reuses/absorbs `list_nets.read_design_rules` (extend it with
  `netclass_patterns`; keep `list_nets --design-rules` output unchanged).
- `BoardRules.net_class(net_name)`: explicit assignment → first matching
  pattern (KiCad order) → `Default`.
- `BoardRules.net_params(net_name, layer=None)`: track width, via size/drill,
  diff-pair width/gap. Precedence: matching `.kicad_dru` rule (later rule in
  file wins, as in KiCad) → net class → Default class → CLI value.
  For `min/opt/max` constraints the router uses `opt`, else `min`.
- `BoardRules.clearance(net_a, net_b, layer=None)`: KiCad semantics —
  max of both classes' clearances, overridden by the last matching
  two-object `.kicad_dru` clearance rule; never below Board Setup
  `min_clearance`.
- `BoardRules.unsupported`: list of (rule name, reason) for the report.

### Rule subset (`.kicad_dru`)

- Conditions: `A.NetClass == 'X'`, `A.NetName == 'x'` (KiCad wildcard
  matching), `A.hasNetclass('X')`, same for `B.`, combined with `&&`, `||`,
  `!`, parentheses. Optional `(layer "...")`, `outer`, `inner`.
- Constraints used for routing: `clearance`, `track_width`, `via_diameter`,
  `hole_size`, `diff_pair_gap`.
- Constraints used for matching/report: `skew`, `length`.
- Constraints only reported (DRC-only): `diff_pair_uncoupled`, `via_count`,
  `edge_clearance`, `hole_clearance`, anything else.
- A rule whose condition uses anything outside the subset goes to
  `unsupported` and is not applied.

### Routing engine changes

- `GridRouteConfig` gains an optional `rules: BoardRules` plus resolved
  per-net maps (`net_track_width`, `net_clearance_class`), filled once at
  startup. `get_net_track_width` consults it (generalises today's
  `power_net_widths`).
- `precompute_net_obstacles(..., routed_class=None)`: when rules are active,
  the expansion for obstacle net O uses `clearance(R, O)` and R's width, where
  R is the class being routed. Cache key becomes `(net_id, routed_class)`.
  Classes are few (typically ≤ 6), so memory grows by at most that factor and
  only entries actually needed are built.
- **Routing runs class by class**: nets are grouped by resolved class; each
  group is routed with its own config (width, via, clearance) against obstacles
  stamped for that class. Groups go narrowest-constraint-first (diff/impedance
  classes, then signal classes, then power), matching the existing
  recommended order.
- **Rip-up stays inside the current class group** in rules mode: a blocker
  from another class is treated as fixed, because rerouting it needs that
  class's obstacle view. Cross-class rip-up is a possible follow-up.
- `route_diff.py`: diff-pair width/gap per pair from its class/rules (an
  explicit `--impedance` still overrides width, and is reported if it
  conflicts with the rule width). `skew` max → intra-pair tolerance
  (`--diff-pair-intra-match` enabled for those pairs); `length` → reported
  after routing (pass/fail per net), no attempt to enforce max length.

### CLI

- `--kicad-rules` on `route.py` and `route_diff.py`. Explicit CLI flags still
  win over the files, and each override is printed.
- Rules mode implies `--no-fix-drc-settings` (the project's rules are the
  spec; loosening them is exactly what must not happen).
- Start-of-run report: classes found, nets per class, resolved values, rules
  applied, rules unsupported.
- End-of-run report: per-net length/skew results vs rules; "DRC-only" list.

## Verification

- Unit tests: `.kicad_pro` parsing (patterns incl. regex, assignments,
  Default fallback), `.kicad_dru` parsing and condition evaluation,
  precedence (later rule wins, class max, board minimum), unsupported-rule
  reporting.
- Obstacle test: two nets of different classes; assert stamped keep-out
  equals `w_o/2 + clearance(R,O) + w_r/2` for each pair.
- End-to-end: a repo test board (e.g. `kicad_files/flat_hierarchy`) given a
  generated `.kicad_pro` + `.kicad_dru` with ≥ 3 classes and one pairwise
  clearance rule; route with `--kicad-rules`; `kicad-cli pcb drc` reports no
  clearance/width violations and `check_connected.py` matches a run without
  rules.
- Regression: existing test suite passes unchanged without the flag.

## Risks / open items for the builder

- Class-by-class routing can lower completion vs one global pass; measure on
  the e2e board and report.
- GUI parity gate tests (`tests/gui_parity/test_manifest_plan_parity.py`) will
  flag the new CLI flag; register it as CLI-only there rather than wiring the
  GUI.
- `route.py` reads/writes a sibling `<output>.kicad_pro`; in rules mode the
  rules must be read from the *input* project and written unchanged to the
  output project.
- Builder's call: exact module boundaries, whether `read_design_rules` moves
  into `kicad_rules.py` or is wrapped.
