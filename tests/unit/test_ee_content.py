"""Every module a job uses exists in the execution environment the job runs in (fix-routergw, gate finding F10: the
cluster job called openstack.cloud.routers_info, which its execution environment de-platformopenshift does not carry;
an unresolvable module is a load-time error of the included file, so the play stopped without its rescue and the
object kept "Creating / JobRunning").

From the repository alone, no image needed:
  job template -> playbook and execution environment   aap-config/controller/job_templates.yml
  execution environment -> image                       aap-config/controller/projects.yml
  image -> its build definition                        eda/<name without de->/decision-environment.yml
                                                       (galaxy collections and python packages, plus what its base
                                                       image carries: BASE_IMAGES below)
  playbook -> task files                               scripts/check-includes.py (include_tasks / import_tasks /
                                                       include_role / roles:, "{{ role_path }}/../../../common/tasks"
                                                       paths), and every task file of a role the job reaches (a
                                                       dynamic include picks one of them at run time)
  task files -> modules                                the task's action key (FQCN or short), action: / local_action:,
                                                       inside block / rescue / always; module_defaults keys; lookup()
                                                       and query() plugins and FQCN filters in templates
A module of a collection the image does not carry fails the test, unless KNOWN lists it with its reason. The
playbook-adjacent collections under eda/rulebooks/collections ship with the project and count for every job.

The helper scripts a job runs (python3 <...>/files/<x>.py) must import in the image: their third-party imports must be
among the definition's python packages or the base image's.

Optional (skipped without podman or without the image locally; never pulls): the image's real `ansible-galaxy
collection list` and `pip list` against what the definition says.
"""
import ast
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
EDA = ROOT / "eda"
JOB_TEMPLATES = ROOT / "aap-config" / "controller" / "job_templates.yml"
EXECUTION_ENVIRONMENTS = ROOT / "aap-config" / "controller" / "projects.yml"
PROJECT_COLLECTIONS = EDA / "rulebooks" / "collections" / "ansible_collections"
SCRIPT_DIRS = [EDA / "common" / "files"] + sorted((EDA / "rulebooks" / "roles").glob("*/files"))

# What a base image carries before the definition adds anything (`ansible-galaxy collection list` and `pip list` in the
# image; the optional image check below compares again whenever the image is present).
BASE_IMAGES = {
    "registry.redhat.io/ansible-automation-platform-25/de-supported-rhel9:latest": {
        "collections": {"ansible.eda", "redhat.insights_eda"},
        "python": {"aiobotocore", "aiohttp", "aiokafka", "ansible-core", "ansible-rulebook", "ansible-runner",
                   "azure-core", "botocore", "certifi", "cffi", "charset-normalizer", "cryptography", "dpath",
                   "idna", "jinja2", "jmespath", "jsonschema", "markupsafe", "packaging", "pexpect", "psycopg",
                   "python-daemon", "python-dateutil", "pyyaml", "requests", "six", "typing-extensions", "urllib3",
                   "websockets", "xxhash", "yarl"},
    },
}
# Packages a definition's python package brings along (only the ones the helpers import).
BRINGS = {"boto3": {"botocore", "s3transfer"}, "openstacksdk": {"keystoneauth1"}, "kubernetes": {"urllib3"}}
# The name a module is imported by -> its python package.
IMPORT_PACKAGE = {"yaml": "pyyaml", "openstack": "openstacksdk", "dateutil": "python-dateutil", "jinja2": "jinja2",
                  "google": "google-auth", "googleapiclient": "google-api-python-client", "jwt": "pyjwt",
                  "kafka": "kafka-python-ng", "OpenSSL": "pyopenssl", "Crypto": "pycryptodome"}

# ansible-core 2.16's ansible.builtin modules and actions (`ansible-doc -l ansible.builtin` in the image), plus the
# legacy short forms; a short name not here belongs to a collection and needs its FQCN.
BUILTIN = set("""add_host apt apt_key apt_repository assemble assert async_status blockinfile command copy cron
deb822_repository debconf debug dnf dnf5 dpkg_selections expect fail fetch file find gather_facts get_url getent git
group group_by hostname import_playbook import_role import_tasks include_role include_tasks include_vars iptables
known_hosts lineinfile meta package package_facts pause ping pip raw reboot replace rpm_key script service
service_facts set_fact set_stats setup shell slurp stat subversion systemd systemd_service sysvinit tempfile template
unarchive uri user validate_argument_spec wait_for wait_for_connection yum yum_repository include""".split())
# Task keywords (ansible-core 2.16 Task, Block and Handler attributes); with_<lookup> is handled apart.
KEYWORDS = set("""name when register vars loop loop_control until retries delay changed_when failed_when ignore_errors
ignore_unreachable no_log tags environment delegate_to delegate_facts run_once notify listen args become become_user
become_method become_flags become_exe check_mode diff any_errors_fatal throttle timeout module_defaults collections
connection debugger async poll remote_user port block rescue always action local_action vars_files
apply""".split())

