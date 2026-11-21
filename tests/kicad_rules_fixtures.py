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
