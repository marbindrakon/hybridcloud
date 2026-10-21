/**
 * Tenant-safe view of CloudOSO and PlatformOpenshift objects (status allow-list).
 *
 * The jobs no longer write Vault paths or secret references into these objects' status; this module is the
 * second line for objects written before that, and for anything a future job adds by mistake. It keeps only the
 * listed status keys (common ones plus each kind's own), drops every key at any depth whose name looks like a
 * Vault path or a secret reference, and drops the spec fields the platform chooses.
 *
 *   tenantSafe(kind, obj)            one object or a list (kind of the items)
 *   transformBody(path, obj, req)    the hook of createApiK8sProxy: filters every answer that carries one of
 *                                    the two kinds, found by the object's own kind (the object, a List and
 *                                    its items, a watch event's object) and by the path (item, list, /status)
 *                                    for objects without a kind; refuses a watch on the two plurals with 400
 *
 * Other kinds pass unchanged (their status carries no Vault paths). The admin dashboard does not use this.
 */
import { normaliseK8sPath } from './apiK8sProxy.js';

/** Status keys every kind keeps. */
export const COMMON_STATUS_KEYS = [
  'phase', 'phaseSince', 'summary', 'conditions', 'observedGeneration', 'message', 'reason', 'ready', 'status',
  'lastReconciledAt', 'lastHandledReconcileRequest', 'realization', 'deletion', 'deletionComplete',
  'deletionBlockedBy', 'waitingFor', 'retry', 'endpoints', 'config', 'references',
];

/**
 * Per kind: status keys kept besides the common ones. A string keeps the key whole (still cleaned of
 * secret-looking names); [key, [subkeys]] keeps only those subkeys of an object.
 */
export const TENANT_STATUS_ALLOW = {
  CloudOSO: [
    'name', 'namespace', 'entity', 'cloudInfrastructure', 'projectId', 'projectName', 'domainId', 'domainName',
    'appCredentialId', 'apiFloatingIPId', 'ingressFloatingIPId', 'apiVIP', 'ingressVIP', 'externalNetwork',
    'slug', 'domain', 'dnsProvider', 'dnsZoneId', 'dnsZoneName', 'dnsProjectId', 'route53ZoneId',
    'leftoverDefaultSecurityGroups',
    ['dns', ['mode', 'zone', 'zoneId', 'ownerProjectId', 'sharedWith', 'shareId', 'nameservers', 'delegated',
      'dnsConfig', 'globalProjectId', 'globalZoneId']],
    ['site', ['name', 'console']],
  ],
  PlatformOpenshift: [
    'name', 'namespace', 'entity', 'billingId', 'description', 'platformType', 'slug', 'domain', 'apiURL', 'appsURL',
    'consoleURL', 'clusterVersion', 'powerState', 'clusterHealth', 'provisionStatus', 'acmAvailable',
    'clusterInstalled', 'buildName', 'awsAccountId', 'awsRegion', 'gcpProject', 'gcpRegion', 'gcpVpc',
    'serviceAccountIssuer', 'network', 'placement', 'publish', 'machineNetwork', 'oidcClientId',
    ['dns', ['provider', 'projectId', 'records']],
    ['site', ['name', 'console']],
  ],
};

/** Spec fields the platform chooses (never shown to tenants). */
export const TENANT_HIDDEN_SPEC = {
  CloudOSO: ['vaultPath', 'route53VaultPath'],
  PlatformOpenshift: ['openstack.pullSecretVaultPath', 'openstack.pullSecretVaultProperty', 'openstack.sshKeyVaultPath'],
};

/** Key names never sent to tenants, at any depth of the status. */
export const SECRET_KEY_RE = /VaultPath|VaultProperty|vaultPaths|Secret$/;

const PLURAL_KIND = { cloudosos: 'CloudOSO', platformopenshifts: 'PlatformOpenshift' };
/** Object kinds (and their list kinds) that are filtered wherever they appear. */
const FILTERED_KIND = {
  CloudOSO: 'CloudOSO',
  CloudOSOList: 'CloudOSO',
  PlatformOpenshift: 'PlatformOpenshift',
  PlatformOpenshiftList: 'PlatformOpenshift',
};
const PATH_RE = /^\/apis\/hybridsovereign\.redhat\/[^/]+\/(?:watch\/)?(?:namespaces\/[^/]+\/)?(cloudosos|platformopenshifts)(?:\/[^/]+)?(?:\/status)?$/;

function isPlainObject(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v);
}

/** Copy without keys that look like Vault paths or secret references, at any depth. */
function scrub(value) {
  if (Array.isArray(value)) return value.map(scrub);
  if (!isPlainObject(value)) return value;
  const out = {};
  for (const [k, v] of Object.entries(value)) {
    if (SECRET_KEY_RE.test(k)) continue;
    out[k] = scrub(v);
  }
  return out;
}

function filterStatus(kind, status) {
  if (!isPlainObject(status)) return status;
  const out = {};
  const allow = new Map();
  for (const k of COMMON_STATUS_KEYS) allow.set(k, null);
  for (const entry of TENANT_STATUS_ALLOW[kind] || []) {
    if (Array.isArray(entry)) allow.set(entry[0], entry[1]);
    else allow.set(entry, null);
  }
  for (const [k, v] of Object.entries(status)) {
    if (!allow.has(k) || SECRET_KEY_RE.test(k)) continue;
    const sub = allow.get(k);
    if (sub && isPlainObject(v)) {
      const picked = {};
      for (const s of sub) if (s in v && !SECRET_KEY_RE.test(s)) picked[s] = scrub(v[s]);
      out[k] = picked;
    } else if (sub) {
      // a key with a subkey list that is not an object carries nothing tenants need
      continue;
    } else {
      out[k] = scrub(v);
    }
  }
  return out;
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
  if ('status' in out) out.status = filterStatus(kind, out.status);
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
  return m ? PLURAL_KIND[m[1]] : null;
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