# Mismatches that exist and are tolerated, each with its reason: (job template, plugin) -> reason. Remove an entry once
# fixed; an entry that no longer matches fails the test (it must not hide a later one).
KNOWN = {}
# Helper imports tolerated: (job template, script, package) -> reason.
KNOWN_IMPORTS = {}


def _load_check_includes():
    spec = importlib.util.spec_from_file_location("check_includes", ROOT / "scripts" / "check-includes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CI = _load_check_includes()


def _yaml(path):
    return yaml.load(path.read_text(encoding="utf-8"), Loader=CI.Loader)


def norm(pkg):
    return re.sub(r"[-_.]+", "-", pkg.strip().lower())


def requirement_name(req):
    return norm(re.split(r"[<>=!~;\[ ]", str(req).strip(), maxsplit=1)[0])


def job_templates():
    """[(name, playbook path, execution environment name)] of the AAP configuration."""
    out = []
    for jt in _yaml(JOB_TEMPLATES).get("controller_templates") or []:
        out.append((jt["name"], EDA / jt["playbook"], jt.get("execution_environment") or ""))
    return out


def execution_environments():
    """{name: image} of the AAP configuration (the image with its tag; the registry part is templated)."""
    return {ee["name"]: str(ee.get("image") or "") for ee in _yaml(EXECUTION_ENVIRONMENTS).get(
        "controller_execution_environments") or []}


def definition_file(ee):
    return EDA / re.sub(r"^de-", "", ee) / "decision-environment.yml"


def provided(ee):
    """(collections, python packages) the execution environment's build definition gives, its base image's included."""
    d = _yaml(definition_file(ee))
    base = BASE_IMAGES[d["images"]["base_image"]["name"]]
    deps = d.get("dependencies") or {}
    galaxy = deps.get("galaxy") or {}
    colls = set(base["collections"])
    for c in (galaxy.get("collections") or []) if isinstance(galaxy, dict) else []:
        name = c["name"] if isinstance(c, dict) else str(c)
        if re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", name):
            colls.add(name)
    python = {norm(p) for p in base["python"]}
    for r in deps.get("python") or []:
        name = requirement_name(r)
        python.add(name)
        python |= BRINGS.get(name, set())
    return colls, python


def project_collections():
    return {f"{ns.name}.{c.name}" for ns in PROJECT_COLLECTIONS.iterdir() if ns.is_dir()
            for c in ns.iterdir() if c.is_dir()}


def reached_files(playbook):
    """The playbook and every task file a job of it may run: what check-includes resolves from it, and every task and
    handler file of each role reached (dynamic includes choose among those)."""
    c = CI.Checker(ROOT, False)
    c.walk_playbook(playbook)
    files = {Path(p) for p in c.visited} | {playbook.resolve()}
    roles = {CI.role_of(f.relative_to(ROOT.resolve())) for f in files if str(f).startswith(str(ROOT.resolve()))}
    for role in filter(None, roles):
        for sub in ("tasks", "handlers"):
            files |= {f.resolve() for f in (ROOT / role / sub).rglob("*.yml")}
    return files


def _task_modules(task):
    """The plugin names one task calls (its action, and module_defaults keys)."""
    out = []
    for key, val in task.items():
        k = str(key)
        if k in ("action", "local_action"):
            name = val.get("module") if isinstance(val, dict) else (str(val).split() or [""])[0]
            if name:
                out.append(name)
        elif k == "module_defaults" and isinstance(val, dict):
            out += [str(m).replace("group/", "", 1) for m in val]
        elif k in KEYWORDS or k.startswith("with_"):
            continue
        else:
            out.append(k)
    return out


def _walk(tasks):
    for t in tasks if isinstance(tasks, list) else []:
        if not isinstance(t, dict):
            continue
        yield t
        for key in ("block", "rescue", "always"):
            yield from _walk(t.get(key))


TEMPLATE_PLUGINS = re.compile(r"""(?:lookup|query|q)\(\s*['"]([a-z0-9_]+\.[a-z0-9_]+\.[a-z0-9_]+)['"]"""
                              r"""|\|\s*([a-z0-9_]+\.[a-z0-9_]+\.[a-z0-9_]+)\b""")


def modules_of(path):
    """[(plugin name, where)] used by a playbook or task file."""
    try:
        docs = list(yaml.load_all(path.read_text(encoding="utf-8"), Loader=CI.Loader))
    except yaml.YAMLError:
        return []
    out = []
    for doc in docs:
        if not isinstance(doc, list):
            continue
        plays = [d for d in doc if isinstance(d, dict) and ("hosts" in d or "import_playbook" in d)]
        tasks = doc
        if plays:
            tasks = []
            for play in plays:
                out += [(str(m).replace("group/", "", 1), "module_defaults")
                        for m in (play.get("module_defaults") or {})]
                for sect in ("pre_tasks", "tasks", "post_tasks", "handlers"):
                    tasks += play.get(sect) or []
        for t in _walk(tasks):
            out += [(m, t.get("name") or "") for m in _task_modules(t)]
    text = path.read_text(encoding="utf-8")
    for m in TEMPLATE_PLUGINS.finditer(text):
        out.append((m.group(1) or m.group(2), "template"))
    return out


def collection_of(plugin):
    parts = plugin.split(".")
    return ".".join(parts[:2]) if len(parts) >= 3 else None


def mismatches():
    """{(job template, plugin): (execution environment, first file using it)} for plugins the job's image lacks."""
    ees = execution_environments()
    shipped = project_collections()
    found = {}
    for name, playbook, ee in job_templates():
        if ee not in ees or not definition_file(ee).is_file():
            found[(name, f"execution environment {ee or '(none)'}")] = (ee, "no build definition")
            continue
        colls = provided(ee)[0] | shipped | {"ansible.builtin", "ansible.legacy"}
        for f in sorted(reached_files(playbook)):
            for plugin, _task in modules_of(f):
                coll = collection_of(plugin)
                if coll is None:
                    if plugin in BUILTIN:
                        continue
                    found.setdefault((name, plugin), (ee, f"{f.relative_to(ROOT.resolve())} (short name)"))
                elif coll not in colls:
                    found.setdefault((name, plugin), (ee, str(f.relative_to(ROOT.resolve()))))
    return found


SCRIPT_REF = re.compile(r"\b([A-Za-z0-9_]+\.py)\b")


def _scripts_by_name():
    idx = {}
    for d in SCRIPT_DIRS:
        for f in d.glob("*.py"):
            idx.setdefault(f.name, []).append(f.resolve())
    return idx


def scripts_of(files, idx):
    """The helper scripts the task files name (argv entries, python3 <path>, os_helper values)."""
    out = set()
    for f in files:
        for m in SCRIPT_REF.finditer(f.read_text(encoding="utf-8")):
            out |= set(idx.get(m.group(1), []))
    return out


def _optional(node, parents):
    """An import inside try: ... except ImportError (or Exception) is optional."""
    for p in parents:
        if isinstance(p, ast.Try) and any(
                h.type is None or (isinstance(h.type, ast.Name) and h.type.id in ("ImportError", "ModuleNotFoundError",
                                                                                 "Exception"))
                or (isinstance(h.type, ast.Tuple) and any(isinstance(e, ast.Name) and e.id in ("ImportError",
                                                                                               "ModuleNotFoundError")
                                                          for e in h.type.elts))
                for h in p.handlers) and any(node is n or node in ast.walk(n) for n in p.body):
            return True
    return False


def imports_of(script, seen=None):
    """{third-party top-level module} a script needs (following its sibling imports), optional imports left out."""
    seen = seen if seen is not None else set()
    if script in seen:
        return set()
    seen.add(script)
    tree = ast.parse(script.read_text(encoding="utf-8"))
    parents = {}
    for p in ast.walk(tree):
        for ch in ast.iter_child_nodes(p):
            parents[ch] = p
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module.split(".")[0]]
        else:
            continue
        chain, p = [], parents.get(node)
        while p is not None:
            chain.append(p)
            p = parents.get(p)
        if _optional(node, chain):
            continue
        for n in names:
            sibling = script.parent / f"{n}.py"
            if sibling.is_file():
                out |= imports_of(sibling.resolve(), seen)
            elif n not in sys.stdlib_module_names and n != "__future__":
                out.add(n)
    return out


