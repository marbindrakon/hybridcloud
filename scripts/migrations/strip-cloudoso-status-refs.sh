#!/usr/bin/env bash
# Remove the Vault paths and secret references that older CloudOSO jobs wrote into the status
# (accountVaultPath, route53VaultPath, vaultPaths, dns.dnsConfigSecret). The jobs derive them again from the
# CloudOSO's site and DNSConfig (eda/common/tasks/resolve_cloudoso_accounts.yml).
#
# An object is stripped only when, right before its patch, the derived values equal the stored ones for every
# stored key (scripts/migrations/cloudoso-derive-check.yml; verdicts only, never values). The patch carries the
# resourceVersion that was checked, so a concurrent writer makes it fail instead of stripping a changed object.
# Objects whose values differ or cannot be derived are kept and listed in $OUT/owner-steps.txt; the script then
# exits 1. There is no way to force one.
#
# Usage: KUBECONFIG=<services> strip-cloudoso-status-refs.sh [--apply] [--out DIR]
#   without --apply: list the objects and their verdicts, change nothing
# Env: OC (default oc), CLOUDOSO_DEFAULTS (JSON; default: the CLOUDOSO_DEFAULTS value in
#      bootstrap/helm/central/values.yaml of this checkout)
set -o pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
OC=${OC:-oc}
APPLY=false
OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=true ;;
    --out) shift; OUT="$1" ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$OUT" ] || OUT="./strip-cloudoso-out-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT" || exit 2
chmod 700 "$OUT"
: > "$OUT/owner-steps.txt"
: > "$OUT/result.tsv"

for tool in "$OC" jq ansible-playbook; do
  command -v "$tool" >/dev/null 2>&1 || { echo "missing tool: $tool" >&2; exit 2; }
done

DEFAULTS_FILE="$OUT/cloudoso-defaults.json"
if [ -n "$CLOUDOSO_DEFAULTS" ]; then
  printf '%s' "$CLOUDOSO_DEFAULTS" | jq '{cloudoso_defaults: .}' > "$DEFAULTS_FILE" || exit 2
else
  command -v yq >/dev/null 2>&1 || { echo "missing tool: yq (or set CLOUDOSO_DEFAULTS)" >&2; exit 2; }
  yq -o json '[.. | select(tag == "!!map") | select(.name == "CLOUDOSO_DEFAULTS") | .value] | .[0] // "{}"' \
    "$ROOT/bootstrap/helm/central/values.yaml" | jq -r . | jq '{cloudoso_defaults: .}' > "$DEFAULTS_FILE" || exit 2
fi

# objects that still carry any of the four keys (names and key names only)
"$OC" get cloudosos.hybridsovereign.redhat -A -o json > "$OUT/cloudosos.json" || exit 2
jq -r '.items[] | . as $o | [
    (if (.status.accountVaultPath? != null) then "accountVaultPath" else empty end),
    (if (.status.route53VaultPath? != null) then "route53VaultPath" else empty end),
    (if (.status.vaultPaths? != null) then "vaultPaths" else empty end),
    (if (.status.dns?.dnsConfigSecret? != null) then "dnsConfigSecret" else empty end)
  ] | select(length > 0) | "\($o.metadata.namespace)\t\($o.metadata.name)\t\(join(","))"' \
  "$OUT/cloudosos.json" > "$OUT/candidates.tsv" || exit 2
rm -f "$OUT/cloudosos.json"

if [ ! -s "$OUT/candidates.tsv" ]; then
  echo "no CloudOSO carries the keys"
  exit 0
fi

kept=0
failed=0
while IFS=$'\t' read -r ns name keys; do
  [ -n "$name" ] || continue
  obj=$("$OC" get cloudosos.hybridsovereign.redhat "$name" -n "$ns" -o json) || { echo "$ns/$name: read failed"; failed=1; continue; }
  rv=$(printf '%s' "$obj" | jq -r .metadata.resourceVersion)
  has_dns=$(printf '%s' "$obj" | jq -r 'if (.status.dns?.dnsConfigSecret? != null) then "yes" else "no" end')
  obj=""
  report="$OUT/$ns.$name.verdicts.json"
  rm -f "$report"
  if ! ansible-playbook "$HERE/cloudoso-derive-check.yml" -e "cdc_namespace=$ns" -e "cdc_name=$name" \
       -e "cdc_report=$report" -e "@$DEFAULTS_FILE" > "$OUT/$ns.$name.check.log" 2>&1 || [ ! -s "$report" ]; then
    echo "$ns/$name: check failed (see $OUT/$ns.$name.check.log)"
    failed=1
    continue
  fi
  bad=$(jq -r 'to_entries | map(select(.value != "equal" and .value != "absent")) | map("\(.key)=\(.value)") | join(",")' "$report")
  verdicts=$(jq -r 'to_entries | map("\(.key)=\(.value)") | join(",")' "$report")
  if [ -n "$bad" ]; then
    echo "$ns/$name: kept ($bad)"
    printf '%s/%s\tkept\t%s\n' "$ns" "$name" "$bad" >> "$OUT/owner-steps.txt"
    printf '%s/%s\tkept\t%s\n' "$ns" "$name" "$verdicts" >> "$OUT/result.tsv"
    kept=1
    continue
  fi
  if [ "$APPLY" != true ]; then
    echo "$ns/$name: would strip ($keys; $verdicts)"
    printf '%s/%s\twould-strip\t%s\n' "$ns" "$name" "$verdicts" >> "$OUT/result.tsv"
    continue
  fi
  patch=$(jq -cn --arg rv "$rv" --arg dns "$has_dns" \
    '{metadata: {resourceVersion: $rv},
      status: ({accountVaultPath: null, route53VaultPath: null, vaultPaths: null}
               + (if $dns == "yes" then {dns: {dnsConfigSecret: null}} else {} end))}')
  if "$OC" patch cloudosos.hybridsovereign.redhat "$name" -n "$ns" --subresource=status --type=merge -p "$patch" >/dev/null; then
    echo "$ns/$name: stripped ($keys)"
    printf '%s/%s\tstripped\t%s\n' "$ns" "$name" "$verdicts" >> "$OUT/result.tsv"
  else
    echo "$ns/$name: patch refused (changed since the check?); run again"
    failed=1
  fi
done < "$OUT/candidates.tsv"

echo "results: $OUT/result.tsv"
[ "$kept" = 0 ] || echo "kept objects (owner steps): $OUT/owner-steps.txt"
[ "$failed" = 0 ] || exit 2
[ "$kept" = 0 ] || exit 1
exit 0
