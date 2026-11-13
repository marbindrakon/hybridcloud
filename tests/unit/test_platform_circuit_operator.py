"""The primary operator's AttachmentCircuit roles (operator/primary/roles/attachmentcircuit, _delete): the platform's
own circuits (ceKind platform) live in the platform's namespace, where no namespace operator runs.

- Parity: the primary roles are the namespace operator's roles task for task, except the namespace check (the
  platform's namespace has no Entity; another ceKind, or another watched namespace, is refused) and, on delete, the
  release of a refused circuit that was never published.
- The decisions in ansible-playbook: which circuits are refused (and with which message), whether a refusal is
  already recorded, whether a deleted circuit is released without a teardown event.
- The roles end to end as the operator runs them (the CR and ansible_operator_meta as extra vars, the role's
  defaults reading PLATFORM_NAMESPACE from the environment), with fake operator_sdk.util.k8s_status and
  kubernetes.core modules and the Kafka publish replaced by a trace: a new platform circuit is acknowledged
  (Creating) and its AttachmentCircuitCreateRequested event published with the same shape the namespace operator
  publishes for a tenant's circuit; a ready one is skipped; Retry now publishes a reconcile; a customer circuit in
  the platform's namespace gets Ready False/InvalidSpec, phase Failed and no event, once; the delete publishes
  AttachmentCircuitDeleteRequested and holds the finalizer until the teardown reports deletionComplete; a refused
  circuit is released at once; a missing PLATFORM_NAMESPACE fails naming it.
Runs in ansible-playbook (skipped when absent).
"""
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_cases  # noqa: E402

LIB = os.path.join(ROOT, "eda", "rulebooks", "collections", "ansible_collections", "hybridsovereign", "shc",
                   "plugins", "module_utils")
sys.path.insert(0, LIB)
import shc_status_reasons as R  # noqa: E402

PRIMARY = os.path.join(ROOT, "operator", "primary", "roles")
NAMESPACE = os.path.join(ROOT, "operator", "namespace", "roles")
COMMON = os.path.join(ROOT, "operator", "roles", "_common")
PLATFORM_NS = "platform-ns"
TENANT_NS = "entity-example"

NS_ONLY = ["Read namespace for entity context", "Assert namespace is Entity-managed"]
GATE = ["Require the platform namespace setting", "Decide whether this operator realizes the circuit",
        "Refuse a circuit the platform's namespace does not realize"]
DELETE_GATE = ["Require the platform namespace setting", "Tell whether the circuit was refused and never published",
               "Release a refused circuit at once (no teardown event)"]


def role_tasks(base, role):
    return load(os.path.join(base, role, "tasks", "main.yml"))


