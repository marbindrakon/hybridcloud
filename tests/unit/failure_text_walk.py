"""The walk of the cluster roles' task files for test_install_failure_text.py: what is written where a tenant reads it.

Sinks (what is written): every fail and assert message (free-form `fail: "msg=..."` too); every status body given to
common/tasks/patch_cr_status.yml (all its string leaves); every refusal given to preflight_stop.yml (`ps_message`,
`ps_reason`); every Event's note, reason and message; the message of every condition a task builds (`conditions`,
`po_preflight_condition`). A value in a sink is allowed when it is:
- an exact path in ALLOWED (a root alone allows any path under it; "root.path" allows exactly that path);
- a variable of the task itself (`vars:`), whose own template is then checked the same way (recursively);
- a fact a task sets (`set_fact`) whose every definition in the walked files is itself allowed (so a set_fact onto
  an allowed name with another system's text is caught at the set_fact);
- `item` of a task whose loop is a literal list or an allowed template;
- the output of the decision filters (`shc_failure_message`, `shc_failure_is_refusal`, `shc_step_failed`).
Refused anywhere: `lookup('vars', ...)`/`query('vars', ...)`, `vars[...]`, `hostvars`, and any use of
`ansible_failed_result` that is not an argument of the decision filters.
A task the cluster roles never run (`when` holds "shc_step is not defined": both roles set shc_step) is skipped.
Every task file of both roles is walked (templated includes are thereby covered), and every file they include.
"""
import os
import re

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
EDA = os.path.join(ROOT, "eda")
ROLES_DIR = os.path.join(EDA, "rulebooks", "roles")
CLUSTER_ROLES = ("platformopenshift_provision", "platformopenshift_teardown")
SKIP_WHEN = "shc_step is not defined"
DECISION_FILTERS = ("shc_failure_message", "shc_failure_is_refusal", "shc_step_failed")
# filters whose output is a record the platform's installation module builds (status.install blocks, digests)
RECORD_FILTERS = ("shc_install_",)
PASS_FILTERS = ("default", "d", "string", "trim", "lower", "upper", "first", "last", "list",
                "truncate", "to_json", "from_json", "regex_replace", "join", "unique", "sort")
# filters whose output is a number or a flag: what they read is never printed
NUMBER_FILTERS = ("length", "count", "int", "bool", "float")

