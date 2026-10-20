import https from "https";
import fs from "fs";

const SA_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt";

let cachedCA = undefined;
let caLoaded = false;

function getCACert() {
  if (caLoaded) return cachedCA;
  try {
    cachedCA = fs.readFileSync(SA_CA_PATH);
  } catch {
    cachedCA = undefined;
  }
  caLoaded = true;
  return cachedCA;
}

function userAccessToken(req) {
  const raw = req.headers["x-forwarded-access-token"];
  if (typeof raw !== "string" || !raw.trim()) return null;
  return raw.trim();
}

function k8sRequest(apiServer, reqPath, method, bearerToken, body, contentType) {
  return new Promise((resolve, reject) => {
    const url = new URL(reqPath, apiServer);
    const opts = {
      hostname: url.hostname,
      port: url.port || 443,
      path: url.pathname + url.search,
      method,
      headers: {
        Authorization: `Bearer ${bearerToken}`,
        Accept: "application/json",
        "Content-Type": contentType || "application/json",
      },
      rejectUnauthorized: !!getCACert(),
      ca: getCACert(),
      timeout: 15000,
    };
    const req = https.request(opts, (res) => {
      let data = "";
      res.on("data", (c) => (data += c));
      res.on("end", () => {
        try {
          resolve({ status: res.statusCode, body: JSON.parse(data) });
        } catch {
          resolve({ status: res.statusCode, body: data });
        }
      });
    });
    req.on("timeout", () => {
      req.destroy();
      reject(new Error("K8s API request timed out"));
    });
    req.on("error", reject);
    if (body) req.write(JSON.stringify(body));
    req.end();
  });
}

const NAME_PATTERN = /^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$/;
// Team RBAC lists (teamAdmin, teamMemberRbac, teamViewerRbac): Rbac CR names in the Team's namespace.
const TEAM_RBAC_KEYS = ["teamAdmin", "teamMemberRbac", "teamViewerRbac"];
function teamRbacList(v) {
  return Array.isArray(v) ? [...new Set(v.filter((x) => typeof x === "string" && NAME_PATTERN.test(x)))] : undefined;
}
const NS_PATTERN = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/;
const CONFIG_PATTERN = /^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$/;

const ENTITY_NS_LABEL_SELECTOR = "hybridsovereign.redhat/entity";

const PLUGINS_NS = "sovereign-cloud-plugins";
const RECONCILE_ANNOTATION = "ansible.sdk.operatorframework.io/reconcileNow";
/**
 * Spec sanitizers. Create and update endpoints accept exactly the fields of this platform's CRDs
 * and pass them through; unknown fields are dropped, wrong types are refused, and nothing the CRD
 * does not require is required here (the API server applies the CRD defaults and rules).
 * Each returns { spec } or { error }.
 */
function isObject(v) {
  return v != null && typeof v === "object" && !Array.isArray(v);
}

/** Non-empty trimmed strings of the listed keys. */
function pickStrings(src, keys) {
  const out = {};
  for (const k of keys) {
    const v = src?.[k];
    if (typeof v === "string" && v.trim()) out[k] = v.trim();
  }
  return out;
}

/** List of non-empty trimmed strings, or undefined when absent/empty; false when not a string list. */
function stringList(v) {
  if (v == null) return undefined;
  if (!Array.isArray(v) || !v.every((x) => typeof x === "string")) return false;
  const out = v.map((x) => x.trim()).filter(Boolean);
  return out.length ? out : undefined;
}

/** Integer in [min, max], undefined when absent, false when invalid. */
function intIn(v, min, max) {
  if (v == null || v === "") return undefined;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isInteger(n) && n >= min && n <= max ? n : false;
}

/** `{ name }` reference: null when absent, false when invalid. */
function sanitizeNameRef(raw) {
  if (raw == null) return null;
  if (!isObject(raw) || typeof raw.name !== "string") return false;
  const name = raw.name.trim();
  if (!name || name.length > 253 || !NAME_PATTERN.test(name)) return false;
  return { name };
}

/** spec.cloudRef of a tenant cloud project: a CloudInfrastructure in the platform namespace. */
function sanitizeCloudRef(raw) {
  return sanitizeNameRef(raw);
}

function sanitizePlatformOpenshiftSpec(raw) {
  if (!isObject(raw)) return { error: "spec must be an object" };
  const spec = {};
  if (raw.type != null) {
    if (raw.type !== "openstack" && raw.type !== "aws" && raw.type !== "gcp") return { error: "spec.type must be openstack, aws or gcp" };
    spec.type = raw.type;
  }
  if (raw.openstack != null) {
    if (!isObject(raw.openstack)) return { error: "spec.openstack must be an object" };
    const os = pickStrings(raw.openstack, [
      "environment",
      "controlPlaneFlavor",
      "workerFlavor",
      "externalNetwork",
      "ocpImageSet",
      "pullSecretVaultPath",
      "pullSecretVaultProperty",
      "sshKeyVaultPath",
      "glanceImageOverride",
      "trustBundle",
    ]);
    const cp = intIn(raw.openstack.controlPlaneCount, 1, 1000);
    const workers = intIn(raw.openstack.workerCount, 0, 1000);
    if (cp === false || workers === false) return { error: "spec.openstack node counts must be integers" };
    if (cp !== undefined) os.controlPlaneCount = cp;
    if (workers !== undefined) os.workerCount = workers;
    spec.openstack = os;
  }
  if (raw.aws != null) {
    if (!isObject(raw.aws)) return { error: "spec.aws must be an object" };
    const aws = pickStrings(raw.aws, ["environment", "region", "controllerFlavor", "workerFlavor"]);
    if (!aws.environment) return { error: "spec.aws.environment is required" };
    if (raw.aws.clusterType != null) {
      if (raw.aws.clusterType !== "standalone" && raw.aws.clusterType !== "ha") {
        return { error: "spec.aws.clusterType must be standalone or ha" };
      }
      aws.clusterType = raw.aws.clusterType;
    }
    const cp = intIn(raw.aws.controlPlaneCount, 0, 1000);
    const workers = intIn(raw.aws.workerCount, 0, 1000);
    if (cp === false || workers === false) return { error: "spec.aws node counts must be integers" };
    if (cp !== undefined) aws.controlPlaneCount = cp;
    if (workers !== undefined) aws.workerCount = workers;
    spec.aws = aws;
  }
  if (raw.gcp != null) {
    if (!isObject(raw.gcp)) return { error: "spec.gcp must be an object" };
    const gcp = pickStrings(raw.gcp, ["environment", "region", "controllerFlavor", "workerFlavor", "network"]);
    if (!gcp.environment) return { error: "spec.gcp.environment is required" };
    if (raw.gcp.publish != null) {
      if (raw.gcp.publish !== "External" && raw.gcp.publish !== "Internal") return { error: "spec.gcp.publish must be External or Internal" };
      gcp.publish = raw.gcp.publish;
    }
    const cp = intIn(raw.gcp.controlPlaneCount, 0, 1000);
    const workers = intIn(raw.gcp.workerCount, 0, 1000);
    if (cp === false || workers === false) return { error: "spec.gcp node counts must be integers" };
    if (cp !== undefined) gcp.controlPlaneCount = cp;
    if (workers !== undefined) gcp.workerCount = workers;
    spec.gcp = gcp;
  }
  if (raw.toolRbac != null) {
    if (!isObject(raw.toolRbac)) return { error: "spec.toolRbac must be an object" };
    const toolRbac = {};
    for (const key of ["clusterAdminRbac", "clusterOperatorRbac", "clusterDeveloperRbac", "clusterViewerRbac"]) {
      const list = stringList(raw.toolRbac[key]);
      if (list === false) return { error: `spec.toolRbac.${key} must be a list of Rbac names` };
      if (list) toolRbac[key] = list;
    }
    if (Object.keys(toolRbac).length) spec.toolRbac = toolRbac;
  }
  const viewers = stringList(raw.clusterViewerRbac);
  if (viewers === false) return { error: "spec.clusterViewerRbac must be a list of Rbac names" };
  if (viewers) spec.clusterViewerRbac = viewers;
  return { spec };
}

function sanitizeCloudOSOSpec(raw) {
  if (!isObject(raw)) return { error: "spec must be an object" };
  const spec = pickStrings(raw, [
    "project",
    "vaultPath",
    "baseDomain",
    "projectDomain",
    "externalNetwork",
    "regionName",
    "route53VaultPath",
    "landingzone",
    "designateZoneId",
    "designateProjectId",
    "vrfId",
  ]);
  const cloudRef = sanitizeCloudRef(raw.cloudRef);
  if (cloudRef === false) return { error: "spec.cloudRef.name is invalid" };
  if (cloudRef) spec.cloudRef = cloudRef;
  if (raw.dnsProvider != null && raw.dnsProvider !== "") {
    if (!["designate", "route53", "none"].includes(raw.dnsProvider)) {
      return { error: "spec.dnsProvider must be designate, route53 or none" };
    }
    spec.dnsProvider = raw.dnsProvider;
  }
  if (raw.enableVRF != null) {
    if (typeof raw.enableVRF !== "boolean") return { error: "spec.enableVRF must be a boolean" };
    spec.enableVRF = raw.enableVRF;
  }
  return { spec };
}

function sanitizeCloudAWSSpec(raw) {
  if (!isObject(raw)) return { error: "spec must be an object" };
  const spec = pickStrings(raw, ["account", "vaultPath", "baseDomain", "landingzone"]);
  if (!spec.account || !spec.vaultPath || !spec.baseDomain) {
    return { error: "spec.account, spec.vaultPath and spec.baseDomain are required" };
  }
  return { spec };
}

