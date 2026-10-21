import { k8sRequest as upstreamRequest, userToken } from "../../shared/server/upstream.js";
import { sendUpstreamError } from "../../shared/server/errors.js";
import {
  NAME_PATTERN,
  NS_PATTERN,
  CONFIG_PATTERN,
  TEAM_RBAC_KEYS,
  VAULTKV_ROLE_KEYS,
  AAPORG_ROLE_KEYS,
  QUAYORG_ROLE_KEYS,
  keepPlatformOnlyFields,
  sanitizeAAPOrgSpec,
  sanitizeAssignmentSpec,
  sanitizeCloudAWSSpec,
  sanitizeCloudGCPSpec,
  sanitizeCloudOSOSpec,
  sanitizeCloudVirtSpec,
  sanitizeHybridNetworkSpec,
  sanitizeNetworkPlacementSpec,
  sanitizePlatformOpenshiftSpec,
  sanitizeProjectSpec,
  sanitizeQuayOrgSpec,
  sanitizeRbacSpec,
  sanitizeVaultKVSpec,
  sanitizeVaultSpec,
} from "./sanitizers.js";
import { recordUpstreamWarnings, upstreamWriteQuery } from "./writeOptions.js";

function userAccessToken(req) {
  return userToken(req);
}

/** Positional form kept for the curated handlers below; see shared/server/upstream.js. */
function k8sRequest(apiServer, reqPath, method, bearerToken, body, contentType) {
  // Writes to the platform API carry fieldValidation=Strict (and dryRun=All for a dry-run request);
  // upstream Warning headers are passed back to the browser (writeOptions.js).
  const query = upstreamWriteQuery(method, reqPath);
  return upstreamRequest({ apiServer, path: reqPath, method, token: bearerToken, body, contentType, query }).then((resp) => {
    recordUpstreamWarnings(resp?.warnings);
    return resp;
  });
}

const ENTITY_NS_LABEL_SELECTOR = "hybridsovereign.redhat/entity";

const PLUGINS_NS = "sovereign-cloud-plugins";
const RECONCILE_ANNOTATION = "ansible.sdk.operatorframework.io/reconcileNow";

