#!/usr/bin/env python3
"""Fail when lab-specific endpoints appear in the dispatch path (shc LAB-PLAN §11.3).

Rulebooks, the EDA common tasks and the AAP config-as-code must take their endpoints from
credentials, inventory variables or the config-as-code job's environment, never from literals.
The scope is a ratchet: add paths here as they are cleaned.

usage: scripts/check-lab-literals.py [repo-root]
"""
import re
import sys
from pathlib import Path

SCOPE = [
    "eda/rulebooks/*.yml",
    "eda/common/tasks/*.yml",
    "eda/rulebooks/common/tasks/*.yml",
    "aap-config/**/*.yml",
]
PATTERNS = {
    "lab domain": re.compile(r"signal9\.(gg|lab)|\.lab\.signal9"),
    "old cluster": re.compile(r"shc-cental5|shc-central5"),
    "lab address": re.compile(r"\b172\.18\.\d{1,3}\.\d{1,3}\b|\b75\.8\.222\.\d{1,3}\b"),
}

root = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[1])
hits = 0
for pattern in SCOPE:
    for path in sorted(root.glob(pattern)):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for what, rx in PATTERNS.items():
                if rx.search(line):
                    print(f"{path.relative_to(root)}:{lineno}: {what}: {line.strip()}")
                    hits += 1
if hits:
    print(f"{hits} lab literal(s) in the dispatch path; move them to credentials, inventory variables or job env")
    sys.exit(1)
print("no lab literals in: " + ", ".join(SCOPE))
