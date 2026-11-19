"""Run task files of the PlatformOpenshift / CloudOSO roles end to end on localhost with fakes (cnet S4 tests).

A temp tree mirrors eda/ (the roles' tasks, files, templates and defaults as symlinks; eda/common/tasks file by file)
so that the roles' "{{ role_path }}/../../../common/..." paths resolve, with these common task files faked:
  patch_cr_status.yml   records {patch: <plural>/<name>, partial, body} in the trace
  nudge_cr.yml          records {nudge: [...]}
  openstack_account.yml sets os_account_clouds_yaml to a fake clouds.yaml, os_account_cacert fake_cacert[<vault path>]
                        (default '')
  openstack_call.yml    records {os: request, as: <the account's vault path>, cacert} and returns fake_os["<action>/<state>"] (or fake_os["<action>"])
  fabric_check.yml      records {fabric: request} and returns fake_fabric[<action>] (default {"errors": []})
  central_create.yml    records {create: <path>/<name>, body} and answers fake_create["<path>/<name>"] (default 201)
  openstack_cluster_credential.yml  records {credential: <build>} and sets cb_provider_creds_vault
and a fake kubernetes.core collection (k8s_info returns FAKE_K8S["<Kind>/<namespace>/<name>"], a list query uses
"<Kind>/<namespace>/"; k8s records {k8s: ...} and reports changed unless FAKE_K8S["changed:<Kind>/<namespace>/<name>"]
is false). No other collection is faked: a task file that needs one the job's
execution environment lacks fails here too (test_ee_content.py checks the definitions). The trace is a list of JSON
lines; the play may end early (meta: end_play) or fail, the trace says what happened.
"""
import json
import os
import shutil
import subprocess
import tempfile
import textwrap

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
EDA = os.path.join(ROOT, "eda")
HAVE_ANSIBLE = shutil.which("ansible-playbook") is not None
# where an execution environment image carries its collections (ansible-galaxy collection list in the image)
SYSTEM_COLLECTIONS = "/usr/share/ansible/collections"
LAST_OUTPUT = ""
LAST_VARS = ""   # with dump_vars: every variable of the play at its end (facts and registered results included)
FAKED = ("patch_cr_status.yml", "nudge_cr.yml", "openstack_account.yml", "openstack_call.yml", "fabric_check.yml",
         "central_create.yml", "openstack_cluster_credential.yml")

TRACE = """- name: trace
  ansible.builtin.shell: cat >> {{ trace_file }}
  args:
    stdin: "{{ %s | to_json }}"
  changed_when: false
"""

FAKE_TASKS = {
    "patch_cr_status.yml": TRACE % "{'patch': cr_plural ~ '/' ~ cr_name, 'partial': cr_status_partial | default(false) | bool, 'body': cr_status_body}",
    "nudge_cr.yml": TRACE % "{'nudge': nudge_crs | default([])}",
    "openstack_account.yml": """- name: fake account
  ansible.builtin.set_fact:
    os_account_clouds_yaml: "{{ fake_clouds_yaml }}"
    os_account_cacert: "{{ (fake_cacert | default({}))[os_account_vault_path] | default('') }}"
    fake_account_loaded: "{{ os_account_vault_path }}"
""",
    "openstack_call.yml": TRACE % ("{'os': os_request, 'as': fake_account_loaded | default(''), "
                                   "'cacert': os_account_cacert | default('')}") + """- name: fake result
  ansible.builtin.set_fact:
    os_result: >-
      {{ fake_os[os_request.action ~ '/' ~ (os_request.state | default(''))]
         | default(fake_os[os_request.action] | default({})) }}
""",
    "fabric_check.yml": TRACE % "{'fabric': fab_check}" + """- name: fake result
  ansible.builtin.set_fact:
    fab_check_result: "{{ fake_fabric[fab_check.action] | default({'errors': []}) }}"
""",
    # the cluster's own site credential (test_osoui_cluster_credential.py runs the real one): its Vault path
    "openstack_cluster_credential.yml": TRACE % "{'credential': cb_app_name}" + """- name: fake credential
  ansible.builtin.set_fact:
    cb_provider_creds_vault: "cluster/oso/{{ cb_app_name }}/build/provider-creds"
""",
    "central_create.yml": TRACE % "{'create': cc_path ~ '/' ~ cc_body.metadata.name, 'body': cc_body}" + """- name: fake answer
  ansible.builtin.set_fact:
    cc_status: "{{ fake_create[cc_path ~ '/' ~ cc_body.metadata.name] | default(201) }}"
""",
}

