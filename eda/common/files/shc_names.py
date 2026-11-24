#!/usr/bin/env python3
"""Every name the platform gives an object on an underlying platform comes from here.

The rules (docs/platform/platform-names.md):

* A name is made of the person's own names, in one order: the tenant, then the environment or the placement, then
  the object. Nothing random is added.
* The parts are joined by a character they cannot contain. Two forms exist:
    "plain"  joined with "_"   where the platform allows an underscore (Keystone, Neutron, AWS tags, the registry,
                               the automation platform, client ids of the identity provider);
    "label"  joined with "--"  where only lower-case letters, digits and hyphens are allowed (Kubernetes names,
                               Google Cloud names, DNS labels). No part may contain "--" (check_name refuses it).
  An access group keeps <tenant>.<group>: that one name is matched in three systems at every sign-in.
* A name is exact as long as it fits and every part obeys the rule for names. When it had to be cut, or a part had
  to be changed to fit the platform's characters, the name ends in DIGEST characters derived from the exact parts,
  so two different inputs never give one name. A name that people type elsewhere and that must match exactly (an
  access group) is never cut: too long is an error.
* A name is for people. Nobody finds an object by a name from here: by a stored id or by the platform's mark.

Use as a module (the other helpers import it) or as a filter over stdin, one JSON request in, one JSON answer out
(eda/common/tasks/names_call.yml):

  {"action": "check", "value": "web", "what": "cluster", "limit": 30}
      -> {"ok": true, "message": ""} | {"ok": false, "message": "<the sentence for the person>"}
  {"action": "name", "kind": "<one of KINDS>", "parts": ["acme-corp", "prod"], ...}
      -> {"name": "acme-corp_prod", "exact": true, "display": "acme-corp / prod"}
  {"action": "join", "form": "plain"|"label", "parts": [...], "limit": 63, "prefix": "", "reserve": 0}
      -> the same answer, for a name no kind covers yet
  {"action": "group", "tenant": "acme-corp", "group": "devs", "limit": 64}
      -> {"name": "acme-corp.devs", ...} | {"error": "..."} when it does not fit (never cut)
"""
import hashlib
import json
import re
import sys

# The rule for every name a person chooses that reaches a platform (the CRDs carry the same rule in CEL).
NAME_MAX = 30
GATEWAY_NAME_MAX = 21          # a gateway's name becomes a Google Cloud service account id (6-30) after "shc-edge-"
NAME_PATTERN = r"^[a-z]([a-z0-9-]*[a-z0-9])?$"
_NAME = re.compile(NAME_PATTERN)

DIGEST = 6
JOINER = {"plain": "_", "label": "--"}
_ALLOWED = {"plain": re.compile(r"[^A-Za-z0-9.-]"), "label": re.compile(r"[^a-z0-9-]")}

# kind -> (form, the platform's limit for the whole name, a fixed beginning, room kept free for what the caller
# appends, how many parts it takes; 0 = one or more). Add a kind here rather than joining by hand anywhere else.
KINDS = {
    # OpenStack
    "openstack-project": ("plain", 64, "", 0, 2),            # <tenant>_<environment>
    "openstack-network": ("plain", 255, "", 3, 2),           # <tenant>_<placement>; subnets append _v4 / _v6
    "openstack-cluster-user": ("plain", 255, "", 0, 3),      # <tenant>_<environment>_<cluster>
    # OpenShift
    "site-namespace": ("label", 63, "", 0, 3),               # <tenant>--<environment>--<namespace>
    # Google Cloud: names of 63; the longest ending a placement's objects get is "-default6" (9)
    "gcp-placement": ("label", 63, "shc-", 9, 2),            # shc-<tenant>--<placement>
    "gcp-dns-zone": ("label", 63, "shc-", 0, 2),             # shc-<environment>--<tenant> (the zone's resource name)
    # AWS: the Name tag of a placement's objects; the longest ending is "-public-<az letter>" (9)
    "aws-placement": ("plain", 255, "", 9, 2),               # <tenant>_<placement>
    # the platform's own services
    "registry-organization": ("plain", 255, "", 0, 2),       # <tenant>_<name>
    "automation-organization": ("plain", 255, "", 0, 2),     # <tenant>_<name>
    "identity-client": ("plain", 255, "", 0, 3),             # <service>_<tenant>_<name>, e.g. vault_acme-corp_web
    # DNS: one label made of several names (reverse-DNS names of gateways and placements)
    "dns-label": ("label", 63, "", 0, 0),
}


