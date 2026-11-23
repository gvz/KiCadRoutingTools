"""Optional LLM move proposer, consulted when the classical policy stalls.

Backend-agnostic: the proposer pipes a prompt to any shell command on stdin and
parses a JSON move list from its stdout. Examples:

    --llm-cmd "ollama run qwen2.5:14b"
    --llm-cmd "claude -p --model claude-sonnet-5-5"
    --llm-cmd "llama-cli -m model.gguf -f /dev/stdin --no-display-prompt"

The LLM never touches the board: its moves go through the same legality check
and the router stays the judge, so a bad suggestion costs one routing run.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
from typing import Callable, Dict, List, Optional

from .board import Move, Placement
from .policy import net_index

PROMPT = """You are helping a PCB auto-placement loop. A router failed to connect some nets.
Propose small footprint moves that make the failed nets routable.

Rules:
- Only move refs listed under MOVABLE. Moves are mm (dx right, dy DOWN) and rotation in degrees (0/90/180/270).
- Keep moves small (0.25 - 3 mm). Courtyards must not overlap; illegal moves are discarded.
- Prefer clearing the corridor between a disconnected pad and the rest of its net, or pulling
  the disconnected part closer. Do not repeat moves that HISTORY shows were rejected.
{context}
BOARD REGION: {region}

FAILED NETS (net: disconnected pads as ref@x,y | other pads of the net):
{failures}

NEARBY PARTS (ref: courtyard x0,y0,x1,y1 | pads count | movable):
{nearby}

HISTORY (most recent last; delta < 0 is an improvement):
{history}

Answer with ONLY a JSON array, at most {k} items, e.g.
[{{"ref": "C123", "dx": 0.5, "dy": -1.0, "drot": 0, "why": "clear corridor for NET_X"}}]
"""


def build_prompt(pl: Placement, issues: List[Dict], history: List[Dict], k: int) -> str:
    a = pl.adapter
    idx = net_index(pl)
    lines, involved = [], set()
    shown = 0
    for iss in issues:
        net = iss["net_name"]
        pts = idx.get(net, [])
        # Skip nets made only of twin parts: the primary's failure is listed already.
        if pts and all(a.canonical_ref(r) != r for r, _, _ in pts):
            continue
        shown += 1
        if shown > 12:
            break
        disc = iss["disconnected_pads"]
        dtxt = ", ".join(f"{r}@{x:.2f},{y:.2f}" for x, y, _l, r in disc[:6]) or "(entire net unrouted)"
        otxt = ", ".join(f"{r}@{x:.2f},{y:.2f}" for r, x, y in pts[:10])
        lines.append(f"- {net}: {dtxt} | {otxt}")
        involved |= {r for r, _, _ in pts}
    # parts near the failures
    near = set()
    boxes = {r: pl.courtyard(r) for r in pl.refs()}
    for r in involved:
        b = boxes[r]
        for o, ob in boxes.items():
            if ob[0] < b[2] + 3 and b[0] < ob[2] + 3 and ob[1] < b[3] + 3 and b[1] < ob[3] + 3:
                near.add(o)
    nearby = []
    for r in sorted(near)[:80]:
        b = boxes[r]
        mov = r not in a.locked and a.is_movable(r)
        nearby.append(f"- {r}: {b[0]:.2f},{b[1]:.2f},{b[2]:.2f},{b[3]:.2f} | {len(pl.pads(r))} | {'yes' if mov else 'no'}")
    hist = [f"- {h['move']['ref']} dx={h['move']['dx']} dy={h['move']['dy']} rot={h['move']['drot']} "
            f"-> {'ACCEPTED' if h['accepted'] else 'rejected'} delta={h['delta']:.0f}"
            for h in history[-15:]] or ["(none)"]
    region = a.region_of(next(iter(involved))) if involved else None
    return PROMPT.format(
        context=("PROJECT NOTES: " + a.llm_context) if a.llm_context else "",
        region=region or pl.board_rect,
        failures="\n".join(lines),
        nearby="\n".join(nearby),
        history="\n".join(hist),
        k=k,
    )


def parse_moves(text: str) -> List[Move]:
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    moves = []
    for d in data if isinstance(data, list) else []:
        try:
            moves.append(Move(str(d["ref"]), float(d.get("dx", 0)), float(d.get("dy", 0)),
                              float(d.get("drot", 0)), "llm: " + str(d.get("why", ""))[:80]))
        except (KeyError, TypeError, ValueError):
            continue
    return moves


class CommandProposer:
    def __init__(self, cmd: str, timeout: int = 300):
        self.cmd = shlex.split(cmd)
        self.timeout = timeout

    def propose(self, pl: Placement, issues: List[Dict], history: List[Dict], k: int = 5,
                log: Optional[Callable] = None) -> List[Move]:
        prompt = build_prompt(pl, issues, history, k)
        try:
            res = subprocess.run(self.cmd, input=prompt, capture_output=True, text=True, timeout=self.timeout)
            out = res.stdout
        except (subprocess.TimeoutExpired, OSError) as e:
            out = ""
            if log:
                log(f"LLM call failed: {e}")
        if log:
            log({"llm_prompt": prompt, "llm_reply": out})
        return parse_moves(out)