# Each value a tenant may read, and why it is the tenant's own or the platform's fixed text.
ALLOWED = {
    # the tenant's own object
    "platformopenshift_cr_name": "the tenant's object name",
    "platformopenshift_cr_namespace": "the tenant's namespace",
    "ansible_operator_meta": "the tenant's object name and namespace",
    "bn_name": "the tenant's object name",
    "platform_spec": "the tenant's spec",
    "cr_name": "the tenant's object name (status writes)",
    "cr_namespace": "the tenant's namespace (status writes)",
    "cr_plural": "the kind's plural",
    "cr_api_version": "the kind's API version",
    "oso_env_name": "the environment name the tenant set in the spec",
    "aws_env_name": "the environment name the tenant set in the spec",
    "gcp_env_name": "the environment name the tenant set in the spec",
    "po_oso_env": "the environment name the tenant set in the spec",
    "oso_network": "the network the tenant chose by name",
    "aws_network": "the network the tenant chose by name",
    "gcp_network": "the network the tenant chose by name",
    "po_network": "the network the tenant chose by name",
    "pnn_name": "the network the tenant chose by name",
    "cb_placement_ref": "the tenant's network placement name",
    # the cluster's own names and addresses (shown to the tenant on its page)
    "cb_app_name": "the build name, shown in status.buildName",
    "cb_cluster_name": "the build name, shown in status.buildName",
    "po_build_name": "the build name, shown in status.buildName",
    "ocp_oidc_client_id": "the build name and a fixed suffix",
    "cb_ocp_name": "the cluster's own name",
    "cb_cluster_domain": "the cluster's own DNS domain (its console and API names)",
    "cb_api_url": "the cluster's own API address",
    "cb_apps_url": "the cluster's own applications address",
    "cb_console_url": "the cluster's own console address",
    "cb_api_vip": "the cluster's own floating address",
    "cb_ingress_vip": "the cluster's own floating address",
    "cb_machine_cidr": "the cluster's own machine network",
    "po_platform_prefixes": "the address ranges of the tenant's own cluster network",
    "aws_region": "the environment's region, shown in the tenant's environment status",
    "gcp_region": "the environment's region, shown in the tenant's environment status",
    "cb_image_set": "the release the tenant chose, or the name of the installation's default release",
    # fixed words of the platform
    "_ocb_missing": "a setting name from a literal list in the same file",
    "teardown_step": "a fixed step word of the teardown",
    "shc_step": "plain words set in the task files and role defaults (checked by the walk's test)",
    "shc_step_what": "a fixed word set in the role defaults",
    "transition_time": "a time the job took",
    "event_time": "a time the job took",
    "platformopenshift_generation": "the object's generation",
    # sentences built by the platform's decisions
    "po_preflight_condition.message": "the refusal's sentence (preflight_stop.yml, checked at every include)",
    "io_obs.install.lastFailure.reason": "a catalogue reason word from the installation module",
    "io_obs.outcome": "a fixed outcome word of the installation module",
    "rsb_recheck.why": "a fixed sentence of the installation module (may_remove_build)",
    "cloudaws_raw.status": "an HTTP status number",
    "cloudgcp_raw.status": "an HTTP status number",
    "helm_deploy_result.rc": "an exit code",
    "cloudoso_cr.resources[0].status.dnsProvider": "compared with a fixed word only",
    "ra_failed": "a reason word, printed only when it matches [A-Za-z]{2,40}",
    # the platform's own decisions (files/po_preflight.py, shc_fabric.py's range check): fixed sentences with the
    # tenant's values (RefusalTexts checks them free of platform identifiers)
    "po_result.message": "a decision of files/po_preflight.py", "po_result.reason": "a decision of files/po_preflight.py",
    "pf.message": "the network decision of files/po_preflight.py", "pf.reason": "the network decision of files/po_preflight.py",
    "pp_outcome.message": "the platform-access decision of files/po_preflight.py", "pp_outcome.reason": "the platform-access decision of files/po_preflight.py",
    "cl_owner.message": "the hub-owner decision of files/po_preflight.py", "cl_owner.reason": "the hub-owner decision of files/po_preflight.py",
    "cb_fip_conflict.message": "the floating-address decision of files/po_preflight.py", "cb_fip_conflict.reason": "the floating-address decision of files/po_preflight.py",
    "po_deferred_wait.message": "a wait noted from a decision of files/po_preflight.py or the network-name check", "po_deferred_wait.reason": "a wait noted from a decision of files/po_preflight.py or the network-name check",
    "fab_check_result.message": "shc_fabric.py's range check: the tenant's ranges and the platform's published reserved ranges", "fab_check_result.reason": "shc_fabric.py's range check: the tenant's ranges and the platform's published reserved ranges",
    "pf.placement": "the tenant's placement name",
    "pf.networkName": "the network's name on the site, the tenant's network",
    "po_result.network.name": "the external network the tenant chose by name",
    "po_result.address": "the address of the cluster's own router's gateway",
    "po_result.note": "the router gateway's note: fixed words and the cluster's router's name",
    "po_result.clusterNetwork": "the cluster's own range", "po_result.serviceNetwork": "the cluster's own range",
    "po_result.machineNetwork": "the cluster's own range",
    "fab_check_result.errors": "shc_fabric.py's range check: the tenant's ranges and the platform's reserved ranges",
    "cb_machine_network": "the cluster's own machine network",
    "cb_cluster_network": "the cluster's own cluster network", "cb_service_network": "the cluster's own service network",
    "cb_credential.reason": "printed only when it is one of the fixed reason words of the credential's texts",
    "cb_credential.user.name": "the cluster's own site user, named after its build",
    "cid_user_name": "the cluster's own site user, named after its build",
    "cb_cluster_installed": "a flag",
    "cb_install_started": "a flag",
    "cb_provision_status": "the installer's state word, only when it matches [A-Za-z]{2,40} (DESIGN 4.1)",
    "cb_base_domain": "the environment's own DNS domain",
    "cloudoso_cr.resources[0].spec.baseDomain": "the environment's own DNS domain (its spec)",
    "rg_address": "the address of the cluster's own router's gateway",
    "rg_infra_id": "the cluster's own infrastructure id (the installer's, from its name)",
    "os_result.flavors_missing": "only the names also in the tenant's spec are printed (select('in', ...))",
    "os_result.images_missing": "only the names also in the tenant's spec are printed (select('in', ...))",
    # the object's own record as read (its status, written by the platform) and bookkeeping flags
    "isk_obj": "the cluster object as read: its own status",
    "isk_renew": "a flag",
    "cb_network_record": "the cluster's network record (the tenant's network or the external network chosen by name)",
    "cb_dns_records": "the cluster's own DNS record names",
    "designate_project_id": "the DNS project recorded in the environment's status (status.dns.projectId)",
    "use_designate_dns": "a flag",
}
# Fields of a status body that hold a record, not text: the CRD's status fields the platform fills with its own
# choices for the tenant's cluster and the tenant's environment (shown on the cluster's and the environment's pages).
# Their values are not read as sentences; the text fields (TEXT_KEYS) are checked value by value.
RECORD_FIELDS = {
    "acmAvailable": "a flag", "clusterInstalled": "a flag", "ready": "a flag", "oidcConfigured": "a flag",
    "apiURL": "the cluster's own API address", "appsURL": "the cluster's own applications address",
    "consoleURL": "the cluster's own console address", "serviceAccountIssuer": "the cluster's own issuer address",
    "awsAccountId": "the account of the tenant's AWS environment (shown on that environment)",
    "gcpProject": "the project of the tenant's Google Cloud environment (shown on that environment)",
    "awsRegion": "the environment's region", "gcpRegion": "the environment's region",
    "awsVpc": "the VPC of the tenant's network placement", "gcpVpc": "the VPC of the tenant's network placement",
    "billingId": "the tenant namespace's label", "entity": "the tenant namespace's label",
    "description": "the tenant namespace's annotation", "name": "the tenant's object name",
    "namespace": "the tenant's namespace", "platformType": "the spec's type", "buildName": "the build name",
    "clusterHealth": "a state word of the platform's check", "powerState": "a state word",
    "clusterVersion": "the cluster's OpenShift version", "provisionStatus": "the installer's state word (DESIGN 4.1)",
    "clusterNetwork": "the cluster's own range", "machineNetwork": "the cluster's own range",
    "serviceNetwork": "the cluster's own range", "network": "the tenant's network", "placement": "its placement",
    "publish": "External or Internal", "platformPrefixes": "the cluster's own addresses",
    "floatingIPs": "the cluster's own floating addresses",
    "credential": "the cluster's own site user and credential record (never its secret)",
    "deletion.at": "a time", "deletion.step": "a fixed step word", "dns": "the cluster's own DNS record names",
    "domain": "the cluster's own DNS domain", "slug": "the environment's short DNS label",
    "install": "a block built by the installation module (shc_install filters)",
    "lastReconciledAt": "a time", "observedGeneration": "the object's generation",
    "oidcClientId": "the build name and a fixed suffix", "status": "a fixed state word",
}
TEXT_KEYS = ("message", "reason", "note", "summary", "conditions")

