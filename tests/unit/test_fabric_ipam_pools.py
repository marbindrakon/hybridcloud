"""The pool wrapper (eda/common/tasks/fabric_ipam_pools.yml) and the IPAM pool actions through fabric_ipam.yml, in
ansible-playbook against an in-memory API: generic defaults carry no pool, a pool from realization is ensured and
chosen, a hold-down of 0 arrives as 0, the allowance, release and re-allocation, and a realization that would drop an
allocated block fails the job, a tolerant caller goes on with the ledger's pools and a realization change of the
hold-down reaches the ledger before a release. Skipped without ansible-playbook."""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

STATE = {"cm": None, "rv": 0}
LOCK = threading.Lock()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        with LOCK:
            if "/configmaps/" in self.path and STATE["cm"] is not None:
                return self.send(200, STATE["cm"])
            if self.path == "/dump":
                return self.send(200, STATE["cm"] or {})
        self.send(404, {"message": "not found"})

    def do_POST(self):
        b = self.body()
        with LOCK:
            if STATE["cm"] is not None:
                return self.send(409, {})
            STATE["rv"] += 1
            b["metadata"]["resourceVersion"] = str(STATE["rv"])
            STATE["cm"] = b
        self.send(201, b)

    def do_PUT(self):
        b = self.body()
        with LOCK:
            if STATE["cm"] is None or b["metadata"].get("resourceVersion") != STATE["cm"]["metadata"]["resourceVersion"]:
                return self.send(409, {})
            STATE["rv"] += 1
            b["metadata"]["resourceVersion"] = str(STATE["rv"])
            STATE["cm"] = b
        self.send(200, b)

    def do_PATCH(self):
        self.body()
        self.send(200, {})




