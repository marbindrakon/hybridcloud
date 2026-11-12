"""Which operator instance turns an object into an event, where the product says the object lives.

Every job test starts at the job; this one starts at the object. It derives, from the two watches files and from the
manifests that deploy the operators, the set of (kind, namespace class) pairs some operator watches:
  - the primary operator: operator/primary/watches.yaml, in the namespaces of its chart's WATCH_NAMESPACE
    (operator/primary/helm/templates/deployment.yaml: the platform's namespace and the plugins namespace);
  - the namespace operator: operator/namespace/watches.yaml, in the one tenant namespace the Entity role deploys it
    for (operator/primary/roles/entity/tasks/deploy_namespace_operator.yml; the chart in operator/namespace/helm is
    the same deployment, one per tenant namespace).
It then derives where objects can exist from the product's own sources (the tenant RBAC of the Entity role, the
primary chart's templates and its plugin configs, the UI's platform forms, the CRD texts that say "only in the
platform namespace") and asserts each such pair is watched. HOME classifies every CRD kind (a new kind must be placed
here), and a kind only a job writes (no operator) is named with its writer. Finally, for each watched kind, the event
its role publishes reaches a job: a rulebook rule on its kind and reason runs a job template that aap-config defines.
"""
import glob
import os
import re
import unittest

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PRIMARY_DEPLOYMENT = os.path.join(ROOT, "operator/primary/helm/templates/deployment.yaml")
NAMESPACE_DEPLOYMENT = os.path.join(ROOT, "operator/namespace/helm/templates/deployment.yaml")
NS_OPERATOR_BY_ENTITY = os.path.join(ROOT, "operator/primary/roles/entity/tasks/deploy_namespace_operator.yml")
WATCHES = {"primary": os.path.join(ROOT, "operator/primary/watches.yaml"),
           "namespace": os.path.join(ROOT, "operator/namespace/watches.yaml")}
CRDS = os.path.join(ROOT, "operator/config/crd/bases")
PRIMARY_TEMPLATES = os.path.join(ROOT, "operator/primary/helm/templates")
PRIMARY_VALUES = os.path.join(ROOT, "operator/primary/helm/values.yaml")
TENANT_RBAC = os.path.join(ROOT, "operator/primary/roles/entity/vars/main.yml")
UI_FORM = os.path.join(ROOT, "ui/packages/shared/src/components/CreateResourceForm.tsx")
RULEBOOKS = os.path.join(ROOT, "eda/rulebooks")
# the job templates: aap-config, and the bootstrap's controller configuration (TransportLink's are only there)
JOB_TEMPLATES = (os.path.join(ROOT, "aap-config/controller/job_templates.yml"),
                 os.path.join(ROOT, "bootstrap/ansible/roles/aap-controller-config/vars/main.yml"))

PLATFORM, PLUGINS, TENANT = "platform", "plugins", "tenant"
# the chart values that name a namespace class in the primary operator's WATCH_NAMESPACE
CHART_NAMESPACE_CLASS = {".Values.namespace": PLATFORM, ".Values.pluginsNamespace": PLUGINS}

# Where each kind lives (the product's word: CRD texts, docs/platform, docs/tenant). Every CRD kind is here.
HOME = {
    "Entity": {PLATFORM}, "HybridFabric": {PLATFORM}, "CloudGateway": {PLATFORM}, "CloudInfrastructure": {PLATFORM},
    "ProviderEdge": {PLATFORM}, "TransportLink": {PLATFORM}, "UIHealthChecker": {PLATFORM},
    # ceKind platform: "only in the platform namespace"; ceKind customer: the tenant's namespace
    "AttachmentCircuit": {PLATFORM, TENANT},
    "RbacConfig": {PLUGINS}, "AAPConfig": {PLUGINS}, "QuayConfig": {PLUGINS},
    # "created in the integrations namespace" (the plugins namespace)
    "DNSConfig": {PLUGINS},
    "Team": {TENANT}, "Assignment": {TENANT}, "Project": {TENANT}, "Persona": {TENANT},
    "PlatformOpenshift": {TENANT}, "CloudOSO": {TENANT}, "CloudAWS": {TENANT}, "CloudGCP": {TENANT},
    "CloudVirt": {TENANT}, "OpenStackMigration": {TENANT}, "Rbac": {TENANT}, "AAPOrg": {TENANT}, "QuayOrg": {TENANT},
    "Vault": {TENANT}, "VaultKV": {TENANT}, "HybridNetwork": {TENANT}, "NetworkPlacement": {TENANT},
}
# Kinds no operator reconciles, with the job that writes them (status only; nobody acts on their spec).
NOT_RECONCILED = {
    # the tenant's read-only summary: created and written by the Entity job (Entity is watched in the platform's
    # namespace)
    "TenantProfile": "eda/rulebooks/roles/entity_provision/tasks/tenant_profile_write.yml",
    # retired upstream kind (the IaaC plugin); its CRD is still shipped, nothing creates or reads one
    "Iaac": None,
}


