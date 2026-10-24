/**
 * Spec sanitizers of the tenant dashboard's curated write routes.
 *
 * Each accepts exactly the spec fields of this platform's CRDs and passes them
 * through: unknown fields are dropped, wrong types are refused, and nothing the
 * CRD does not require is required here (the API server applies the CRD
 * defaults, enums and rules, and the routes send fieldValidation=Strict). The
 * only deliberate drops are the fields that are set by the platform and refused
 * from tenant users (PLATFORM_ONLY_FIELDS).
 *
 * ui/scripts/tests/sanitizers.test.mjs builds a full spec from every CRD's
 * spec.properties and fails when a sanitizer drops a field that is not listed
 * here, so a CRD change cannot silently lose fields again (UX-089).
 *
 * Every sanitizer returns { spec } or { error }.
 */

export const NAME_PATTERN = /^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$/;
export const NS_PATTERN = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/;
export const CONFIG_PATTERN = /^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$/;
const DNS_LABEL = /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/;

/**
 * Fields set by the platform; requests from tenant users that set them are
 * refused by admission. The tenant routes never send them, and an edit keeps
 * the stored value (keepPlatformOnlyFields).
 */
export const PLATFORM_ONLY_FIELDS = {
  CloudOSO: ["vaultPath", "route53VaultPath"],
  PlatformOpenshift: ["openstack.pullSecretVaultPath", "openstack.pullSecretVaultProperty", "openstack.sshKeyVaultPath"],
};

export function isObject(v) {
  return v != null && typeof v === "object" && !Array.isArray(v);
}

/** Non-empty trimmed strings of the listed keys. */
export function pickStrings(src, keys) {
  const out = {};
  for (const k of keys) {
    const v = src?.[k];
    if (typeof v === "string" && v.trim()) out[k] = v.trim();
  }
  return out;
}

/** List of non-empty trimmed strings, or undefined when absent/empty; false when not a string list. */
export function stringList(v) {
  if (v == null) return undefined;
  if (!Array.isArray(v) || !v.every((x) => typeof x === "string")) return false;
  const out = v.map((x) => x.trim()).filter(Boolean);
  return out.length ? out : undefined;
}

/**
 * List of Rbac names (deduplicated): undefined when absent, [] when empty (an
 * edit that clears the list), false when not a list of valid names.
 */
export function rbacNameList(v) {
  if (v == null) return undefined;
  if (!Array.isArray(v)) return false;
  const out = [];
  for (const item of v) {
    if (typeof item !== "string") return false;
    const t = item.trim();
    if (!t) continue;
    if (!NAME_PATTERN.test(t)) return false;
    if (!out.includes(t)) out.push(t);
  }
  return out;
}

/** One Rbac name: undefined when absent or empty, false when invalid. */
export function rbacName(v) {
  if (v == null || v === "") return undefined;
  if (typeof v !== "string" || !NAME_PATTERN.test(v.trim())) return false;
  return v.trim();
}

/** Integer in [min, max], undefined when absent, false when invalid. */
export function intIn(v, min, max) {
  if (v == null || v === "") return undefined;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isInteger(n) && n >= min && n <= max ? n : false;
}

/** Boolean: undefined when absent, false (the value) or true, or the string "invalid". */
function bool(v) {
  if (v == null) return undefined;
  return typeof v === "boolean" ? v : "invalid";
}

/** `{ name }` reference: null when absent, false when invalid. */
export function sanitizeNameRef(raw) {
  if (raw == null) return null;
  if (!isObject(raw) || typeof raw.name !== "string") return false;
  const name = raw.name.trim();
  if (!name || name.length > 253 || !NAME_PATTERN.test(name)) return false;
  return { name };
}

/** spec.cloudRef of a tenant environment: a CloudInfrastructure in the platform namespace. */
export function sanitizeCloudRef(raw) {
  return sanitizeNameRef(raw);
}