class Parity(unittest.TestCase):
    def test_reconcile_role_is_the_namespace_operators_but_the_gate(self):
        ns, pr = role_tasks(NAMESPACE, "attachmentcircuit"), role_tasks(PRIMARY, "attachmentcircuit")
        self.assertEqual(len(ns), 1)
        self.assertEqual(len(pr), 1)
        self.assertEqual(ns[0]["rescue"], pr[0]["rescue"])
        ns_block = [t for t in ns[0]["block"] if t["name"] not in NS_ONLY]
        pr_block = [t for t in pr[0]["block"] if t["name"] not in GATE]
        self.assertEqual(len(ns_block), len(ns[0]["block"]) - 2)
        self.assertEqual(len(pr_block), len(pr[0]["block"]) - 3)
        self.assertEqual(ns_block, pr_block)
        # the gate sits where the Entity check sits: after the Retry-now read, before any skip, status write or event
        names = [t["name"] for t in pr[0]["block"]]
        self.assertEqual(names[2:5], GATE)
        self.assertEqual(names[1], "Read the Retry-now request")

    def test_delete_role_is_the_namespace_operators_after_the_release(self):
        ns, pr = role_tasks(NAMESPACE, "attachmentcircuit_delete"), role_tasks(PRIMARY, "attachmentcircuit_delete")
        self.assertEqual(ns[0]["rescue"], pr[0]["rescue"])
        self.assertEqual([t["name"] for t in pr[0]["block"][:3]], DELETE_GATE)
        self.assertEqual(pr[0]["block"][3:], ns[0]["block"])

    def test_refusal_publishes_nothing_and_ends_the_reconcile(self):
        refuse = load(os.path.join(PRIMARY, "attachmentcircuit", "tasks", "refuse.yml"))
        self.assertEqual(refuse[-1], {"name": "End the reconcile of a refused circuit (no event)",
                                      "ansible.builtin.meta": "end_host"})
        with open(os.path.join(PRIMARY, "attachmentcircuit", "tasks", "refuse.yml")) as f:
            text = f.read()
        self.assertNotIn("publish_operator_event", text)
        status = by_name(refuse, "Record the refusal")["operator_sdk.util.k8s_status"]["status"]
        self.assertEqual(status["summary"], R.REASONS["InvalidSpec"]["summary"])
        self.assertEqual(status["conditions"][0]["reason"], "InvalidSpec")
        self.assertEqual(R.REASONS["InvalidSpec"]["class"], R.FAILED)

    def test_both_roles_read_the_platform_namespace_from_the_deployment(self):
        for role in ("attachmentcircuit", "attachmentcircuit_delete"):
            d = load(os.path.join(PRIMARY, role, "defaults", "main.yml"))
            self.assertEqual(d["attachmentcircuit_platform_namespace"], "{{ lookup('ansible.builtin.env', 'PLATFORM_NAMESPACE') }}")
        with open(os.path.join(ROOT, "operator/primary/helm/templates/deployment.yaml")) as f:
            text = f.read()
        self.assertIn('- name: PLATFORM_NAMESPACE\n              value: "{{ .Values.namespace }}"', text)

    def test_primary_operator_rbac_covers_the_kind_with_status_and_finalizers(self):
        import yaml
        with open(os.path.join(ROOT, "operator/primary/helm/templates/clusterrole.yaml")) as f:
            text = f.read().replace('{{- include "primary-operator.labels" . | nindent 4 }}', "")
        role = next(d for d in yaml.safe_load_all(text) if d and d["metadata"]["name"] == "hybridsovereign-primary-operator"
                    and d["kind"] == "ClusterRole")
        rule = next(r for r in role["rules"] if "hybridsovereign.redhat" in r["apiGroups"])
        # "*" is every resource of the group and its subresources (status, finalizers)
        self.assertEqual(rule["resources"], ["*"])
        self.assertTrue({"get", "list", "watch", "update", "patch"} <= set(rule["verbs"]))