function teamRbacList(v) {
  return Array.isArray(v) ? [...new Set(v.filter((x) => typeof x === "string" && NAME_PATTERN.test(x)))] : undefined;
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

  const ENTITIES_PATH = "/apis/hybridsovereign.redhat/v1alpha1/entities";

  return {
    async listEntities(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      try {
        const resp = await k8sRequest(apiServer, ENTITIES_PATH, "GET", token);
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("Entity list error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) {
          console.error("listNamespaces: K8s API returned", resp.status, JSON.stringify(resp.body).slice(0, 200));
          return sendUpstreamError(res, resp);
        }
        const items = Array.isArray(resp.body?.items) ? resp.body.items : [];
        const names = items
          .map((item) => item?.metadata?.name)
          .filter((n) => typeof n === "string" && n.length > 0);
        res.json(names.map((name) => ({ name })));
      } catch (err) {
        console.error("Namespace list error:", err.message);
        sendUpstreamError(res, err);
      }
    },

    async listRbacConfigs(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      try {
        const resp = await k8sRequest(apiServer, RBAC_CONFIGS_PATH, "GET", token);
        if (resp.status >= 400) return sendUpstreamError(res, resp);
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
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("Rbac list error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        const items = Array.isArray(resp.body?.items) ? resp.body.items : [];
        const names = items
          .map((i) => i.metadata?.name)
          .filter((n) => typeof n === "string" && n.toLowerCase().includes(q));
        res.json(names.slice(0, 15));
      } catch (err) {
        console.error("Rbac search error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("Rbac get error:", err.message);
        sendUpstreamError(res, err);
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
      // spec.config is optional: the platform uses its default identity provider connection.
      const clean = sanitizeRbacSpec(spec);
      if (clean.error) return res.status(400).json({ message: clean.error });
      const rbacSpec = clean.spec;
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("Rbac create error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json({ message: `Rbac '${name}' deleted` });
      } catch (err) {
        console.error("Rbac delete error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("Vault list error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("Team list error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("Assignment list error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("Assignment get error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("Project list error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("PlatformOpenshift list error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("CloudOSO list error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("Team create error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json({ message: `Team '${name}' deleted` });
      } catch (err) {
        console.error("Team delete error:", err.message);
        sendUpstreamError(res, err);
      }
    },

    async createProject(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name, namespace, spec } = req.body || {};
      if (!name || !namespace) return res.status(400).json({ message: "name and namespace required" });
      if (!NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      // All three access lists are written (the interim form of a role assignment on the project).
      const clean = sanitizeProjectSpec(spec);
      if (clean.error) return res.status(400).json({ message: clean.error });
      const project = {
        apiVersion: "hybridsovereign.redhat/v1alpha1",
        kind: "Project",
        metadata: { name, namespace },
        spec: clean.spec,
      };
      try {
        const resp = await k8sRequest(apiServer, projectsCollectionPath(namespace), "POST", token, project);
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("Project create error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json({ message: `Project '${name}' deleted` });
      } catch (err) {
        console.error("Project delete error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("Project get error:", err.message);
        sendUpstreamError(res, err);
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
        const clean = sanitizeProjectSpec(spec, { update: true });
        if (clean.error) return res.status(400).json({ message: clean.error });
        const resp = await k8sRequest(apiServer, urlPath, "PATCH", token, { spec: clean.spec }, "application/merge-patch+json");
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("Project patch error:", err.message);
        sendUpstreamError(res, err);
      }
    },

    async createAssignment(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name, namespace, spec } = req.body || {};
      if (!name || !namespace) return res.status(400).json({ message: "name and namespace required" });
      if (!NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      const clean = sanitizeAssignmentSpec(spec || {});
      if (clean.error) return res.status(400).json({ message: clean.error });
      const assignment = {
        apiVersion: "hybridsovereign.redhat/v1alpha1",
        kind: "Assignment",
        metadata: { name, namespace },
        spec: clean.spec,
      };
      try {
        const resp = await k8sRequest(apiServer, assignmentsCollectionPath(namespace), "POST", token, assignment);
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("Assignment create error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json({ message: `Assignment '${name}' deleted` });
      } catch (err) {
        console.error("Assignment delete error:", err.message);
        sendUpstreamError(res, err);
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
        const clean = sanitizeAssignmentSpec(spec || {});
        if (clean.error) return res.status(400).json({ message: clean.error });
        const getPath = `${assignmentsCollectionPath(namespace)}/${encodeURIComponent(name)}`;
        const getCurrent = await k8sRequest(apiServer, getPath, "GET", token);
        if (getCurrent.status >= 400) return sendUpstreamError(res, getCurrent);

        const updated = {
          apiVersion: "hybridsovereign.redhat/v1alpha1",
          kind: "Assignment",
          metadata: {
            name,
            namespace,
            resourceVersion: getCurrent.body.metadata.resourceVersion,
          },
          // The edit sends the whole spec, toolRbac included (it was dropped before).
          spec: clean.spec,
        };
        const resp = await k8sRequest(apiServer, getPath, "PUT", token, updated);
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("Assignment update error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("PlatformOpenshift create error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json({ message: `PlatformOpenshift '${name}' deleted` });
      } catch (err) {
        console.error("PlatformOpenshift delete error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("PlatformOpenshift get error:", err.message);
        sendUpstreamError(res, err);
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
        // The editor sends the whole spec: replace it (fields left out are removed), except the
        // platform-only fields, which keep their stored values.
        const current = await k8sRequest(apiServer, urlPath, "GET", token);
        if (current.status >= 400) return sendUpstreamError(res, current);
        const value = keepPlatformOnlyFields("PlatformOpenshift", clean.spec, current.body?.spec);
        const patch = [
          { op: "test", path: "/metadata/resourceVersion", value: current.body?.metadata?.resourceVersion },
          { op: "replace", path: "/spec", value },
        ];
        const resp = await k8sRequest(apiServer, urlPath, "PATCH", token, patch, "application/json-patch+json");
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("PlatformOpenshift patch error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("CloudOSO create error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json({ message: `CloudOSO '${name}' deleted` });
      } catch (err) {
        console.error("CloudOSO delete error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("CloudOSO get error:", err.message);
        sendUpstreamError(res, err);
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
        // The editor sends the whole spec: replace it (fields left out are removed), except the
        // platform-only fields, which keep their stored values.
        const current = await k8sRequest(apiServer, urlPath, "GET", token);
        if (current.status >= 400) return sendUpstreamError(res, current);
        const value = keepPlatformOnlyFields("CloudOSO", clean.spec, current.body?.spec);
        const patch = [
          { op: "test", path: "/metadata/resourceVersion", value: current.body?.metadata?.resourceVersion },
          { op: "replace", path: "/spec", value },
        ];
        const resp = await k8sRequest(apiServer, urlPath, "PATCH", token, patch, "application/json-patch+json");
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("CloudOSO patch error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        const raw = resp.body?.data?.["catalog.json"] || "{}";
        try {
          res.json(JSON.parse(raw));
        } catch {
          res.json({ providers: [], vms: {} });
        }
      } catch (err) {
        console.error("MTV catalog error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("OpenStackMigration list error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("OpenStackMigration create error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json({ message: `OpenStackMigration '${name}' deleted` });
      } catch (err) {
        console.error("OpenStackMigration delete error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("OpenStackMigration get error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("CloudAWS list error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("CloudAWS create error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("CloudAWS get error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("CloudAWS patch error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json({ message: `CloudAWS '${name}' deleted` });
      } catch (err) {
        console.error("CloudAWS delete error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("CloudGCP list error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("CloudGCP create error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("CloudGCP get error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("CloudGCP patch error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json({ message: `CloudGCP '${name}' deleted` });
      } catch (err) {
        console.error("CloudGCP delete error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("CloudVirt list error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("CloudVirt create error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("CloudVirt get error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("CloudVirt patch error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json({ message: `CloudVirt '${name}' deleted` });
      } catch (err) {
        console.error("CloudVirt delete error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("HybridNetwork list error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("HybridNetwork get error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("HybridNetwork create error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("HybridNetwork patch error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json({ message: `HybridNetwork '${name}' deleted` });
      } catch (err) {
        console.error("HybridNetwork delete error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("NetworkPlacement list error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("NetworkPlacement get error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("NetworkPlacement create error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json({ message: `NetworkPlacement '${name}' deleted` });
      } catch (err) {
        console.error("NetworkPlacement delete error:", err.message);
        sendUpstreamError(res, err);
      }
    },

    async createVault(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name, namespace, spec } = req.body || {};
      if (!name || !namespace || !spec) return res.status(400).json({ message: "name, namespace, spec required" });
      if (!NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      const clean = sanitizeVaultSpec(spec);
      if (clean.error) return res.status(400).json({ message: clean.error });
      const vault = {
        apiVersion: "hybridsovereign.redhat/v1alpha1",
        kind: "Vault",
        metadata: { name, namespace },
        spec: clean.spec,
      };
      try {
        const resp = await k8sRequest(apiServer, vaultsCollectionPath(namespace), "POST", token, vault);
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("Vault create error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json({ message: `Vault '${name}' deleted` });
      } catch (err) {
        console.error("Vault delete error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("Vault get error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("VaultKV list error:", err.message);
        sendUpstreamError(res, err);
      }
    },

    async createVaultKV(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const { name, namespace, spec } = req.body || {};
      if (!name || !namespace || !spec) return res.status(400).json({ message: "name, namespace, spec required" });
      if (!NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid name" });
      if (!NS_PATTERN.test(namespace)) return res.status(400).json({ message: "Invalid namespace" });
      const clean = sanitizeVaultKVSpec(spec);
      if (clean.error) return res.status(400).json({ message: clean.error });
      const vaultKV = {
        apiVersion: "hybridsovereign.redhat/v1alpha1",
        kind: "VaultKV",
        metadata: { name, namespace },
        spec: clean.spec,
      };
      try {
        const resp = await k8sRequest(apiServer, vaultKVsCollectionPath(namespace), "POST", token, vaultKV);
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("VaultKV create error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json({ message: `VaultKV '${name}' deleted` });
      } catch (err) {
        console.error("VaultKV delete error:", err.message);
        sendUpstreamError(res, err);
      }
    },

    async listAAPConfigs(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      try {
        const resp = await k8sRequest(apiServer, AAP_CONFIGS_PATH, "GET", token);
        if (resp.status >= 400) return sendUpstreamError(res, resp);
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
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("AAPOrg list error:", err.message);
        sendUpstreamError(res, err);
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
      // spec.aapConfig is optional: the platform uses its default automation connection.
      const clean = sanitizeAAPOrgSpec(spec);
      if (clean.error) return res.status(400).json({ message: clean.error });
      const aapOrg = {
        apiVersion: "hybridsovereign.redhat/v1alpha1",
        kind: "AAPOrg",
        metadata: { name, namespace },
        spec: clean.spec,
      };
      try {
        const resp = await k8sRequest(apiServer, aaporgsCollectionPath(namespace), "POST", token, aapOrg);
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("AAPOrg create error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json({ message: `AAPOrg '${name}' deleted` });
      } catch (err) {
        console.error("AAPOrg delete error:", err.message);
        sendUpstreamError(res, err);
      }
    },

    async listQuayConfigs(req, res) {
      const token = userAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      try {
        const resp = await k8sRequest(apiServer, QUAY_CONFIGS_PATH, "GET", token);
        if (resp.status >= 400) return sendUpstreamError(res, resp);
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
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("QuayOrg list error:", err.message);
        sendUpstreamError(res, err);
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
      // spec.quayConfig is optional: the platform uses its default registry connection.
      const clean = sanitizeQuayOrgSpec(spec);
      if (clean.error) return res.status(400).json({ message: clean.error });
      const quayOrg = {
        apiVersion: "hybridsovereign.redhat/v1alpha1",
        kind: "QuayOrg",
        metadata: { name, namespace },
        spec: clean.spec,
      };
      try {
        const resp = await k8sRequest(apiServer, quayorgsCollectionPath(namespace), "POST", token, quayOrg);
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("QuayOrg create error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json({ message: `QuayOrg '${name}' deleted` });
      } catch (err) {
        console.error("QuayOrg delete error:", err.message);
        sendUpstreamError(res, err);
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
        if (getCurrent.status >= 400) return sendUpstreamError(res, getCurrent);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("Team update error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("Team get error:", err.message);
        sendUpstreamError(res, err);
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
        if (getCurrent.status >= 400) return sendUpstreamError(res, getCurrent);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("Rbac update error:", err.message);
        sendUpstreamError(res, err);
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
        if (getCurrent.status >= 400) return sendUpstreamError(res, getCurrent);
        const current = getCurrent.body;
        // A role list in the request replaces the stored one (empty clears it); an absent list is kept.
        const clean = sanitizeVaultKVSpec(spec || {}, { update: true });
        if (clean.error) return res.status(400).json({ message: clean.error });
        const updated = {
          ...current,
          metadata: { ...current.metadata },
          spec: { ...current.spec, ...clean.spec },
        };
        for (const k of VAULTKV_ROLE_KEYS) if (Array.isArray(updated.spec[k]) && !updated.spec[k].length) delete updated.spec[k];
        const resp = await k8sRequest(apiServer, urlPath, "PUT", token, updated);
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("VaultKV update error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("VaultKV get error:", err.message);
        sendUpstreamError(res, err);
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
        if (getCurrent.status >= 400) return sendUpstreamError(res, getCurrent);
        const current = getCurrent.body;
        const clean = sanitizeAAPOrgSpec(spec || {}, { update: true });
        if (clean.error) return res.status(400).json({ message: clean.error });
        const updated = {
          ...current,
          metadata: { ...current.metadata },
          spec: { ...current.spec, ...clean.spec },
        };
        for (const k of AAPORG_ROLE_KEYS) if (Array.isArray(updated.spec[k]) && !updated.spec[k].length) delete updated.spec[k];
        const resp = await k8sRequest(apiServer, urlPath, "PUT", token, updated);
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("AAPOrg update error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("AAPOrg get error:", err.message);
        sendUpstreamError(res, err);
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
        if (getCurrent.status >= 400) return sendUpstreamError(res, getCurrent);
        const current = getCurrent.body;
        const clean = sanitizeQuayOrgSpec(spec || {}, { update: true });
        if (clean.error) return res.status(400).json({ message: clean.error });
        const updated = {
          ...current,
          metadata: { ...current.metadata },
          spec: { ...current.spec, ...clean.spec },
        };
        for (const k of QUAYORG_ROLE_KEYS) if (Array.isArray(updated.spec[k]) && !updated.spec[k].length) delete updated.spec[k];
        const resp = await k8sRequest(apiServer, urlPath, "PUT", token, updated);
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("QuayOrg update error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("QuayOrg get error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("Persona list error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("Persona get error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("Persona create error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp);
        res.json(resp.body);
      } catch (err) {
        console.error("Persona annotation patch error:", err.message);
        sendUpstreamError(res, err);
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
        if (resp.status >= 400) return sendUpstreamError(res, resp, { kind, verb: "patch", namespace, name });
        res.json({ message: "Annotation patched", resource: resp.body });
      } catch (err) {
        console.error("patchAnnotation error:", err.message);
        sendUpstreamError(res, err);
      }
    },

  };
}