# Where an allowed name may be set from (a set_fact onto an allowed name reads only these, or allowed values): the
# tenant's own objects as read, and the platform's own derivations. A path here allows the paths under it.
SOURCES = {
    "event_payload.regarding": "the event's reference to the tenant's object",
    "shc_live_spec": "the tenant's spec as read", "shc_live_cr": "the tenant's object as read",
    "platformopenshift_cr.resources[0]": "the tenant's object as read", "po_cr": "the tenant's object as read",
    "po_cr_current.resources[0]": "the tenant's object as read",
    "cloudoso_cr.resources[0]": "the tenant's environment as read",
    "cloudaws_raw.json.status": "the tenant's environment's status as read",
    "cloudgcp_raw.json.status": "the tenant's environment's status as read",
    "cb_placement.metadata": "the tenant's placement as read", "cb_placement.status": "the tenant's placement as read",
    "bn_build_name": "the build name (build_name.yml)", "rpp_build_name_in_effect": "the build name",
    "aws_domain": "the environment's own DNS domain", "gcp_domain": "the environment's own DNS domain",
    "cd_info.resources[0].status.webConsoleURL": "the cluster's own console address",
    "cd_info.resources[0].status.conditions": "only the Provisioned reason, kept when it matches [A-Za-z]{2,40}",
    "os_result.api.address": "the cluster's own floating address", "os_result.ingress.address": "the same",
    "os_result.records": "the cluster's own DNS record names",
    "cid_cred.user": "the cluster's own site user", "cid_recorded": "the cluster's recorded build",
    "fabric_realization": "the installation's settings", "fabric_realization_defaults": "the platform's defaults",
}
# sources for one name only
NAME_SOURCES = {"ra_failed": ("os_result",)}   # a reason word, printed only when it matches [A-Za-z]{2,40}