K8S_INFO = '''#!/usr/bin/python
import json, os
from ansible.module_utils.basic import AnsibleModule
def main():
    m = AnsibleModule(argument_spec={}, check_invalid_arguments=False) if False else AnsibleModule(
        argument_spec=dict(host=dict(), api_key=dict(no_log=True), validate_certs=dict(type="bool"),
                           api_version=dict(), kind=dict(), name=dict(), namespace=dict(),
                           field_selectors=dict(type="list"), label_selectors=dict(type="list")))
    p = m.params
    data = json.load(open(os.environ["FAKE_K8S"]))
    key = "%s/%s/%s" % (p["kind"], p["namespace"] or "", p["name"] or "")
    got = data.get(key, [] )
    res = got if isinstance(got, list) else [got]
    with open(os.environ["FAKE_TRACE"], "a") as f:
        f.write(json.dumps({"k8s_info": key}) + "\\n")
    m.exit_json(changed=False, resources=res)
main()
'''
K8S = '''#!/usr/bin/python
import json, os
from ansible.module_utils.basic import AnsibleModule
def main():
    m = AnsibleModule(argument_spec=dict(host=dict(), api_key=dict(no_log=True), validate_certs=dict(type="bool"),
                                         state=dict(), api_version=dict(), kind=dict(), name=dict(), namespace=dict(),
                                         definition=dict(type="raw"), wait=dict(type="bool"), wait_timeout=dict(type="int")))
    p = m.params
    with open(os.environ["FAKE_TRACE"], "a") as f:
        rec = {"k8s": {k: p[k] for k in ("state", "kind", "name", "namespace")}}
        if p["definition"] is not None:
            rec["definition"] = p["definition"]
        f.write(json.dumps(rec) + "\\n")
    # FAKE_K8S "changed:<Kind>/<namespace>/<name>": false = the call changed nothing (a delete of an absent object)
    data = json.load(open(os.environ["FAKE_K8S"]))
    key = "%s/%s/%s" % (p["kind"], p["namespace"] or "", p["name"] or "")
    m.exit_json(changed=bool(data.get("changed:" + key, True)))
main()
'''