/** Optional connection name (config refs are resolved by the platform when omitted). */
function configName(raw, field) {
  if (raw == null || raw === "") return { value: undefined };
  if (typeof raw !== "string" || !CONFIG_PATTERN.test(raw.trim())) {
    return { error: `${field} must be the name of a connection (lowercase letters, digits and hyphens)` };
  }
  return { value: raw.trim() };
}

function deletionProtection(raw, spec) {
  const dp = bool(raw?.deletionProtection);
  if (dp === "invalid") return "spec.deletionProtection must be a boolean";
  if (dp !== undefined) spec.deletionProtection = dp;
  return null;
}

/** Role lists of a service object: each present list is validated and passed. */
function roleLists(raw, keys, spec, { keepEmpty = false } = {}) {
  for (const key of keys) {
    const list = rbacNameList(raw?.[key]);
    if (list === false) return `spec.${key} must be a list of access group names`;
    if (list === undefined) continue;
    if (list.length || keepEmpty) spec[key] = list;
  }
  return null;
}

/* ------------------------------------------------------------------ access and services */

export function sanitizeRbacSpec(raw) {
  if (!isObject(raw)) return { error: "spec must be an object" };
  const spec = {};
  const cfg = configName(raw.config, "spec.config");
  if (cfg.error) return { error: cfg.error };
  if (cfg.value) spec.config = cfg.value;
  if (typeof raw.description === "string" && raw.description.trim()) spec.description = raw.description.trim();
  return { spec };
}

export function sanitizeVaultSpec(raw) {
  if (!isObject(raw)) return { error: "spec must be an object" };
  const spec = {};
  const ha = bool(raw.ha);
  if (ha === "invalid") return { error: "spec.ha must be a boolean" };
  spec.ha = ha !== false;
  const cfg = configName(raw.rbacConfig, "spec.rbacConfig");
  if (cfg.error) return { error: cfg.error };
  if (cfg.value) spec.rbacConfig = cfg.value;
  return { spec };
}

export const VAULTKV_ROLE_KEYS = ["vaultAdminRbac", "vaultReaderRbac", "vaultOpsRbac", "vaultDeveloperRbac"];
export const AAPORG_ROLE_KEYS = ["aapAdminRbac", "aapJobExecutorRbac", "aapViewerRbac"];
export const QUAYORG_ROLE_KEYS = ["quayAdminRbac", "quayCreatorRbac", "quayMemberRbac"];
export const TEAM_RBAC_KEYS = ["teamAdmin", "teamMemberRbac", "teamViewerRbac"];
export const PROJECT_RBAC_KEYS = ["projectAdmin", "projectDeveloperRbac", "projectViewerRbac"];
export const ASSIGNMENT_TOOL_RBAC_KEYS = ["assignmentAdmin", "assignmentDeveloper", "assignmentViewer", "assignmentOps"];

export function sanitizeVaultKVSpec(raw, { update = false } = {}) {
  if (!isObject(raw)) return { error: "spec must be an object" };
  const spec = {};
  const vault = typeof raw.vault === "string" ? raw.vault.trim() : "";
  if (vault) {
    if (!NAME_PATTERN.test(vault)) return { error: "spec.vault must be the name of a Vault in this namespace" };
    spec.vault = vault;
  } else if (!update) {
    return { error: "spec.vault (a Vault in this namespace) is required" };
  }
  const err = roleLists(raw, VAULTKV_ROLE_KEYS, spec, { keepEmpty: update });
  if (err) return { error: err };
  return { spec };
}

export function sanitizeAAPOrgSpec(raw, { update = false } = {}) {
  if (!isObject(raw)) return { error: "spec must be an object" };
  const spec = {};
  const cfg = configName(raw.aapConfig, "spec.aapConfig");
  if (cfg.error) return { error: cfg.error };
  if (cfg.value) spec.aapConfig = cfg.value;
  const err = roleLists(raw, AAPORG_ROLE_KEYS, spec, { keepEmpty: update });
  if (err) return { error: err };
  return { spec };
}