# Values a record field may take besides ALLOWED and SOURCES (each the platform's own record of the cluster).
RECORD_VALUES = {
    "cb_issuer": "the cluster's own issuer address", "resolved_entity": "the tenant namespace's label",
    "resolved_billing_id": "the tenant namespace's label", "resolved_description": "the tenant namespace's annotation",
    "cb_cluster_version": "the cluster's OpenShift version", "cb_power_state": "a state word",
    "aws_account": "the account of the tenant's AWS environment", "gcp_project": "the project of the tenant's Google "
    "Cloud environment", "cb_vpc_id": "the placement's VPC", "cb_vpc_name": "the placement's VPC",
    "pp_prefixes": "the cluster's own addresses", "cb_fips": "the cluster's own floating addresses",
    "platform_type": "the spec's type", "dr_records": "the cluster's own DNS record names",
    "cb_credential.credential.id": "the cluster's own credential record (never its secret)",
    "cb_credential.credential.createdAt": "a time", "cb_credential.user.id": "the cluster's own site user",
    "cb_credential.user.domainId": "the cluster's own site user's domain", "cc_domain": "the environment's domain",
    "cb_slug": "the environment's short DNS label", "oso_domain": "the environment's DNS domain",
    "cb_acm_available": "a flag", "cb_cluster_network": "the cluster's own range",
    "cb_service_network": "the cluster's own range", "cb_machine_network": "the cluster's own range",
}


def record_value_allowed(p):
    return any(p == k or p.startswith(k + ".") or p.startswith(k + "[") for k in RECORD_VALUES)


def source_allowed(p):
    return any(p == k or p.startswith(k + ".") or p.startswith(k + "[") for k in SOURCES)


# Allowed names that are the outputs of the platform's own decision helpers or modules (their texts are the platform's,
# checked by their own tests): a set_fact onto them is the helper call task's parse of the helper's answer.
DECISION_RESULTS = ("po_result", "pf", "pp_outcome", "cl_owner", "cb_fip_conflict", "po_deferred_wait",
                    "fab_check_result", "isk_obj")

# Inputs of a shared task file: allowed inside it, checked as sinks at every include of it.
INPUTS = {"preflight_stop.yml": ("ps_message", "ps_reason"), "patch_cr_status.yml": ("cr_status_body",)}

_JINJA = None


def _env():
    global _JINJA
    if _JINJA is None:
        import jinja2
        _JINJA = jinja2.Environment()
    return _JINJA


def _chain(n):
    from jinja2 import nodes
    if isinstance(n, nodes.Name):
        return n.name
    if isinstance(n, nodes.Getattr):
        c = _chain(n.node)
        return c and c + "." + n.attr
    if isinstance(n, nodes.Getitem) and isinstance(n.arg, nodes.Const):
        c = _chain(n.node)
        return c and "%s[%r]" % (c, n.arg.value) if isinstance(n.arg.value, str) else (c and "%s[%d]" % (c, n.arg.value))
    if isinstance(n, nodes.Filter) and n.name in PASS_FILTERS and n.node is not None:
        return _chain(n.node)
    return None