def import_mismatches(only=None):
    """{(job template, script, package): execution environment} for helper imports the job's image lacks."""
    ees = execution_environments()
    idx = _scripts_by_name()
    found = {}
    for name, playbook, ee in job_templates():
        if (only and name not in only) or ee not in ees or not definition_file(ee).is_file():
            continue
        python = provided(ee)[1]
        for script in sorted(scripts_of(reached_files(playbook), idx)):
            for mod in sorted(imports_of(script)):
                pkg = norm(IMPORT_PACKAGE.get(mod, mod))
                if pkg not in python:
                    found[(name, str(script.relative_to(ROOT.resolve())), pkg)] = ee
    return found


class Definitions(unittest.TestCase):
    def test_every_job_template_has_a_build_definition(self):
        ees = execution_environments()
        for name, playbook, ee in job_templates():
            with self.subTest(job_template=name):
                self.assertTrue(playbook.is_file(), playbook)
                self.assertIn(ee, ees, f"{name}: execution environment {ee!r} is not configured")
                self.assertTrue(definition_file(ee).is_file(), f"{name}: {definition_file(ee)} is missing")
                base = _yaml(definition_file(ee))["images"]["base_image"]["name"]
                self.assertIn(base, BASE_IMAGES, f"{ee}: base image {base} unknown to this test (add what it carries)")

    def test_the_cluster_job_reaches_the_router_step(self):
        # the derivation itself: the cluster job reaches the step of F10 and the helper it runs
        files = {str(f) for f in reached_files(EDA / "rulebooks" / "platformopenshift-provision-playbook.yml")}
        rg = EDA / "rulebooks" / "roles" / "platformopenshift_provision" / "tasks" / "router_gateway.yml"
        self.assertIn(str(rg.resolve()), files)
        self.assertIn(str((EDA / "common" / "tasks" / "openstack_call.yml").resolve()), files)
        names = {m for f in files for m, _ in modules_of(Path(f))}
        self.assertIn("kubernetes.core.k8s_info", names)
        self.assertIn("hybridsovereign.shc.cr_status", names)
        scripts = {s.name for s in scripts_of({Path(f) for f in files}, _scripts_by_name())}
        self.assertTrue({"shc_openstack.py", "po_preflight.py"} <= scripts, scripts)