function sanitizeCloudGCPSpec(raw) {
  if (!isObject(raw)) return { error: "spec must be an object" };
  const spec = {};
  const cloudRef = sanitizeCloudRef(raw.cloudRef);
  if (!cloudRef) return { error: "spec.cloudRef.name (a CloudInfrastructure of type gcp) is required" };
  spec.cloudRef = cloudRef;
  return { spec };
}

const DNS_LABEL = /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/;

function sanitizeCloudVirtSpec(raw) {
  if (!isObject(raw)) return { error: "spec must be an object" };
  const spec = pickStrings(raw, ["description", "storageClass"]);
  const cloudRef = sanitizeCloudRef(raw.cloudRef);
  if (!cloudRef) return { error: "spec.cloudRef.name (a CloudInfrastructure of type openshift) is required" };
  spec.cloudRef = cloudRef;
  if (raw.namespaces != null) {
    if (!Array.isArray(raw.namespaces)) return { error: "spec.namespaces must be a list" };
    const namespaces = [];
    for (const n of raw.namespaces) {
      if (!isObject(n) || typeof n.name !== "string" || !DNS_LABEL.test(n.name.trim()) || n.name.trim().length > 30) {
        return { error: "each spec.namespaces[].name must be a DNS label of at most 30 characters" };
      }
      const item = { name: n.name.trim() };
      if (n.network != null && n.network !== "") {
        if (typeof n.network !== "string" || !NAME_PATTERN.test(n.network.trim())) {
          return { error: "spec.namespaces[].network must be a HybridNetwork name" };
        }
        item.network = n.network.trim();
      }
      namespaces.push(item);
    }
    spec.namespaces = namespaces;
  }
  if (raw.quota != null) {
    if (!isObject(raw.quota)) return { error: "spec.quota must be an object" };
    const quota = {};
    for (const key of ["cpu", "memory", "storage"]) {
      const v = raw.quota[key];
      if (v == null || v === "") continue;
      if (typeof v === "number" && Number.isFinite(v)) quota[key] = v;
      else if (typeof v === "string" && v.trim()) quota[key] = v.trim();
      else return { error: `spec.quota.${key} must be a quantity` };
    }
    const vms = intIn(raw.quota.virtualMachines, 0, Number.MAX_SAFE_INTEGER);
    if (vms === false) return { error: "spec.quota.virtualMachines must be an integer of at least 0" };
    if (vms !== undefined) quota.virtualMachines = vms;
    if (Object.keys(quota).length) spec.quota = quota;
  }
  if (raw.toolRbac != null) {
    if (!isObject(raw.toolRbac)) return { error: "spec.toolRbac must be an object" };
    const toolRbac = {};
    for (const key of ["editRbac", "viewRbac"]) {
      const list = stringList(raw.toolRbac[key]);
      if (list === false) return { error: `spec.toolRbac.${key} must be a list of Rbac names` };
      if (list) toolRbac[key] = list;
    }
    if (Object.keys(toolRbac).length) spec.toolRbac = toolRbac;
  }
  return { spec };
}

function sanitizeHybridNetworkSpec(raw) {
  if (!isObject(raw)) return { error: "spec must be an object" };
  const spec = pickStrings(raw, ["description"]);
  // the address space; the API checks the prefixes themselves
  const prefixes = stringList(raw.prefixes);
  if (!prefixes) return { error: "spec.prefixes must be a non-empty list of IPv4/IPv6 prefixes" };
  spec.prefixes = prefixes;
  const fabricRef = sanitizeNameRef(raw.fabricRef);
  if (fabricRef === false) return { error: "spec.fabricRef.name is invalid" };
  if (fabricRef) spec.fabricRef = fabricRef;
  const mtu = intIn(raw.mtu, 1280, 9216);
  if (mtu === false) return { error: "spec.mtu must be an integer from 1280 to 9216" };
  if (mtu !== undefined) spec.mtu = mtu;
  const viewers = stringList(raw.networkViewerRbac);
  if (viewers === false) return { error: "spec.networkViewerRbac must be a list of Rbac names" };
  if (viewers) spec.networkViewerRbac = viewers;
  if (raw.egress != null) {
    if (!isObject(raw.egress)) return { error: "spec.egress must be an object" };
    if (raw.egress.internet != null) {
      if (typeof raw.egress.internet !== "boolean") return { error: "spec.egress.internet must be a boolean" };
      spec.egress = { internet: raw.egress.internet };
    }
  }
  return { spec };
}

function sanitizeNetworkPlacementSpec(raw) {
  if (!isObject(raw)) return { error: "spec must be an object" };
  const network = typeof raw.network === "string" ? raw.network.trim() : "";
  if (!network || !NAME_PATTERN.test(network)) return { error: "spec.network (a HybridNetwork name) is required" };
  const backend = raw.backend;
  if (!isObject(backend) || (backend.kind !== "CloudOSO" && backend.kind !== "CloudVirt")) {
    return { error: "spec.backend.kind must be CloudOSO or CloudVirt" };
  }
  const backendName = typeof backend.name === "string" ? backend.name.trim() : "";
  if (!backendName || !NAME_PATTERN.test(backendName)) return { error: "spec.backend.name is required" };
  const spec = { network, backend: { kind: backend.kind, name: backendName } };
  // a placement's subnet per family: a size and/or a pinned prefix (the API checks both)
  for (const family of ["ipv4", "ipv6"]) {
    const req = raw[family];
    if (req == null) continue;
    if (!isObject(req)) return { error: `spec.${family} must be an object` };
    const out = {};
    const length = intIn(req.prefixLength, 0, 128);
    if (length === false) return { error: `spec.${family}.prefixLength must be an integer` };
    if (length !== undefined) out.prefixLength = length;
    if (req.prefix != null) {
      if (typeof req.prefix !== "string" || !req.prefix.trim()) return { error: `spec.${family}.prefix must be a prefix` };
      out.prefix = req.prefix.trim();
    }
    if (!Object.keys(out).length) return { error: `spec.${family} needs prefixLength or prefix` };
    spec[family] = out;
  }
  if (!spec.ipv4) return { error: "spec.ipv4 (a prefixLength or a pinned prefix) is required" };
  return { spec };
}

const KIND_PLURALS = {
  Entity: "entities",
  Team: "teams",
  Assignment: "assignments",
  Project: "projects",
  PlatformOpenshift: "platformopenshifts",
  CloudOSO: "cloudosos",
  CloudAWS: "cloudawss",
  CloudGCP: "cloudgcps",
  CloudVirt: "cloudvirts",
  OpenStackMigration: "openstackmigrations",
  Rbac: "rbacs",
  RbacConfig: "rbacconfigs",
  Vault: "vaults",
  VaultKV: "vaultkvs",
  AAPConfig: "aapconfigs",
  AAPOrg: "aaporgs",
  QuayConfig: "quayconfigs",
  QuayOrg: "quayorgs",
  Iaac: "iaacs",
  Persona: "personas",
  HybridNetwork: "hybridnetworks",
  NetworkPlacement: "networkplacements",
};

const PERSONA_TYPES = new Set([
  "assignmentAdmin",
  "auditor",
  "BobsTeam",
  "cloudOSOAdmin",
  "cloudOSOView",
  "cloudAWSAdmin",
  "cloudAWSView",
  "entityAdmin",
  "identityAdmin",
  "platformOpenshiftAdmin",
  "platformOpenshiftView",
  "teamAdmin",
  "teamView",
  "projectAdmin",
  "projectView",
]);
const RBAC_CONFIGS_PATH = `/apis/hybridsovereign.redhat/v1alpha1/namespaces/${PLUGINS_NS}/rbacconfigs`;
const AAP_CONFIGS_PATH = `/apis/hybridsovereign.redhat/v1alpha1/namespaces/${PLUGINS_NS}/aapconfigs`;
const QUAY_CONFIGS_PATH = `/apis/hybridsovereign.redhat/v1alpha1/namespaces/${PLUGINS_NS}/quayconfigs`;