def uses(template):
    """(paths read, problems) of a template: full attribute paths through pass-through filters; decision filters'
    arguments are not read (their output is the platform's sentence)."""
    from jinja2 import nodes
    try:
        ast = _env().parse(template)
    except Exception as e:  # noqa: BLE001
        return set(), ["unparsable template: %s" % e]
    stored = {n.name for n in ast.find_all(nodes.Name) if n.ctx in ("store", "param")}
    paths, problems = set(), []

    def visit(n):
        # what only decides (a condition, a comparison, a test, a number) is never printed
        if isinstance(n, nodes.CondExpr):
            visit(n.expr1)
            if n.expr2 is not None:
                visit(n.expr2)
            return
        if isinstance(n, nodes.If):
            for b in list(n.body) + list(n.else_) + [x for e in n.elif_ for x in e.body]:
                visit(b)
            return
        if isinstance(n, (nodes.Compare, nodes.Test, nodes.Not)):
            return
        if isinstance(n, nodes.Filter) and n.name in NUMBER_FILTERS:
            for a in list(n.args) + [k.value for k in n.kwargs]:
                visit(a)
            return
        if isinstance(n, nodes.Filter) and (n.name.rsplit(".", 1)[-1] in DECISION_FILTERS
                                            or n.name.rsplit(".", 1)[-1].startswith(RECORD_FILTERS)):
            return
        if isinstance(n, nodes.Call) and isinstance(n.node, nodes.Name) and n.node.name in ("lookup", "query", "q"):
            if n.args and isinstance(n.args[0], nodes.Const) and n.args[0].value in ("vars", "varnames", "env"):
                problems.append("%s('%s', ...)" % (n.node.name, n.args[0].value))
            for a in list(n.args[1:]) + [k.value for k in n.kwargs]:
                visit(a)
            return
        c = _chain(n) if isinstance(n, (nodes.Name, nodes.Getattr, nodes.Getitem, nodes.Filter)) else None
        if c is not None and isinstance(n, (nodes.Name, nodes.Getattr, nodes.Getitem, nodes.Filter)):
            root = re.split(r"[.\[]", c, 1)[0]
            if root not in stored and root not in ("loop", "true", "false", "none", "True", "False", "None"):
                paths.add(c)
            if isinstance(n, nodes.Filter):
                for a in list(n.args) + [k.value for k in n.kwargs]:
                    visit(a)
                inner = n.node
                while isinstance(inner, nodes.Filter) and inner.name in PASS_FILTERS:
                    for a in list(inner.args) + [k.value for k in inner.kwargs]:
                        visit(a)
                    inner = inner.node
            return
        for child in n.iter_child_nodes():
            visit(child)

    visit(ast)
    for p in paths:
        if p.split(".")[0].split("[")[0] in ("vars", "hostvars"):
            problems.append("reads %s" % p)
    return paths, problems


def path_allowed(p):
    root = re.split(r"[.\[]", p, 1)[0]
    if root in ALLOWED:
        return True
    return p in ALLOWED


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from strings(v)


def _leaves(value, key=""):
    """(key path, template) of the templated leaves of a status body; a body that is a template: (None, it)."""
    if isinstance(value, str):
        if "{{" in value or "{%" in value:
            yield (key or None), value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _leaves(v, "%s.%s" % (key, k) if key else k)
    elif isinstance(value, list):
        for v in value:
            yield from _leaves(v, key + "[]")


def _record(key):
    k = key.replace("[]", "")
    parts = k.split(".")
    return any(".".join(parts[:i]) in RECORD_FIELDS for i in range(1, len(parts) + 1))


def _include(task):
    for k in ("ansible.builtin.include_tasks", "include_tasks", "ansible.builtin.import_tasks", "import_tasks"):
        if k in task:
            ref = task[k]
            return ref.get("file") if isinstance(ref, dict) else ref
    return None


def _when(task):
    w = task.get("when")
    return [str(x) for x in (w if isinstance(w, list) else [w])] if w is not None else []


