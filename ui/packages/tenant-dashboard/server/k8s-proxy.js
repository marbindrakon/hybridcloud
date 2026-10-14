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
const NS_PATTERN = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/;
const CONFIG_PATTERN = /^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$/;

const ENTITY_NS_LABEL_SELECTOR = "hybridsovereign.redhat/entity";

const PLUGINS_NS = "sovereign-cloud-plugins";
const RECONCILE_ANNOTATION = "ansible.sdk.operatorframework.io/reconcileNow";
/**
 * spec.cloudRef of a tenant cloud project: a CloudInfrastructure in sovereign-cloud. Returns the
 * sanitized reference, null when absent, or false when present but invalid.
 */
function sanitizeCloudRef(raw) {
  if (raw == null) return null;
  if (typeof raw !== "object" || typeof raw.name !== "string") return false;
  const name = raw.name.trim();
  if (!name || name.length > 253 || !NAME_PATTERN.test(name)) return false;
  if (raw.kind != null && raw.kind !== "CloudInfrastructure") return false;
  return { kind: "CloudInfrastructure", name };
}

const KIND_PLURALS = {
  Entity: "entities",
  Team: "teams",
  Assignment: "assignments",
  Project: "projects",
  PlatformOpenshift: "platformopenshifts",
  CloudOSO: "cloudosos",
  CloudAWS: "cloudawss",
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

  function cloudvirtsCollectionPath(namespace) {
    return `/apis/hybridsovereign.redhat/v1alpha1/namespaces/${encodeURIComponent(namespace)}/cloudvirts`;
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
          ...(Array.isArray(spec?.teamAdmin) && spec.teamAdmin.length ? { teamAdmin: spec.teamAdmin } : {}),
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
      if (!spec || typeof spec !== "object") {
        return res.status(400).json({ message: "spec is required" });
      }
      const { type } = spec;
      if (type !== "aws" && type !== "openstack" && type !== "hosted") {
        return res.status(400).json({ message: "spec.type must be aws, openstack, or hosted" });
      }

      function toPositiveInt(raw, fallback) {
        const n = typeof raw === "number" ? raw : Number.parseInt(String(raw), 10);
        return Number.isFinite(n) && n >= 1 ? n : fallback;
      }

      function sanitizeToolRbac(raw) {
        if (!raw || typeof raw !== "object") return undefined;
        const out = {};
        for (const key of [
          "clusterAdminRbac",
          "clusterOperatorRbac",
          "clusterDeveloperRbac",
          "clusterViewerRbac",
        ]) {
          const list = raw[key];
          if (!Array.isArray(list)) continue;
          const cleaned = list
            .filter((v) => typeof v === "string")
            .map((v) => v.trim())
            .filter(Boolean);
          if (cleaned.length) out[key] = cleaned;
        }
        return Object.keys(out).length ? out : undefined;
      }

      let platformSpec;
      if (type === "aws") {
        const aws = spec.aws;
        if (!aws || typeof aws.environment !== "string" || !aws.environment.trim()) {
          return res.status(400).json({ message: "spec.aws.environment is required" });
        }
        const clusterType =
          aws.clusterType === "ha" || aws.clusterType === "standalone"
            ? aws.clusterType
            : "standalone";
        platformSpec = {
          type: "aws",
          aws: {
            environment: aws.environment.trim(),
            region:
              typeof aws.region === "string" && aws.region.trim()
                ? aws.region.trim()
                : "us-east-1",
            clusterType,
            controlPlaneCount: toPositiveInt(aws.controlPlaneCount, 3),
            workerCount: toPositiveInt(aws.workerCount, 2),
            controllerFlavor:
              typeof aws.controllerFlavor === "string" && aws.controllerFlavor.trim()
                ? aws.controllerFlavor.trim()
                : "m5.xlarge",
            workerFlavor:
              typeof aws.workerFlavor === "string" && aws.workerFlavor.trim()
                ? aws.workerFlavor.trim()
                : "m5.large",
          },
        };
      } else if (type === "hosted") {
        const hosted = spec.hosted;
        if (!hosted || typeof hosted.environment !== "string" || !hosted.environment.trim()) {
          return res.status(400).json({ message: "spec.hosted.environment is required" });
        }
        platformSpec = {
          type: "hosted",
          hosted: {
            environment: hosted.environment.trim(),
            nodePoolReplicas: toPositiveInt(hosted.nodePoolReplicas, 2),
          },
        };
      } else {
        const os = spec.openstack;
        if (!os || typeof os.environment !== "string" || !os.environment.trim()) {
          return res.status(400).json({ message: "spec.openstack.environment is required" });
        }
        platformSpec = {
          type: "openstack",
          openstack: {
            environment: os.environment.trim(),
            controlPlaneCount: toPositiveInt(os.controlPlaneCount, 3),
            workerCount: toPositiveInt(os.workerCount, 3),
            controlPlaneFlavor:
              typeof os.controlPlaneFlavor === "string" && os.controlPlaneFlavor.trim()
                ? os.controlPlaneFlavor.trim()
                : "m1.xlarge",
            workerFlavor:
              typeof os.workerFlavor === "string" && os.workerFlavor.trim()
                ? os.workerFlavor.trim()
                : "m1.large",
            externalNetwork:
              typeof os.externalNetwork === "string" && os.externalNetwork.trim()
                ? os.externalNetwork.trim()
                : "ext-net",
          },
        };
      }

      const toolRbac = sanitizeToolRbac(spec.toolRbac);
      if (toolRbac) platformSpec.toolRbac = toolRbac;

      // Explicit address plan; omitted CIDRs are allocated from the CloudInfrastructure defaults.
      // Clusters do not join a fabric: spec.fabric and networking.allocateFromFabric/fabricRef are dropped.
      if (spec.networking != null) {
        if (typeof spec.networking !== "object") {
          return res.status(400).json({ message: "spec.networking must be an object" });
        }
        const networking = {};
        for (const key of ["clusterNetwork", "serviceNetwork"]) {
          const list = spec.networking[key];
          if (list == null) continue;
          if (!Array.isArray(list) || !list.every((v) => typeof v === "string")) {
            return res.status(400).json({ message: `spec.networking.${key} must be a list of CIDRs` });
          }
          const cleaned = list.map((v) => v.trim()).filter(Boolean);
          if (cleaned.length) networking[key] = cleaned;
        }
        if (typeof spec.networking.allowConflict === "boolean") {
          networking.allowConflict = spec.networking.allowConflict;
        }
        if (Object.keys(networking).length) platformSpec.networking = networking;
      }

      const platform = {
        apiVersion: "hybridsovereign.redhat/v1alpha1",
        kind: "PlatformOpenshift",
        metadata: { name, namespace },
        spec: platformSpec,
      };
      try {
        const resp = await k8sRequest(
          apiServer,
          platformopenshiftsCollectionPath(namespace),
          "POST",
          token,
          platform,
        );
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
      try {
        const urlPath = `${platformopenshiftsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const patch = [{ op: "merge", path: "/spec", value: spec }];
        const resp = await k8sRequest(apiServer, urlPath, "PATCH", token, { spec }, "application/merge-patch+json");
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
      if (!spec || typeof spec !== "object") {
        return res.status(400).json({ message: "spec is required" });
      }
      const {
        project,
        baseDomain,
        projectDomain,
        externalNetwork,
        route53VaultPath,
        landingzone,
        designateZoneId,
        designateProjectId,
      } = spec;
      // Credentials and site settings come from the CloudInfrastructure named by spec.cloudRef.
      const cloudRef = sanitizeCloudRef(spec.cloudRef);
      if (!cloudRef) {
        return res.status(400).json({ message: "spec.cloudRef.name (a CloudInfrastructure of type openstack) is required" });
      }
      if (!project || typeof project !== "string" || !project.trim()) {
        return res.status(400).json({ message: "spec.project is required" });
      }
      if (!baseDomain || typeof baseDomain !== "string" || !baseDomain.trim()) {
        return res.status(400).json({ message: "spec.baseDomain is required" });
      }
      // Optional: unset values fall back to the CloudInfrastructure.
      const optional = (key, value) =>
        value && typeof value === "string" && value.trim() ? { [key]: value.trim() } : {};
      const cloudoso = {
        apiVersion: "hybridsovereign.redhat/v1alpha1",
        kind: "CloudOSO",
        metadata: { name, namespace },
        spec: {
          cloudRef,
          project: project.trim(),
          baseDomain: baseDomain.trim(),
          ...optional("projectDomain", projectDomain),
          ...optional("externalNetwork", externalNetwork),
          ...optional("route53VaultPath", route53VaultPath),
          ...optional("landingzone", landingzone),
          ...optional("designateZoneId", designateZoneId),
          ...optional("designateProjectId", designateProjectId),
        },
      };
      try {
        const resp = await k8sRequest(apiServer, cloudososCollectionPath(namespace), "POST", token, cloudoso);
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
      if (spec.enableVRF === true && (!spec.vrfId || typeof spec.vrfId !== "string" || !spec.vrfId.trim())) {
        return res.status(400).json({ message: "spec.vrfId is required when enableVRF is true" });
      }
      try {
        const urlPath = `${cloudososCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const patch = [{ op: "replace", path: "/spec", value: spec }];
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
      // With spec.cloudRef the credentials come from the CloudInfrastructure (type aws).
      const cloudRef = sanitizeCloudRef(spec?.cloudRef);
      if (cloudRef === false) {
        return res.status(400).json({ message: "spec.cloudRef.name is invalid" });
      }
      const hasCredRef =
        spec?.credentialsSecretRef &&
        typeof spec.credentialsSecretRef === "object" &&
        typeof spec.credentialsSecretRef.name === "string" &&
        spec.credentialsSecretRef.name.trim();
      const hasVault = spec?.vaultPath && typeof spec.vaultPath === "string" && spec.vaultPath.trim();
      if (!spec?.account || !spec?.baseDomain || (!cloudRef && !hasCredRef && !hasVault)) {
        return res.status(400).json({
          message:
            "spec.account, spec.baseDomain, and spec.cloudRef.name (or credentialsSecretRef.name / vaultPath) are required",
        });
      }
      const cloudaws = {
        apiVersion: "hybridsovereign.redhat/v1alpha1",
        kind: "CloudAWS",
        metadata: { name, namespace },
        spec: {
          account: spec.account,
          baseDomain: spec.baseDomain,
          ...(cloudRef
            ? { cloudRef }
            : hasCredRef
              ? { credentialsSecretRef: { name: spec.credentialsSecretRef.name.trim() } }
              : { vaultPath: spec.vaultPath }),
          ...(spec?.landingzone ? { landingzone: spec.landingzone } : {}),
        },
      };
      try {
        const resp = await k8sRequest(apiServer, cloudawssCollectionPath(namespace), "POST", token, cloudaws);
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
      try {
        const urlPath = `${cloudawssCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const patch = [{ op: "replace", path: "/spec", value: spec }];
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
      // Credentials and site settings come from the CloudInfrastructure named by spec.cloudRef.
      const cloudRef = sanitizeCloudRef(spec?.cloudRef);
      if (!cloudRef || !spec?.baseDomain) {
        return res.status(400).json({
          message: "spec.cloudRef.name (a CloudInfrastructure of type openshift) and spec.baseDomain are required",
        });
      }
      const quotaHard = spec?.vmNamespaceQuota?.hard;
      if (quotaHard != null && (typeof quotaHard !== "object" || Array.isArray(quotaHard))) {
        return res.status(400).json({ message: "spec.vmNamespaceQuota.hard must be an object" });
      }
      const cloudvirt = {
        apiVersion: "hybridsovereign.redhat/v1alpha1",
        kind: "CloudVirt",
        metadata: { name, namespace },
        spec: {
          cloudRef,
          baseDomain: spec.baseDomain,
          ...(spec?.storageClass ? { storageClass: spec.storageClass } : {}),
          ...(spec?.networkAttachment ? { networkAttachment: spec.networkAttachment } : {}),
          ...(quotaHard ? { vmNamespaceQuota: { hard: quotaHard } } : {}),
        },
      };
      try {
        const resp = await k8sRequest(apiServer, cloudvirtsCollectionPath(namespace), "POST", token, cloudvirt);
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
      try {
        const urlPath = `${cloudvirtsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const patch = [{ op: "replace", path: "/spec", value: spec }];
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
            ...(Array.isArray(spec?.teamAdmin) ? { teamAdmin: spec.teamAdmin } : {}),
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
        { resource: "cloudvirts",          group: "hybridsovereign.redhat" },
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