def _link(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.symlink(src, dst)


def build_tree(tmp, real=(), fakes=None):
    roles = os.path.join(tmp, "eda", "rulebooks", "roles")
    for role in ("platformopenshift_provision", "platformopenshift_teardown", "cloudoso_provision", "cloudoso_teardown",
                 "networkplacement_provision", "cloudinfrastructure_provision"):
        for sub in ("tasks", "files", "templates", "defaults"):
            src = os.path.join(EDA, "rulebooks", "roles", role, sub)
            if os.path.isdir(src):
                _link(src, os.path.join(roles, role, sub))
    ctasks = os.path.join(EDA, "common", "tasks")
    fakes = fakes or {}
    for f in os.listdir(ctasks):
        if (f not in FAKED or f in real) and f not in fakes:
            _link(os.path.join(ctasks, f), os.path.join(tmp, "eda", "common", "tasks", f))
    for f, body in fakes.items():
        with open(os.path.join(tmp, "eda", "common", "tasks", f), "w") as fh:
            fh.write("---\n" + body)
    for f, body in FAKE_TASKS.items():
        if f in real or f in fakes:
            continue
        with open(os.path.join(tmp, "eda", "common", "tasks", f), "w") as fh:
            fh.write("---\n" + body)
    _link(os.path.join(EDA, "common", "files"), os.path.join(tmp, "eda", "common", "files"))
    _link(os.path.join(EDA, "common", "vars"), os.path.join(tmp, "eda", "common", "vars"))
    coll = os.path.join(tmp, "coll", "ansible_collections")
    for ns, name, mods in (("kubernetes", "core", {"k8s_info.py": K8S_INFO, "k8s.py": K8S}),):
        d = os.path.join(coll, ns, name, "plugins", "modules")
        os.makedirs(d)
        with open(os.path.join(coll, ns, name, "galaxy.yml"), "w") as fh:
            yaml.safe_dump({"namespace": ns, "name": name, "version": "0.0.1", "readme": "x", "authors": ["x"]}, fh)
        for f, body in mods.items():
            with open(os.path.join(d, f), "w") as fh:
                fh.write(body)
    return roles


def run(role, task_file, extra_vars, k8s=None, files=None, result=None, tasks=None, real=(), fakes=None,
        dump_vars=False):
    """Run <role>/tasks/<task_file> (role_path = that role); with result (a Jinja expression) the trace ends with
    {"result": <its value>} when the file ran to its end; tasks (a task list) runs in place of the file, with the
    same role_path; real: common task files of FAKED to run unfaked (e.g. fabric_check.yml: the real helper); fakes:
    {file name: task list text} replacing more common task files (TRACE is the trace task's template); dump_vars:
    every variable the play holds at its end (also after a failure) goes to LAST_VARS as JSON text, for a test that
    hunts for secrets in facts and registered results. Returns (rc, trace list, output tail)."""
    tmp = tempfile.mkdtemp(prefix="shc-po-harness-")
    try:
        roles = build_tree(tmp, real, fakes)
        role_path = os.path.join(roles, role)
        trace = os.path.join(tmp, "trace.jsonl")
        open(trace, "w").close()
        with open(os.path.join(tmp, "k8s.json"), "w") as fh:
            json.dump(k8s or {}, fh)
        clouds = os.path.join(tmp, "clouds.yaml")
        with open(clouds, "w") as fh:
            yaml.safe_dump({"clouds": {"openstack": {"auth": {"auth_url": "https://keystone.example.org/v3",
                                                               "username": "user-not-in-data", "password": "password-not-in-data",
                                                               "domain_name": "domain-not-in-data"},
                                                      "region_name": "regionOne"}}}, fh)
        for rel, content in (files or {}).items():
            path = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                yaml.safe_dump(content, fh)
        target = os.path.join(role_path, "tasks", task_file)
        if tasks is not None:
            target = os.path.join(tmp, "given_tasks.yml")
            with open(target, "w") as fh:
                yaml.safe_dump(tasks, fh)
        dump = os.path.join(tmp, "vars-dump.json")
        with open(os.path.join(tmp, "play.yml"), "w") as fh:
            fh.write(textwrap.dedent(f'''\
                - hosts: localhost
                  connection: local
                  gather_facts: false
                  tasks:
                    - block:
                        - ansible.builtin.include_tasks: {target}
                '''))
            if result:
                fh.write("        - name: trace the result\n          ansible.builtin.shell: cat >> {{ trace_file }}\n"
                         "          args:\n            stdin: \"{{ {'result': %s} | to_json }}\"\n"
                         "          changed_when: false\n" % result.replace('"', '\\"'))
            if dump_vars:
                fh.write("      always:\n        - name: dump every variable\n          ansible.builtin.copy:\n"
                         "            content: \"{{ hostvars['localhost'] | to_json }}\"\n"
                         f"            dest: {dump}\n          no_log: true\n")
        with open(os.path.join(tmp, "ansible.cfg"), "w") as fh:
            fh.write("[defaults]\n")
        base = {"role_path": role_path, "trace_file": trace, "fake_clouds_yaml": clouds, "fake_os": {},
                "fake_fabric": {}, "fake_create": {}}
        with open(os.path.join(tmp, "vars.json"), "w") as fh:
            json.dump(dict(base, **extra_vars), fh)
        env = dict(os.environ, ANSIBLE_CONFIG=os.path.join(tmp, "ansible.cfg"),
                   # the image's own collections last (the fakes and the project's collections win)
                   ANSIBLE_COLLECTIONS_PATH=os.pathsep.join([os.path.join(tmp, "coll"), os.path.join(EDA, "rulebooks", "collections"),
                                                             SYSTEM_COLLECTIONS]),
                   ANSIBLE_LOCALHOST_WARNING="false", ANSIBLE_INVENTORY_UNPARSED_WARNING="false",
                   ANSIBLE_REMOTE_TMP=os.path.join(tmp, "remote-tmp"), ANSIBLE_LOCAL_TEMP=os.path.join(tmp, "local-tmp"),
                   ANSIBLE_COLLECTIONS_SCAN_SYS_PATH="false",
                   FAKE_K8S=os.path.join(tmp, "k8s.json"), FAKE_TRACE=trace)
        proc = subprocess.run(["ansible-playbook", "-i", "localhost,", "play.yml", "-e", "@vars.json"], cwd=tmp, env=env,
                              capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
        with open(trace) as fh:
            lines = [json.loads(ln) for ln in fh if ln.strip()]
        global LAST_OUTPUT, LAST_VARS
        LAST_OUTPUT = proc.stdout + proc.stderr   # the whole job output (a test that hunts for secrets reads it)
        LAST_VARS = ""
        if dump_vars:
            if not os.path.exists(dump):
                raise AssertionError("the play did not dump its variables: " + LAST_OUTPUT[-3000:])
            with open(dump) as fh:
                LAST_VARS = fh.read()
        return proc.returncode, lines, proc.stdout[-4000:] + proc.stderr[-2000:]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def patches(trace, kind="platformopenshifts"):
    return [t for t in trace if "patch" in t and t["patch"].startswith(kind + "/")]


def os_calls(trace, action=None):
    return [t["os"] for t in trace if "os" in t and (action is None or t["os"].get("action") == action)]
