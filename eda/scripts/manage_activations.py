#!/usr/bin/env python3
"""
Manage EDA rulebook activations for Sovereign Cloud.

Usage:
    manage_activations.py create  --eda-host URL --password PASS [--event-stream NAME]
    manage_activations.py delete  --eda-host URL --password PASS
    manage_activations.py status  --eda-host URL --password PASS
    manage_activations.py restart --eda-host URL --password PASS
    manage_activations.py sync    --eda-host URL --password PASS [--project-name NAME]
"""
import argparse, hashlib, json, requests, sys, time, urllib3

urllib3.disable_warnings()

CR_TYPES = [
    "entity", "team", "assignment", "project", "persona",
    "platformopenshift", "cloudoso", "cloudaws",
    "rbac", "rbacconfig", "vault", "vaultkv",
    "aapconfig", "aaporg", "quayconfig", "quayorg",
    "transportlink", "uihealthchecker",
    # shc (P7.1, tenant DNS D1)
    "cloudinfrastructure", "dnsconfig",
]

# shc (design/P7-PLAN.md P7.3.3): rulebooks that serve several kinds with one activation
# (name, rulebook, decision environment)
SHARED_ACTIVATIONS = [
    ("fabric-events-activation", "fabric-events.yml", "de-cloudoso"),
]

DE_MAP = {
    "entity": "de-entity",
    "team": "de-team",
    "assignment": "de-assignment",
    "project": "de-project",
    "persona": "de-persona",
    "platformopenshift": "de-platformopenshift",
    "cloudoso": "de-cloudoso",
    "cloudaws": "de-cloudaws",
    "rbac": "de-plugin-rbac",
    "rbacconfig": "de-plugin-rbac",
    "vault": "de-plugin-vault",
    "vaultkv": "de-plugin-vault",
    "aapconfig": "de-plugin-aap",
    "aaporg": "de-plugin-aap",
    "quayconfig": "de-plugin-quay",
    "quayorg": "de-plugin-quay",
    "transportlink": "de-hybridvpc",
    "uihealthchecker": "de-hybridvpc",
    "cloudinfrastructure": "de-cloudoso",
    "dnsconfig": "de-cloudoso",
}


def api(host, auth, method, path, **kwargs):
    url = f"{host}/api/eda/v1/{path}"
    resp = getattr(requests, method)(url, auth=auth, verify=False, **kwargs)
    return resp


def get_all(host, auth, path, key="results"):
    items = []
    url = f"{host}/api/eda/v1/{path}?page_size=100"
    while url:
        resp = requests.get(url, auth=auth, verify=False)
        resp.raise_for_status()
        data = resp.json()
        items.extend(data.get(key, []))
        url = data.get("next")
    return items


def resolve_des(host, auth):
    des = get_all(host, auth, "decision-environments/")
    return {d["name"]: d["id"] for d in des}


def resolve_rulebooks(host, auth):
    rbs = get_all(host, auth, "rulebooks/")
    return {r["name"]: r for r in rbs}


def resolve_event_stream(host, auth, name):
    streams = get_all(host, auth, "event-streams/")
    for s in streams:
        if s["name"] == name:
            return s["id"]
    raise SystemExit(f"Event stream '{name}' not found. Available: {[s['name'] for s in streams]}")


def resolve_aap_credential(host, auth, name="sovereign-aap-controller-credential"):
    creds = get_all(host, auth, "eda-credentials/")
    for c in creds:
        if c["name"] == name:
            return c["id"]
    raise SystemExit(
        f"EDA credential '{name}' not found. Run eda-config (job-aap-controller-config / eda-config) first. "
        f"Available: {[c['name'] for c in creds]}"
    )


def cmd_create(args):
    auth = ("admin", args.password)
    de_ids = resolve_des(args.eda_host, auth)
    rb_map = resolve_rulebooks(args.eda_host, auth)
    es_id = resolve_event_stream(args.eda_host, auth, args.event_stream)
    aap_cred_id = resolve_aap_credential(args.eda_host, auth, args.aap_credential)

    activations_to_create = []
    for cr in CR_TYPES:
        for action in ("create", "delete"):
            rb_name = f"{cr}-{action}.yml"
            act_name = f"{cr}-{action}-activation"
            de_name = DE_MAP.get(cr)
            if rb_name not in rb_map:
                print(f"  SKIP: rulebook {rb_name} not found")
                continue
            if de_name not in de_ids:
                print(f"  SKIP: DE {de_name} not found for {cr}")
                continue
            activations_to_create.append((act_name, rb_map[rb_name], de_ids[de_name]))
    for act_name, rb_name, de_name in SHARED_ACTIVATIONS:
        if rb_name not in rb_map:
            print(f"  SKIP: rulebook {rb_name} not found")
            continue
        if de_name not in de_ids:
            print(f"  SKIP: DE {de_name} not found for {act_name}")
            continue
        activations_to_create.append((act_name, rb_map[rb_name], de_ids[de_name]))

    print(f"Creating {len(activations_to_create)} activations...")
    ok = fail = 0
    for act_name, rb, de_id in activations_to_create:
        rb_hash = hashlib.sha256(rb.get("rulesets", "").encode()).hexdigest()
        source_mappings = json.dumps([{
            "source_name": "__SOURCE_1",
            "rulebook_hash": rb_hash,
            "rulebook_name": "",
            "event_stream_name": args.event_stream,
            "event_stream_id": es_id,
        }])
        payload = {
            "name": act_name,
            "organization_id": 1,
            "rulebook_id": rb["id"],
            "decision_environment_id": de_id,
            "is_enabled": True,
            "restart_policy": "always",
            "eda_credentials": [aap_cred_id],
            "source_mappings": source_mappings,
        }
        resp = api(args.eda_host, auth, "post", "activations/", json=payload)
        if resp.status_code in (200, 201):
            print(f"  OK: {act_name} (id={resp.json().get('id')})")
            ok += 1
        else:
            print(f"  FAIL: {act_name} -> {resp.status_code}: {resp.text[:200]}")
            fail += 1
        time.sleep(0.3)

    print(f"\nDone: {ok} OK, {fail} FAIL")
    return 0 if fail == 0 else 1