ROLE = r'''---
- name: Create the ledger
  ansible.builtin.command:
    argv: [python3, "{{ role_path }}/../../../common/files/shc_ledger.py"]
    stdin: >-
      {{ {'action': 'ledger-ensure', 'api': services_api_host, 'token': services_bearer_token,
          'namespace': fabric_platform_namespace, 'fabric': 'lab', 'fabric_uid': 'u', 'pools': {'vni': [1, 9]}} | to_json }}
  changed_when: false

- name: Realization
  ansible.builtin.include_tasks: "{{ role_path }}/../../../common/tasks/fabric_realization.yml"
  vars:
    fab_real_fabric: lab

- name: No pools in the generic defaults
  ansible.builtin.assert:
    that:
      - fab_real.ipam.publicPools == {}
      - fab_real.ipam.holdDownDays == 7
      - fab_real.ipam.provider == 'ledger'

- name: Pools task with no pools
  ansible.builtin.include_tasks: "{{ role_path }}/../../../common/tasks/fabric_ipam_pools.yml"
  vars:
    fab_ipam_pools_fabric: lab

- name: No pool
  ansible.builtin.assert:
    that: fab_ipam_pool == ''

- name: Realization with a test pool (hold-down 0, allowance 1)
  ansible.builtin.include_tasks: "{{ role_path }}/../../../common/tasks/fabric_realization.yml"
  vars:
    fab_real_fabric: lab
    fabric_realization:
      ipam: {holdDownDays: 0}
      fabrics:
        lab:
          ipam:
            publicPools:
              test-v6: {family: 6, prefixes: ["2001:db8:4000::/55"], blockLengths: [56, 60], defaultBlock: 56,
                        perTenantBlocks: 1}

- name: Pools task
  ansible.builtin.include_tasks: "{{ role_path }}/../../../common/tasks/fabric_ipam_pools.yml"
  vars:
    fab_ipam_pools_fabric: lab

- name: The pool is chosen and hold-down 0 arrived as 0
  ansible.builtin.assert:
    that:
      - fab_ipam_pool == 'test-v6'
      - fab_ipam_pools_usage[0].holdDownDays == 0
      - fab_ipam_pools_usage[0].blocksFree == 2

- name: Allocate a block
  ansible.builtin.include_tasks: "{{ role_path }}/../../../common/tasks/fabric_ipam.yml"
  vars:
    fab_ipam: {action: ipam-pool-allocate, fabric: lab, pool: "{{ fab_ipam_pool }}", owner: HybridNetwork/entity-acme/a,
               tenant: acme, want: {family: 6}}

- name: Allocated
  ansible.builtin.assert:
    that: fab_ipam_result.prefixes == ['2001:db8:4000::/56']

- name: Second network of the tenant waits on the allowance
  ansible.builtin.include_tasks: "{{ role_path }}/../../../common/tasks/fabric_ipam.yml"
  vars:
    fab_ipam: {action: ipam-pool-allocate, fabric: lab, owner: HybridNetwork/entity-acme/b, tenant: acme,
               want: [{family: 6, prefixLength: 56}]}

- name: Allowance
  ansible.builtin.assert:
    that:
      - fab_ipam_result.waitingFor == 'allowance'
      - "'allowance is used (1 of 1 blocks)' in fab_ipam_result.waiting"

- name: Release
  ansible.builtin.include_tasks: "{{ role_path }}/../../../common/tasks/fabric_ipam.yml"
  vars:
    fab_ipam: {action: ipam-pool-release, fabric: lab, owner: HybridNetwork/entity-acme/a}

- name: Released (and reported changed)
  ansible.builtin.assert:
    that:
      - fab_ipam_result.released == ['2001:db8:4000::/56']
      - _fab_ipam is changed

- name: With hold-down 0 the same block goes to the waiting network
  ansible.builtin.include_tasks: "{{ role_path }}/../../../common/tasks/fabric_ipam.yml"
  vars:
    fab_ipam: {action: ipam-pool-allocate, fabric: lab, owner: HybridNetwork/entity-acme/b, tenant: acme,
               want: [{family: 6, prefixLength: 56}]}

- name: Re-allocated
  ansible.builtin.assert:
    that: fab_ipam_result.prefixes == ['2001:db8:4000::/56']

- name: A realization that drops the allocated block
  ansible.builtin.include_tasks: "{{ role_path }}/../../../common/tasks/fabric_realization.yml"
  vars:
    fab_real_fabric: lab
    fabric_realization:
      fabrics:
        lab:
          ipam:
            publicPools:
              test-v6: {family: 6, prefixes: ["2001:db8:8000::/55"], blockLengths: [56], defaultBlock: 56}

- name: Is refused
  block:
    - name: Pools task (must fail)
      ansible.builtin.include_tasks: "{{ role_path }}/../../../common/tasks/fabric_ipam_pools.yml"
      vars:
        fab_ipam_pools_fabric: lab
    - name: Not reached
      ansible.builtin.set_fact:
        _not_refused: true
  rescue:
    - name: Refused as expected
      ansible.builtin.set_fact:
        _refused_msg: "{{ ansible_failed_result.msg }}"

- name: The refusal
  ansible.builtin.assert:
    that:
      - _not_refused is not defined
      - "'would lie outside the pool' in _refused_msg"

- name: Tolerant (the fabric job, a release) goes on with the ledger's pools
  ansible.builtin.include_tasks: "{{ role_path }}/../../../common/tasks/fabric_ipam_pools.yml"
  vars:
    fab_ipam_pools_fabric: lab
    fab_ipam_pools_tolerant: true

- name: The refusal is reported, the rows are the ledger's
  ansible.builtin.assert:
    that:
      - "'would lie outside the pool' in fab_ipam_pools_refused"
      - fab_ipam_pool == ''
      - not fab_ipam_pools_changed
      - fab_ipam_pools_usage | length == 1
      - fab_ipam_pools_usage[0].prefixes == ['2001:db8:4000::/55']
      - fab_ipam_pools_usage[0].blocksUsed == 1

- name: A malformed realization (default size not offered)
  ansible.builtin.include_tasks: "{{ role_path }}/../../../common/tasks/fabric_realization.yml"
  vars:
    fab_real_fabric: lab
    fabric_realization:
      fabrics:
        lab:
          ipam:
            publicPools:
              test-v6: {family: 6, prefixes: ["2001:db8:4000::/55"], blockLengths: [60], defaultBlock: 56}

- name: Tolerant with a malformed realization
  ansible.builtin.include_tasks: "{{ role_path }}/../../../common/tasks/fabric_ipam_pools.yml"
  vars:
    fab_ipam_pools_fabric: lab
    fab_ipam_pools_tolerant: true

- name: The error is reported, the ledger's pool stays
  ansible.builtin.assert:
    that:
      - fab_ipam_pools_refused | length > 0
      - fab_ipam_pools_usage[0].defaultBlock == 56

- name: A realization change of the hold-down only
  ansible.builtin.include_tasks: "{{ role_path }}/../../../common/tasks/fabric_realization.yml"
  vars:
    fab_real_fabric: lab
    fabric_realization:
      fabrics:
        lab:
          ipam:
            publicPools:
              test-v6: {family: 6, prefixes: ["2001:db8:4000::/55"], blockLengths: [56, 60], defaultBlock: 56,
                        holdDownDays: 3, perTenantBlocks: 1}

- name: Ensured before the release (as the teardown does)
  ansible.builtin.include_tasks: "{{ role_path }}/../../../common/tasks/fabric_ipam_pools.yml"
  vars:
    fab_ipam_pools_fabric: lab
    fab_ipam_pools_tolerant: true

- name: Taken, changed, refusal cleared
  ansible.builtin.assert:
    that:
      - fab_ipam_pools_refused == ''
      - fab_ipam_pools_changed | bool
      - fab_ipam_pools_usage[0].holdDownDays == 3

- name: Release with the new hold-down
  ansible.builtin.include_tasks: "{{ role_path }}/../../../common/tasks/fabric_ipam.yml"
  vars:
    fab_ipam: {action: ipam-pool-release, fabric: lab, owner: HybridNetwork/entity-acme/b}

- name: Held down for 3 days, not freed at once
  ansible.builtin.assert:
    that:
      - fab_ipam_result.released == ['2001:db8:4000::/56']
      - fab_ipam_result.holdDownUntil | default('', true) | length > 0
      - fab_ipam_result.holdDownUntil[:10] > now(utc=true).strftime('%Y-%m-%d')

- name: The same ensure again changes nothing
  ansible.builtin.include_tasks: "{{ role_path }}/../../../common/tasks/fabric_ipam_pools.yml"
  vars:
    fab_ipam_pools_fabric: lab
    fab_ipam_pools_tolerant: true

- name: Unchanged
  ansible.builtin.assert:
    that:
      - not (fab_ipam_pools_changed | bool)
      - fab_ipam_pools_usage[0].blocksHeldDown == 1
'''

