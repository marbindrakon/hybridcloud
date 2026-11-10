#!/bin/bash
# Publishes each issuer's discovery document and JWKS into ConfigMap oidc-documents (served by the issuer front end),
# with issuer/jwks_uri rewritten to BASE_URL/<name>.
#
# Environment (rendered by the chart):
#   NAMESPACE          namespace of oidc-documents and of the static issuers' ConfigMaps
#   BASE_URL           public issuer base
#   CLUSTERS           one issuer per line: "<name> local" | "<name> acm" | "<name> url <base URL> [<keys path>]"
#   MSA                ManagedServiceAccount whose token (Secret <MSA> in namespace <name>) reads an acm cluster
#   PROXY_URL          cluster-proxy user server (<PROXY_URL>/<name> is the cluster's API)
#   STATIC_LABEL       label (=true) of the static issuers' ConfigMaps; STATIC_NAME_LABEL holds the issuer name
#   SA_CA              service CA bundle (cluster-proxy and url sources)
#   WORKDIR, TERMINATION_LOG  scratch directory, the container's termination message file
#
# An issuer that cannot be fetched keeps the documents it last published. The run fails only for a fault: the local
# cluster, a url source or a static issuer that fails, an acm cluster that fails while its ManagedCluster is
# available (or whose ManagedCluster cannot be read), or oidc-documents that cannot be read or written. An acm cluster
# whose ManagedCluster is not available (powered off, paused, unreachable) is reported and is not a fault.
# Annotation <ANN_PREFIX><name> on oidc-documents records when each issuer's documents were last fetched.
# The age of kept documents is recorded there and deliberately not judged: a site may be off or paused for days.
set -o pipefail
SA_CA=${SA_CA:-/var/run/secrets/kubernetes.io/serviceaccount/service-ca.crt}
ANN_PREFIX=${ANN_PREFIX:-shc.signal9.gg/oidc-fetched-}
cd "${WORKDIR:-/tmp}" && rm -rf old new && mkdir -p old new || exit 1
rc=0; report=()
now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
note() { report+=("$1"); echo "$1"; }
fault() { note "$1 (fault)"; rc=1; }
finish() {
  if [ "$rc" -ne 0 ]; then printf '%s\n' "${report[@]}" | head -c 4000 > "${TERMINATION_LOG:-/dev/termination-log}" 2>/dev/null; fi
  exit "$rc"
}

# the last published documents: an issuer that cannot be fetched now keeps them. Without them nothing is applied
# (applying would drop every issuer that is not fetched in this run).
if ! oc get configmap oidc-documents -n "$NAMESPACE" -o json > current.json; then
  fault "oidc-documents: cannot be read"; finish
fi
jq -r '.data // {} | keys[]' current.json | while read -r k; do jq -r --arg k "$k" '.data[$k]' current.json > "old/$k"; done

publish() {  # <name> <oc args...>: fetch, rewrite issuer, check, stage
  local n=$1; shift
  oc "$@" get --raw /openid/v1/jwks > "new/${n}__keys.json" \
    && oc "$@" get --raw /.well-known/openid-configuration \
      | jq --arg iss "$BASE_URL/$n" '.issuer = $iss | .jwks_uri = ($iss + "/keys.json")' \
        > "new/${n}__openid-configuration" \
    && test "$(jq -r .issuer "new/${n}__openid-configuration")" = "$BASE_URL/$n" \
    && jq -e '.keys | length > 0' "new/${n}__keys.json" >/dev/null \
    || { rm -f "new/${n}__keys.json" "new/${n}__openid-configuration"; return 1; }
}
publish_url() {  # <name> <base URL> <keys path>: an HTTPS discovery endpoint (service CA), e.g. SPIRE's
  local n=$1 u=$2 k=$3
  curl -sf --max-time 20 --cacert "$SA_CA" "$u$k" > "new/${n}__keys.json" \
    && curl -sf --max-time 20 --cacert "$SA_CA" "$u/.well-known/openid-configuration" \
      | jq --arg iss "$BASE_URL/$n" '.issuer = $iss | .jwks_uri = ($iss + "/keys.json")' \
        > "new/${n}__openid-configuration" \
    && test "$(jq -r .issuer "new/${n}__openid-configuration")" = "$BASE_URL/$n" \
    && jq -e '.keys | length > 0' "new/${n}__keys.json" >/dev/null \
    || { rm -f "new/${n}__keys.json" "new/${n}__openid-configuration"; return 1; }
}
availability() {  # <ManagedCluster>: prints "<status> <reason> <since>" of its Available condition
  oc get managedcluster "$1" -o json \
    | jq -r '([.status.conditions // [] | .[] | select(.type == "ManagedClusterConditionAvailable")] | first)
             // {status: "Unknown", reason: "NoAvailableCondition", lastTransitionTime: "-"}
             | "\(.status) \(.reason // "-") \(.lastTransitionTime // "-")"'
}

