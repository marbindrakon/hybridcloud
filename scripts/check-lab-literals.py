#!/usr/bin/env python3
"""Fail when lab-specific endpoints appear in the dispatch path (shc LAB-PLAN §11.3).

Rulebooks, the EDA common tasks and the AAP config-as-code must take their endpoints from
credentials, inventory variables or the config-as-code job's environment, never from literals.
The scope is a ratchet: add paths here as they are cleaned.

usage: scripts/check-lab-literals.py [repo-root | --self-test]
"""
import re
import sys
from pathlib import Path

SCOPE = [
    "eda/rulebooks/*.yml",
    "eda/common/tasks/*.yml",
    "aap-config/**/*.yml",
    # §11.6 phase C: tenant cloud roles (the OpenStack path of PlatformOpenshift provision)
    "eda/common/files/*.py",
    # IPv6 V3: the generic realization defaults ship no lab value (lab values live in the installation's
    # FABRIC_REALIZATION)
    "eda/common/vars/*.yml",
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
    "eda/rulebooks/roles/platformopenshift_provision/tasks/remove_stopped_build.yml",
    "eda/rulebooks/roles/platformopenshift_provision/tasks/install_*.yml",
    "eda/rulebooks/roles/platformopenshift_provision/templates/*openstack*.j2",
    "eda/rulebooks/roles/platformopenshift_provision/templates/build-values.yaml.j2",
    # the tenant, access, team and health-check roles
    "eda/rulebooks/roles/entity_*/**/*.yml",
    "eda/rulebooks/roles/project_*/**/*.yml",
    "eda/rulebooks/roles/team_*/**/*.yml",
    "eda/rulebooks/roles/persona_*/**/*.yml",
    "eda/rulebooks/roles/rbac_*/**/*.yml",
    "eda/rulebooks/roles/rbacconfig_*/**/*.yml",
    "eda/rulebooks/roles/uihealthchecker_*/**/*.yml",
]
PATTERNS = {
    "lab domain": re.compile(r"signal9\.(gg|lab)|\.lab\.signal9"),
    "old cluster": re.compile(r"shc-cental5|shc-central5"),
    "lab address": re.compile(r"\b172\.18\.\d{1,3}\.\d{1,3}\b|\b75\.8\.222\.\d{1,3}\b"),
    # the lab's IPv6 space (the owner's /44 and the platform /48 in it)
    "lab IPv6 prefix": re.compile(r"\b2602:0{0,3}81f:", re.I),
}

# The platform's label and annotation keys of the earlier release, under the lab's domain. For one release the
# charts and jobs carry and select them beside the same word under hybridsovereign.redhat/ (a teardown looks for
# both), so they are product history, not a lab value. Only these exact keys are allowed.
EARLIER_KEYS = re.compile(r"\bshc\.signal9\.gg/(?:oidc-static|oidc-name|oidc-fetched-[A-Za-z0-9_.-]*|fabric|"
                          r"cloud-identity|spiffe-id)(?![A-Za-z0-9_-])")


def checked(line):
    """The line as the patterns see it: the earlier release's keys removed."""
    return EARLIER_KEYS.sub("", line)


if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
    for line, lab in [("loop: [hybridsovereign.redhat/oidc-name, shc.signal9.gg/oidc-name]", False),
                      ("shc.signal9.gg/fabric: route-reflectors", False),
                      ("selector: {shc.signal9.gg/oidc-fetched-acme: 'true'}", False),
                      ("shc.signal9.gg/platform-reach: null", True),
                      ("shc.signal9.gg/fabricx: y", True),
                      ("host: api.central.shc.signal9.gg", True)]:
        got = bool(PATTERNS["lab domain"].search(checked(line)))
        if got != lab:
            print(f"self-test: {line!r}: lab literal {got}, want {lab}")
            sys.exit(1)
    print("self-test: ok")
    sys.exit(0)

root = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[1])
hits = 0
for pattern in SCOPE:
    for path in sorted(root.glob(pattern)):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = checked(line)
            for what, rx in PATTERNS.items():
                if rx.search(line):
                    print(f"{path.relative_to(root)}:{lineno}: {what}: {line.strip()}")
                    hits += 1
if hits:
    print(f"{hits} lab literal(s) in the dispatch path; move them to credentials, inventory variables or job env")
    sys.exit(1)
print("no lab literals in: " + ", ".join(SCOPE))