export function sanitizeQuayOrgSpec(raw, { update = false } = {}) {
  if (!isObject(raw)) return { error: "spec must be an object" };
  const spec = {};
  const cfg = configName(raw.quayConfig, "spec.quayConfig");
  if (cfg.error) return { error: cfg.error };
  if (cfg.value) spec.quayConfig = cfg.value;
  const err = roleLists(raw, QUAYORG_ROLE_KEYS, spec, { keepEmpty: update });
  if (err) return { error: err };
  return { spec };
}

/* ------------------------------------------------------------------ clusters */

export function sanitizeTeamSpec(raw, { update = false } = {}) {
  if (raw == null) raw = {};
  if (!isObject(raw)) return { error: "spec must be an object" };
  const spec = {};
  if (raw.features != null) {
    if (!isObject(raw.features)) return { error: "spec.features must be an object" };
    spec.features = {};
    for (const f of ["istio", "argo"]) {
      const v = bool(raw.features[f]);
      if (v === "invalid") return { error: `spec.features.${f} must be a boolean` };
      if (v !== undefined) spec.features[f] = v;
    }
  }
  const cfg = configName(raw.rbacConfig, "spec.rbacConfig");
  if (cfg.error) return { error: cfg.error };
  if (cfg.value) spec.rbacConfig = cfg.value;
  const err = roleLists(raw, TEAM_RBAC_KEYS, spec, { keepEmpty: update });
  if (err) return { error: err };
  return { spec };
}

export function sanitizeProjectSpec(raw, { update = false } = {}) {
  if (raw == null) raw = {};
  if (!isObject(raw)) return { error: "spec must be an object" };
  const spec = pickStrings(raw, ["displayName", "description"]);
  const cfg = configName(raw.rbacConfig, "spec.rbacConfig");
  if (cfg.error) return { error: cfg.error };
  if (cfg.value) spec.rbacConfig = cfg.value;
  const err = roleLists(raw, PROJECT_RBAC_KEYS, spec, { keepEmpty: update });
  if (err) return { error: err };
  return { spec };
}

export function sanitizeAssignmentSpec(raw) {
  if (!isObject(raw)) return { error: "spec must be an object" };
  const spec = {};
  const team = typeof raw.team === "string" ? raw.team.trim() : "";
  if (team) {
    if (!NAME_PATTERN.test(team)) return { error: "spec.team must be the name of a team setup in this namespace" };
    spec.team = team;
  }
  const projects = stringList(raw.projects);
  if (projects === false) return { error: "spec.projects must be a list of app project names" };
  if (projects) {
    if (!projects.every((p) => NAME_PATTERN.test(p))) return { error: "spec.projects must be a list of app project names" };
    spec.projects = projects;
  }
  if (typeof raw.openshift === "string" && raw.openshift.trim()) {
    if (!NAME_PATTERN.test(raw.openshift.trim())) return { error: "spec.openshift must be the name of an OpenShift cluster" };
    spec.openshift = raw.openshift.trim();
  }
  if (raw.toolRbac != null) {
    if (!isObject(raw.toolRbac)) return { error: "spec.toolRbac must be an object" };
    const toolRbac = {};
    for (const key of ASSIGNMENT_TOOL_RBAC_KEYS) {
      const v = rbacName(raw.toolRbac[key]);
      if (v === false) return { error: `spec.toolRbac.${key} must be the name of an access group` };
      if (v) toolRbac[key] = v;
    }
    if (Object.keys(toolRbac).length) spec.toolRbac = toolRbac;
  }
  return { spec };
}