PLAY = """- name: IPAM pools wrapper test
  hosts: localhost
  connection: local
  gather_facts: false
  vars:
    services_api_host: "http://127.0.0.1:{port}"
    services_bearer_token: t
    fabric_platform_namespace: sovereign-cloud
  roles:
    - t
"""


@unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook not installed")
class PoolsWrapper(unittest.TestCase):
    def test_wrapper(self):
        STATE.update({"cm": None, "rv": 0})
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        tmp = tempfile.mkdtemp(prefix="shc-ipam-pools-")
        try:
            os.makedirs(os.path.join(tmp, "rulebooks", "roles", "t", "tasks"))
            os.symlink(os.path.join(ROOT, "eda", "common"), os.path.join(tmp, "common"))
            with open(os.path.join(tmp, "rulebooks", "roles", "t", "tasks", "main.yml"), "w") as f:
                f.write(ROLE)
            with open(os.path.join(tmp, "play.yml"), "w") as f:
                f.write(PLAY.format(port=srv.server_address[1]))
            with open(os.path.join(tmp, "ansible.cfg"), "w") as f:
                f.write("[defaults]\n")
            env = dict(os.environ, ANSIBLE_CONFIG=os.path.join(tmp, "ansible.cfg"),
                       ANSIBLE_ROLES_PATH=os.path.join(tmp, "rulebooks", "roles"),
                       ANSIBLE_LOCALHOST_WARNING="false", ANSIBLE_INVENTORY_UNPARSED_WARNING="false",
                       ANSIBLE_REMOTE_TMP=os.path.join(tmp, "remote-tmp"),
                       ANSIBLE_LOCAL_TEMP=os.path.join(tmp, "local-tmp"))
            proc = subprocess.run(["ansible-playbook", "-i", "localhost,", "play.yml"], cwd=tmp, env=env,
                                  capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
            self.assertEqual(proc.returncode, 0, proc.stdout[-3000:] + proc.stderr[-2000:])
            # the strict refusal (the pool task's own rescue re-raises, the role's rescue catches it) and the two
            # tolerant refusals
            self.assertIn("rescued=4", proc.stdout)
            led = json.loads(STATE["cm"]["data"]["ledger.json"])
            pool = led["ipamPools"]["test-v6"]
            self.assertEqual(pool["alloc"], {})
            self.assertEqual(pool["holdDownDays"], 3)
            self.assertEqual(list(pool["released"]), ["2001:db8:4000::/56"])
        finally:
            srv.shutdown()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