def _load(path):
    with open(path) as f:
        return yaml.safe_load(f)


def _read(path):
    with open(path) as f:
        return f.read()


def _crds():
    out = {}
    for path in sorted(glob.glob(os.path.join(CRDS, "*.yaml"))):
        crd = _load(path)
        out[crd["spec"]["names"]["kind"]] = crd
    return out


def _env_value(text, name):
    m = re.search(r"- name: %s\s*\n\s*value:\s*\"?([^\"\n]+)\"?" % re.escape(name), text)
    return m.group(1) if m else None


def operator_namespace_classes():
    """{operator: {namespace class}} from the manifests that deploy the operators."""
    with open(PRIMARY_DEPLOYMENT) as f:
        primary = _env_value(f.read(), "WATCH_NAMESPACE")
    tokens = re.findall(r"\{\{\s*([^}]+?)\s*\}\}", primary or "")
    with open(NS_OPERATOR_BY_ENTITY) as f:
        by_entity = _env_value(f.read(), "WATCH_NAMESPACE")
    with open(NAMESPACE_DEPLOYMENT) as f:
        ns_chart = _env_value(f.read(), "WATCH_NAMESPACE")
    classes = {"primary": {CHART_NAMESPACE_CLASS[t] for t in tokens}}
    # one tenant namespace per namespace operator: the Entity's namespace, and the chart's single namespace value
    if by_entity == "{{ entity_tenant_namespace }}" and re.fullmatch(r"\{\{\s*\.Values\.namespace(\s*\|\s*quote)?\s*\}\}",
                                                                     ns_chart or ""):
        classes["namespace"] = {TENANT}
    else:
        classes["namespace"] = set()
    return classes, tokens, by_entity, ns_chart


def watched_pairs():
    classes = operator_namespace_classes()[0]
    out = {}
    for op, path in WATCHES.items():
        for w in _load(path):
            for c in classes[op]:
                out.setdefault((w["kind"], c), []).append(op)
    return out


