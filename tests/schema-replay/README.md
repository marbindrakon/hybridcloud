# Schema replay

Loads exported `hybridsovereign.redhat` objects into a throwaway Kubernetes API server under candidate CRDs (and
admission policies) and reports every rejection and every field the server pruned, defaulted or changed. Run it
before any CRD or admission-policy change reaches a cluster: a field that a new schema prunes, or a CEL rule that
refuses an existing object, shows here instead of in a tenant's status.

## Input

`export.sh OUT_DIR` (read-only, current KUBECONFIG) writes:

| Path | Content |
|---|---|
| `OUT_DIR/objects/<plural>.hybridsovereign.redhat.json` | every object of every kind, status included |
| `OUT_DIR/objects/COUNTS.txt` | objects per kind |
| `OUT_DIR/crds/<name>.json` | the cluster's CRDs (the baseline for upgrade mode) |
| `OUT_DIR/namespaces.json` | the namespaces of those objects, labels only |

It never reads Secrets.

## Run

```
python3 -I tests/schema-replay/replay.py --objects OUT_DIR/objects --namespaces OUT_DIR/namespaces.json \
  --crds operator/primary/helm/templates/crds.yaml [--baseline-crds OUT_DIR/crds] \
  [--policies <rendered policies.yaml> ...] [--backend envtest|kind|kubeconfig] [--as USER] \
  [--inventory status-keys.json] [--report report.json] [--exec CMD] [--keep]
```

- **fresh** (no `--baseline-crds`): install the candidate CRDs, create every object, write its status
  (`/status` subresource), read it back.
- **upgrade** (`--baseline-crds`): install the baseline CRDs, load the objects, apply the candidate CRDs, then send
  a no-op update of every object's spec and status (ratcheting and transition rules run as on a live update).
- `--policies`: rendered ValidatingAdmissionPolicies and bindings (e.g.
  `helm template ... --show-only templates/gitops-policies.yaml`); Warn results are collected per object.
- `--as USER`: create and update as USER (bound to cluster-admin in the throwaway cluster), to see the policies as
  that user would.
- `--inventory`: the union of status key paths per kind across the objects (the live status-key inventory).
- `--exec CMD`: run CMD with `KUBECONFIG` (and `REPLAY_KUBECTL`) pointing at the loaded cluster before it is torn
  down; its exit status counts. `test-policies.sh` uses it.

Writes use field validation `warn`, like the controllers' writes: an unknown field is pruned (a diff), not refused.
Exit status 0 = zero rejections and zero diffs. Any diff is a failure: decide whether the schema or the object is
wrong before the change ships.

## Backends

| Backend | Needs | Notes |
|---|---|---|
| `envtest` (default) | `KUBEBUILDER_ASSETS` = a directory with `etcd`, `kube-apiserver`, `kubectl` (controller-tools envtest release for the cluster's Kubernetes minor), `openssl` | Unprivileged subprocesses on free localhost ports; works inside the ansible-runner image (`/usr/local/kubebuilder/bin`). No controller-manager: VAP `status.typeChecking` stays empty, so check it on the cluster after a rollout. |
| `kind` | `kind` and podman or docker | Pinned `kindest/node` image; the cluster `shc-replay` is deleted unless `--keep`. Not available inside the runner image. |
| `kubeconfig` | `--kubeconfig` of an existing **empty** cluster | Nothing is cleaned up. |

## Self-tests and policy tests

```
tests/schema-replay/selftest.sh OUT_DIR/objects        # a removed status field must show as pruned, a refusing CEL rule as rejections
tests/schema-replay/test-policies.sh OUT_DIR/objects <primary values.yaml>   # shc-fabric-ledger-guard, shc-gitops-managed (Warn and Deny), shc-gitops-delete-guard behaviour
#   POLICY_CHECK_ARGS="--unmarked <kind>/<ns>/<name>" also checks that an object without the managed-by label can be deleted
python3 -m unittest discover -s tests/schema-replay -p 'test_*.py'          # ledger allocation count
make replay OBJECTS=OUT_DIR/objects BASELINE_CRDS=OUT_DIR/crds
```