def cmd_delete(args):
    auth = ("admin", args.password)
    activations = get_all(args.eda_host, auth, "activations/")
    if not activations:
        print("No activations to delete.")
        return 0

    print(f"Deleting {len(activations)} activations...")
    for a in activations:
        resp = api(args.eda_host, auth, "delete", f"activations/{a['id']}/")
        status = "OK" if resp.status_code in (200, 204) else f"FAIL({resp.status_code})"
        print(f"  {status}: {a['name']} (id={a['id']})")
    print("Done.")
    return 0


def cmd_status(args):
    auth = ("admin", args.password)
    activations = get_all(args.eda_host, auth, "activations/")
    if not activations:
        print("No activations found.")
        return 0

    running = sum(1 for a in activations if a["status"] == "running")
    print(f"Total: {len(activations)}, Running: {running}")
    for a in sorted(activations, key=lambda x: x["name"]):
        marker = "+" if a["status"] == "running" else "-"
        print(f"  [{marker}] {a['name']}: {a['status']} (id={a['id']})")
    return 0


def cmd_restart(args):
    auth = ("admin", args.password)
    activations = get_all(args.eda_host, auth, "activations/")
    if not activations:
        print("No activations to restart.")
        return 0

    print(f"Restarting {len(activations)} activations...")
    ok = fail = 0
    for a in activations:
        resp = api(args.eda_host, auth, "post", f"activations/{a['id']}/restart/")
        if resp.status_code in (200, 202, 204):
            ok += 1
        else:
            fail += 1
            print(f"  FAIL: {a['name']} -> {resp.status_code}")

    print(f"Done: {ok} restarted, {fail} failed")
    return 0 if fail == 0 else 1


def cmd_sync(args):
    auth = ("admin", args.password)
    projects = get_all(args.eda_host, auth, "projects/")
    proj = next((p for p in projects if p["name"] == args.project_name), None)
    if not proj:
        print(f"Project '{args.project_name}' not found. Available: {[p['name'] for p in projects]}")
        return 1

    pid = proj["id"]
    print(f"Syncing project '{args.project_name}' (id={pid})...")
    resp = api(args.eda_host, auth, "post", f"projects/{pid}/sync/")
    if resp.status_code not in (200, 202):
        print(f"  Sync trigger failed: {resp.status_code}")
        return 1

    for _ in range(24):
        time.sleep(5)
        r = api(args.eda_host, auth, "get", f"projects/{pid}/")
        state = r.json().get("import_state", "?")
        git_hash = r.json().get("git_hash", "?")[:12]
        print(f"  state={state}  hash={git_hash}")
        if state == "completed":
            print("Sync complete.")
            return 0
        if state == "failed":
            print(f"Sync failed: {r.json().get('import_error','?')}")
            return 1

    print("Sync timed out.")
    return 1


def main():
    parser = argparse.ArgumentParser(description="Manage EDA rulebook activations")
    parser.add_argument("command", choices=["create", "delete", "status", "restart", "sync"])
    parser.add_argument("--eda-host", required=True, help="EDA controller URL")
    parser.add_argument("--password", required=True, help="EDA admin password")
    parser.add_argument("--event-stream", default="sovereign-operator-events",
                        help="Event stream name (default: sovereign-operator-events)")
    parser.add_argument("--aap-credential", default="sovereign-aap-controller-credential",
                        help="EDA AAP credential name (default: sovereign-aap-controller-credential)")
    parser.add_argument("--project-name", default="sovereign-eda-rulebooks",
                        help="EDA project name for sync command")
    args = parser.parse_args()

    commands = {
        "create": cmd_create,
        "delete": cmd_delete,
        "status": cmd_status,
        "restart": cmd_restart,
        "sync": cmd_sync,
    }
    sys.exit(commands[args.command](args))


if __name__ == "__main__":
    main()
