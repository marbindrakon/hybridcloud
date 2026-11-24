/**
 * The status fields a tenant page shows, per kind a tenant reads: one list for the tenant dashboard's server
 * (shared/server/tenantSafe.js) and for the tenant pages of both dashboards and both console plugins
 * (redactForTenant in the shared UI, the overview and the JSON tab). Pure: no imports, so the browser bundles
 * can use it too.
 *
 * A key not listed is not shown to tenants: platform ids (the platform's projects, zones, clients, policies),
 * fabric numbers (VNIs, route targets, the realization), addresses of platform services and the platform's
 * delivery bookkeeping (jobs, events). The platform keeps those in status for its own later runs; admin pages
 * show everything. Keys whose name looks like a Vault path or a secret reference are dropped at any depth.
 *
 * An entry is a key kept whole (string) or [key, [subkeys]]: only those subkeys of an object, or of each object
 * of a list; subkeys nest the same way. A kind's entry wins over a common key of the same name.
 */

/** Status keys every kind keeps: its state, its sentence, what it waits for, and the tenant's own labels. */
export const COMMON_STATUS_KEYS = [
  'phase', 'phaseSince', 'summary', 'conditions', 'observedGeneration', 'message', 'reason', 'ready', 'status',
  'realized', 'lastReconciledAt', 'lastHandledReconcileRequest', 'deletion', 'deletionComplete',
  'deletionBlockedBy', 'waitingFor', 'retry', 'references',
  'name', 'namespace', 'entity', 'billingId', 'description',
];

// the access groups that can use an environment on its site and their state, key by key
const ACCESS = ['access', ['signInClient', 'groupDomain', 'removing',
  ['groups', ['rbac', 'group', 'access', 'role', 'state', 'message']]]];
// how people and tools reach an environment, key by key
const ENDPOINTS = ['endpoints', ['console', ['signIn', ['method', 'idpName', 'start']], 'signInGroups', 'api', 'project',
  'projectId', 'domain', 'domainId', 'region', 'location',
  ['apiCredentials', ['state', 'validMinutesAfterSignIn', 'source']]]];
// an environment's zone: its name, how it is delegated and its name servers (not the platform's ids)
const ENV_DNS = ['dns', ['mode', 'zone', 'nameservers', 'delegated']];

export const TENANT_STATUS_ALLOW = {
  CloudOSO: [
    // the tenant's own project and domain on the site (the CLI and the dashboard ask for them)
    'cloudInfrastructure', 'projectId', 'projectName', 'domainId', 'domainName',
    // addresses the tenant reaches its environment on, the site's external network it chooses
    'apiVIP', 'ingressVIP', 'externalNetwork',
    'domain', 'dnsProvider', 'leftoverDefaultSecurityGroups',
    ENV_DNS,
    ['site', ['name', 'console']],
    ACCESS,
    ENDPOINTS,
  ],
  CloudVirt: [
    // the site's console and API endpoint the tenant signs in to, its namespaces there and their machines
    ['site', ['name', 'console', 'endpoint']],
    'storageClass',
    ['namespaces', ['name', 'namespace', 'network', 'ready', 'message']],
    'virtualMachines',
    ENV_DNS,
    ACCESS,
    ENDPOINTS,
  ],
  CloudAWS: [
    // the tenant's own account and region
    'awsAccountId', 'awsRegion', 'cloudInfrastructure', 'domain',
    ENV_DNS,
    ['site', ['name', 'console']],
    ACCESS,
    ENDPOINTS,
  ],
  CloudGCP: [
    // the tenant's own project and region
    'project', 'projectNumber', 'region', 'cloudInfrastructure', 'domain',
    ENV_DNS,
    ['site', ['name', 'console']],
    ACCESS,
    ENDPOINTS,
  ],
  PlatformOpenshift: [
    'platformType', 'domain', 'apiURL', 'appsURL', 'consoleURL', 'clusterVersion', 'powerState', 'clusterHealth',
    'provisionStatus', 'acmAvailable', 'clusterInstalled', 'installStarted',
    // the installer's name for the cluster's resources in the tenant's own account or project
    'buildName',
    'awsAccountId', 'awsRegion', 'gcpProject', 'gcpRegion', 'gcpVpc',
    // the cluster's networks and how it is published
    'network', 'placement', 'publish', 'machineNetwork', 'externalNetwork', 'clusterNetwork', 'serviceNetwork',
    'platformPrefixes',
    // the cluster's addresses (not the site's ids)
    ['floatingIPs', [['api', ['address']], ['ingress', ['address']]]],
    // the cluster's DNS records by name (not the platform's DNS project or zone ids)
    ['dns', ['provider', ['records', ['zone', 'name', 'type']]]],
    ['site', ['name', 'console']],
    // OpenStack clusters: the cluster's own user and credential on the site, ids and names only
    ['credential', ['id', 'user', 'userId', 'domain', 'domainId', 'createdAt']],
    // the installation: stage, attempts, times and the platform's sentence about the last failed attempt
    ['install', ['stage', 'attempt', 'attemptsLimit', 'run', 'startedAt', 'endedAt', 'observedAt',
      ['lastFailure', ['attempt', 'reason', 'message', 'time']]]],
  ],
  HybridNetwork: [
    // the fabric the tenant chose, the network's address space, egress, IPv6, platform access and size
    'fabric', 'egress', 'ipv6', 'platformAccess', 'mtu', 'placementCount', 'placementsReady', 'addressSpace',
  ],
  NetworkPlacement: [
    'site', 'mtu', 'prefixes', 'ipv6', 'use', 'namespaces', 'backendReady', 'gatewayReady', 'validated',
    'lastValidatedAt',
    ['subnets', ['family', 'prefix', 'gateway', 'source']],
    // the network on an OpenStack site the tenant chooses for a server: its name, id and subnets
    ['siteNetwork', ['name', 'id', ['subnets', ['family', 'prefix', 'gateway', 'id']]]],
    // on AWS and Google Cloud: the network in the tenant's own account or project
    ['realization', [['site', ['vpc', 'subnet', 'subnets', 'region', 'account']]]],
  ],
  AttachmentCircuit: [
    // what the tenant's router needs: the VLAN, the platform's AS number, the sessions and the agreed ranges
    'fabric', 'link', 'vlan', 'peAsn', 'sessions', 'ceConfig', 'notes',
  ],
  VaultKV: [
    'kvPath', 'loginUrl', 'vaultUrl', 'adminRbacGroups', 'readerRbacGroups', 'opsRbacGroups', 'developerRbacGroups',
  ],
  Vault: ['vaultUrl', 'loginUrl'],
  QuayOrg: ['orgName', 'quayUrl', 'orgUrl', 'loginUrl', 'adminGroups', 'creatorGroups', 'memberGroups', 'teams'],
  AAPOrg: ['orgName', 'aapControllerUrl', 'loginUrl', 'orgUrl', 'adminGroups', 'executorGroups', 'viewerGroups',
    'teamMappings'],
  Project: [],
  Team: ['features'],
  // the delete dialog names the cluster and the policy that put the assignment there
  Assignment: ['team', 'teamReady', 'projects', 'openshift', 'openshiftReady', 'assignmentProvisioned', 'clusterName',
    'acmPolicyName'],
  Rbac: ['group', 'creator'],
  Persona: ['keycloakGroup'],
  OpenStackMigration: ['source', 'vmName', 'cloudoso', 'targetProject', 'migrationMethod', 'phases'],
  TenantProfile: ['displayName', 'contacts', 'sites', 'ipv6Platform', 'reservedRanges', 'managedClusterRanges',
    'observedEntityGeneration', 'lastUpdatedTime'],
};