class Walk(object):
    """All task files of the cluster roles under `base` (a copy of the repository may be given) and the files they
    include; tasks flattened as (file, task, skipped, inherited vars)."""

    def __init__(self, base=ROOT):
        self.base = base
        self.eda = os.path.join(base, "eda")
        self.tasks = []
        self.files = set()
        roles = os.path.join(self.eda, "rulebooks", "roles")
        for role in CLUSTER_ROLES:
            d = os.path.join(roles, role, "tasks")
            for f in sorted(os.listdir(d)):
                if f.endswith(".yml"):
                    self._file(os.path.join(d, f), os.path.join(roles, role), False)

    def _resolve(self, cur, ref, role_dir):
        if not ref or "{{" in ref.replace("{{ role_path }}", ""):
            return None
        ref = ref.replace("{{ role_path }}", role_dir)
        if os.path.isabs(ref):
            return os.path.normpath(ref)
        for b in (os.path.join(role_dir, "tasks"), os.path.dirname(cur)):
            p = os.path.normpath(os.path.join(b, ref))
            if os.path.exists(p):
                return p
        return None

    def _file(self, path, role_dir, skip):
        if path in self.files:
            return
        self.files.add(path)
        with open(path) as fh:
            self._tasks(yaml.safe_load(fh) or [], path, role_dir, skip, {})

    def _tasks(self, tasks, path, role_dir, skip, outer_vars):
        for t in tasks or []:
            if not isinstance(t, dict):
                continue
            s = skip or any(SKIP_WHEN in w for w in _when(t))
            tv = dict(outer_vars, **(t.get("vars") or {}))
            self.tasks.append((path, t, s, tv))
            for k in ("block", "rescue", "always"):
                if k in t:
                    self._tasks(t[k], path, role_dir, s, tv)
            p = self._resolve(path, _include(t), role_dir)
            if p:
                self._file(p, role_dir, s)

    # ---- the facts set anywhere, and their definitions
    def include_sites(self, target):
        """(file, task, vars) of every task that includes `target` (by its file name)."""
        out = []
        for path, t, skip, tv in self.tasks:
            inc = _include(t) or ""
            if not skip and os.path.basename(inc.replace("{{ role_path }}", "")) == os.path.basename(target):
                out.append((path, t, tv))
        return out

    def role_defaults(self):
        names = set()
        for role in CLUSTER_ROLES:
            p = os.path.join(self.eda, "rulebooks", "roles", role, "defaults", "main.yml")
            with open(p) as fh:
                names |= set((yaml.safe_load(fh) or {}).keys())
        return names

    def definitions(self):
        out = {}
        for path, t, skip, tv in self.tasks:
            if skip:
                continue
            for k in ("ansible.builtin.set_fact", "set_fact"):
                if isinstance(t.get(k), dict):
                    for name, val in t[k].items():
                        if name != "cacheable":
                            out.setdefault(name, []).append((path, t, tv, val))
        return out

    def sinks(self):
        """(file, task, task vars, what, template) of every value written where a tenant reads it."""
        out = []
        for path, t, skip, tv in self.tasks:
            if skip:
                continue
            for k in ("ansible.builtin.fail", "fail"):
                v = t.get(k)
                if isinstance(v, dict) and "msg" in v:
                    out.append((path, t, tv, "fail message", str(v["msg"])))
                elif isinstance(v, str):
                    out.append((path, t, tv, "fail message", v))
            for k in ("ansible.builtin.assert", "assert"):
                v = t.get(k)
                if isinstance(v, dict):
                    for mk in ("fail_msg", "msg"):
                        if mk in v:
                            out.append((path, t, tv, "assert message", str(v[mk])))
            inc = _include(t) or ""
            base = os.path.basename(inc)
            bodies = []
            if base == "patch_cr_status.yml":
                bodies.append((t.get("vars") or {}).get("cr_status_body"))
            for k in ("hybridsovereign.shc.cr_status", "cr_status"):
                if isinstance(t.get(k), dict):
                    bodies.append(t[k].get("status"))
            for k in ("ansible.builtin.uri", "uri"):
                u = t.get(k)
                if isinstance(u, dict) and "status" in str(u.get("url") or "") \
                        and str(u.get("method") or "GET").upper() in ("PATCH", "PUT", "POST"):
                    # a status written by hand: every value is read as text
                    for s2 in strings(u.get("body")):
                        out.append((path, t, tv, "status written with uri", s2))
            for body in bodies:
                for key, s2 in _leaves(body):
                    if key is None or key.split(".")[-1].split("[")[0] in TEXT_KEYS \
                            or key.split(".")[0].split("[")[0] in TEXT_KEYS:
                        out.append((path, t, tv, "status %s" % (key or "body"), s2))
                    elif not _record(key):
                        out.append((path, t, tv, "status field %s (not a known record field)" % key, "{{ __unknown }}"))
                    else:
                        out.append((path, t, tv, "record " + key, s2))
            if base in INPUTS and base != "patch_cr_status.yml":
                for var in INPUTS[base]:
                    for s in strings((t.get("vars") or {}).get(var)):
                        out.append((path, t, tv, "%s of %s" % (var, base), s))
            for k in ("kubernetes.core.k8s", "k8s"):
                d = (t.get(k) or {}).get("definition") if isinstance(t.get(k), dict) else None
                if isinstance(d, dict) and d.get("kind") == "Event":
                    for f in ("note", "reason", "message"):
                        for s in strings(d.get(f)):
                            out.append((path, t, tv, "event " + f, s))
            for k in ("ansible.builtin.set_fact", "set_fact"):
                if isinstance(t.get(k), dict):
                    for name, val in t[k].items():
                        if name == "po_preflight_condition" or name.endswith("_condition") or name == "conditions":
                            for s in strings(val):
                                out.append((path, t, tv, "condition " + name, s))
        return out

    # ---- the check
    def problems(self):
        defs = self.definitions()
        defaults = self.role_defaults()
        found = []
        seen_fact = {}

        def fact_ok(name, depth):
            if name in seen_fact:
                return seen_fact[name]
            seen_fact[name] = True   # a cycle counts as allowed while it is checked
            ok = name in defs and all(not check(val, dtv, dt, depth + 1, here) for here, dt, dtv, val in defs[name])
            seen_fact[name] = ok
            return ok

        def check(template, tv, task, depth, path):
            bad = []
            for s in strings(template):
                paths, probs = uses(s)
                bad += probs
                for p in sorted(paths):
                    root = re.split(r"[.\[]", p, 1)[0]
                    if path_allowed(p):
                        continue
                    if os.path.basename(path) in INPUTS and root in INPUTS[os.path.basename(path)]:
                        continue
                    if root == "item":
                        lp = task.get("loop", task.get("with_items"))
                        if isinstance(lp, list) and all(not isinstance(x, str) or "{{" not in x for x in lp):
                            continue
                        if isinstance(lp, str) and depth < 6 and not check(lp, tv, task, depth + 1, path):
                            continue
                        bad.append("item of loop %r" % (lp,))
                        continue
                    if root in tv and depth < 6:
                        sub = check(tv[root], {k: v for k, v in tv.items() if k != root}, task, depth + 1, path)
                        if not sub:
                            continue
                        bad += sub
                        continue
                    if root in defs and depth < 6 and fact_ok(root, depth):
                        continue
                    if root not in defs and depth < 6:
                        # an input of this file: checked at every include that gives it
                        sites = [(sp, st, stv) for sp, st, stv in self.include_sites(path) if root in stv]
                        if sites and all(not check(stv[root], {k: v for k, v in stv.items() if k != root}, st,
                                                   depth + 1, sp) for sp, st, stv in sites):
                            continue
                        if not sites and root in defaults:
                            continue
                    bad.append(p)
            return bad

        # a fact set onto an allowed name must itself be allowed (or the name would carry anything)
        for name, ds in sorted(defs.items()):
            if name in ALLOWED and name not in DECISION_RESULTS:
                for path, t, tv, val in ds:
                    extra = NAME_SOURCES.get(name, ())
                    bad = [b for b in check(val, tv, t, 1, path)
                           if not source_allowed(b) and re.split(r"[.\[]", b, 1)[0] not in extra]
                    if bad:
                        found.append("%s: %r sets the allowed %s from %s" % (os.path.relpath(path, self.eda),
                                                                            t.get("name"), name, sorted(set(bad))))
        for path, t, tv, what, template in self.sinks():
            bad = check(template, tv, t, 0, path)
            if what.startswith("record "):
                bad = [b for b in bad if not source_allowed(b) and not record_value_allowed(b)]
            if bad:
                found.append("%s: %r (%s) writes %s" % (os.path.relpath(path, self.eda), t.get("name"), what,
                                                         sorted(set(bad))))
        # the failed task's text: only as an argument of the decision filters, in any walked file
        for p in sorted(self.files):
            with open(p) as fh:
                text = fh.read()
            n = text.count("ansible_failed_result")
            ok = len(re.findall(r"shc_failure_(?:message|is_refusal)\(\s*ansible_failed_result", text))
            if n != ok:
                found.append("%s: ansible_failed_result used outside the decision filters" % os.path.relpath(p, self.eda))
        return found
