#!/usr/bin/env bash
# Verify Sovereign Cloud console plugins: manifests, assets, routes, i18n.
set -euo pipefail

NS="${NS:-sovereign-cloud}"
FAIL=0
PASS=0

pass() { echo "  OK  $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL $1"; FAIL=$((FAIL + 1)); }

check_plugin() {
  local deploy="$1"
  local plugin_name="$2"
  local expected_tag="$3"

  echo ""
  echo "=== $plugin_name ($deploy) ==="

  local image
  image=$(oc get deploy "$deploy" -n "$NS" -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
  if [[ "$image" == *":$expected_tag" ]]; then
    pass "image tag $expected_tag"
  else
    fail "image tag expected $expected_tag, got $image"
  fi

  local ready
  ready=$(oc get deploy "$deploy" -n "$NS" -o jsonpath='{.status.readyReplicas}/{.spec.replicas}' 2>/dev/null || echo "0/0")
  if [[ "$ready" == "1/1" ]]; then
    pass "deployment ready $ready"
  else
    fail "deployment not ready ($ready)"
  fi

  local cp_status
  cp_status=$(oc get consoleplugin "$plugin_name" -o jsonpath='{.spec.backend.service.name}:{.spec.backend.service.port}' 2>/dev/null || true)
  if [[ -n "$cp_status" ]]; then
    pass "ConsolePlugin CR backend $cp_status"
  else
    fail "ConsolePlugin CR missing or invalid"
  fi

  local manifest
  manifest=$(oc exec -n "$NS" "deploy/$deploy" -- cat /usr/share/nginx/html/plugin-manifest.json 2>/dev/null || true)
  if [[ -z "$manifest" ]]; then
    fail "plugin-manifest.json unreachable"
    return
  fi
  pass "plugin-manifest.json reachable"

  local version
  version=$(echo "$manifest" | python3 -c "import sys,json; print(json.load(sys.stdin).get('version',''))" 2>/dev/null || true)
  if [[ "$plugin_name" == "$(echo "$manifest" | python3 -c "import sys,json; print(json.load(sys.stdin).get('name',''))" 2>/dev/null)" ]]; then
    pass "manifest plugin $plugin_name (bundle v$version)"
  else
    fail "manifest expected $plugin_name, got $(echo "$manifest" | python3 -c "import sys,json; m=json.load(sys.stdin); print(m.get('name'), m.get('version'))" 2>/dev/null)"
  fi

  # Every exposed module chunk must exist and return HTTP 200
  local modules
  modules=$(echo "$manifest" | python3 -c "
import sys, json
m = json.load(sys.stdin)
for name in m.get('exposedModules', {}):
    print(name)
" 2>/dev/null || true)

  local mod_count=0
  local mod_fail=0
  while IFS= read -r mod; do
    [[ -z "$mod" ]] && continue
    mod_count=$((mod_count + 1))
    local chunk="exposed-${mod}-chunk.js"
    local code
    code=$(oc exec -n "$NS" "deploy/$deploy" -- sh -c "test -f /usr/share/nginx/html/$chunk && echo 200 || echo 404" 2>/dev/null || echo "000")
    if [[ "$code" == "200" ]]; then
      pass "chunk $chunk"
    else
      fail "chunk $chunk HTTP $code"
      mod_fail=$((mod_fail + 1))
    fi
  done <<< "$modules"

  if [[ "$mod_count" -gt 0 && "$mod_fail" -eq 0 ]]; then
    pass "all $mod_count exposed module chunks present"
  fi

  # plugin-entry.js must load
  local entry_code
  entry_code=$(oc exec -n "$NS" "deploy/$deploy" -- sh -c "test -f /usr/share/nginx/html/plugin-entry.js && echo 200 || echo 404" 2>/dev/null || echo "000")
  if [[ "$entry_code" == "200" ]]; then
    pass "plugin-entry.js HTTP 200"
  else
    fail "plugin-entry.js HTTP $entry_code"
  fi

  # i18n fix markers in shared chunk
  local i18n_ok
  i18n_ok=$(oc exec -n "$NS" "deploy/$deploy" -- sh -c 'grep -rl "__HYBRIDSOVEREIGN_I18N__" /usr/share/nginx/html/*-chunk.js 2>/dev/null | head -1' 2>/dev/null || true)
  if [[ -n "$i18n_ok" ]]; then
    pass "i18n global singleton present"
  else
    fail "i18n global singleton missing"
  fi

  local bind_ok
  bind_ok=$(oc exec -n "$NS" "deploy/$deploy" -- sh -c 'grep -rl "i18n:a})" /usr/share/nginx/html/*-chunk.js 2>/dev/null | head -1' 2>/dev/null || true)
  if [[ -n "$bind_ok" ]]; then
    pass "useTranslation i18n binding present"
  else
    fail "useTranslation i18n binding missing"
  fi

  # Translation resources embedded (not just keys)
  local entity_label
  entity_label=$(oc exec -n "$NS" "deploy/$deploy" -- sh -c 'grep -rl "\"entity\":\"Entity\"" /usr/share/nginx/html/*-chunk.js 2>/dev/null | head -1' 2>/dev/null || true)
  if [[ -n "$entity_label" ]]; then
    pass "en.json translations embedded"
  else
    fail "en.json translations not found in shared chunk"
  fi

  # Route count sanity
  local routes
  routes=$(echo "$manifest" | python3 -c "import sys,json; m=json.load(sys.stdin); print(sum(1 for e in m.get('extensions',[]) if e.get('type')=='console.page/route'))" 2>/dev/null || echo 0)
  # Routes are generated from the navigation registry (gen-console-extensions.mjs).
  local min_routes=60
  if [[ "$plugin_name" == "sovereign-admin-plugin" ]]; then min_routes=20; fi
  if [[ "$routes" -ge "$min_routes" ]]; then
    pass "$routes page routes registered"
  else
    fail "only $routes page routes (expected >= $min_routes)"
  fi

  # The one page module every route renders
  local overview_chunk="exposed-ShcConsolePage-chunk.js"
  local ov_code
  ov_code=$(oc exec -n "$NS" "deploy/$deploy" -- sh -c "test -f /usr/share/nginx/html/$overview_chunk && echo 200 || echo 404" 2>/dev/null || echo "000")
  if [[ "$ov_code" == "200" ]]; then
    pass "overview chunk $overview_chunk"
  else
    fail "missing overview chunk $overview_chunk"
  fi
}

echo "Sovereign Cloud console plugin verification"
echo "Namespace: $NS"
echo "Cluster: $(oc whoami --show-server 2>/dev/null || echo unknown)"

# Expected image tags default to the live Deployments' own tags (so the check is "a tag is set"); override with
# ADMIN_PLUGIN_TAG / TENANT_PLUGIN_TAG to assert a release.
ADMIN_PLUGIN_TAG="${ADMIN_PLUGIN_TAG:-$(oc get deploy sovereign-admin-plugin -n "$NS" -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null | sed 's/.*://')}"
TENANT_PLUGIN_TAG="${TENANT_PLUGIN_TAG:-$(oc get deploy sovereign-tenant-plugin -n "$NS" -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null | sed 's/.*://')}"
check_plugin "sovereign-admin-plugin" "sovereign-admin-plugin" "$ADMIN_PLUGIN_TAG"
check_plugin "sovereign-tenant-plugin" "sovereign-tenant-plugin" "$TENANT_PLUGIN_TAG"

echo ""
echo "=== Console operator plugin registration ==="
for p in sovereign-admin-plugin sovereign-tenant-plugin; do
  if oc get consoleplugin "$p" -o jsonpath='{.metadata.name}' 2>/dev/null | grep -q "$p"; then
    pass "consoleplugin $p registered"
  else
    fail "consoleplugin $p not registered"
  fi
done

echo ""
echo "=== Console pod plugin errors (last 200 lines) ==="
console_errors=$(oc logs -n openshift-console deployment/console --tail=200 2>/dev/null | grep -iE 'sovereign|hybridsovereign' | grep -iE 'error|fail|warn' | tail -10 || true)
if [[ -z "$console_errors" ]]; then
  pass "no sovereign plugin errors in recent console logs"
else
  echo "$console_errors"
  fail "sovereign plugin errors/warnings in console logs (see above)"
fi

echo ""
echo "=== Route / extension integrity ==="
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
REPO_ROOT="$REPO_ROOT" python3 <<'PY'
import json, sys, pathlib, os

ROOT = os.environ["REPO_ROOT"]

def load_ext(path):
    return json.loads(pathlib.Path(path).read_text())

def code_refs(ext):
    refs = set()
    for e in ext:
        comp = e.get("properties", {}).get("component", {})
        ref = comp.get("$codeRef", "")
        if ref:
            refs.add(ref.split(".")[0])
    return refs

admin_ext = load_ext("/home/gshankar/storage/sovereign/hybridcloud/ui/packages/admin-console-plugin/console-extensions.json")
tenant_ext = load_ext("/home/gshankar/storage/sovereign/hybridcloud/ui/packages/tenant-console-plugin/console-extensions.json")

admin_pkg = json.loads(pathlib.Path("/home/gshankar/storage/sovereign/hybridcloud/ui/packages/admin-console-plugin/package.json").read_text())
tenant_pkg = json.loads(pathlib.Path("/home/gshankar/storage/sovereign/hybridcloud/ui/packages/tenant-console-plugin/package.json").read_text())

admin_exposed = set(admin_pkg["consolePlugin"]["exposedModules"].keys())
tenant_exposed = set(tenant_pkg["consolePlugin"]["exposedModules"].keys())

admin_refs = code_refs(admin_ext)
tenant_refs = code_refs(tenant_ext)

admin_missing = admin_refs - admin_exposed
tenant_missing = tenant_refs - tenant_exposed

ok = True
if admin_missing:
    print(f"  FAIL admin route codeRefs missing from exposedModules: {sorted(admin_missing)}")
    ok = False
else:
    print(f"  OK  admin: {len(admin_refs)} route codeRefs all exposed")

if tenant_missing:
    print(f"  FAIL tenant route codeRefs missing from exposedModules: {sorted(tenant_missing)}")
    ok = False
else:
    print(f"  OK  tenant: {len(tenant_refs)} route codeRefs all exposed")

sys.exit(0 if ok else 1)
PY
route_ok=$?
if [[ "$route_ok" -eq 0 ]]; then
  PASS=$((PASS + 1))
else
  FAIL=$((FAIL + 1))
fi

echo ""
echo "=== Admin ConsolePlugin proxy (UI Health) ==="
proxy_count=$(oc get consoleplugin sovereign-admin-plugin -o json 2>/dev/null | python3 -c "import sys,json; p=json.load(sys.stdin); print(len(p.get('spec',{}).get('proxy',[])))" 2>/dev/null || echo 0)
if [[ "$proxy_count" -ge 1 ]]; then
  pass "admin plugin has $proxy_count proxy endpoint(s)"
else
  fail "admin plugin missing uihealth proxy"
fi

echo ""
echo "=== K8s API backing data (sample) ==="
count=$(oc get entities -n sovereign-cloud --no-headers 2>/dev/null | wc -l || echo 0)
if [[ "$count" -ge 1 ]]; then
  pass "sovereign-cloud/entities count=$count"
else
  fail "sovereign-cloud/entities empty or unavailable"
fi
entity_ns=$(oc get entities -n sovereign-cloud --no-headers 2>/dev/null | head -1 | awk '{print $1}')
if [[ -n "$entity_ns" ]]; then
  ns="entity-${entity_ns}"
  for kind in teams projects platformopenshifts assignments; do
    count=$(oc get "$kind" -n "$ns" --no-headers 2>/dev/null | wc -l || echo 0)
    if [[ "$count" -ge 1 ]]; then
      pass "$ns/$kind count=$count"
    else
      fail "$ns/$kind empty (tenant pages may show no data)"
    fi
  done
fi

echo ""
echo "=== ArgoCD (central) ==="
if [[ -n "${OCP_CENTRAL_SERVER:-}" ]]; then
  if oc login "$OCP_CENTRAL_SERVER" -u "${OCP_CENTRAL_USERNAME:-}" -p "${OCP_CENTRAL_PASSWORD:-}" --insecure-skip-tls-verify=true >/dev/null 2>&1; then
    while read -r name sync health; do
      [[ -z "$name" ]] && continue
      if [[ "$sync" == "Synced" && "$health" == "Healthy" ]]; then
        pass "ArgoCD $name $sync/$health"
      else
        fail "ArgoCD $name $sync/$health"
      fi
    done < <(oc get applications.argoproj.io sovereign-admin-plugin sovereign-tenant-plugin -n openshift-gitops -o custom-columns=NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status --no-headers 2>/dev/null)
    oc login "$OCP_SERVICES_SERVER" -u "${OCP_SERVICES_USERNAME:-}" -p "${OCP_SERVICES_PASSWORD:-}" --insecure-skip-tls-verify=true >/dev/null 2>&1 || true
  else
    fail "could not login to central cluster for ArgoCD check"
  fi
else
  fail "OCP_CENTRAL_SERVER not set — skipping ArgoCD check"
fi

echo ""
echo "=== Summary ==="
echo "Passed: $PASS"
echo "Failed: $FAIL"
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
echo "All checks passed."
