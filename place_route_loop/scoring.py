"""Grade a routed board: connectivity (authoritative) + optional kicad-cli DRC."""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from check_connected import run_connectivity_check

W_BROKEN_NET = 1000.0   # each net that is not fully connected
W_DISCONNECTED_PAD = 50.0
W_DRC_ERROR = 5.0
W_ROUTE_FAILED = 1e9


@dataclass
class Score:
    total: float
    broken_nets: int = 0
    disconnected_pads: int = 0
    drc_errors: int = 0
    drc_by_type: Dict[str, int] = field(default_factory=dict)
    issues: List[Dict] = field(default_factory=list)   # check_connected issues (trimmed)
    route_failed: bool = False

    def summary(self) -> str:
        if self.route_failed:
            return "ROUTE FAILED"
        return (f"score={self.total:.0f} broken_nets={self.broken_nets} "
                f"disc_pads={self.disconnected_pads} drc_errors={self.drc_errors}")

    def to_dict(self) -> Dict:
        return {
            "total": self.total, "broken_nets": self.broken_nets,
            "disconnected_pads": self.disconnected_pads, "drc_errors": self.drc_errors,
            "drc_by_type": self.drc_by_type, "route_failed": self.route_failed,
            "broken": [i["net_name"] for i in self.issues],
        }


def _trim_issue(i: Dict) -> Dict:
    return {
        "net_name": i["net_name"],
        "unrouted": bool(i.get("unrouted")),
        "num_pads": i.get("num_pads", 0),
        # each: (x, y, layer, ref)
        "disconnected_pads": [tuple(p) for p in (i.get("disconnected_pads") or [])],
    }


def run_drc(pcb: Path, out_json: Path) -> Optional[Dict]:
    cmd = ["kicad-cli", "pcb", "drc", "--severity-all", "--all-track-errors",
           "--refill-zones", "--format", "json", "--output", str(out_json), str(pcb)]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if not out_json.is_file():
        return None
    return json.loads(out_json.read_text())


def grade(pcb: Path, work: Path, with_drc: bool = True, w_drc: float = W_DRC_ERROR) -> Score:
    if not pcb.is_file():
        return Score(total=W_ROUTE_FAILED, route_failed=True)
    with contextlib.redirect_stdout(io.StringIO()):
        issues = run_connectivity_check(str(pcb), quiet=True)
    trimmed = [_trim_issue(i) for i in issues]
    n_pads = 0
    for t in trimmed:
        # Unrouted nets report no pad list; count all but one pad as disconnected.
        n_pads += len(t["disconnected_pads"]) if t["disconnected_pads"] else max(t["num_pads"] - 1, 1)

    drc_errors, by_type = 0, {}
    if with_drc:
        d = run_drc(pcb, work / (pcb.stem + "_drc.json"))
        if d is not None:
            errs = [v for v in d.get("violations", []) if v.get("severity") == "error"]
            drc_errors = len(errs)
            by_type = dict(Counter(v.get("type", "?") for v in errs))

    total = W_BROKEN_NET * len(trimmed) + W_DISCONNECTED_PAD * n_pads + w_drc * drc_errors
    return Score(total=total, broken_nets=len(trimmed), disconnected_pads=n_pads,
                 drc_errors=drc_errors, drc_by_type=by_type, issues=trimmed)