declare -A why
names=""
while read -r -u 3 n src url keys; do
  [ -n "$n" ] || continue
  names="$names $n"
  case "$src" in
    local)
      publish "$n" || fault "$n: fetch from this cluster's API failed" ;;
    url)
      publish_url "$n" "$url" "${keys:-/keys}" || fault "$n: fetch from $url failed" ;;
    acm)
      T=$(oc get secret "$MSA" -n "$n" -o jsonpath='{.data.token}' | base64 -d)
      if [ -n "$T" ] && publish "$n" --server="$PROXY_URL/$n" --token="$T" --certificate-authority="$SA_CA"; then
        unset T; continue
      fi
      [ -n "$T" ] && what="fetch through the cluster proxy failed" || what="no ManagedServiceAccount token"
      unset T
      if ! a=$(availability "$n"); then
        fault "$n: $what, and ManagedCluster $n cannot be read"
      elif [ "${a%% *}" = True ]; then
        fault "$n: $what while ManagedCluster $n is available"
      else
        read -r st reason since <<<"$a"
        why[$n]="unreachable: ManagedCluster $n Available=$st ($reason) since $since; $what"
      fi ;;
    *)
      fault "$n: unknown source '$src'" ;;
  esac
done 3<<<"$CLUSTERS"

# static issuers: ConfigMaps labelled STATIC_LABEL=true in this namespace, issuer name in label STATIC_NAME_LABEL, key
# set in data keys.json (an issuer whose signing key exists before its cluster does, e.g. an AWS cluster in STS mode).
# Names of the issuers above are not taken.
static=""
oc get configmap -n "$NAMESPACE" -l "$STATIC_LABEL=true" -o json \
  | jq -c --arg l "$STATIC_NAME_LABEL" '.items[] | {n: (.metadata.labels[$l] // ""), k: (.data["keys.json"] // "")}' \
  > static.jsonl || fault "static issuers: list failed"
while read -r -u 3 e; do
  n=$(jq -r .n <<<"$e")
  if ! [[ "$n" =~ ^[a-z0-9-]+$ ]] || [[ " $names " == *" $n "* ]]; then fault "static: skipping '$n'"; continue; fi
  jq -r .k <<<"$e" > "new/${n}__keys.json"
  jq -n --arg iss "$BASE_URL/$n" '{issuer: $iss, jwks_uri: ($iss + "/keys.json"),
    response_types_supported: ["id_token"], subject_types_supported: ["public"],
    id_token_signing_alg_values_supported: ["RS256"], claims_supported: ["aud", "exp", "sub", "iat", "iss"]}' \
    > "new/${n}__openid-configuration"
  if jq -e '.keys | length > 0' "new/${n}__keys.json" >/dev/null 2>&1; then static="$static $n"
  else fault "$n: static key set invalid"; rm -f "new/${n}__keys.json" "new/${n}__openid-configuration"; fi
done 3< static.jsonl

# assemble: fetched documents, else the last published ones; one line per issuer
args=(); ann="{}"; published=""
for n in $names $static; do
  last=$(jq -r --arg a "$ANN_PREFIX$n" '.metadata.annotations[$a] // ""' current.json)
  if [ -s "new/${n}__keys.json" ] && [ -s "new/${n}__openid-configuration" ]; then
    d=new; at=$now; published="$published $n"; line="$n: fetched"
  elif [ -s "old/${n}__keys.json" ] && [ -s "old/${n}__openid-configuration" ]; then
    d=old; at=$last; line="$n: keeping the documents fetched ${last:-at an unrecorded time}"
  else
    [ -n "${why[$n]}" ] && note "$n: nothing published yet (${why[$n]})"
    continue
  fi
  [ -n "${why[$n]}" ] && line="$line (${why[$n]})"
  [ "$d" = old ] && note "$line" || echo "$line"
  args+=(--from-file="${n}__keys.json=$d/${n}__keys.json" --from-file="${n}__openid-configuration=$d/${n}__openid-configuration")
  [ -n "$at" ] && ann=$(jq -c --arg a "$ANN_PREFIX$n" --arg v "$at" '. + {($a): $v}' <<<"$ann")
done
oc create configmap oidc-documents -n "$NAMESPACE" --dry-run=client -o json "${args[@]}" \
  | jq --argjson ann "$ann" '.metadata.annotations = $ann' \
  | oc apply --server-side --force-conflicts --field-manager=oidc-sync -f - >/dev/null \
  || fault "oidc-documents: apply failed"
echo "published:$published"
finish