class ModulesInTheExecutionEnvironment(unittest.TestCase):
    def test_every_module_exists_in_the_jobs_execution_environment(self):
        found = mismatches()
        new = {k: v for k, v in found.items() if k not in KNOWN}
        self.assertEqual(new, {}, "\n".join(f"{jt}: {plugin} not in {ee} (used in {where})"
                                            for (jt, plugin), (ee, where) in sorted(new.items())))
        self.assertEqual(sorted(k for k in KNOWN if k not in found), [], "KNOWN entries that no longer match")

    def test_helper_imports_exist_in_the_jobs_execution_environment(self):
        found = import_mismatches()
        new = {k: v for k, v in found.items() if k not in KNOWN_IMPORTS}
        self.assertEqual(new, {}, "\n".join(f"{jt}: {script} imports {pkg}, not in {ee}"
                                            for (jt, script, pkg), ee in sorted(new.items())))
        self.assertEqual(sorted(k for k in KNOWN_IMPORTS if k not in found), [])


def _in_image(image, entry, *args):
    proc = subprocess.run(["podman", "run", "--rm", "--network", "none", "--entrypoint", entry, image, *args],
                          capture_output=True, text=True, timeout=180)
    return proc.returncode, proc.stdout


def _local_images():
    """{execution environment: a local image of the configured name and tag} (any registry: the configuration's
    registry part is templated), "" when none is present."""
    proc = subprocess.run(["podman", "images", "--format", "{{.Repository}}:{{.Tag}}"], capture_output=True, text=True)
    local = proc.stdout.split() if proc.returncode == 0 else []
    out = {}
    for ee, image in execution_environments().items():
        tail = image.split("}}", 1)[-1].lstrip("/")      # "{{ registry }}/de-x:tag"
        out[ee] = next((i for i in local if tail and (i == tail or i.endswith("/" + tail))), "")
    return out


@unittest.skipUnless(shutil.which("podman"), "podman not installed")
class TheRealImages(unittest.TestCase):
    """The definition against the image the AAP configuration names, where that image is present locally."""

    def test_collections_and_packages(self):
        checked = 0
        used = {ee for _, _, ee in job_templates()}
        for ee, image in sorted(_local_images().items()):
            if ee not in used or not image or not definition_file(ee).is_file():
                continue
            checked += 1
            with self.subTest(image=image):
                colls, python = provided(ee)
                rc, out = _in_image(image, "ansible-galaxy", "collection", "list")
                self.assertEqual(rc, 0)
                real = {ln.split()[0] for ln in out.splitlines() if re.match(r"^[a-z0-9_]+\.[a-z0-9_]+\s", ln)}
                self.assertEqual(sorted(colls - real), [], f"{image}: the definition says more than the image has")
                rc, out = _in_image(image, "python3", "-m", "pip", "list", "--format=freeze")
                self.assertEqual(rc, 0)
                pkgs = {norm(ln.split("==")[0]) for ln in out.splitlines() if "==" in ln}
                self.assertEqual(sorted(python - pkgs), [], f"{image}: python packages the image lacks")
        if not checked:
            self.skipTest("none of the configured images is present locally")


if __name__ == "__main__":
    unittest.main()