export function sanitizePlatformOpenshiftSpec(raw) {
  if (!isObject(raw)) return { error: "spec must be an object" };
  const spec = {};
  const dpErr = deletionProtection(raw, spec);
  if (dpErr) return { error: dpErr };
  if (raw.type != null) {
    if (raw.type !== "openstack" && raw.type !== "aws" && raw.type !== "gcp") return { error: "spec.type must be openstack, aws or gcp" };
    spec.type = raw.type;
  }
  if (raw.openstack != null) {
    if (!isObject(raw.openstack)) return { error: "spec.openstack must be an object" };
    // pullSecretVaultPath, pullSecretVaultProperty and sshKeyVaultPath are platform-only (never sent).
    const os = pickStrings(raw.openstack, [
      "environment",
      "controlPlaneFlavor",
      "workerFlavor",
      "externalNetwork",
      "ocpImageSet",
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
  for (const cloud of ["aws", "gcp"]) {
    const src = raw[cloud];
    if (src == null) continue;
    if (!isObject(src)) return { error: `spec.${cloud} must be an object` };
    const out = pickStrings(src, ["environment", "region", "controllerFlavor", "workerFlavor", "network"]);
    if (!out.environment) return { error: `spec.${cloud}.environment is required` };
    if (src.publish != null) {
      if (src.publish !== "External" && src.publish !== "Internal") return { error: `spec.${cloud}.publish must be External or Internal` };
      out.publish = src.publish;
    }
    if (cloud === "aws" && src.clusterType != null) {
      if (src.clusterType !== "standalone" && src.clusterType !== "ha") {
        return { error: "spec.aws.clusterType must be standalone or ha" };
      }
      out.clusterType = src.clusterType;
    }
    const cp = intIn(src.controlPlaneCount, 0, 1000);
    const workers = intIn(src.workerCount, 0, 1000);
    if (cp === false || workers === false) return { error: `spec.${cloud} node counts must be integers` };
    if (cp !== undefined) out.controlPlaneCount = cp;
    if (workers !== undefined) out.workerCount = workers;
    spec[cloud] = out;
  }
  if (raw.toolRbac != null) {
    if (!isObject(raw.toolRbac)) return { error: "spec.toolRbac must be an object" };
    const toolRbac = {};
    for (const key of ["clusterAdminRbac", "clusterOperatorRbac", "clusterDeveloperRbac", "clusterViewerRbac"]) {
      const list = stringList(raw.toolRbac[key]);
      if (list === false) return { error: `spec.toolRbac.${key} must be a list of access group names` };
      if (list) toolRbac[key] = list;
    }
    if (Object.keys(toolRbac).length) spec.toolRbac = toolRbac;
  }
  const viewers = stringList(raw.clusterViewerRbac);
  if (viewers === false) return { error: "spec.clusterViewerRbac must be a list of access group names" };
  if (viewers) spec.clusterViewerRbac = viewers;
  return { spec };
}

/* ------------------------------------------------------------------ environments */

export function sanitizeCloudOSOSpec(raw) {
  if (!isObject(raw)) return { error: "spec must be an object" };
  // vaultPath and route53VaultPath are platform-only (never sent).
  const spec = pickStrings(raw, [
    "project",
    "baseDomain",
    "projectDomain",
    "externalNetwork",
    "regionName",
    "landingzone",
    "designateZoneId",
    "designateProjectId",
    "vrfId",
  ]);
  const dpErr = deletionProtection(raw, spec);
  if (dpErr) return { error: dpErr };
  const cloudRef = sanitizeCloudRef(raw.cloudRef);
  if (cloudRef === false) return { error: "spec.cloudRef.name is invalid" };
  if (cloudRef) spec.cloudRef = cloudRef;
  if (raw.dnsProvider != null && raw.dnsProvider !== "") {
    if (!["designate", "route53", "none"].includes(raw.dnsProvider)) {
      return { error: "spec.dnsProvider must be designate, route53 or none" };
    }
    spec.dnsProvider = raw.dnsProvider;
  }
  const vrf = bool(raw.enableVRF);
  if (vrf === "invalid") return { error: "spec.enableVRF must be a boolean" };
  if (vrf !== undefined) spec.enableVRF = vrf;
  return { spec };
}

/** CloudAWS: the AWS site; the platform assigns the account (the CRD requires only cloudRef). */
export function sanitizeCloudAWSSpec(raw) {
  if (!isObject(raw)) return { error: "spec must be an object" };
  const spec = {};
  const cloudRef = sanitizeCloudRef(raw.cloudRef);
  if (!cloudRef) return { error: "spec.cloudRef.name (an AWS site) is required" };
  spec.cloudRef = cloudRef;
  const dpErr = deletionProtection(raw, spec);
  if (dpErr) return { error: dpErr };
  return { spec };
}

export function sanitizeCloudGCPSpec(raw) {
  if (!isObject(raw)) return { error: "spec must be an object" };
  const spec = {};
  const cloudRef = sanitizeCloudRef(raw.cloudRef);
  if (!cloudRef) return { error: "spec.cloudRef.name (a Google Cloud site) is required" };
  spec.cloudRef = cloudRef;
  const dpErr = deletionProtection(raw, spec);
  if (dpErr) return { error: dpErr };
  return { spec };
}

export function sanitizeCloudVirtSpec(raw) {
  if (!isObject(raw)) return { error: "spec must be an object" };
  const spec = pickStrings(raw, ["description", "storageClass"]);
  const cloudRef = sanitizeCloudRef(raw.cloudRef);
  if (!cloudRef) return { error: "spec.cloudRef.name (an OpenShift Virtualization site) is required" };
  spec.cloudRef = cloudRef;
  const dpErr = deletionProtection(raw, spec);
  if (dpErr) return { error: dpErr };
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
          return { error: "spec.namespaces[].network must be a network name" };
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
      if (list === false) return { error: `spec.toolRbac.${key} must be a list of access group names` };
      if (list) toolRbac[key] = list;
    }
    if (Object.keys(toolRbac).length) spec.toolRbac = toolRbac;
  }
  return { spec };
}

/* ------------------------------------------------------------------ networks */

export function sanitizeHybridNetworkSpec(raw) {
  if (!isObject(raw)) return { error: "spec must be an object" };
  const spec = pickStrings(raw, ["description"]);
  // the address space: the tenant's own prefixes and/or IPv6 space the platform assigns; the API checks both
  const prefixes = stringList(raw.prefixes);
  if (prefixes === false) return { error: "spec.prefixes must be a list of IPv4/IPv6 prefixes" };
  if (prefixes) spec.prefixes = prefixes;
  if (raw.platformPrefixes != null) {
    if (!Array.isArray(raw.platformPrefixes)) return { error: "spec.platformPrefixes must be a list" };
    const platform = [];
    for (const q of raw.platformPrefixes) {
      if (!isObject(q)) return { error: "spec.platformPrefixes entries must be objects" };
      const family = intIn(q.family, 6, 6);
      if (family === undefined || family === false) return { error: "spec.platformPrefixes[].family must be 6" };
      const entry = { family };
      const length = intIn(q.prefixLength, 48, 64);
      if (length === false) return { error: "spec.platformPrefixes[].prefixLength must be an integer from 48 to 64" };
      if (length !== undefined) entry.prefixLength = length;
      if (q.prefix != null) {
        if (typeof q.prefix !== "string" || !q.prefix.trim()) return { error: "spec.platformPrefixes[].prefix must be a prefix" };
        entry.prefix = q.prefix.trim();
      }
      platform.push(entry);
    }
    if (platform.length) spec.platformPrefixes = platform;
  }
  if (!spec.prefixes && !spec.platformPrefixes) {
    return { error: "the network needs address space: spec.prefixes, spec.platformPrefixes or both" };
  }
  const dpErr = deletionProtection(raw, spec);
  if (dpErr) return { error: dpErr };
  const fabricRef = sanitizeNameRef(raw.fabricRef);
  if (fabricRef === false) return { error: "spec.fabricRef.name is invalid" };
  if (fabricRef) spec.fabricRef = fabricRef;
  const mtu = intIn(raw.mtu, 1280, 9216);
  if (mtu === false) return { error: "spec.mtu must be an integer from 1280 to 9216" };
  if (mtu !== undefined) spec.mtu = mtu;
  const viewers = stringList(raw.networkViewerRbac);
  if (viewers === false) return { error: "spec.networkViewerRbac must be a list of access group names" };
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

/** The environment kinds a network can be placed on (the CRD's spec.backend.kind enum). */
export const PLACEMENT_BACKEND_KINDS = ["CloudOSO", "CloudVirt", "CloudAWS", "CloudGCP"];
/** NetworkPlacement spec.ipv6.source (the CRD's enum). */
export const PLACEMENT_IPV6_SOURCES = ["Network", "Provider"];

export function sanitizeNetworkPlacementSpec(raw) {
  if (!isObject(raw)) return { error: "spec must be an object" };
  const network = typeof raw.network === "string" ? raw.network.trim() : "";
  if (!network || !NAME_PATTERN.test(network)) return { error: "spec.network (a network name) is required" };
  const backend = raw.backend;
  if (!isObject(backend) || !PLACEMENT_BACKEND_KINDS.includes(backend.kind)) {
    return { error: `spec.backend.kind must be one of ${PLACEMENT_BACKEND_KINDS.join(", ")}` };
  }
  const backendName = typeof backend.name === "string" ? backend.name.trim() : "";
  if (!backendName || !NAME_PATTERN.test(backendName)) return { error: "spec.backend.name is required" };
  const spec = { network, backend: { kind: backend.kind, name: backendName } };
  // a placement's subnet per family: a size and/or a pinned prefix; IPv6 also a source (the API checks all)
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
    if (family === "ipv6" && req.source != null) {
      if (!PLACEMENT_IPV6_SOURCES.includes(req.source)) {
        return { error: `spec.ipv6.source must be one of ${PLACEMENT_IPV6_SOURCES.join(", ")}` };
      }
      out.source = req.source;
    }
    // ipv4 always needs a size or a pin; ipv6 may be empty on a public-cloud site (the site's default applies)
    if (family === "ipv4" && !Object.keys(out).length) return { error: "spec.ipv4 needs prefixLength or prefix" };
    spec[family] = out;
  }
  if (!spec.ipv4 && !spec.ipv6) return { error: "the placement needs spec.ipv4, spec.ipv6 or both" };
  return { spec };
}

/* ------------------------------------------------------------------ edits */

function getPath(obj, dotted) {
  return dotted.split(".").reduce((o, k) => (isObject(o) ? o[k] : undefined), obj);
}

function setPath(obj, dotted, value) {
  const parts = dotted.split(".");
  let node = obj;
  for (const p of parts.slice(0, -1)) {
    if (!isObject(node[p])) node[p] = {};
    node = node[p];
  }
  node[parts[parts.length - 1]] = value;
}

/**
 * An edit replaces the whole spec: copy the stored platform-only fields into
 * the new spec so a tenant edit neither removes nor changes them.
 */
export function keepPlatformOnlyFields(kind, cleanSpec, currentSpec) {
  const fields = PLATFORM_ONLY_FIELDS[kind] || [];
  const out = JSON.parse(JSON.stringify(cleanSpec || {}));
  for (const f of fields) {
    const stored = getPath(currentSpec, f);
    if (stored === undefined) continue;
    const parent = f.includes(".") ? f.split(".").slice(0, -1).join(".") : null;
    // Only where the edited spec still has the parent block (an openstack block removed stays removed).
    if (parent && !isObject(getPath(out, parent))) continue;
    setPath(out, f, stored);
  }
  return out;
}

/** Sanitizer per kind, for the drift test and generic callers. */
export const SANITIZERS = {
  Rbac: sanitizeRbacSpec,
  Vault: sanitizeVaultSpec,
  VaultKV: sanitizeVaultKVSpec,
  AAPOrg: sanitizeAAPOrgSpec,
  QuayOrg: sanitizeQuayOrgSpec,
  Team: sanitizeTeamSpec,
  Project: sanitizeProjectSpec,
  Assignment: sanitizeAssignmentSpec,
  PlatformOpenshift: sanitizePlatformOpenshiftSpec,
  CloudOSO: sanitizeCloudOSOSpec,
  CloudAWS: sanitizeCloudAWSSpec,
  CloudGCP: sanitizeCloudGCPSpec,
  CloudVirt: sanitizeCloudVirtSpec,
  HybridNetwork: sanitizeHybridNetworkSpec,
  NetworkPlacement: sanitizeNetworkPlacementSpec,
};
