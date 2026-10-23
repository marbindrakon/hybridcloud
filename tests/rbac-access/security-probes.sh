#!/usr/bin/env bash
# Admission probes for the platform-only fields (shc-platform-only-fields) and the tenant namespace label
# (shc-namespace-label-integrity), run as the identities that really write these objects.
#
# Granted tenant users write CloudOSO and PlatformOpenshift objects with their own token (the tenant dashboard and
# the console plugins forward the signed-in user's token), so the policy matrix runs as granted fixture users:
# an entity admin and a cloud admin of the fixture tenant. A user without a grant is the RBAC row (403 before
# admission). Every request is a server-side dry run: nothing is created or changed.
#
# Usage: KUBECONFIG=<kubeconfig allowed to impersonate> security-probes.sh [--entity NAME] [--out DIR]
# Env (defaults for the gate fixture):
#   OC                  oc binary (default oc)
#   PROBE_ADMIN_USER    entity admin user           (default uxr-admin)
#   PROBE_ADMIN_GROUP   its group                   (default <entity>.uxr-admins)
#   PROBE_INFRA_USER    cloud admin user            (default uxr-infra)
#   PROBE_INFRA_GROUP   its group                   (default <entity>.uxr-infra)
#   PROBE_NOBODY_USER   signed-in user, no grant    (default uxr-nobody)
#   PROBE_AUTOMATION    platform automation username (default the shc-automation service account)
# Verdicts per row: policy (the policy's message, as a warning in Warn mode or a refusal in Deny mode),
# rbac-403 (refused by RBAC before admission), none (accepted without the policy's message), error.
# Exit 1 when a row's verdict differs from its expectation (a row may accept several verdicts).
set -o pipefail

ENTITY=uxr-corp
OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --entity) shift; ENTITY="$1" ;;
    --out) shift; OUT="$1" ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
OC=${OC:-oc}
NS="entity-$ENTITY"
ADMIN_USER=${PROBE_ADMIN_USER:-uxr-admin}
ADMIN_GROUP=${PROBE_ADMIN_GROUP:-$ENTITY.uxr-admins}
INFRA_USER=${PROBE_INFRA_USER:-uxr-infra}
INFRA_GROUP=${PROBE_INFRA_GROUP:-$ENTITY.uxr-infra}
NOBODY_USER=${PROBE_NOBODY_USER:-uxr-nobody}
AUTOMATION=${PROBE_AUTOMATION:-system:serviceaccount:open-cluster-management-agent-addon:shc-automation}
OPERATOR=system:serviceaccount:sovereign-cloud:hybridsovereign-primary-operator
[ -n "$OUT" ] || OUT="./security-probes-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT/manifests" || exit 2
: > "$OUT/result.tsv"

SIGNED_IN=(--as-group=system:authenticated --as-group=system:authenticated:oauth)
AS_ADMIN=(--as="$ADMIN_USER" --as-group="$ADMIN_GROUP" "${SIGNED_IN[@]}")
AS_INFRA=(--as="$INFRA_USER" --as-group="$INFRA_GROUP" "${SIGNED_IN[@]}")
AS_NOBODY=(--as="$NOBODY_USER" "${SIGNED_IN[@]}")
# a platform person (kube:admin and its groups); the probes do not rely on the caller's own identity, which may be
# system:admin (system:masters is not a platform group of the policies)
AS_PLATFORM=(--as=kube:admin --as-group=system:cluster-admins --as-group=system:authenticated)

po() {  # name, extra openstack field line
  cat > "$OUT/manifests/$1.yaml" <<YAML
apiVersion: hybridsovereign.redhat/v1alpha1
kind: PlatformOpenshift
metadata:
  name: $1
  namespace: $NS
spec:
  type: openstack
  openstack:
    environment: probe-env
$2
YAML
}
oso() {  # name, extra spec line
  cat > "$OUT/manifests/$1.yaml" <<YAML
apiVersion: hybridsovereign.redhat/v1alpha1
kind: CloudOSO
metadata:
  name: $1
  namespace: $NS
spec:
  cloudRef:
    name: probe-site
$2
YAML
}
nsm() {  # name, label value, annotation line
  cat > "$OUT/manifests/$1.yaml" <<YAML
apiVersion: v1
kind: Namespace
metadata:
  name: $1
  labels:
    hybridsovereign.redhat/entity: $2
$3
YAML
}