def _ac(ns=PLATFORM_NS, kind="platform", status=None, generation=1, deleting=False, annotations=None):
    spec = {"edgeRef": {"name": "edge-a", "bearer": "b1"}, "routing": {"protocol": "bgp", "bgp": {"peerAsn": 64512}}}
    if kind is not None:
        spec["ceKind"] = kind
    if kind == "customer":
        spec["network"] = "net-a"
    meta = {"name": "hub-a", "namespace": ns, "uid": "uid-1", "generation": generation,
            "annotations": dict(annotations or {})}
    if deleting:
        meta["deletionTimestamp"] = "2026-11-12T10:00:00Z"
    return {"apiVersion": "hybridsovereign.redhat/v1alpha1", "kind": "AttachmentCircuit", "metadata": meta,
            "spec": spec, "status": status or {}}


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Decisions(unittest.TestCase):
    def test_which_circuits_are_refused(self):
        role = role_tasks(PRIMARY, "attachmentcircuit")
        facts = by_name(role, "Set AttachmentCircuit CR facts")["ansible.builtin.set_fact"]
        gate = by_name(role, "Decide whether this operator realizes the circuit")["ansible.builtin.set_fact"]
        steps = [{"ansible.builtin.set_fact": {"_hybridsovereign_redhat_attachmentcircuit": "{{ case.cr }}",
                                               "ansible_operator_meta": "{{ case.meta }}"}},
                 {"ansible.builtin.set_fact": facts}, {"ansible.builtin.set_fact": gate}]
        cases = []
        for ns, kind in ((PLATFORM_NS, "platform"), (PLATFORM_NS, "customer"), (PLATFORM_NS, None),
                         ("plugins-ns", "platform")):
            cr = _ac(ns=ns, kind=kind)
            cases.append({"cr": cr, "meta": {"name": "hub-a", "namespace": ns}})
        out = run_cases(steps, cases, "attachmentcircuit_refusal | default('', true) | trim",
                        {"attachmentcircuit_platform_namespace": PLATFORM_NS})
        self.assertEqual(out[0], "")
        for msg in out[1:]:
            self.assertNotIn("\n", msg)
            self.assertTrue(msg.startswith("AttachmentCircuit hub-a: "), msg)
        self.assertIn("spec.ceKind customer is not realized in the platform's namespace", out[1])
        self.assertIn("spec.ceKind customer", out[2])  # the CRD's default
        self.assertIn("namespace plugins-ns is not the platform's namespace (platform-ns)", out[3])

    def test_a_recorded_refusal_is_not_written_again(self):
        steps = load(os.path.join(PRIMARY, "attachmentcircuit", "tasks", "refuse.yml"))[:1]
        msg = "AttachmentCircuit hub-a: refused"
        ready = {"type": "Ready", "status": "False", "reason": "InvalidSpec", "message": msg}
        cases = [{"status": "failed", "message": msg, "conditions": [ready]},
                 {"status": "failed", "message": "older text", "conditions": [ready]},
                 {"status": "reconciling", "message": msg, "conditions": [dict(ready, reason="AwaitingEDA")]},
                 {}]
        steps = [{"ansible.builtin.set_fact": {"_hybridsovereign_redhat_attachmentcircuit": "{{ {'status': case} }}",
                                               "attachmentcircuit_refusal": msg}}] + steps
        self.assertEqual(run_cases(steps, cases, "_attachmentcircuit_refusal_recorded | bool"),
                         [True, False, False, False])

    def test_which_deletions_are_released_without_an_event(self):
        role = role_tasks(PRIMARY, "attachmentcircuit_delete")
        decide = by_name(role, "Tell whether the circuit was refused and never published")["ansible.builtin.set_fact"]
        steps = [{"ansible.builtin.set_fact": {"_hybridsovereign_redhat_attachmentcircuit": "{{ case.cr }}",
                                               "ansible_operator_meta": "{{ case.meta }}"}},
                 {"ansible.builtin.set_fact": decide}]
        published = {"delivery": {"eventId": "e1", "reason": "AttachmentCircuitCreateRequested"}}
        rows = [(PLATFORM_NS, "platform", None), (PLATFORM_NS, "platform", published), (PLATFORM_NS, "customer", None),
                (PLATFORM_NS, "customer", published), ("plugins-ns", "platform", None)]
        cases = [{"cr": _ac(ns=ns, kind=k, status=st), "meta": {"name": "hub-a", "namespace": ns}} for ns, k, st in rows]
        self.assertEqual(run_cases(steps, cases, "attachmentcircuit_delete_unrealized | bool",
                                   {"attachmentcircuit_platform_namespace": PLATFORM_NS}),
                         [False, False, True, False, True])


# ---- the roles end to end with fake modules ------------------------------------------------------------------------

FAKE_MODULE = '''#!/usr/bin/python
import json, os
from ansible.module_utils.basic import AnsibleModule

NAMES = ("api_version", "kind", "name", "namespace", "status", "conditions", "state", "definition", "host",
         "api_key", "validate_certs", "label_selectors", "field_selectors", "merge_type", "wait", "force")


def main():
    m = AnsibleModule(argument_spec={n: dict(type="raw") for n in NAMES}, supports_check_mode=True)
    p = {k: v for k, v in m.params.items() if v is not None}
    if %(INFO)s:
        with open(os.environ["FAKE_K8S"]) as f:
            objects = json.load(f)
        key = "%%s/%%s/%%s" %% (p.get("kind"), p.get("namespace") or "", p.get("name") or "")
        m.exit_json(changed=False, resources=objects.get(key, []))
    with open(os.environ["SHC_TRACE"], "a") as f:
        f.write(json.dumps({%(TAG)s: p}) + "\\n")
    m.exit_json(changed=True)


main()
'''

