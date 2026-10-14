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
    # §11.6 phase C: tenant cloud roles (the OpenStack path of PlatformOpenshift provision)
    "eda/common/files/*.py",
    "eda/rulebooks/roles/cloudoso_*/**/*.yml",
    # P7.1: CloudInfrastructure
    "eda/rulebooks/roles/cloudinfrastructure_*/**/*.yml",
    "eda/rulebooks/roles/dnsconfig_*/**/*.yml",
    # P7.3.3: fabric kinds
    "eda/rulebooks/roles/hybridfabric_*/**/*.yml",
    "eda/rulebooks/roles/cloudgateway_*/**/*.yml",
    "eda/rulebooks/roles/hybridnetwork_*/**/*.yml",
    "eda/rulebooks/roles/networkplacement_*/**/*.yml",
    "eda/rulebooks/roles/cloudvirt_*/**/*.yml",
    "eda/rulebooks/roles/platformopenshift_teardown/**/*.yml",
    "eda/rulebooks/roles/platformopenshift_provision/defaults/*.yml",
    "eda/rulebooks/roles/platformopenshift_provision/tasks/openstack*.yml",
    "eda/rulebooks/roles/platformopenshift_provision/tasks/designate_dns.yml",
    "eda/rulebooks/roles/platformopenshift_provision/tasks/keycloak_*.yml",
    "eda/rulebooks/roles/platformopenshift_provision/tasks/cleanup_stale_clusterbuild.yml",
    "eda/rulebooks/roles/platformopenshift_provision/templates/*openstack*.j2",
    "eda/rulebooks/roles/platformopenshift_provision/templates/build-values.yaml.j2",
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