def _descriptions(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "description" and isinstance(v, str):
                yield v
            else:
                yield from _descriptions(v)
    elif isinstance(node, list):
        for v in node:
            yield from _descriptions(v)


def required_pairs():
    """{(kind, class): source} where the product's own sources say an object of the kind can exist."""
    crds = _crds()
    plural_kind = {c["spec"]["names"]["plural"]: k for k, c in crds.items()}
    need = {}
    # tenants create these in their namespace (the Entity role's namespace RBAC)
    for role in _load(TENANT_RBAC)["entity_named_roles"]:
        for plural in role.get("crud_resources") or []:
            need.setdefault((plural_kind[plural], TENANT), "tenant RBAC (%s)" % role["role_name"])
    # the primary chart creates these in its namespaces
    for path in sorted(glob.glob(os.path.join(PRIMARY_TEMPLATES, "*.yaml"))):
        with open(path) as f:
            text = f.read()
        for kind, ns in re.findall(r"apiVersion: hybridsovereign\.redhat/\S+\s*\nkind:\s*(\w+)\s*\nmetadata:\s*\n"
                                   r"(?:\s+.*\n)*?\s+namespace:\s*\{\{\s*\$?(\.Values\.\w+)\s*\}\}", text):
            need.setdefault((kind, CHART_NAMESPACE_CLASS[ns]), os.path.relpath(path, ROOT))
    for kind in (_load(PRIMARY_VALUES).get("pluginConfigs") or {}):
        need.setdefault((kind, PLUGINS), "primary chart pluginConfigs")
    # the UI's platform forms create in the platform namespace
    with open(UI_FORM) as f:
        ui = f.read()
    forms = re.search(r"const PLATFORM_FORMS = new Set<\w+>\(\[([^\]]*)\]", ui).group(1)
    kinds = dict(re.findall(r"^\s+(\w+): '(\w+)',$", ui.split("const FORM_KINDS")[1].split("};")[0], re.M))
    for form in re.findall(r"'(\w+)'", forms):
        need.setdefault((kinds[form], PLATFORM), "UI platform form %s" % form)
    # an API text that places (part of) a kind in the platform namespace
    for kind, crd in crds.items():
        for text in _descriptions(crd):
            if re.search(r"only in the platform(?:'s)? namespace", " ".join(text.split())):
                need.setdefault((kind, PLATFORM), "CRD %s description" % kind)
    return need


class WatchedWhereTheyLive(unittest.TestCase):
    def test_the_deployments_are_read(self):
        classes, tokens, by_entity, ns_chart = operator_namespace_classes()
        self.assertEqual(classes["primary"], {PLATFORM, PLUGINS}, tokens)
        self.assertEqual(classes["namespace"], {TENANT}, (by_entity, ns_chart))

    def test_every_crd_kind_is_placed(self):
        crds = set(_crds())
        self.assertEqual(crds - set(HOME) - set(NOT_RECONCILED), set())
        self.assertEqual((set(HOME) | set(NOT_RECONCILED)) - crds, set())
        self.assertEqual(set(HOME) & set(NOT_RECONCILED), set())

    def test_the_sources_agree_with_the_placement(self):
        need = required_pairs()
        # the sources were read (a renamed file or pattern cannot make this pass by finding nothing)
        self.assertIn(("AttachmentCircuit", TENANT), need)
        self.assertIn(("AttachmentCircuit", PLATFORM), need)
        self.assertIn(("ProviderEdge", PLATFORM), need)
        self.assertIn(("RbacConfig", PLUGINS), need)
        self.assertIn(("TransportLink", PLATFORM), need)
        for (kind, cls), source in sorted(need.items()):
            with self.subTest(kind=kind, namespace=cls):
                self.assertIn(cls, HOME.get(kind, set()), "%s says %s lives in the %s namespace" % (source, kind, cls))

    def test_every_object_is_watched_where_it_lives(self):
        watched = watched_pairs()
        missing = sorted((k, c) for k, classes in HOME.items() for c in classes if (k, c) not in watched)
        self.assertEqual(missing, [], "no operator turns these objects into events")

    def test_the_platform_circuit_is_watched_by_the_primary_operator(self):
        self.assertEqual(watched_pairs()[("AttachmentCircuit", PLATFORM)], ["primary"])
        self.assertEqual(watched_pairs()[("AttachmentCircuit", TENANT)], ["namespace"])

    def test_no_object_is_reconciled_by_two_operators(self):
        for pair, ops in watched_pairs().items():
            self.assertEqual(len(ops), 1, pair)

    def test_a_kind_watched_by_both_operators_keeps_one_finalizer(self):
        by = {op: {w["kind"]: w for w in _load(path)} for op, path in WATCHES.items()}
        for kind in set(by["primary"]) & set(by["namespace"]):
            with self.subTest(kind=kind):
                self.assertEqual(by["primary"][kind]["finalizer"]["name"], by["namespace"][kind]["finalizer"]["name"])
                self.assertEqual(by["primary"][kind].get("reconcilePeriod"), by["namespace"][kind].get("reconcilePeriod"))

    def test_kinds_without_an_operator_name_their_writer(self):
        watched = {k for k, _ in watched_pairs()}
        for kind, writer in NOT_RECONCILED.items():
            with self.subTest(kind=kind):
                self.assertNotIn(kind, watched)
                if writer:
                    with open(os.path.join(ROOT, writer)) as f:
                        self.assertIn("kind: %s" % kind, f.read())


def _rules():
    out = []
    for path in sorted(glob.glob(os.path.join(RULEBOOKS, "*.yml"))):
        doc = _load(path)
        if not isinstance(doc, list):
            continue
        for rb in doc:
            if isinstance(rb, dict) and "sources" in rb:
                for rule in rb.get("rules") or []:
                    out.append((os.path.basename(path), rule))
    return out


class TheEventReachesAJob(unittest.TestCase):
    def test_every_watched_kind_has_a_create_and_a_delete_route(self):
        templates = {t["name"] for path in JOB_TEMPLATES for t in _load(path)["controller_templates"]}
        rules = _rules()
        for op, path in WATCHES.items():
            for w in _load(path):
                kind = w["kind"]
                for reason in ("%sCreateRequested" % kind, "%sDeleteRequested" % kind):
                    with self.subTest(operator=op, kind=kind, reason=reason):
                        role = "role" if reason.endswith("CreateRequested") else None
                        role_dir = os.path.join(os.path.dirname(path), "roles",
                                                w["role"] if role else w["finalizer"]["role"])
                        text = "".join(_read(p) for p in glob.glob(os.path.join(role_dir, "tasks", "*.yml")))
                        if reason not in text:
                            continue  # this role publishes no such event (e.g. a delete that only cleans up)
                        routes = [r for _, r in rules
                                  if re.search(r'regarding\.kind (?:== "%s"|in \[[^\]]*"%s")' % (kind, kind),
                                               r.get("condition", ""))
                                  and reason in r.get("condition", "")]
                        self.assertTrue(routes, "no rulebook rule takes %s of %s" % (reason, kind))
                        for r in routes:
                            # the rules route on kind and reason only, never on the operator or the namespace
                            self.assertNotIn("namespace", r["condition"])
                            self.assertNotIn("reportingController", r["condition"])
                            self.assertIn(r["action"]["run_job_template"]["name"], templates)


# What this round's design (cluster on a hybrid network, revision 2; the service filter; the router advertisement
# service) promises in status, who writes it, and where its object lives: (kind, class, status key, writer files).
DESIGN_STATUS = [
    ("HybridNetwork", TENANT, "platformAccess", ["eda/rulebooks/roles/hybridnetwork_provision/tasks/platform_access.yml"]),
    ("HybridNetwork", TENANT, "PlatformReachable", ["eda/rulebooks/roles/hybridnetwork_provision/files/shc_platform_access.py"]),
    ("NetworkPlacement", TENANT, "'use'", ["eda/rulebooks/roles/networkplacement_provision/tasks/backend_cloudoso.yml"]),
    ("NetworkPlacement", TENANT, "routerAdvertisement",
     ["eda/rulebooks/roles/networkplacement_provision/tasks/router_advertisement.yml"]),
    ("PlatformOpenshift", TENANT, "platformPrefixes",
     ["eda/rulebooks/roles/platformopenshift_provision/tasks/cloud_internal_reach.yml"]),
    ("PlatformOpenshift", TENANT, "Preflight", ["eda/rulebooks/roles/platformopenshift_provision/tasks/main.yml"]),
    # TenantProfile is not reconciled: the Entity job (Entity: platform namespace) writes it into the tenant's namespace
    ("Entity", PLATFORM, "reservedRanges", ["eda/rulebooks/roles/entity_provision/tasks/tenant_profile_compute.yml"]),
    ("Entity", PLATFORM, "managedClusterRanges", ["eda/rulebooks/roles/entity_provision/tasks/tenant_profile_compute.yml"]),
    ("ProviderEdge", PLATFORM, "ceKinds", ["eda/rulebooks/roles/provideredge_provision/tasks/main.yml"]),
    ("HybridFabric", PLATFORM, "reservedRanges", ["eda/rulebooks/roles/hybridfabric_provision/tasks/main.yml"]),
    ("HybridFabric", PLATFORM, "hf_service.status", ["eda/rulebooks/roles/hybridfabric_provision/tasks/main.yml"]),
    ("AttachmentCircuit", PLATFORM, "advertisedRanges",
     ["eda/rulebooks/roles/attachmentcircuit_provision/tasks/platform_circuit.yml"]),
]


class DesignStatusHasAWriterAndAWatcher(unittest.TestCase):
    def test_each_promised_status_is_written_by_a_job_its_operator_starts(self):
        watched = watched_pairs()
        for kind, cls, key, writers in DESIGN_STATUS:
            with self.subTest(kind=kind, key=key):
                self.assertIn((kind, cls), watched, "nothing starts the job that writes %s.%s" % (kind, key))
                self.assertTrue(any(key in _read(os.path.join(ROOT, w)) for w in writers), (kind, key, writers))


if __name__ == "__main__":
    unittest.main()