AMQ_FAKE = """---
- name: Skip AMQ publish when disabled
  ansible.builtin.meta: end_host
  when: not (amq_kafka_enabled | default(true) | bool)

- name: Publish (trace)
  ansible.builtin.shell: cat >> "$SHC_TRACE"
  args:
    stdin: "{{ {'published': amq_event_payload} | to_json }}"
  changed_when: true
"""


def run_role(base, role, cr, env_ns=PLATFORM_NS, extra=None):
    """Run an operator role as ansible-operator does; returns (rc, [trace entries], output tail)."""
    tmp = tempfile.mkdtemp(prefix="shc-op-test-")
    try:
        roles = os.path.join(tmp, "roles")
        shutil.copytree(COMMON, os.path.join(roles, "_common"))
        with open(os.path.join(roles, "_common", "tasks", "amq_publish.yml"), "w") as f:
            f.write(AMQ_FAKE)
        shutil.copytree(os.path.join(base, role), os.path.join(roles, role))
        coll = os.path.join(tmp, "collections", "ansible_collections")
        for ns, name, module, info, tag in (("operator_sdk", "util", "k8s_status", False, "status_write"),
                                            ("kubernetes", "core", "k8s_info", True, "info"),
                                            ("kubernetes", "core", "k8s", False, "k8s")):
            d = os.path.join(coll, ns, name, "plugins", "modules")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(coll, ns, name, "galaxy.yml"), "w") as f:
                f.write("namespace: %s\nname: %s\nversion: 1.0.0\n" % (ns, name))
            with open(os.path.join(d, module + ".py"), "w") as f:
                f.write(FAKE_MODULE % {"INFO": "True" if info else "False", "TAG": json.dumps(tag)})
        trace = os.path.join(tmp, "trace.jsonl")
        open(trace, "w").close()
        objects = {"AttachmentCircuit/%s/%s" % (cr["metadata"]["namespace"], cr["metadata"]["name"]): [cr],
                   "Namespace//%s" % TENANT_NS: [{"metadata": {"name": TENANT_NS,
                                                               "labels": {"hybridsovereign.redhat/entity": "example"}}}]}
        with open(os.path.join(tmp, "k8s.json"), "w") as f:
            json.dump(objects, f)
        with open(os.path.join(tmp, "vars.json"), "w") as f:
            json.dump(dict({"_hybridsovereign_redhat_attachmentcircuit": cr,
                            "ansible_operator_meta": {"name": cr["metadata"]["name"],
                                                      "namespace": cr["metadata"]["namespace"]}}, **(extra or {})), f)
        with open(os.path.join(tmp, "play.yml"), "w") as f:
            f.write(textwrap.dedent("""\
                - hosts: localhost
                  connection: local
                  gather_facts: false
                  roles:
                    - %s
                """ % role))
        with open(os.path.join(tmp, "ansible.cfg"), "w") as f:
            f.write("[defaults]\nroles_path = %s\ncollections_path = %s\n" % (roles, os.path.join(tmp, "collections")))
        env = dict(os.environ, ANSIBLE_CONFIG=os.path.join(tmp, "ansible.cfg"), SHC_TRACE=trace,
                   FAKE_K8S=os.path.join(tmp, "k8s.json"), ANSIBLE_LOCALHOST_WARNING="false",
                   ANSIBLE_INVENTORY_UNPARSED_WARNING="false", ANSIBLE_REMOTE_TMP=os.path.join(tmp, "remote-tmp"),
                   ANSIBLE_LOCAL_TEMP=os.path.join(tmp, "local-tmp"), ANSIBLE_COLLECTIONS_PATH=os.path.join(tmp, "collections"))
        env.pop("PLATFORM_NAMESPACE", None)
        if env_ns is not None:
            env["PLATFORM_NAMESPACE"] = env_ns
        proc = subprocess.run(["ansible-playbook", "-i", "localhost,", "play.yml", "-e", "@vars.json"], cwd=tmp,
                              env=env, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
        entries = []
        with open(trace) as f:
            for line in f:
                e = json.loads(line)
                for v in e.values():
                    if isinstance(v, dict) and isinstance(v.get("status"), str):
                        v["status"] = ast.literal_eval(v["status"])
                entries.append(e)
        return proc.returncode, entries, proc.stdout[-3000:] + proc.stderr[-1500:]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def published(entries):
    return [e["published"] for e in entries if "published" in e]


def status_writes(entries):
    return [e["status_write"]["status"] for e in entries if "status_write" in e]


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class EndToEnd(unittest.TestCase):
    maxDiff = None

    def test_a_new_platform_circuit_is_published_like_a_tenants_circuit(self):
        rc, entries, out = run_role(PRIMARY, "attachmentcircuit", _ac())
        self.assertEqual(rc, 0, out)
        events = published(entries)
        self.assertEqual(len(events), 1, out)
        ev = events[0]
        self.assertEqual((ev["reason"], ev["action"], ev["reportingController"]),
                         ("AttachmentCircuitCreateRequested", "Create", "attachmentcircuit-operator"))
        self.assertEqual(ev["regarding"], {"apiVersion": "hybridsovereign.redhat/v1alpha1", "kind": "AttachmentCircuit",
                                           "name": "hub-a", "namespace": PLATFORM_NS, "uid": "uid-1"})
        self.assertEqual(json.loads(ev["note"])["ceKind"], "platform")
        writes = status_writes(entries)
        self.assertEqual(writes[0]["phase"], "Creating")
        self.assertEqual(writes[0]["conditions"][0]["reason"], "AwaitingEDA")
        self.assertTrue(any((w.get("delivery") or {}).get("eventId") == ev["eventId"] for w in writes))
        self.assertTrue(any((w.get("delivery") or {}).get("publishedAt") for w in writes))

        # the namespace operator, for a tenant's circuit: the same writes and the same event, but the namespace
        tenant = _ac(ns=TENANT_NS, kind="customer")
        rc2, entries2, out2 = run_role(NAMESPACE, "attachmentcircuit", tenant, env_ns=None)
        self.assertEqual(rc2, 0, out2)
        ev2 = published(entries2)[0]
        self.assertEqual(sorted(ev2), sorted(ev))
        self.assertEqual({k: ev2[k] for k in ("reason", "action", "reportingController", "cluster")},
                         {k: ev[k] for k in ("reason", "action", "reportingController", "cluster")})
        shape = lambda ws: [sorted(w) for w in ws]  # noqa: E731
        self.assertEqual(shape(status_writes(entries2)), shape(writes))

    def test_a_ready_platform_circuit_is_skipped_and_retry_now_publishes(self):
        ready = {"status": "ready", "ready": True, "observedGeneration": 1, "phase": "Ready",
                 "conditions": [{"type": "Ready", "status": "True", "reason": "Ready"}]}
        rc, entries, out = run_role(PRIMARY, "attachmentcircuit", _ac(status=ready))
        self.assertEqual((rc, entries), (0, []), out)
        req = {"ansible.sdk.operatorframework.io/reconcileNow": "2026-11-12T10:00:00Z"}
        rc, entries, out = run_role(PRIMARY, "attachmentcircuit", _ac(status=ready, annotations=req))
        self.assertEqual(rc, 0, out)
        self.assertEqual([e["reason"] for e in published(entries)], ["AttachmentCircuitReconcileRequested"])
        # a re-run of a ready circuit keeps Ready: only Reconciling/Queued before the event
        self.assertNotIn("ready", status_writes(entries)[0])

    def test_a_customer_circuit_in_the_platform_namespace_is_refused_once(self):
        rc, entries, out = run_role(PRIMARY, "attachmentcircuit", _ac(kind="customer"))
        self.assertEqual(rc, 0, out)
        self.assertEqual(published(entries), [])
        writes = status_writes(entries)
        self.assertEqual(len(writes), 1, out)
        w = writes[0]
        self.assertEqual((w["status"], w["ready"], w["phase"]), ("failed", False, "Failed"))
        self.assertEqual(w["conditions"][0]["reason"], "InvalidSpec")
        self.assertIn("spec.ceKind customer is not realized in the platform's namespace", w["message"])
        self.assertEqual(w["summary"], R.REASONS["InvalidSpec"]["summary"])
        # the next reconcile (period, Retry now) finds it recorded: no write, no event
        recorded = {"status": "failed", "ready": False, "phase": "Failed", "message": w["message"],
                    "conditions": [{"type": "Ready", "status": "False", "reason": "InvalidSpec", "message": w["message"]}]}
        req = {"ansible.sdk.operatorframework.io/reconcileNow": "2026-11-12T10:00:00Z"}
        rc, entries, out = run_role(PRIMARY, "attachmentcircuit", _ac(kind="customer", status=recorded, annotations=req))
        self.assertEqual((rc, entries), (0, []), out)

    def test_a_platform_circuit_outside_the_platform_namespace_is_refused(self):
        rc, entries, out = run_role(PRIMARY, "attachmentcircuit", _ac(ns="plugins-ns"))
        self.assertEqual(rc, 0, out)
        self.assertEqual(published(entries), [])
        self.assertIn("namespace plugins-ns is not the platform's namespace (platform-ns)",
                      status_writes(entries)[0]["message"])

    def test_a_missing_platform_namespace_fails_naming_it(self):
        rc, entries, out = run_role(PRIMARY, "attachmentcircuit", _ac(), env_ns=None)
        self.assertNotEqual(rc, 0)
        self.assertEqual(published(entries), [])
        w = status_writes(entries)[-1]
        self.assertEqual(w["conditions"][0]["reason"], "OperatorFailed")
        self.assertIn("PLATFORM_NAMESPACE", w["message"])

    def test_the_delete_publishes_the_teardown_and_holds_until_it_completes(self):
        st = {"status": "ready", "ready": True, "phase": "Ready",
              "delivery": {"eventId": "e1", "reason": "AttachmentCircuitCreateRequested", "completedEventId": "e1",
                           "publishedAt": "2026-11-12T09:00:00Z"}}
        rc, entries, out = run_role(PRIMARY, "attachmentcircuit_delete", _ac(status=st, deleting=True))
        self.assertNotEqual(rc, 0, "the finalizer must hold until the teardown reports")
        self.assertIn("Waiting for EDA to complete deletion", out)
        events = published(entries)
        self.assertEqual([(e["reason"], e["action"]) for e in events], [("AttachmentCircuitDeleteRequested", "Delete")])
        self.assertEqual(events[0]["regarding"]["namespace"], PLATFORM_NS)
        self.assertTrue(any(w.get("phase") == "Deleting" for w in status_writes(entries)))
        self.assertFalse(any(w.get("status") == "failed" for w in status_writes(entries)))
        # the teardown job reported: released (role succeeds, ansible-operator removes the finalizer), no new event
        done = dict(st, deletionComplete=True, phase="Deleting",
                    delivery=dict(st["delivery"], eventId="e2", reason="AttachmentCircuitDeleteRequested",
                                  completedEventId="e2"))
        rc, entries, out = run_role(PRIMARY, "attachmentcircuit_delete", _ac(status=done, deleting=True))
        self.assertEqual(rc, 0, out)
        self.assertEqual(published(entries), [])

    def test_a_refused_circuit_is_released_without_an_event(self):
        refused = {"status": "failed", "ready": False, "phase": "Failed",
                   "conditions": [{"type": "Ready", "status": "False", "reason": "InvalidSpec"}]}
        rc, entries, out = run_role(PRIMARY, "attachmentcircuit_delete", _ac(kind="customer", status=refused,
                                                                             deleting=True))
        self.assertEqual((rc, entries), (0, []), out)


if __name__ == "__main__":
    unittest.main()