fail=0
row() {  # id, expected verdicts (a|b), manifest or "-", then the oc arguments
  local id="$1" want="$2" file="$3"; shift 3
  local log="$OUT/$id.log" verdict
  if [ "$file" = "-" ]; then
    "$OC" "$@" > "$log" 2>&1
  else
    "$OC" create --dry-run=server -f "$OUT/manifests/$file.yaml" "$@" > "$log" 2>&1
  fi
  local rc=$?
  if grep -qE 'is chosen by the platform|Only the platform sets the tenant label' "$log"; then verdict=policy
  elif grep -qE 'is forbidden: User .* cannot ' "$log"; then verdict=rbac-403
  elif [ "$rc" = 0 ]; then verdict=none
  else verdict=error
  fi
  local ok=FAIL
  case "|$want|" in *"|$verdict|"*) ok=ok ;; esac
  [ "$ok" = ok ] || fail=1
  printf '%s\t%s\t%s\t%s\n' "$id" "$verdict" "$want" "$ok" | tee -a "$OUT/result.tsv"
}

po po-pullsecret '    pullSecretVaultPath: probe/path'
po po-pullprop '    pullSecretVaultProperty: probe'
po po-sshkey '    sshKeyVaultPath: probe/path'
po po-plain '    workerCount: 3'
oso oso-vault '  vaultPath: probe/path'
oso oso-route53 '  route53VaultPath: probe/path'
oso oso-plain '  project: probe'
nsm uxr-p0-probe "$ENTITY" ''
nsm uxr-p0-override "$ENTITY" '  annotations:
    hybridsovereign.redhat/label-override: probe'

# P3 as granted tenant users (the real writers)
row P3-01 policy po-pullsecret "${AS_ADMIN[@]}"
row P3-02 policy po-pullprop "${AS_ADMIN[@]}"
row P3-03 policy po-sshkey "${AS_ADMIN[@]}"
row P3-04 policy oso-vault "${AS_INFRA[@]}"
row P3-05 policy oso-route53 "${AS_INFRA[@]}"
row P3-06 policy oso-vault "${AS_ADMIN[@]}"
# no false positives
row P3-07 none po-plain "${AS_ADMIN[@]}"
row P3-08 none oso-plain "${AS_INFRA[@]}"
# platform identities are exempt
row P3-09 none oso-vault "${AS_PLATFORM[@]}"
row P3-10 'none|rbac-403' oso-vault --as="$AUTOMATION"
# RBAC row: a signed-in user without a grant never reaches admission
row P3-11 rbac-403 oso-vault "${AS_NOBODY[@]}"
# P0: the tenant label on a namespace
row P0-01 policy uxr-p0-probe "${AS_PLATFORM[@]}"
row P0-02 none uxr-p0-override "${AS_PLATFORM[@]}"
row P0-03 'none|rbac-403' uxr-p0-probe --as="$OPERATOR"
row P0-04 rbac-403 - label namespace "$NS" "hybridsovereign.redhat/entity=$ENTITY-x" --overwrite --dry-run=server "${AS_ADMIN[@]}"
row P0-05 'none|error' - label namespace "$NS" "hybridsovereign.redhat/entity=$ENTITY" --overwrite --dry-run=server "${AS_PLATFORM[@]}"
# Q3: project self-provisioning
sp_row() {  # id, expected yes|no, then the identity arguments
  local id="$1" want="$2"; shift 2
  local got
  got=$("$OC" auth can-i create projectrequests.project.openshift.io "$@" 2> "$OUT/$id.err" | tail -1)
  local ok=FAIL
  [ "$got" = "$want" ] && ok=ok
  [ "$ok" = ok ] || fail=1
  printf '%s\t%s\t%s\t%s\n' "$id" "${got:-error}" "$want" "$ok" | tee -a "$OUT/result.tsv"
}
sp_row SP-01 no "${AS_NOBODY[@]}"
sp_row SP-02 no "${AS_ADMIN[@]}"
sp_row SP-03 yes --as=probe-platform --as-group=sovereign-admin --as-group=system:authenticated

echo "results: $OUT/result.tsv"
exit $fail
