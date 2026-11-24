/**
 * Tenant-safe view of the objects a tenant reads (status allow-list of tenantStatusAllow.js).
 *
 * It keeps only the listed status keys of each kind (common ones plus the kind's own), drops every key at any
 * depth whose name looks like a Vault path or a secret reference, and drops the spec fields the platform chooses.
 * The same list serves the tenant pages in the browser, so the console plugins (which read the API directly)
 * show what the tenant dashboard shows.
 *
 *   tenantSafe(kind, obj)            one object or a list (kind of the items)
 *   transformBody(path, obj, req)    the hook of createApiK8sProxy: filters every answer that carries one of
 *                                    the listed kinds, found by the object's own kind (the object, a List and
 *                                    its items, a watch event's object) and by the path (item, list, /status)
 *                                    for objects without a kind; refuses a watch on their plurals with 400
 *   tenantSafeResponses()            Express middleware for the dashboard's own /api/<plural> routes: every
 *                                    JSON answer (an object or an array of objects) through tenantSafe
 *
 * Other kinds pass unchanged. The admin dashboard does not use this.
 */
import { normaliseK8sPath } from './apiK8sProxy.js';
import { COMMON_STATUS_KEYS, TENANT_STATUS_ALLOW, SECRET_KEY_RE, filterTenantStatus } from './tenantStatusAllow.js';

export { COMMON_STATUS_KEYS, TENANT_STATUS_ALLOW, SECRET_KEY_RE };

/** Spec fields the platform chooses (never shown to tenants). */
export const TENANT_HIDDEN_SPEC = {
  CloudOSO: ['vaultPath', 'route53VaultPath'],
  PlatformOpenshift: ['openstack.pullSecretVaultPath', 'openstack.pullSecretVaultProperty', 'openstack.sshKeyVaultPath'],
};

/** Plural of each filtered kind (every kind with a tenant allow-list), and the kinds by their own and list kind. */
const PLURAL_KIND = Object.fromEntries(Object.keys(TENANT_STATUS_ALLOW).map((k) => [`${k.toLowerCase()}s`, k]));
const FILTERED_KIND = Object.fromEntries(Object.keys(TENANT_STATUS_ALLOW).flatMap((k) => [[k, k], [`${k}List`, k]]));
const PATH_RE = /^\/apis\/hybridsovereign\.redhat\/[^/]+\/(?:watch\/)?(?:namespaces\/[^/]+\/)?([a-z]+)(?:\/[^/]+)?(?:\/status)?$/;

function isPlainObject(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v);
}

function dropSpecPaths(kind, spec) {
  const paths = TENANT_HIDDEN_SPEC[kind];
  if (!paths || !isPlainObject(spec)) return spec;
  const copy = JSON.parse(JSON.stringify(spec));
  for (const p of paths) {
    const parts = p.split('.');
    let cur = copy;
    for (let i = 0; i < parts.length - 1 && isPlainObject(cur); i += 1) cur = cur[parts[i]];
    if (isPlainObject(cur)) delete cur[parts[parts.length - 1]];
  }
  return copy;
}

function safeItem(kind, item) {
  if (!isPlainObject(item)) return item;
  const out = { ...item };
  if ('spec' in out) out.spec = dropSpecPaths(kind, out.spec);
  if ('status' in out) out.status = filterTenantStatus(kind, out.status);
  return out;
}

/** Tenant-safe copy of one object or a list of `kind`; other kinds are returned unchanged. */
export function tenantSafe(kind, obj) {
  if (!TENANT_STATUS_ALLOW[kind] || !isPlainObject(obj)) return obj;
  if (Array.isArray(obj.items)) return { ...obj, items: obj.items.map((i) => safeItem(kind, i)) };
  return safeItem(kind, obj);
}

/** Kind served by a raw API path (normalised as the proxy forwards it), or null when it is not a filtered resource. */
export function filteredKindForPath(path) {
  const norm = normaliseK8sPath(path);
  if ('error' in norm) return null;
  const m = PATH_RE.exec(norm.pathname);
  return m ? PLURAL_KIND[m[1]] ?? null : null;
}

/** Filtered kind of an object by its own kind, else the fallback (the path's kind). */
function kindOf(obj, fallback) {
  return (isPlainObject(obj) && FILTERED_KIND[obj.kind]) || fallback || null;
}

/** A watch event: { type, object } without a kind of its own. */
function isWatchEvent(obj) {
  return isPlainObject(obj) && !('kind' in obj) && typeof obj.type === 'string' && 'object' in obj;
}

/** Tenant-safe copy of any answer: one object, a list (by list kind or item kinds) or a watch event. */
function safeAnswer(obj, pathKind) {
  if (!isPlainObject(obj) || obj.kind === 'Status') return obj;
  if (isWatchEvent(obj)) return { ...obj, object: safeAnswer(obj.object, pathKind) };
  const kind = kindOf(obj, pathKind);
  if (Array.isArray(obj.items)) {
    return {
      ...obj,
      items: obj.items.map((item) => {
        const k = kindOf(item, kind);
        return k ? safeItem(k, item) : item;
      }),
    };
  }
  return kind ? safeItem(kind, obj) : obj;
}

/** createApiK8sProxy hook: obj is null before a watch is forwarded (refused on the filtered resources). */
export function transformBody(path, obj, _req) {
  const pathKind = filteredKindForPath(path);
  if (obj === null) {
    if (!pathKind) return null;
    const err = new Error('Watching this resource is not available here; reload the list instead.');
    err.status = 400;
    throw err;
  }
  return safeAnswer(obj, pathKind);
}

/**
 * Express middleware (mounted on /api): answers of the dashboard's own routes for a listed kind
 * (/api/<plural> and /api/<plural>/<name>) go through tenantSafe, an array item by item.
 */
export function tenantSafeResponses() {
  return (req, res, next) => {
    const plural = String(req.path || '').split('/')[1] || '';
    const kind = PLURAL_KIND[plural];
    if (!kind) return next();
    const json = res.json.bind(res);
    res.json = (body) => json(Array.isArray(body) ? body.map((item) => tenantSafe(kind, item)) : tenantSafe(kind, body));
    return next();
  };
}
