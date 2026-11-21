"""Shared helpers for kicad_rules tests."""
import json
import os


def write_pro(tmp_path, classes=None, patterns=None, assignments=None, rules=None,
              name="board.kicad_pro"):
    """Write a minimal KiCad 8+/10 .kicad_pro and return its path as str."""
    net_settings = {}
    if classes is not None:
        net_settings["classes"] = classes
        net_settings["netclass_patterns"] = patterns or []
        net_settings["netclass_assignments"] = assignments or {}
    pro = {
        "net_settings": net_settings,
        "board": {"design_settings": {"rules": rules or {}}},
    }
    path = os.path.join(str(tmp_path), name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pro, f)
    return path


# --- spec checks on routed output (kicad-cli DRC + check_connected) ---------

import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402

KICAD_CLI = shutil.which("kicad-cli")
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def kicad_drc(pcb_path):
    """Violations from `kicad-cli pcb drc --refill-zones --format json`."""
    report = os.path.splitext(pcb_path)[0] + ".drc.json"
    subprocess.run([KICAD_CLI, "pcb", "drc", "--refill-zones", "--format", "json",
                    "-o", report, pcb_path], capture_output=True, text=True,
                   timeout=600, check=False)
    with open(report, encoding="utf-8") as f:
        return json.load(f)["violations"]


def violation_nets(v, nets):
    """The nets of `nets` that a DRC violation's items name ("... [net] ...")."""
    text = " ".join(i.get("description", "") for i in v.get("items", []))
    return {n for n in nets if f"[{n}]" in text}


def actual_mm(v):
    m = re.search(r"actual ([\d.]+) mm", v.get("description", ""))
    return float(m.group(1)) if m else None


def check_connected(pcb_path, nets, env):
    """Run check_connected.py on `nets`; returns the CompletedProcess."""
    return subprocess.run([sys.executable, os.path.join(_REPO, "check_connected.py"),
                           pcb_path, "--nets", *nets], cwd=_REPO, env=env,
                          capture_output=True, text=True, timeout=600)