export function createK8sHandlers(apiServer) {
  function rbacsCollectionPath(namespace) {
    return `/apis/hybridsovereign.redhat/v1alpha1/namespaces/${encodeURIComponent(namespace)}/rbacs`;
  }
  function vaultsCollectionPath(namespace) {
    return `/apis/hybridsovereign.redhat/v1alpha1/namespaces/${encodeURIComponent(namespace)}/vaults`;
  }
  function vaultKVsCollectionPath(namespace) {
    return `/apis/hybridsovereign.redhat/v1alpha1/namespaces/${encodeURIComponent(namespace)}/vaultkvs`;
  }
  function aaporgsCollectionPath(namespace) {
    return `/apis/hybridsovereign.redhat/v1alpha1/namespaces/${encodeURIComponent(namespace)}/aaporgs`;
  }
  function quayorgsCollectionPath(namespace) {
    return `/apis/hybridsovereign.redhat/v1alpha1/namespaces/${encodeURIComponent(namespace)}/quayorgs`;
  }
  function teamsCollectionPath(namespace) {
    return `/apis/hybridsovereign.redhat/v1alpha1/namespaces/${encodeURIComponent(namespace)}/teams`;
  }
  function assignmentsCollectionPath(namespace) {
    return `/apis/hybridsovereign.redhat/v1alpha1/namespaces/${encodeURIComponent(namespace)}/assignments`;
  }
  function projectsCollectionPath(namespace) {
    return `/apis/hybridsovereign.redhat/v1alpha1/namespaces/${encodeURIComponent(namespace)}/projects`;
  }
  function platformopenshiftsCollectionPath(namespace) {
    return `/apis/hybridsovereign.redhat/v1alpha1/namespaces/${encodeURIComponent(namespace)}/platformopenshifts`;
  }
  function cloudososCollectionPath(namespace) {
    return `/apis/hybridsovereign.redhat/v1alpha1/namespaces/${encodeURIComponent(namespace)}/cloudosos`;
  }
  function openstackmigrationsCollectionPath(namespace) {
    return `/apis/hybridsovereign.redhat/v1alpha1/namespaces/${encodeURIComponent(namespace)}/openstackmigrations`;
  }
  const MTV_CATALOG_PATH = "/api/v1/namespaces/sovereign-cloud/configmaps/mtv-migration-catalog";
  function cloudawssCollectionPath(namespace) {
    return `/apis/hybridsovereign.redhat/v1alpha1/namespaces/${encodeURIComponent(namespace)}/cloudawss`;
  }

  function cloudgcpsCollectionPath(namespace) {
    return `/apis/hybridsovereign.redhat/v1alpha1/namespaces/${encodeURIComponent(namespace)}/cloudgcps`;
  }

  function cloudvirtsCollectionPath(namespace) {
    return `/apis/hybridsovereign.redhat/v1alpha1/namespaces/${encodeURIComponent(namespace)}/cloudvirts`;
  }

  function hybridnetworksCollectionPath(namespace) {
    return `/apis/hybridsovereign.redhat/v1alpha1/namespaces/${encodeURIComponent(namespace)}/hybridnetworks`;
  }

  function networkplacementsCollectionPath(namespace) {
    return `/apis/hybridsovereign.redhat/v1alpha1/namespaces/${encodeURIComponent(namespace)}/networkplacements`;
  }
  function personasCollectionPath(namespace) {
    return `/apis/hybridsovereign.redhat/v1alpha1/namespaces/${encodeURIComponent(namespace)}/personas`;
  }

  function validateRbacNameList(arr, fieldLabel) {
    if (arr == null) return { ok: true, value: [] };
    if (!Array.isArray(arr)) {
      return { ok: false, message: `${fieldLabel} must be an array of Rbac names` };
    }
    const out = [];
    for (const item of arr) {
      if (typeof item !== "string") {
        return { ok: false, message: `Invalid Rbac name in ${fieldLabel}` };
      }
      const t = item.trim();
      if (!NAME_PATTERN.test(t)) {
        return { ok: false, message: `Invalid Rbac name in ${fieldLabel}` };
      }
      out.push(t);
    }
    return { ok: true, value: out };
  }

  const ENTITIES_PATH = "/apis/hybridsovereign.redhat/v1alpha1/entities";

  return {
    async listEntities(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      try {
        const resp = await k8sRequest(apiServer, ENTITIES_PATH, "GET", token);
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) {
          return res.status(502).json({ message: "Failed to list entities from cluster" });
        }
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("Entity list error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async listNamespaces(req, res) {
      const token = userAccessToken(req);
      if (!token) {
        console.error("listNamespaces: no X-Forwarded-Access-Token header found");
        return res.status(401).json({ message: "Unauthorized - no access token" });
      }
      const path =
        `/api/v1/namespaces?labelSelector=${encodeURIComponent(ENTITY_NS_LABEL_SELECTOR)}`;
      try {
        const resp = await k8sRequest(apiServer, path, "GET", token);
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) {
          console.error("listNamespaces: K8s API returned", resp.status, JSON.stringify(resp.body).slice(0, 200));
          return res.status(502).json({ message: "Failed to list namespaces from cluster" });
        }
        const items = Array.isArray(resp.body?.items) ? resp.body.items : [];
        const names = items
          .map((item) => item?.metadata?.name)
          .filter((n) => typeof n === "string" && n.length > 0);
        res.json(names.map((name) => ({ name })));
      } catch (err) {
        console.error("Namespace list error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async listRbacConfigs(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      try {
        const resp = await k8sRequest(apiServer, RBAC_CONFIGS_PATH, "GET", token);
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) {
          return res.status(502).json({ message: "Failed to list RbacConfigs" });
        }
        const items = Array.isArray(resp.body?.items) ? resp.body.items : [];
        const configs = items
          .filter((i) => i.status?.ready)
          .map((i) => ({
            name: i.metadata.name,
            type: i.spec?.type || "unknown",
            ready: i.status?.ready || false,
          }));
        res.json(configs);
      } catch (err) {
        console.error("RbacConfig list error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async listRbacs(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const namespace = req.query.namespace;
      if (!namespace || typeof namespace !== "string" || !NS_PATTERN.test(namespace)) {
        return res.status(400).json({
          message: "namespace query parameter is required and must be a valid namespace name",
        });
      }
      try {
        const resp = await k8sRequest(apiServer, rbacsCollectionPath(namespace), "GET", token);
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) {
          return res.status(502).json({ message: "Failed to list Rbac resources from cluster" });
        }
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("Rbac list error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async searchRbacs(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const namespace = req.query.namespace;
      const q = (req.query.q || "").toString().trim().toLowerCase();
      if (!namespace || typeof namespace !== "string" || !NS_PATTERN.test(namespace)) {
        return res.status(400).json({ message: "namespace query parameter required" });
      }
      if (!q) return res.json([]);
      try {
        const path = `${rbacsCollectionPath(namespace)}?limit=100`;
        const resp = await k8sRequest(apiServer, path, "GET", token);
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) {
          return res.status(502).json({ message: "Failed to search Rbac resources" });
        }
        const items = Array.isArray(resp.body?.items) ? resp.body.items : [];
        const names = items
          .map((i) => i.metadata?.name)
          .filter((n) => typeof n === "string" && n.toLowerCase().includes(q));
        res.json(names.slice(0, 15));
      } catch (err) {
        console.error("Rbac search error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async get(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) {
        return res.status(400).json({ message: "Invalid Rbac name" });
      }
      if (!namespace || typeof namespace !== "string" || !NS_PATTERN.test(namespace)) {
        return res.status(400).json({
          message: "namespace query parameter is required and must be a valid namespace name",
        });
      }
      try {
        const urlPath = `${rbacsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "GET", token);
        if (resp.status === 404) return res.status(404).json({ message: `Rbac '${name}' not found` });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) {
          return res.status(502).json({ message: "Failed to fetch Rbac from cluster" });
        }
        res.json(resp.body);
      } catch (err) {
        console.error("Rbac get error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async create(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name, namespace, spec } = req.body || {};
      if (!name || !namespace || !spec) {
        return res.status(400).json({ message: "name, namespace, and spec are required" });
      }
      if (!NAME_PATTERN.test(name)) {
        return res.status(400).json({ message: "Name must be lowercase alphanumeric with hyphens" });
      }
      if (!NS_PATTERN.test(namespace)) {
        return res.status(400).json({ message: "namespace must be a valid namespace name" });
      }
      const config = spec.config;
      if (typeof config !== "string" || !config.trim()) {
        return res.status(400).json({ message: "spec.config is required" });
      }
      const trimmedConfig = config.trim();
      if (!CONFIG_PATTERN.test(trimmedConfig)) {
        return res.status(400).json({
          message: "spec.config must be a valid RbacConfig resource name (lowercase, hyphens)",
        });
      }
      const rbacSpec = { config: trimmedConfig };
      if (typeof spec.description === "string" && spec.description.trim()) {
        rbacSpec.description = spec.description.trim();
      }
      const rbac = {
        apiVersion: "hybridsovereign.redhat/v1alpha1",
        kind: "Rbac",
        metadata: {
          name,
          namespace,
          annotations: {
            "hybridsovereign.redhat/creator": req.headers["x-forwarded-user"] || "unknown",
          },
        },
        spec: rbacSpec,
      };
      try {
        const resp = await k8sRequest(apiServer, rbacsCollectionPath(namespace), "POST", token, rbac);
        if (resp.status === 409) return res.status(409).json({ message: `Rbac '${name}' already exists` });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) {
          return res.status(502).json({
            message: typeof resp.body?.message === "string" ? resp.body.message : "Failed to create Rbac",
          });
        }
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("Rbac create error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async remove(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) {
        return res.status(400).json({ message: "Invalid Rbac name" });
      }
      if (!namespace || typeof namespace !== "string" || !NS_PATTERN.test(namespace)) {
        return res.status(400).json({
          message: "namespace query parameter is required and must be a valid namespace name",
        });
      }
      try {
        const urlPath = `${rbacsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "DELETE", token);
        if (resp.status === 404) return res.status(404).json({ message: `Rbac '${name}' not found` });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) {
          return res.status(502).json({ message: "Failed to delete Rbac" });
        }
        res.json({ message: `Rbac '${name}' deleted` });
      } catch (err) {
        console.error("Rbac delete error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async listVaults(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const namespace = req.query.namespace;
      if (!namespace || !NS_PATTERN.test(namespace)) {
        return res.status(400).json({ message: "namespace query parameter required" });
      }
      try {
        const resp = await k8sRequest(apiServer, vaultsCollectionPath(namespace), "GET", token);
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to list Vaults" });
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("Vault list error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async listTeams(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const namespace = req.query.namespace;
      if (!namespace || !NS_PATTERN.test(namespace)) {
        return res.status(400).json({ message: "namespace query parameter required" });
      }
      try {
        const resp = await k8sRequest(apiServer, teamsCollectionPath(namespace), "GET", token);
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to list Teams" });
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("Team list error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async listAssignments(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const namespace = req.query.namespace;
      if (!namespace || !NS_PATTERN.test(namespace)) {
        return res.status(400).json({ message: "namespace query parameter required" });
      }
      try {
        const resp = await k8sRequest(apiServer, assignmentsCollectionPath(namespace), "GET", token);
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to list Assignments" });
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("Assignment list error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async getAssignment(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${assignmentsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "GET", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to fetch Assignment" });
        res.json(resp.body);
      } catch (err) {
        console.error("Assignment get error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async listProjects(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const namespace = req.query.namespace;
      if (!namespace || !NS_PATTERN.test(namespace)) {
        return res.status(400).json({ message: "namespace query parameter required" });
      }
      try {
        const resp = await k8sRequest(apiServer, projectsCollectionPath(namespace), "GET", token);
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to list Projects" });
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("Project list error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async listPlatformOpenshifts(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const namespace = req.query.namespace;
      if (!namespace || !NS_PATTERN.test(namespace)) {
        return res.status(400).json({ message: "namespace query parameter required" });
      }
      try {
        const resp = await k8sRequest(apiServer, platformopenshiftsCollectionPath(namespace), "GET", token);
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) {
          return res.status(502).json({ message: "Failed to list PlatformOpenshifts" });
        }
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("PlatformOpenshift list error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async listCloudOSOs(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const namespace = req.query.namespace;
      if (!namespace || !NS_PATTERN.test(namespace)) {
        return res.status(400).json({ message: "namespace query parameter required" });
      }
      try {
        const resp = await k8sRequest(apiServer, cloudososCollectionPath(namespace), "GET", token);
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to list CloudOSOs" });
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("CloudOSO list error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async createTeam(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name, namespace, spec } = req.body || {};
      if (!name || !namespace) return res.status(400).json({ message: "name and namespace required" });
      if (!NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      const team = {
        apiVersion: "hybridsovereign.redhat/v1alpha1",
        kind: "Team",
        metadata: { name, namespace },
        spec: {
          features: {
            istio: spec?.features?.istio === true,
            argo: spec?.features?.argo === true,
          },
          ...(spec?.rbacConfig ? { rbacConfig: spec.rbacConfig } : {}),
          ...Object.fromEntries(
            TEAM_RBAC_KEYS.map((k) => [k, teamRbacList(spec?.[k])]).filter(([, l]) => l && l.length),
          ),
        },
      };
      try {
        const resp = await k8sRequest(apiServer, teamsCollectionPath(namespace), "POST", token, team);
        if (resp.status === 409) return res.status(409).json({ message: `Team '${name}' already exists` });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: resp.body?.message || "Failed to create Team" });
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("Team create error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async deleteTeam(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${teamsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "DELETE", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to delete Team" });
        res.json({ message: `Team '${name}' deleted` });
      } catch (err) {
        console.error("Team delete error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async createProject(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name, namespace, spec } = req.body || {};
      if (!name || !namespace) return res.status(400).json({ message: "name and namespace required" });
      if (!NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      const project = {
        apiVersion: "hybridsovereign.redhat/v1alpha1",
        kind: "Project",
        metadata: { name, namespace },
        spec: {
          ...(spec?.description ? { description: spec.description } : {}),
          ...(spec?.displayName ? { displayName: spec.displayName } : {}),
          ...(spec?.rbacConfig ? { rbacConfig: spec.rbacConfig } : {}),
          ...(Array.isArray(spec?.projectAdmin) && spec.projectAdmin.length ? { projectAdmin: spec.projectAdmin } : {}),
        },
      };
      try {
        const resp = await k8sRequest(apiServer, projectsCollectionPath(namespace), "POST", token, project);
        if (resp.status === 409) return res.status(409).json({ message: `Project '${name}' already exists` });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: resp.body?.message || "Failed to create Project" });
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("Project create error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async deleteProject(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${projectsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "DELETE", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to delete Project" });
        res.json({ message: `Project '${name}' deleted` });
      } catch (err) {
        console.error("Project delete error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async getProject(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${projectsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "GET", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to get Project" });
        res.json(resp.body);
      } catch (err) {
        console.error("Project get error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async patchProject(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      const { spec } = req.body || {};
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      if (!spec) return res.status(400).json({ message: "spec is required" });
      try {
        const urlPath = `${projectsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "PATCH", token, { spec }, "application/merge-patch+json");
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: resp.body?.message || "Failed to patch Project" });
        res.json(resp.body);
      } catch (err) {
        console.error("Project patch error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async createAssignment(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name, namespace, spec } = req.body || {};
      if (!name || !namespace) return res.status(400).json({ message: "name and namespace required" });
      if (!NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      const assignment = {
        apiVersion: "hybridsovereign.redhat/v1alpha1",
        kind: "Assignment",
        metadata: { name, namespace },
        spec: {
          ...(spec?.team ? { team: spec.team } : {}),
          ...(Array.isArray(spec?.projects) && spec.projects.length ? { projects: spec.projects } : {}),
          ...(typeof spec?.openshift === "string" && spec.openshift.trim() ? { openshift: spec.openshift.trim() } : {}),
          ...(spec?.toolRbac && typeof spec.toolRbac === "object" ? { toolRbac: spec.toolRbac } : {}),
        },
      };
      try {
        const resp = await k8sRequest(apiServer, assignmentsCollectionPath(namespace), "POST", token, assignment);
        if (resp.status === 409) return res.status(409).json({ message: `Assignment '${name}' already exists` });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: resp.body?.message || "Failed to create Assignment" });
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("Assignment create error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async deleteAssignment(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${assignmentsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "DELETE", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to delete Assignment" });
        res.json({ message: `Assignment '${name}' deleted` });
      } catch (err) {
        console.error("Assignment delete error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async updateAssignment(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      const { spec } = req.body || {};
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const getPath = `${assignmentsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const getCurrent = await k8sRequest(apiServer, getPath, "GET", token);
        if (getCurrent.status === 404) return res.status(404).json({ message: "Not found" });
        if (getCurrent.status >= 400) return res.status(502).json({ message: "Failed to fetch Assignment" });

        const updated = {
          apiVersion: "hybridsovereign.redhat/v1alpha1",
          kind: "Assignment",
          metadata: {
            name,
            namespace,
            resourceVersion: getCurrent.body.metadata.resourceVersion,
          },
          spec: {
            ...(spec?.team ? { team: spec.team } : {}),
            ...(Array.isArray(spec?.projects) && spec.projects.length ? { projects: spec.projects } : {}),
            ...(typeof spec?.openshift === "string" && spec.openshift.trim() ? { openshift: spec.openshift.trim() } : {}),
          },
        };
        const resp = await k8sRequest(apiServer, getPath, "PUT", token, updated);
        if (resp.status === 409) return res.status(409).json({ message: "Conflict - resource was modified" });
        if (resp.status >= 400) {
          return res.status(502).json({ message: resp.body?.message || "Failed to update" });
        }
        res.json(resp.body);
      } catch (err) {
        console.error("Assignment update error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async createPlatformOpenshift(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name, namespace, spec } = req.body || {};
      if (!name || !namespace) return res.status(400).json({ message: "name and namespace required" });
      if (!NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      const clean = sanitizePlatformOpenshiftSpec(spec);
      if (clean.error) return res.status(400).json({ message: clean.error });
      const obj = {
        apiVersion: "hybridsovereign.redhat/v1alpha1",
        kind: "PlatformOpenshift",
        metadata: { name, namespace },
        spec: clean.spec,
      };
      try {
        const resp = await k8sRequest(apiServer, platformopenshiftsCollectionPath(namespace), "POST", token, obj);
        if (resp.status === 409) return res.status(409).json({ message: `PlatformOpenshift '${name}' already exists` });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) {
          return res.status(502).json({ message: resp.body?.message || "Failed to create PlatformOpenshift" });
        }
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("PlatformOpenshift create error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async deletePlatformOpenshift(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${platformopenshiftsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "DELETE", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to delete PlatformOpenshift" });
        res.json({ message: `PlatformOpenshift '${name}' deleted` });
      } catch (err) {
        console.error("PlatformOpenshift delete error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async getPlatformOpenshift(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${platformopenshiftsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "GET", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to get PlatformOpenshift" });
        res.json(resp.body);
      } catch (err) {
        console.error("PlatformOpenshift get error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async patchPlatformOpenshift(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      const { spec } = req.body || {};
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      if (!spec) return res.status(400).json({ message: "spec is required" });
      const clean = sanitizePlatformOpenshiftSpec(spec);
      if (clean.error) return res.status(400).json({ message: clean.error });
      try {
        const urlPath = `${platformopenshiftsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        // The editor sends the whole spec: replace it (fields left out are removed).
        const patch = [{ op: "replace", path: "/spec", value: clean.spec }];
        const resp = await k8sRequest(apiServer, urlPath, "PATCH", token, patch, "application/json-patch+json");
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: resp.body?.message || "Failed to patch PlatformOpenshift" });
        res.json(resp.body);
      } catch (err) {
        console.error("PlatformOpenshift patch error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async createCloudOSO(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name, namespace, spec } = req.body || {};
      if (!name || !namespace) return res.status(400).json({ message: "name and namespace required" });
      if (!NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      const clean = sanitizeCloudOSOSpec(spec);
      if (clean.error) return res.status(400).json({ message: clean.error });
      const obj = {
        apiVersion: "hybridsovereign.redhat/v1alpha1",
        kind: "CloudOSO",
        metadata: { name, namespace },
        spec: clean.spec,
      };
      try {
        const resp = await k8sRequest(apiServer, cloudososCollectionPath(namespace), "POST", token, obj);
        if (resp.status === 409) return res.status(409).json({ message: `CloudOSO '${name}' already exists` });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) {
          return res.status(502).json({ message: resp.body?.message || "Failed to create CloudOSO" });
        }
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("CloudOSO create error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async deleteCloudOSO(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${cloudososCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "DELETE", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to delete CloudOSO" });
        res.json({ message: `CloudOSO '${name}' deleted` });
      } catch (err) {
        console.error("CloudOSO delete error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async getCloudOSO(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${cloudososCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "GET", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to get CloudOSO" });
        res.json(resp.body);
      } catch (err) {
        console.error("CloudOSO get error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async patchCloudOSO(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      const { spec } = req.body || {};
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      if (!spec) return res.status(400).json({ message: "spec is required" });
      const clean = sanitizeCloudOSOSpec(spec);
      if (clean.error) return res.status(400).json({ message: clean.error });
      try {
        const urlPath = `${cloudososCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        // The editor sends the whole spec: replace it (fields left out are removed).
        const patch = [{ op: "replace", path: "/spec", value: clean.spec }];
        const resp = await k8sRequest(apiServer, urlPath, "PATCH", token, patch, "application/json-patch+json");
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: resp.body?.message || "Failed to patch CloudOSO" });
        res.json(resp.body);
      } catch (err) {
        console.error("CloudOSO patch error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async getMtvCatalog(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      try {
        const resp = await k8sRequest(apiServer, MTV_CATALOG_PATH, "GET", token);
        if (resp.status === 404) {
          return res.json({ providers: [], vms: {} });
        }
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to get MTV catalog ConfigMap" });
        const raw = resp.body?.data?.["catalog.json"] || "{}";
        try {
          res.json(JSON.parse(raw));
        } catch {
          res.json({ providers: [], vms: {} });
        }
      } catch (err) {
        console.error("MTV catalog error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async listOpenStackMigrations(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const namespace = req.query.namespace;
      if (!namespace || !NS_PATTERN.test(namespace)) {
        return res.status(400).json({ message: "namespace query parameter required" });
      }
      try {
        const resp = await k8sRequest(apiServer, openstackmigrationsCollectionPath(namespace), "GET", token);
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to list OpenStackMigrations" });
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("OpenStackMigration list error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async createOpenStackMigration(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name, namespace, spec } = req.body || {};
      if (!name || !namespace) return res.status(400).json({ message: "name and namespace required" });
      if (!NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      if (!spec || typeof spec !== "object") {
        return res.status(400).json({ message: "spec is required" });
      }
      const { source, vmName, cloudoso, providerNamespace } = spec;
      if (!source || typeof source !== "string" || !source.trim()) {
        return res.status(400).json({ message: "spec.source is required" });
      }
      if (!vmName || typeof vmName !== "string" || !vmName.trim()) {
        return res.status(400).json({ message: "spec.vmName is required" });
      }
      if (!cloudoso || typeof cloudoso !== "string" || !cloudoso.trim()) {
        return res.status(400).json({ message: "spec.cloudoso is required" });
      }
      const cr = {
        apiVersion: "hybridsovereign.redhat/v1alpha1",
        kind: "OpenStackMigration",
        metadata: { name, namespace },
        spec: {
          source: source.trim(),
          vmName: vmName.trim(),
          cloudoso: cloudoso.trim(),
          providerNamespace: (providerNamespace || "openshift-mtv").trim(),
        },
      };
      try {
        const resp = await k8sRequest(apiServer, openstackmigrationsCollectionPath(namespace), "POST", token, cr);
        if (resp.status === 409) return res.status(409).json({ message: `OpenStackMigration '${name}' already exists` });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) {
          return res.status(502).json({ message: resp.body?.message || "Failed to create OpenStackMigration" });
        }
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("OpenStackMigration create error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async deleteOpenStackMigration(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${openstackmigrationsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "DELETE", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to delete OpenStackMigration" });
        res.json({ message: `OpenStackMigration '${name}' deleted` });
      } catch (err) {
        console.error("OpenStackMigration delete error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async getOpenStackMigration(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${openstackmigrationsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "GET", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to fetch OpenStackMigration" });
        res.json(resp.body);
      } catch (err) {
        console.error("OpenStackMigration get error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async listCloudAWSs(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const namespace = req.query.namespace;
      if (!namespace || !NS_PATTERN.test(namespace)) {
        return res.status(400).json({ message: "namespace query parameter required" });
      }
      try {
        const resp = await k8sRequest(apiServer, cloudawssCollectionPath(namespace), "GET", token);
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to list CloudAWSs" });
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("CloudAWS list error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async createCloudAWS(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name, namespace, spec } = req.body || {};
      if (!name || !namespace) return res.status(400).json({ message: "name and namespace required" });
      if (!NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      const clean = sanitizeCloudAWSSpec(spec);
      if (clean.error) return res.status(400).json({ message: clean.error });
      const obj = {
        apiVersion: "hybridsovereign.redhat/v1alpha1",
        kind: "CloudAWS",
        metadata: { name, namespace },
        spec: clean.spec,
      };
      try {
        const resp = await k8sRequest(apiServer, cloudawssCollectionPath(namespace), "POST", token, obj);
        if (resp.status === 409) return res.status(409).json({ message: `CloudAWS '${name}' already exists` });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) {
          return res.status(502).json({ message: resp.body?.message || "Failed to create CloudAWS" });
        }
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("CloudAWS create error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async getCloudAWS(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${cloudawssCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "GET", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to get CloudAWS" });
        res.json(resp.body);
      } catch (err) {
        console.error("CloudAWS get error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async patchCloudAWS(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      const { spec } = req.body || {};
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      if (!spec) return res.status(400).json({ message: "spec is required" });
      const clean = sanitizeCloudAWSSpec(spec);
      if (clean.error) return res.status(400).json({ message: clean.error });
      try {
        const urlPath = `${cloudawssCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        // The editor sends the whole spec: replace it (fields left out are removed).
        const patch = [{ op: "replace", path: "/spec", value: clean.spec }];
        const resp = await k8sRequest(apiServer, urlPath, "PATCH", token, patch, "application/json-patch+json");
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: resp.body?.message || "Failed to patch CloudAWS" });
        res.json(resp.body);
      } catch (err) {
        console.error("CloudAWS patch error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async deleteCloudAWS(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${cloudawssCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "DELETE", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to delete CloudAWS" });
        res.json({ message: `CloudAWS '${name}' deleted` });
      } catch (err) {
        console.error("CloudAWS delete error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async listCloudGCPs(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const namespace = req.query.namespace;
      if (!namespace || !NS_PATTERN.test(namespace)) {
        return res.status(400).json({ message: "namespace query parameter required" });
      }
      try {
        const resp = await k8sRequest(apiServer, cloudgcpsCollectionPath(namespace), "GET", token);
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to list CloudGCPs" });
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("CloudGCP list error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async createCloudGCP(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name, namespace, spec } = req.body || {};
      if (!name || !namespace) return res.status(400).json({ message: "name and namespace required" });
      if (!NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      const clean = sanitizeCloudGCPSpec(spec);
      if (clean.error) return res.status(400).json({ message: clean.error });
      const obj = {
        apiVersion: "hybridsovereign.redhat/v1alpha1",
        kind: "CloudGCP",
        metadata: { name, namespace },
        spec: clean.spec,
      };
      try {
        const resp = await k8sRequest(apiServer, cloudgcpsCollectionPath(namespace), "POST", token, obj);
        if (resp.status === 409) return res.status(409).json({ message: `CloudGCP '${name}' already exists` });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) {
          return res.status(502).json({ message: resp.body?.message || "Failed to create CloudGCP" });
        }
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("CloudGCP create error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async getCloudGCP(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${cloudgcpsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "GET", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to get CloudGCP" });
        res.json(resp.body);
      } catch (err) {
        console.error("CloudGCP get error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async patchCloudGCP(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      const { spec } = req.body || {};
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      if (!spec) return res.status(400).json({ message: "spec is required" });
      const clean = sanitizeCloudGCPSpec(spec);
      if (clean.error) return res.status(400).json({ message: clean.error });
      try {
        const urlPath = `${cloudgcpsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        // The editor sends the whole spec: replace it (fields left out are removed).
        const patch = [{ op: "replace", path: "/spec", value: clean.spec }];
        const resp = await k8sRequest(apiServer, urlPath, "PATCH", token, patch, "application/json-patch+json");
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: resp.body?.message || "Failed to patch CloudGCP" });
        res.json(resp.body);
      } catch (err) {
        console.error("CloudGCP patch error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async deleteCloudGCP(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${cloudgcpsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "DELETE", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to delete CloudGCP" });
        res.json({ message: `CloudGCP '${name}' deleted` });
      } catch (err) {
        console.error("CloudGCP delete error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },


    async listCloudVirts(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const namespace = req.query.namespace;
      if (!namespace || !NS_PATTERN.test(namespace)) {
        return res.status(400).json({ message: "namespace query parameter required" });
      }
      try {
        const resp = await k8sRequest(apiServer, cloudvirtsCollectionPath(namespace), "GET", token);
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to list CloudVirts" });
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("CloudVirt list error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async createCloudVirt(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name, namespace, spec } = req.body || {};
      if (!name || !namespace) return res.status(400).json({ message: "name and namespace required" });
      if (!NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      const clean = sanitizeCloudVirtSpec(spec);
      if (clean.error) return res.status(400).json({ message: clean.error });
      const obj = {
        apiVersion: "hybridsovereign.redhat/v1alpha1",
        kind: "CloudVirt",
        metadata: { name, namespace },
        spec: clean.spec,
      };
      try {
        const resp = await k8sRequest(apiServer, cloudvirtsCollectionPath(namespace), "POST", token, obj);
        if (resp.status === 409) return res.status(409).json({ message: `CloudVirt '${name}' already exists` });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) {
          return res.status(502).json({ message: resp.body?.message || "Failed to create CloudVirt" });
        }
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("CloudVirt create error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async getCloudVirt(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${cloudvirtsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "GET", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to get CloudVirt" });
        res.json(resp.body);
      } catch (err) {
        console.error("CloudVirt get error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async patchCloudVirt(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      const { spec } = req.body || {};
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      if (!spec) return res.status(400).json({ message: "spec is required" });
      const clean = sanitizeCloudVirtSpec(spec);
      if (clean.error) return res.status(400).json({ message: clean.error });
      try {
        const urlPath = `${cloudvirtsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        // The editor sends the whole spec: replace it (fields left out are removed).
        const patch = [{ op: "replace", path: "/spec", value: clean.spec }];
        const resp = await k8sRequest(apiServer, urlPath, "PATCH", token, patch, "application/json-patch+json");
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: resp.body?.message || "Failed to patch CloudVirt" });
        res.json(resp.body);
      } catch (err) {
        console.error("CloudVirt patch error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async deleteCloudVirt(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${cloudvirtsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "DELETE", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to delete CloudVirt" });
        res.json({ message: `CloudVirt '${name}' deleted` });
      } catch (err) {
        console.error("CloudVirt delete error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async listHybridNetworks(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const namespace = req.query.namespace;
      if (!namespace || !NS_PATTERN.test(namespace)) {
        return res.status(400).json({ message: "namespace query parameter required" });
      }
      try {
        const resp = await k8sRequest(apiServer, hybridnetworksCollectionPath(namespace), "GET", token);
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to list HybridNetworks" });
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("HybridNetwork list error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async getHybridNetwork(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${hybridnetworksCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "GET", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to get HybridNetwork" });
        res.json(resp.body);
      } catch (err) {
        console.error("HybridNetwork get error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async createHybridNetwork(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name, namespace, spec } = req.body || {};
      if (!name || !namespace) return res.status(400).json({ message: "name and namespace required" });
      if (!NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      const clean = sanitizeHybridNetworkSpec(spec);
      if (clean.error) return res.status(400).json({ message: clean.error });
      const obj = {
        apiVersion: "hybridsovereign.redhat/v1alpha1",
        kind: "HybridNetwork",
        metadata: { name, namespace },
        spec: clean.spec,
      };
      try {
        const resp = await k8sRequest(apiServer, hybridnetworksCollectionPath(namespace), "POST", token, obj);
        if (resp.status === 409) return res.status(409).json({ message: `HybridNetwork '${name}' already exists` });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) {
          return res.status(502).json({ message: resp.body?.message || "Failed to create HybridNetwork" });
        }
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("HybridNetwork create error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async patchHybridNetwork(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      const { spec } = req.body || {};
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      if (!spec) return res.status(400).json({ message: "spec is required" });
      const clean = sanitizeHybridNetworkSpec(spec);
      if (clean.error) return res.status(400).json({ message: clean.error });
      try {
        const urlPath = `${hybridnetworksCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        // The editor sends the whole spec: replace it (fields left out are removed).
        const patch = [{ op: "replace", path: "/spec", value: clean.spec }];
        const resp = await k8sRequest(apiServer, urlPath, "PATCH", token, patch, "application/json-patch+json");
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: resp.body?.message || "Failed to patch HybridNetwork" });
        res.json(resp.body);
      } catch (err) {
        console.error("HybridNetwork patch error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async deleteHybridNetwork(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${hybridnetworksCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "DELETE", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to delete HybridNetwork" });
        res.json({ message: `HybridNetwork '${name}' deleted` });
      } catch (err) {
        console.error("HybridNetwork delete error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async listNetworkPlacements(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const namespace = req.query.namespace;
      if (!namespace || !NS_PATTERN.test(namespace)) {
        return res.status(400).json({ message: "namespace query parameter required" });
      }
      try {
        const resp = await k8sRequest(apiServer, networkplacementsCollectionPath(namespace), "GET", token);
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to list NetworkPlacements" });
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("NetworkPlacement list error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async getNetworkPlacement(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${networkplacementsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "GET", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to get NetworkPlacement" });
        res.json(resp.body);
      } catch (err) {
        console.error("NetworkPlacement get error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async createNetworkPlacement(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name, namespace, spec } = req.body || {};
      if (!name || !namespace) return res.status(400).json({ message: "name and namespace required" });
      if (!NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      const clean = sanitizeNetworkPlacementSpec(spec);
      if (clean.error) return res.status(400).json({ message: clean.error });
      const obj = {
        apiVersion: "hybridsovereign.redhat/v1alpha1",
        kind: "NetworkPlacement",
        metadata: { name, namespace },
        spec: clean.spec,
      };
      try {
        const resp = await k8sRequest(apiServer, networkplacementsCollectionPath(namespace), "POST", token, obj);
        if (resp.status === 409) return res.status(409).json({ message: `NetworkPlacement '${name}' already exists` });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) {
          return res.status(502).json({ message: resp.body?.message || "Failed to create NetworkPlacement" });
        }
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("NetworkPlacement create error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async deleteNetworkPlacement(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${networkplacementsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "DELETE", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to delete NetworkPlacement" });
        res.json({ message: `NetworkPlacement '${name}' deleted` });
      } catch (err) {
        console.error("NetworkPlacement delete error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async createVault(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name, namespace, spec } = req.body || {};
      if (!name || !namespace || !spec) return res.status(400).json({ message: "name, namespace, spec required" });
      if (!NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      const vault = {
        apiVersion: "hybridsovereign.redhat/v1alpha1",
        kind: "Vault",
        metadata: { name, namespace },
        spec: { ha: spec.ha !== false, rbacConfig: spec.rbacConfig },
      };
      try {
        const resp = await k8sRequest(apiServer, vaultsCollectionPath(namespace), "POST", token, vault);
        if (resp.status === 409) return res.status(409).json({ message: `Vault '${name}' already exists` });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: resp.body?.message || "Failed to create Vault" });
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("Vault create error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async deleteVault(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${vaultsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "DELETE", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to delete Vault" });
        res.json({ message: `Vault '${name}' deleted` });
      } catch (err) {
        console.error("Vault delete error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async getVault(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${vaultsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "GET", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to fetch Vault" });
        res.json(resp.body);
      } catch (err) {
        console.error("Vault get error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async listVaultKVs(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const namespace = req.query.namespace;
      if (!namespace || !NS_PATTERN.test(namespace)) {
        return res.status(400).json({ message: "namespace query parameter required" });
      }
      try {
        const resp = await k8sRequest(apiServer, vaultKVsCollectionPath(namespace), "GET", token);
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to list VaultKVs" });
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("VaultKV list error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async createVaultKV(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name, namespace, spec } = req.body || {};
      if (!name || !namespace || !spec) return res.status(400).json({ message: "name, namespace, spec required" });
      if (!NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      const vaultKV = {
        apiVersion: "hybridsovereign.redhat/v1alpha1",
        kind: "VaultKV",
        metadata: { name, namespace },
        spec: {
          vault: spec.vault,
          vaultAdminRbac: spec.vaultAdminRbac || [],
          vaultReaderRbac: spec.vaultReaderRbac || [],
        },
      };
      try {
        const resp = await k8sRequest(apiServer, vaultKVsCollectionPath(namespace), "POST", token, vaultKV);
        if (resp.status === 409) return res.status(409).json({ message: `VaultKV '${name}' already exists` });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: resp.body?.message || "Failed to create VaultKV" });
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("VaultKV create error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async deleteVaultKV(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${vaultKVsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "DELETE", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to delete VaultKV" });
        res.json({ message: `VaultKV '${name}' deleted` });
      } catch (err) {
        console.error("VaultKV delete error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async listAAPConfigs(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      try {
        const resp = await k8sRequest(apiServer, AAP_CONFIGS_PATH, "GET", token);
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) {
          return res.status(502).json({ message: "Failed to list AAPConfigs" });
        }
        const items = Array.isArray(resp.body?.items) ? resp.body.items : [];
        const configs = items
          .filter((i) => i.status?.ready)
          .map((i) => ({
            name: i.metadata.name,
            ready: i.status?.ready || false,
            aapControllerUrl: i.status?.aapControllerUrl || "",
          }));
        res.json(configs);
      } catch (err) {
        console.error("AAPConfig list error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async listAAPOrgs(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const namespace = req.query.namespace;
      if (!namespace || typeof namespace !== "string" || !NS_PATTERN.test(namespace)) {
        return res.status(400).json({
          message: "namespace query parameter is required and must be a valid namespace name",
        });
      }
      try {
        const resp = await k8sRequest(apiServer, aaporgsCollectionPath(namespace), "GET", token);
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) {
          return res.status(502).json({ message: "Failed to list AAPOrgs" });
        }
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("AAPOrg list error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async createAAPOrg(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name, namespace, spec } = req.body || {};
      if (!name || !namespace || !spec) {
        return res.status(400).json({ message: "name, namespace, and spec are required" });
      }
      if (!NAME_PATTERN.test(name)) {
        return res.status(400).json({ message: "Name must be lowercase alphanumeric with hyphens" });
      }
      if (!NS_PATTERN.test(namespace)) {
        return res.status(400).json({ message: "namespace must be a valid namespace name" });
      }
      const aapConfig = spec.aapConfig;
      if (typeof aapConfig !== "string" || !aapConfig.trim()) {
        return res.status(400).json({ message: "spec.aapConfig is required" });
      }
      const trimmedConfig = aapConfig.trim();
      if (!CONFIG_PATTERN.test(trimmedConfig)) {
        return res.status(400).json({ message: "spec.aapConfig must be a valid resource name" });
      }
      const adminCheck = validateRbacNameList(spec.aapAdminRbac, "spec.aapAdminRbac");
      if (!adminCheck.ok) return res.status(400).json({ message: adminCheck.message });
      const execCheck = validateRbacNameList(spec.aapJobExecutorRbac, "spec.aapJobExecutorRbac");
      if (!execCheck.ok) return res.status(400).json({ message: execCheck.message });
      const aapOrg = {
        apiVersion: "hybridsovereign.redhat/v1alpha1",
        kind: "AAPOrg",
        metadata: { name, namespace },
        spec: {
          aapConfig: trimmedConfig,
          aapAdminRbac: adminCheck.value,
          aapJobExecutorRbac: execCheck.value,
        },
      };
      try {
        const resp = await k8sRequest(apiServer, aaporgsCollectionPath(namespace), "POST", token, aapOrg);
        if (resp.status === 409) return res.status(409).json({ message: `AAPOrg '${name}' already exists` });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) {
          return res.status(502).json({
            message: typeof resp.body?.message === "string" ? resp.body.message : "Failed to create AAPOrg",
          });
        }
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("AAPOrg create error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async deleteAAPOrg(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${aaporgsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "DELETE", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to delete AAPOrg" });
        res.json({ message: `AAPOrg '${name}' deleted` });
      } catch (err) {
        console.error("AAPOrg delete error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async listQuayConfigs(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      try {
        const resp = await k8sRequest(apiServer, QUAY_CONFIGS_PATH, "GET", token);
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) {
          return res.status(502).json({ message: "Failed to list QuayConfigs" });
        }
        const items = Array.isArray(resp.body?.items) ? resp.body.items : [];
        const configs = items
          .filter((i) => i.status?.ready)
          .map((i) => ({
            name: i.metadata.name,
            ready: i.status?.ready || false,
            quayUrl: i.status?.quayUrl || "",
          }));
        res.json(configs);
      } catch (err) {
        console.error("QuayConfig list error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async listQuayOrgs(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const namespace = req.query.namespace;
      if (!namespace || typeof namespace !== "string" || !NS_PATTERN.test(namespace)) {
        return res.status(400).json({
          message: "namespace query parameter is required and must be a valid namespace name",
        });
      }
      try {
        const resp = await k8sRequest(apiServer, quayorgsCollectionPath(namespace), "GET", token);
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) {
          return res.status(502).json({ message: "Failed to list QuayOrgs" });
        }
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("QuayOrg list error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async createQuayOrg(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name, namespace, spec } = req.body || {};
      if (!name || !namespace || !spec) {
        return res.status(400).json({ message: "name, namespace, and spec are required" });
      }
      if (!NAME_PATTERN.test(name)) {
        return res.status(400).json({ message: "Name must be lowercase alphanumeric with hyphens" });
      }
      if (!NS_PATTERN.test(namespace)) {
        return res.status(400).json({ message: "namespace must be a valid namespace name" });
      }
      const quayConfig = spec.quayConfig;
      if (typeof quayConfig !== "string" || !quayConfig.trim()) {
        return res.status(400).json({ message: "spec.quayConfig is required" });
      }
      const trimmedConfig = quayConfig.trim();
      if (!CONFIG_PATTERN.test(trimmedConfig)) {
        return res.status(400).json({ message: "spec.quayConfig must be a valid resource name" });
      }
      const adminCheck = validateRbacNameList(spec.quayAdminRbac, "spec.quayAdminRbac");
      if (!adminCheck.ok) return res.status(400).json({ message: adminCheck.message });
      const creatorCheck = validateRbacNameList(spec.quayCreatorRbac, "spec.quayCreatorRbac");
      if (!creatorCheck.ok) return res.status(400).json({ message: creatorCheck.message });
      const memberCheck = validateRbacNameList(spec.quayMemberRbac, "spec.quayMemberRbac");
      if (!memberCheck.ok) return res.status(400).json({ message: memberCheck.message });
      const quayOrg = {
        apiVersion: "hybridsovereign.redhat/v1alpha1",
        kind: "QuayOrg",
        metadata: { name, namespace },
        spec: {
          quayConfig: trimmedConfig,
          quayAdminRbac: adminCheck.value,
          quayCreatorRbac: creatorCheck.value,
          quayMemberRbac: memberCheck.value,
        },
      };
      try {
        const resp = await k8sRequest(apiServer, quayorgsCollectionPath(namespace), "POST", token, quayOrg);
        if (resp.status === 409) return res.status(409).json({ message: `QuayOrg '${name}' already exists` });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) {
          return res.status(502).json({
            message: typeof resp.body?.message === "string" ? resp.body.message : "Failed to create QuayOrg",
          });
        }
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("QuayOrg create error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async deleteQuayOrg(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${quayorgsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "DELETE", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to delete QuayOrg" });
        res.json({ message: `QuayOrg '${name}' deleted` });
      } catch (err) {
        console.error("QuayOrg delete error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async updateTeam(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      const { spec } = req.body || {};
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${teamsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const getCurrent = await k8sRequest(apiServer, urlPath, "GET", token);
        if (getCurrent.status === 404) return res.status(404).json({ message: "Not found" });
        if (getCurrent.status >= 400) return res.status(502).json({ message: "Failed to fetch Team" });
        const current = getCurrent.body;
        const updated = {
          ...current,
          metadata: { ...current.metadata },
          spec: {
            ...current.spec,
            features: {
              istio: spec?.features?.istio === true,
              argo: spec?.features?.argo === true,
            },
            ...(spec?.rbacConfig ? { rbacConfig: spec.rbacConfig } : { rbacConfig: current.spec?.rbacConfig }),
            // A list in the request replaces the stored one (empty clears it); an absent list is kept.
            ...Object.fromEntries(
              TEAM_RBAC_KEYS.map((k) => [k, teamRbacList(spec?.[k])]).filter(([, l]) => l !== undefined),
            ),
          },
        };
        const resp = await k8sRequest(apiServer, urlPath, "PUT", token, updated);
        if (resp.status === 409) return res.status(409).json({ message: "Conflict" });
        if (resp.status >= 400) return res.status(502).json({ message: resp.body?.message || "Failed to update Team" });
        res.json(resp.body);
      } catch (err) {
        console.error("Team update error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async getTeam(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${teamsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "GET", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to fetch Team" });
        res.json(resp.body);
      } catch (err) {
        console.error("Team get error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async updateRbac(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      const { spec } = req.body || {};
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${rbacsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const getCurrent = await k8sRequest(apiServer, urlPath, "GET", token);
        if (getCurrent.status === 404) return res.status(404).json({ message: "Not found" });
        if (getCurrent.status >= 400) return res.status(502).json({ message: "Failed to fetch Rbac" });
        const current = getCurrent.body;
        const rbacSpec = { ...current.spec };
        if (typeof spec?.config === "string" && spec.config.trim() && CONFIG_PATTERN.test(spec.config.trim())) {
          rbacSpec.config = spec.config.trim();
        }
        if (typeof spec?.description === "string") {
          rbacSpec.description = spec.description.trim() || undefined;
          if (!rbacSpec.description) delete rbacSpec.description;
        }
        const updated = { ...current, metadata: { ...current.metadata }, spec: rbacSpec };
        const resp = await k8sRequest(apiServer, urlPath, "PUT", token, updated);
        if (resp.status === 409) return res.status(409).json({ message: "Conflict" });
        if (resp.status >= 400) return res.status(502).json({ message: resp.body?.message || "Failed to update Rbac" });
        res.json(resp.body);
      } catch (err) {
        console.error("Rbac update error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async updateVaultKV(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      const { spec } = req.body || {};
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${vaultKVsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const getCurrent = await k8sRequest(apiServer, urlPath, "GET", token);
        if (getCurrent.status === 404) return res.status(404).json({ message: "Not found" });
        if (getCurrent.status >= 400) return res.status(502).json({ message: "Failed to fetch VaultKV" });
        const current = getCurrent.body;
        const adminCheck = validateRbacNameList(spec?.vaultAdminRbac, "spec.vaultAdminRbac");
        if (!adminCheck.ok) return res.status(400).json({ message: adminCheck.message });
        const readerCheck = validateRbacNameList(spec?.vaultReaderRbac, "spec.vaultReaderRbac");
        if (!readerCheck.ok) return res.status(400).json({ message: readerCheck.message });
        const updated = {
          ...current,
          metadata: { ...current.metadata },
          spec: {
            ...current.spec,
            vaultAdminRbac: adminCheck.value.length ? adminCheck.value : (current.spec?.vaultAdminRbac || []),
            vaultReaderRbac: readerCheck.value.length ? readerCheck.value : (current.spec?.vaultReaderRbac || []),
          },
        };
        const resp = await k8sRequest(apiServer, urlPath, "PUT", token, updated);
        if (resp.status === 409) return res.status(409).json({ message: "Conflict" });
        if (resp.status >= 400) return res.status(502).json({ message: resp.body?.message || "Failed to update VaultKV" });
        res.json(resp.body);
      } catch (err) {
        console.error("VaultKV update error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async getVaultKV(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${vaultKVsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "GET", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to fetch VaultKV" });
        res.json(resp.body);
      } catch (err) {
        console.error("VaultKV get error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async updateAAPOrg(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      const { spec } = req.body || {};
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${aaporgsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const getCurrent = await k8sRequest(apiServer, urlPath, "GET", token);
        if (getCurrent.status === 404) return res.status(404).json({ message: "Not found" });
        if (getCurrent.status >= 400) return res.status(502).json({ message: "Failed to fetch AAPOrg" });
        const current = getCurrent.body;
        const adminCheck = validateRbacNameList(spec?.aapAdminRbac, "spec.aapAdminRbac");
        if (!adminCheck.ok) return res.status(400).json({ message: adminCheck.message });
        const execCheck = validateRbacNameList(spec?.aapJobExecutorRbac, "spec.aapJobExecutorRbac");
        if (!execCheck.ok) return res.status(400).json({ message: execCheck.message });
        const updated = {
          ...current,
          metadata: { ...current.metadata },
          spec: {
            ...current.spec,
            aapAdminRbac: adminCheck.value,
            aapJobExecutorRbac: execCheck.value,
            ...(typeof spec?.description === "string" ? { description: spec.description } : {}),
          },
        };
        const resp = await k8sRequest(apiServer, urlPath, "PUT", token, updated);
        if (resp.status === 409) return res.status(409).json({ message: "Conflict" });
        if (resp.status >= 400) return res.status(502).json({ message: resp.body?.message || "Failed to update AAPOrg" });
        res.json(resp.body);
      } catch (err) {
        console.error("AAPOrg update error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async getAAPOrg(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${aaporgsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "GET", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to fetch AAPOrg" });
        res.json(resp.body);
      } catch (err) {
        console.error("AAPOrg get error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async updateQuayOrg(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      const { spec } = req.body || {};
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${quayorgsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const getCurrent = await k8sRequest(apiServer, urlPath, "GET", token);
        if (getCurrent.status === 404) return res.status(404).json({ message: "Not found" });
        if (getCurrent.status >= 400) return res.status(502).json({ message: "Failed to fetch QuayOrg" });
        const current = getCurrent.body;
        const adminCheck = validateRbacNameList(spec?.quayAdminRbac, "spec.quayAdminRbac");
        if (!adminCheck.ok) return res.status(400).json({ message: adminCheck.message });
        const creatorCheck = validateRbacNameList(spec?.quayCreatorRbac, "spec.quayCreatorRbac");
        if (!creatorCheck.ok) return res.status(400).json({ message: creatorCheck.message });
        const memberCheck = validateRbacNameList(spec?.quayMemberRbac, "spec.quayMemberRbac");
        if (!memberCheck.ok) return res.status(400).json({ message: memberCheck.message });
        const updated = {
          ...current,
          metadata: { ...current.metadata },
          spec: {
            ...current.spec,
            quayAdminRbac: adminCheck.value,
            quayCreatorRbac: creatorCheck.value,
            quayMemberRbac: memberCheck.value,
          },
        };
        const resp = await k8sRequest(apiServer, urlPath, "PUT", token, updated);
        if (resp.status === 409) return res.status(409).json({ message: "Conflict" });
        if (resp.status >= 400) return res.status(502).json({ message: resp.body?.message || "Failed to update QuayOrg" });
        res.json(resp.body);
      } catch (err) {
        console.error("QuayOrg update error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async getQuayOrg(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${quayorgsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "GET", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to fetch QuayOrg" });
        res.json(resp.body);
      } catch (err) {
        console.error("QuayOrg get error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async listPersonas(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const namespace = req.query.namespace;
      if (!namespace || !NS_PATTERN.test(namespace)) {
        return res.status(400).json({ message: "namespace query parameter required" });
      }
      try {
        const resp = await k8sRequest(apiServer, personasCollectionPath(namespace), "GET", token);
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to list Personas" });
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("Persona list error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async getPersona(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      try {
        const urlPath = `${personasCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const resp = await k8sRequest(apiServer, urlPath, "GET", token);
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to fetch Persona" });
        res.json(resp.body);
      } catch (err) {
        console.error("Persona get error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async createPersona(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name, namespace, spec } = req.body || {};
      if (!name || !namespace) return res.status(400).json({ message: "name and namespace required" });
      if (!NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      const rbac = spec?.rbac;
      const type = spec?.type;
      if (!rbac || typeof rbac !== "string" || !rbac.trim()) {
        return res.status(400).json({ message: "spec.rbac is required" });
      }
      if (!type || typeof type !== "string" || !PERSONA_TYPES.has(type.trim())) {
        return res.status(400).json({ message: "spec.type must be a valid Persona role type" });
      }
      const persona = {
        apiVersion: "hybridsovereign.redhat/v1alpha1",
        kind: "Persona",
        metadata: { name, namespace },
        spec: { rbac: rbac.trim(), type: type.trim() },
      };
      try {
        const resp = await k8sRequest(apiServer, personasCollectionPath(namespace), "POST", token, persona);
        if (resp.status === 409) return res.status(409).json({ message: `Persona '${name}' already exists` });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) {
          return res.status(502).json({ message: resp.body?.message || "Failed to create Persona" });
        }
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("Persona create error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async patchPersonaAnnotation(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name } = req.params;
      const namespace = req.query.namespace;
      const { annotations } = req.body || {};
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!namespace || !NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      if (!annotations || typeof annotations !== "object" || Array.isArray(annotations)) {
        return res.status(400).json({ message: "annotations object is required" });
      }
      const urlPath = `${personasCollectionPath(namespace)}/${encodeURIComponent(name)}`;
      const patchBody = { metadata: { annotations } };
      try {
        const resp = await k8sRequest(apiServer, urlPath, "PATCH", token, patchBody, "application/merge-patch+json");
        if (resp.status === 404) return res.status(404).json({ message: "Not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) {
          return res.status(502).json({ message: resp.body?.message || "Failed to patch Persona annotations" });
        }
        res.json(resp.body);
      } catch (err) {
        console.error("Persona annotation patch error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async patchAnnotation(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const {
        group = "hybridsovereign.redhat",
        version = "v1alpha1",
        kind,
        name,
        namespace,
        annotation,
        value,
      } = req.body || {};
      if (typeof group !== "string" || group !== "hybridsovereign.redhat") {
        return res.status(400).json({ message: "Invalid API group" });
      }
      if (typeof version !== "string" || version !== "v1alpha1") {
        return res.status(400).json({ message: "Invalid API version" });
      }
      if (!kind || typeof kind !== "string" || !KIND_PLURALS[kind]) {
        return res.status(400).json({ message: "Invalid or unsupported kind" });
      }
      if (!name || !NAME_PATTERN.test(name)) {
        return res.status(400).json({ message: "Invalid resource name" });
      }
      if (!namespace || !NS_PATTERN.test(namespace)) {
        return res.status(400).json({ message: "Invalid namespace" });
      }
      if (annotation !== RECONCILE_ANNOTATION) {
        return res.status(400).json({ message: "Unsupported annotation" });
      }
      if (typeof value !== "string" || !value.trim()) {
        return res.status(400).json({ message: "value must be a non-empty string" });
      }
      const plural = KIND_PLURALS[kind];
      const urlPath =
        `/apis/${group}/${version}/namespaces/${encodeURIComponent(namespace)}/${plural}/${encodeURIComponent(name)}`;
      const patchBody = {
        metadata: {
          annotations: {
            [annotation]: value,
          },
        },
      };
      try {
        const resp = await k8sRequest(
          apiServer,
          urlPath,
          "PATCH",
          token,
          patchBody,
          "application/merge-patch+json",
        );
        if (resp.status === 404) return res.status(404).json({ message: "Resource not found" });
        if (resp.status === 403) return res.status(403).json({ message: "Forbidden" });
        if (resp.status >= 400) {
          const msg = typeof resp.body?.message === "string" ? resp.body.message : "Failed to patch annotation";
          return res.status(502).json({ message: msg });
        }
        res.json({ message: "Annotation patched", resource: resp.body });
      } catch (err) {
        console.error("patchAnnotation error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async getPermissions(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const namespace = req.query.namespace;
      if (!namespace || !NS_PATTERN.test(namespace)) {
        return res.status(400).json({ message: "Invalid namespace" });
      }

      const RESOURCE_TYPES = [
        { resource: "cloudosos",           group: "hybridsovereign.redhat" },
        { resource: "cloudawss",           group: "hybridsovereign.redhat" },
        { resource: "cloudgcps",           group: "hybridsovereign.redhat" },
        { resource: "cloudvirts",          group: "hybridsovereign.redhat" },
        { resource: "hybridnetworks",      group: "hybridsovereign.redhat" },
        { resource: "networkplacements",   group: "hybridsovereign.redhat" },
        { resource: "platformopenshifts",  group: "hybridsovereign.redhat" },
        { resource: "vaultkvs",            group: "hybridsovereign.redhat" },
        { resource: "vaults",              group: "hybridsovereign.redhat" },
        { resource: "aaporgs",             group: "hybridsovereign.redhat" },
        { resource: "quayorgs",            group: "hybridsovereign.redhat" },
        { resource: "projects",            group: "hybridsovereign.redhat" },
        { resource: "teams",               group: "hybridsovereign.redhat" },
        { resource: "assignments",         group: "hybridsovereign.redhat" },
        { resource: "rbacs",               group: "hybridsovereign.redhat" },
        { resource: "personas",            group: "hybridsovereign.redhat" },
      ];
      const VERBS = ["list", "create", "delete"];
      const SSAR_PATH = "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews";

      async function checkOne(resource, group, verb) {
        try {
          const body = {
            apiVersion: "authorization.k8s.io/v1",
            kind: "SelfSubjectAccessReview",
            spec: {
              resourceAttributes: {
                namespace,
                verb,
                group,
                resource,
              },
            },
          };
          const resp = await k8sRequest(apiServer, SSAR_PATH, "POST", token, body);
          return resp.status === 201 && resp.body?.status?.allowed === true;
        } catch {
          return false;
        }
      }

      try {
        const permissions = {};
        for (const { resource, group } of RESOURCE_TYPES) {
          permissions[resource] = {};
          for (const verb of VERBS) {
            permissions[resource][verb] = await checkOne(resource, group, verb);
          }
        }
        res.json(permissions);
      } catch (err) {
        console.error("getPermissions error:", err.message);
        res.status(502).json({ message: "Failed to check permissions" });
      }
    },
  };
}
