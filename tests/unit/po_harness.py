"""Run task files of the PlatformOpenshift / CloudOSO roles end to end on localhost with fakes (cnet S4 tests).

A temp tree mirrors eda/ (the roles' tasks, files, templates and defaults as symlinks; eda/common/tasks file by file)
so that the roles' "{{ role_path }}/../../../common/..." paths resolve, with these common task files faked:
  patch_cr_status.yml   records {patch: <plural>/<name>, partial, body} in the trace
  nudge_cr.yml          records {nudge: [...]}
  openstack_account.yml sets os_account_clouds_yaml to a fake clouds.yaml, os_account_cacert ''
  openstack_call.yml    records {os: request} and returns fake_os["<action>/<state>"] (or fake_os["<action>"])
  fabric_check.yml      records {fabric: request} and returns fake_fabric[<action>] (default {"errors": []})
  central_create.yml    records {create: <path>/<name>, body} and answers fake_create["<path>/<name>"] (default 201)
and a fake kubernetes.core collection (k8s_info returns FAKE_K8S["<Kind>/<namespace>/<name>"], a list query uses
"<Kind>/<namespace>/"; k8s records {k8s: ...}) plus openstack.cloud.routers_info (returns fake routers). The trace
is a list of JSON lines; the play may end early (meta: end_play) or fail, the trace says what happened.
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
FAKED = ("patch_cr_status.yml", "nudge_cr.yml", "openstack_account.yml", "openstack_call.yml", "fabric_check.yml",
         "central_create.yml")

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
    os_account_cacert: ""
""",
    "openstack_call.yml": TRACE % "{'os': os_request}" + """- name: fake result
  ansible.builtin.set_fact:
    os_result: >-
      {{ fake_os[os_request.action ~ '/' ~ (os_request.state | default(''))]
         | default(fake_os[os_request.action] | default({})) }}
""",
    "fabric_check.yml": TRACE % "{'fabric': fab_check}" + """- name: fake result
  ansible.builtin.set_fact:
    fab_check_result: "{{ fake_fabric[fab_check.action] | default({'errors': []}) }}"
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
    m.exit_json(changed=True)
main()
'''
ROUTERS = '''#!/usr/bin/python
import json, os
from ansible.module_utils.basic import AnsibleModule
def main():
    import yaml
    m = AnsibleModule(argument_spec=dict(cloud=dict(type="raw"), name=dict(), ca_cert=dict()))
    cfg = yaml.safe_load(open(os.environ["OS_CLIENT_CONFIG_FILE"]))["clouds"][m.params["cloud"]]
    with open(os.environ["FAKE_TRACE"], "a") as f:
        f.write(json.dumps({"routers_info": m.params["name"], "project": cfg["auth"].get("project_id"),
                            "scope": sorted(k for k in cfg["auth"] if k.endswith("domain_name"))}) + "\\n")
    m.exit_json(changed=False, routers=json.load(open(os.environ["FAKE_ROUTERS"])))
main()
'''


def _link(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.symlink(src, dst)


def build_tree(tmp, real=(), fakes=None):
    roles = os.path.join(tmp, "eda", "rulebooks", "roles")
    for role in ("platformopenshift_provision", "platformopenshift_teardown", "cloudoso_provision", "cloudoso_teardown",
                 "networkplacement_provision"):
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
    for ns, name, mods in (("kubernetes", "core", {"k8s_info.py": K8S_INFO, "k8s.py": K8S}),
                           ("openstack", "cloud", {"routers_info.py": ROUTERS})):
        d = os.path.join(coll, ns, name, "plugins", "modules")
        os.makedirs(d)
        with open(os.path.join(coll, ns, name, "galaxy.yml"), "w") as fh:
            yaml.safe_dump({"namespace": ns, "name": name, "version": "0.0.1", "readme": "x", "authors": ["x"]}, fh)
        for f, body in mods.items():
            with open(os.path.join(d, f), "w") as fh:
                fh.write(body)
    return roles


def run(role, task_file, extra_vars, k8s=None, routers=None, files=None, result=None, tasks=None, real=(), fakes=None):
    """Run <role>/tasks/<task_file> (role_path = that role); with result (a Jinja expression) the trace ends with
    {"result": <its value>} when the file ran to its end; tasks (a task list) runs in place of the file, with the
    same role_path; real: common task files of FAKED to run unfaked (e.g. fabric_check.yml: the real helper); fakes:
    {file name: task list text} replacing more common task files (TRACE is the trace task's template).
    Returns (rc, trace list, output tail)."""
    tmp = tempfile.mkdtemp(prefix="shc-po-harness-")
    try:
        roles = build_tree(tmp, real, fakes)
        role_path = os.path.join(roles, role)
        trace = os.path.join(tmp, "trace.jsonl")
        open(trace, "w").close()
        with open(os.path.join(tmp, "k8s.json"), "w") as fh:
            json.dump(k8s or {}, fh)
        with open(os.path.join(tmp, "routers.json"), "w") as fh:
            json.dump(routers or [], fh)
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
        with open(os.path.join(tmp, "play.yml"), "w") as fh:
            fh.write(textwrap.dedent(f'''\
                - hosts: localhost
                  connection: local
                  gather_facts: false
                  tasks:
                    - ansible.builtin.include_tasks: {target}
                '''))
            if result:
                fh.write("    - name: trace the result\n      ansible.builtin.shell: cat >> {{ trace_file }}\n"
                         "      args:\n        stdin: \"{{ {'result': %s} | to_json }}\"\n      changed_when: false\n"
                         % result.replace('"', '\\"'))
        with open(os.path.join(tmp, "ansible.cfg"), "w") as fh:
            fh.write("[defaults]\n")
        base = {"role_path": role_path, "trace_file": trace, "fake_clouds_yaml": clouds, "fake_os": {},
                "fake_fabric": {}, "fake_create": {}}
        with open(os.path.join(tmp, "vars.json"), "w") as fh:
            json.dump(dict(base, **extra_vars), fh)
        env = dict(os.environ, ANSIBLE_CONFIG=os.path.join(tmp, "ansible.cfg"),
                   ANSIBLE_COLLECTIONS_PATH=os.path.join(tmp, "coll") + os.pathsep + os.path.join(EDA, "rulebooks", "collections"),
                   ANSIBLE_LOCALHOST_WARNING="false", ANSIBLE_INVENTORY_UNPARSED_WARNING="false",
                   ANSIBLE_REMOTE_TMP=os.path.join(tmp, "remote-tmp"), ANSIBLE_LOCAL_TEMP=os.path.join(tmp, "local-tmp"),
                   ANSIBLE_COLLECTIONS_SCAN_SYS_PATH="false",
                   FAKE_K8S=os.path.join(tmp, "k8s.json"), FAKE_TRACE=trace,
                   FAKE_ROUTERS=os.path.join(tmp, "routers.json"))
        proc = subprocess.run(["ansible-playbook", "-i", "localhost,", "play.yml", "-e", "@vars.json"], cwd=tmp, env=env,
                              capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
        with open(trace) as fh:
            lines = [json.loads(ln) for ln in fh if ln.strip()]
        return proc.returncode, lines, proc.stdout[-4000:] + proc.stderr[-2000:]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def patches(trace, kind="platformopenshifts"):
    return [t for t in trace if "patch" in t and t["patch"].startswith(kind + "/")]


def os_calls(trace, action=None):
    return [t["os"] for t in trace if "os" in t and (action is None or t["os"].get("action") == action)]
