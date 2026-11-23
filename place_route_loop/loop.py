"""Place -> route -> grade -> move loop (greedy with tabu, optional annealing)."""
from __future__ import annotations

import argparse
import json
import math
import random
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .board import Move, Placement
from .llm import CommandProposer
from .policy import candidates, ranked_legal
from .project import ProjectAdapter, load_adapter
from .scoring import Score, grade


class Loop:
    def __init__(self, adapter: ProjectAdapter, work: Path, args: argparse.Namespace):
        self.a = adapter
        self.work = work
        self.args = args
        self.history: List[Dict] = []
        self.tabu: Set[Tuple] = set()
        self.proposer = CommandProposer(args.llm_cmd) if args.llm_cmd else None
        self.hist_file = work / "history.jsonl"
        self.llm_file = work / "llm.jsonl"
        self.best_place = work / "best_placement.kicad_pcb"
        self.best_routed = work / "best_routed.kicad_pcb"
        self.rng = random.Random(args.seed)

    # ------------------------------------------------------------ helpers
    def log(self, msg) -> None:
        if isinstance(msg, dict):
            with self.llm_file.open("a") as f:
                f.write(json.dumps(msg) + "\n")
        else:
            print(msg, flush=True)

    def route_and_grade(self, placement: Path, tag: str) -> Score:
        d = self.work / tag
        d.mkdir(parents=True, exist_ok=True)
        if placement.resolve() != self.a.placement_pcb.resolve():
            shutil.copy2(placement, self.a.placement_pcb)
        shutil.copy2(self.a.placement_pcb, d / "placement.kicad_pcb")
        if self.a.routed_pcb.exists():
            self.a.routed_pcb.unlink()
        t0 = time.time()
        rc = self.a.route(d / "route.log")
        dt = time.time() - t0
        if rc != 0 or not self.a.routed_pcb.is_file():
            self.log(f"  [{tag}] route failed rc={rc} ({dt:.0f}s)")
            return Score(total=1e9, route_failed=True)
        # Grade in place: kicad-cli DRC needs the sibling .kicad_pro/.kicad_dru rules.
        s = grade(self.a.routed_pcb, d, with_drc=self.a.run_drc, w_drc=self.args.w_drc)
        shutil.copy2(self.a.routed_pcb, d / "routed.kicad_pcb")
        self.log(f"  [{tag}] {s.summary()} ({dt:.0f}s route)")
        return s

    # ------------------------------------------------------------ main loop
    def run(self) -> Score:
        args, a = self.args, self.a
        original = self.work / "original_placement.kicad_pcb"
        if not original.exists():
            shutil.copy2(a.placement_pcb, original)
        shutil.copy2(original, self.best_place)

        if args.baseline_routed:
            self.log(f"Grading existing routed board {args.baseline_routed}")
            best = grade(Path(args.baseline_routed), self.work, with_drc=a.run_drc, w_drc=args.w_drc)
            shutil.copy2(args.baseline_routed, self.best_routed)
            self.log(f"  [baseline] {best.summary()}")
        else:
            self.log("Routing baseline placement ...")
            best = self.route_and_grade(original, "iter_000_baseline")
            if best.route_failed:
                raise SystemExit("baseline route failed; fix the pipeline first")
            shutil.copy2(self.work / "iter_000_baseline" / "routed.kicad_pcb", self.best_routed)
        if a.after_baseline:
            a.after_baseline(a)
            self.log(f"Locked parts: {len(a.locked)}")

        t_start = time.time()
        stall = 0
        temp = args.anneal
        try:
            for it in range(1, args.iterations + 1):
                if best.broken_nets == 0 and best.drc_errors == 0:
                    self.log("Board is fully routed and DRC-clean. Done.")
                    break
                if args.time_budget and time.time() - t_start > args.time_budget * 60:
                    self.log("Time budget exhausted.")
                    break

                pl = Placement(self.best_place, a)
                move = self.pick_move(pl, best, stall)
                if move is None:
                    self.log("No legal candidate moves left. Stopping.")
                    break
                self.log(f"iter {it}: {move.ref} dx={move.dx:+.2f} dy={move.dy:+.2f} "
                         f"rot={move.drot:+.0f}  ({move.reason})")
                pl.apply(move)
                cand = self.work / "candidate_placement.kicad_pcb"
                pl.save(cand)
                s = self.route_and_grade(cand, f"iter_{it:03d}")
                delta = s.total - best.total

                accept = delta < 0
                if not accept and temp > 0 and not s.route_failed:
                    accept = self.rng.random() < math.exp(-delta / temp)
                temp *= args.anneal_decay

                rec = {"iter": it, "move": move.to_dict(), "score": s.to_dict(),
                       "best_before": best.total, "delta": delta, "accepted": accept,
                       "time": time.time()}
                self.history.append(rec)
                with self.hist_file.open("a") as f:
                    f.write(json.dumps(rec) + "\n")

                if accept:
                    shutil.copy2(cand, self.best_place)
                    shutil.copy2(self.work / f"iter_{it:03d}" / "routed.kicad_pcb", self.best_routed)
                    improved = delta < 0
                    best = s
                    stall = 0 if improved else stall + 1
                    # positions changed: old tabu entries are stale; forbid undoing this move
                    self.tabu = {Move(move.ref, -move.dx, -move.dy, -move.drot).key()}
                    self.log(f"  ACCEPT (delta {delta:+.0f}) -> {best.summary()}")
                else:
                    self.tabu.add(move.key())
                    stall += 1
                    self.log(f"  reject (delta {delta:+.0f})")
                if not args.keep_iter_boards:
                    self.prune(it)
        finally:
            # Leave the project with the best placement and its routed board.
            shutil.copy2(self.best_place, a.placement_pcb)
            shutil.copy2(self.best_routed, a.routed_pcb)
            self.log(f"Best: {best.summary()}")
            self.log(f"  placement: {self.best_place}\n  routed:    {self.best_routed}")
        return best

    def pick_move(self, pl: Placement, best: Score, stall: int) -> Optional[Move]:
        args = self.args
        if self.proposer and args.llm_every and stall > 0 and stall % args.llm_every == 0:
            self.log(f"  stalled {stall} iters -> asking LLM")
            for m in self.proposer.propose(pl, best.issues, self.history, k=5, log=self.log):
                m = Move(self.a.canonical_ref(m.ref), m.dx, m.dy, m.drot, m.reason)
                if m.key() in self.tabu:
                    continue
                why = pl.legality(m) if pl.adapter.is_movable(m.ref) else "not movable"
                if why is None:
                    return m
                self.log(f"  LLM move {m.ref} illegal: {why}")
        cands = candidates(pl, best.issues)
        legal = ranked_legal(pl, cands, self.tabu, k=max(1, args.explore))
        if not legal:
            return None
        # small exploration: occasionally pick among the top few instead of the best
        if len(legal) > 1 and self.rng.random() < args.explore_prob:
            return self.rng.choice(legal[: args.explore])[1]
        return legal[0][1]

    def prune(self, it: int) -> None:
        d = self.work / f"iter_{it:03d}"
        for f in ("placement.kicad_pcb",):
            p = d / f
            if p.exists() and not self.history[-1]["accepted"]:
                p.unlink()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="place_route_loop", description=__doc__)
    ap.add_argument("--project", required=True, type=Path, help="project adapter .py defining make_adapter()")
    ap.add_argument("--work", type=Path, default=None, help="work dir (default: <project dir>/_placeloop)")
    ap.add_argument("--iterations", type=int, default=30)
    ap.add_argument("--time-budget", type=float, default=0, help="minutes; 0 = unlimited")
    ap.add_argument("--baseline-routed", default=None, help="grade this routed board as baseline instead of routing (must sit beside its .kicad_pro/.kicad_dru, or DRC uses default rules)")
    ap.add_argument("--anneal", type=float, default=0.0, help="initial annealing temperature (score units); 0 = greedy")
    ap.add_argument("--anneal-decay", type=float, default=0.85)
    ap.add_argument("--explore", type=int, default=3, help="pick among top-N legal candidates")
    ap.add_argument("--explore-prob", type=float, default=0.25)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--llm-cmd", default=None, help="shell command: prompt on stdin, JSON moves on stdout")
    ap.add_argument("--llm-every", type=int, default=3, help="consult LLM after this many non-improving iters")
    ap.add_argument("--w-drc", type=float, default=5.0, help="score weight per DRC error (broken net = 1000)")
    ap.add_argument("--keep-iter-boards", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="grade baseline, print candidate moves, exit")
    args = ap.parse_args(argv)

    adapter = load_adapter(args.project)
    work = args.work or (args.project.resolve().parent / "_placeloop")
    work.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        rb = Path(args.baseline_routed) if args.baseline_routed else adapter.routed_pcb
        s = grade(rb, work, with_drc=adapter.run_drc, w_drc=args.w_drc)
        print(f"baseline {rb}: {s.summary()}")
        if adapter.after_baseline:
            adapter.after_baseline(adapter)
            print(f"locked parts: {len(adapter.locked)}")
        pl = Placement(adapter.placement_pcb, adapter)
        cands = candidates(pl, s.issues)
        legal = ranked_legal(pl, cands, set(), k=15)
        print(f"{len(cands)} candidates, top legal:")
        for c, m in legal:
            print(f"  {m.ref:6s} dx={m.dx:+.2f} dy={m.dy:+.2f} rot={m.drot:+.0f} hpwl{c:+.2f}  {m.reason}")
        if args.llm_cmd:
            from .llm import build_prompt
            print("\n--- LLM prompt ---\n" + build_prompt(pl, s.issues, [], 5))
        return 0

    best = Loop(adapter, work, args).run()
    return 0 if best.broken_nets == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
