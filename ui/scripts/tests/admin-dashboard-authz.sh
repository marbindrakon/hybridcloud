#!/usr/bin/env bash
# Live regression check for the admin dashboard authorization (UX-097, I-14).
#
#   admin-dashboard-authz.sh <dashboard-url> <non-admin-token-file> <admin-token-file>
#
# Token files hold one OpenShift access token each (for example from
# `oc whoami -t` after an `oc login` as that user); they are read, never
# printed. Every former and current /api path is called with a bearer token
# (the oauth-proxy accepts bearer tokens through --openshift-delegate-urls):
#   - the non-admin token must get 401/403 (or a sign-in redirect) everywhere;
#   - the admin token must reach the live routes and get 404 for the removed ones.
# Prints a table and exits 1 on any leak or unexpected answer.
set -o pipefail
url="${1%/}"; nonadmin_file="$2"; admin_file="$3"
if [ -z "$url" ] || [ ! -r "$nonadmin_file" ] || [ ! -r "$admin_file" ]; then
  echo "usage: $0 <dashboard-url> <non-admin-token-file> <admin-token-file>" >&2
  exit 2
fi
nonadmin="$(tr -d '\n' < "$nonadmin_file")"
admin="$(tr -d '\n' < "$admin_file")"

# method path admin-expectation (live = 2xx/4xx answer from the app, gone = 404)
routes=(
  "GET /api/user live"
  "GET /api/k8s/apis/hybridsovereign.redhat/v1alpha1/namespaces/sovereign-cloud/entities live"
  "POST /api/uihealth/probe live"
  "GET /api/overview/crs gone"
  "GET /api/routes gone"
  "GET /api/operators gone"
  "GET /api/rbacconfigs gone"
  "GET /api/entities gone"
  "POST /api/entities gone"
  "GET /api/entities/x/rbacs gone"
  "GET /api/entities/x/personas gone"
  "GET /api/routes/healthcheck?url=https://example.invalid/ gone"
  "POST /api/routes/healthcheck/batch gone"
)

code() {
  local method="$1" path="$2" token="$3"
  curl -sk -o /dev/null -w '%{http_code}' -X "$method" \
    -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
    $( [ "$method" = POST ] && printf -- '--data {}' ) "$url$path"
}

fail=0
printf '%-6s %-75s %-9s %-9s %s\n' METHOD PATH NONADMIN ADMIN RESULT
for r in "${routes[@]}"; do
  read -r method path expect <<< "$r"
  n="$(code "$method" "$path" "$nonadmin")"
  a="$(code "$method" "$path" "$admin")"
  ok=yes
  case "$n" in 401|403|302) ;; *) ok=no ;; esac
  if [ "$expect" = gone ]; then
    [ "$a" = 404 ] || ok=no
  else
    case "$a" in 2??|400) ;; *) ok=no ;; esac
  fi
  [ "$ok" = yes ] || fail=1
  printf '%-6s %-75s %-9s %-9s %s\n' "$method" "$path" "$n" "$a" "$([ "$ok" = yes ] && echo ok || echo LEAK/UNEXPECTED)"
done
exit "$fail"