def check_name(value, what="name", limit=NAME_MAX):
    """"" when value obeys the rule for names, else the sentence that tells the person what to change."""
    value = "" if value is None else str(value)
    limit = int(limit or NAME_MAX)
    if _NAME.match(value) and "--" not in value and len(value) <= limit:
        return ""
    return ("The %s name '%s' is not allowed: use lower-case letters, digits and single hyphens, start with a "
            "letter, end with a letter or digit, at most %d characters." % (what, value[:80], limit))


def _digest(parts, prefix):
    return hashlib.sha256("\0".join([prefix] + parts).encode()).hexdigest()[:DIGEST]


def join(parts, form, limit, prefix="", reserve=0):
    """The name for parts on a platform of this form: exact when it fits, otherwise cut with a derived ending.

    reserve: characters the caller will append to the name (an ending such as "-fabric6"), so the longest
    derived name still fits the limit. Returns {"name", "exact", "display"}.
    """
    if form not in JOINER:
        raise ValueError("names: unknown form %r" % (form,))
    parts = ["" if p is None else str(p).strip() for p in parts]
    if not parts or any(not p for p in parts):
        raise ValueError("names: every part is needed, got %r" % (parts,))
    limit, reserve, prefix = int(limit), int(reserve or 0), str(prefix or "")
    room = limit - reserve
    sep = JOINER[form]
    clean = [p.lower() if form == "label" else p for p in parts]
    clean = [_ALLOWED[form].sub("-", p) for p in clean]      # the joiner itself never comes from a part
    if form == "label":
        clean = [re.sub(r"-{2,}", "-", p).strip("-") or "x" for p in clean]
    exact = clean == parts
    name = prefix + sep.join(clean)
    if form == "label" and not re.match(r"[a-z]", name):
        name, exact = "n" + name, False                       # these platforms want a letter first
    if exact and len(name) <= room:
        return {"name": name, "exact": True, "display": display(parts)}
    mark = ("-" if form == "label" else "_") + _digest(parts, prefix)
    head = name[: room - len(mark)].rstrip("-_.")
    if not head:
        raise ValueError("names: %d characters leave no room for a name" % (limit,))
    return {"name": head + mark, "exact": False, "display": display(parts)}


def name(kind, parts):
    """The name of one of KINDS for these parts."""
    if kind not in KINDS:
        raise ValueError("names: unknown kind %r" % (kind,))
    form, limit, prefix, reserve, count = KINDS[kind]
    if count and len(parts or []) != count:
        raise ValueError("names: %s takes %d parts, got %d" % (kind, count, len(parts or [])))
    return join(parts, form, limit, prefix=prefix, reserve=reserve)


def group(tenant, group_name, limit=64):
    """<tenant>.<group>: matched by name at every sign-in, so exact or an error, never cut."""
    tenant, group_name = str(tenant or "").strip(), str(group_name or "").strip()
    if not tenant or not group_name:
        raise ValueError("names: a group needs the tenant and the group")
    if "." in tenant:
        raise ValueError("names: a tenant's name cannot contain a dot")
    value = tenant + "." + group_name
    if len(value) > int(limit):
        raise ValueError("The group name %s has %d characters; at most %d fit here. Use a shorter group name."
                         % (value, len(value), int(limit)))
    return {"name": value, "exact": True, "display": display([tenant, group_name])}


def display(parts):
    """What a description, an annotation or a tag says beside a name: the parts in full, in order."""
    return " / ".join(str(p) for p in parts)


def _act(req):
    action = req.get("action")
    if action == "check":
        message = check_name(req.get("value"), req.get("what") or "name", req.get("limit") or NAME_MAX)
        return {"ok": not message, "message": message}
    if action == "name":
        return name(req.get("kind"), req.get("parts") or [])
    if action == "join":
        return join(req.get("parts") or [], req.get("form"), req.get("limit") or 63, req.get("prefix") or "",
                    req.get("reserve") or 0)
    if action == "group":
        return group(req.get("tenant"), req.get("group"), req.get("limit") or 64)
    raise ValueError("names: unknown action %r" % (action,))


def main():
    try:
        print(json.dumps(_act(json.load(sys.stdin))))
        return 0
    except Exception as e:  # noqa: BLE001 - every failure becomes the job's message
        print(json.dumps({"error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