/** Key names never sent to tenants, at any depth of the status. */
export const SECRET_KEY_RE = /VaultPath|VaultProperty|vaultPaths|Secret$/;

function isPlainObject(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v);
}

/** Copy without keys that look like Vault paths or secret references, at any depth. */
export function scrubSecrets(value) {
  if (Array.isArray(value)) return value.map(scrubSecrets);
  if (!isPlainObject(value)) return value;
  const out = {};
  for (const [k, v] of Object.entries(value)) {
    if (SECRET_KEY_RE.test(k)) continue;
    out[k] = scrubSecrets(v);
  }
  return out;
}

/** The listed subkeys of an object (or of each object of a list), nested entries alike; undefined otherwise. */
function pick(sub, v) {
  if (Array.isArray(v)) return v.filter(isPlainObject).map((item) => pick(sub, item));
  if (!isPlainObject(v)) return undefined;
  const out = {};
  for (const entry of sub) {
    const [key, inner] = Array.isArray(entry) ? entry : [entry, null];
    if (!(key in v) || SECRET_KEY_RE.test(key)) continue;
    if (!inner) {
      out[key] = scrubSecrets(v[key]);
      continue;
    }
    const p = pick(inner, v[key]);
    if (p !== undefined) out[key] = p;
  }
  return out;
}

/** True for a kind with a tenant allow-list. */
export function hasTenantStatusAllow(kind) {
  return Object.prototype.hasOwnProperty.call(TENANT_STATUS_ALLOW, kind);
}

/** The status a tenant page shows for `kind` (a kind without a list: only the secret-looking keys go). */
export function filterTenantStatus(kind, status) {
  if (!isPlainObject(status)) return status;
  if (!hasTenantStatusAllow(kind)) return scrubSecrets(status);
  const out = {};
  const allow = new Map();
  for (const k of COMMON_STATUS_KEYS) allow.set(k, null);
  for (const entry of TENANT_STATUS_ALLOW[kind]) {
    if (Array.isArray(entry)) allow.set(entry[0], entry[1]);
    else allow.set(entry, null);
  }
  for (const [k, v] of Object.entries(status)) {
    if (!allow.has(k) || SECRET_KEY_RE.test(k)) continue;
    const sub = allow.get(k);
    if (!sub) {
      out[k] = scrubSecrets(v);
      continue;
    }
    // a key with a subkey list that is not an object (or a list holding none) carries nothing tenants need
    if (Array.isArray(v) ? !v.some(isPlainObject) : !isPlainObject(v)) continue;
    const p = pick(sub, v);
    if (p !== undefined) out[k] = p;
  }
  return out;
}
