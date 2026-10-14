import https from "https";
import fs from "fs";

const SA_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt";
const SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token";

const CENTRAL_SECRET_DIR = "/etc/central-reader";
const CENTRAL_TOKEN_PATH = `${CENTRAL_SECRET_DIR}/token`;
const CENTRAL_CA_PATH = `${CENTRAL_SECRET_DIR}/ca.crt`;
const CENTRAL_API_PATH = `${CENTRAL_SECRET_DIR}/api_server`;

function readFileOrNull(p) {
  try { return fs.readFileSync(p, "utf-8").trim(); } catch { return null; }
}

function getSAToken() { return readFileOrNull(SA_TOKEN_PATH); }
function getCentralToken() { return readFileOrNull(CENTRAL_TOKEN_PATH); }
function getCentralApiServer() { return readFileOrNull(CENTRAL_API_PATH) || process.env.CENTRAL_API_SERVER || ""; }

let cachedCA, caLoaded = false;
function getCACert() {
  if (caLoaded) return cachedCA;
  try { cachedCA = fs.readFileSync(SA_CA_PATH); } catch { cachedCA = undefined; }
  caLoaded = true;
  return cachedCA;
}

let centralCA, centralCALoaded = false;
function getCentralCACert() {
  if (centralCALoaded) return centralCA;
  try { centralCA = fs.readFileSync(CENTRAL_CA_PATH); } catch { centralCA = undefined; }
  centralCALoaded = true;
  return centralCA;
}

function getUserAccessToken(req) {
  const h = req.headers["x-forwarded-access-token"];
  return typeof h === "string" && h.trim().length ? h.trim() : null;
}

function centralTlsInsecure() {
  return process.env.CENTRAL_API_TLS_INSECURE !== "false";
}

function k8sRequest(apiServer, reqPath, method, token, body, customCA, contentType, { insecure = false } = {}) {
  return new Promise((resolve, reject) => {
    const url = new URL(reqPath, apiServer);
    const ca = insecure ? undefined : (customCA || getCACert());
    const opts = {
      hostname: url.hostname,
      port: url.port || 443,
      path: url.pathname + url.search,
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/json",
        "Content-Type": contentType || "application/json",
      },
      rejectUnauthorized: !insecure && !!ca,
      ca,
      timeout: 15000,
    };
    const req = https.request(opts, (res) => {
      let data = "";
      res.on("data", (c) => (data += c));
      res.on("end", () => {
        try { resolve({ status: res.statusCode, body: JSON.parse(data) }); }
        catch { resolve({ status: res.statusCode, body: data }); }
      });
    });
    req.on("timeout", () => { req.destroy(); reject(new Error("K8s API request timed out")); });
    req.on("error", reject);
    if (body) req.write(JSON.stringify(body));
    req.end();
  });
}

const EXCLUDED_NS = /^(openshift-|kube-|default$)/;

const HS_KINDS = [
  { path: "/apis/hybridsovereign.redhat/v1alpha1/entities", kind: "Entity" },
  { path: "/apis/hybridsovereign.redhat/v1alpha1/teams", kind: "Team" },
  { path: "/apis/hybridsovereign.redhat/v1alpha1/assignments", kind: "Assignment" },
  { path: "/apis/hybridsovereign.redhat/v1alpha1/projects", kind: "Project" },
  { path: "/apis/hybridsovereign.redhat/v1alpha1/platformopenshifts", kind: "PlatformOpenshift" },
  { path: "/apis/hybridsovereign.redhat/v1alpha1/cloudosos", kind: "CloudOSO" },
  { path: "/apis/hybridsovereign.redhat/v1alpha1/cloudawss", kind: "CloudAWS" },
  { path: "/apis/hybridsovereign.redhat/v1alpha1/cloudvirts", kind: "CloudVirt" },
  { path: "/apis/hybridsovereign.redhat/v1alpha1/rbacs", kind: "Rbac" },
  { path: "/apis/hybridsovereign.redhat/v1alpha1/vaultkvs", kind: "VaultKV" },
  { path: "/apis/hybridsovereign.redhat/v1alpha1/aaporgs", kind: "AAPOrg" },
  { path: "/apis/hybridsovereign.redhat/v1alpha1/quayorgs", kind: "QuayOrg" },
  { path: "/apis/hybridsovereign.redhat/v1alpha1/iaacs", kind: "Iaac" },
  { path: "/apis/hybridsovereign.redhat/v1alpha1/personas", kind: "Persona" },
  { path: "/apis/hybridsovereign.redhat/v1alpha1/cloudinfrastructures", kind: "CloudInfrastructure" },
  { path: "/apis/hybridsovereign.redhat/v1alpha1/hybridfabrics", kind: "HybridFabric" },
  { path: "/apis/hybridsovereign.redhat/v1alpha1/cloudgateways", kind: "CloudGateway" },
  { path: "/apis/hybridsovereign.redhat/v1alpha1/transportlinks", kind: "TransportLink" },
  { path: "/apis/hybridsovereign.redhat/v1alpha1/hybridnetworks", kind: "HybridNetwork" },
  { path: "/apis/hybridsovereign.redhat/v1alpha1/networkplacements", kind: "NetworkPlacement" },
  { path: "/apis/hybridsovereign.redhat/v1alpha1/uihealthcheckers", kind: "UIHealthChecker" },
];

const NAME_PATTERN = /^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$/;
const NS_PATTERN = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/;
const BILLING_PATTERN = /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$/;
const RECONCILE_ANNOTATION = "ansible.sdk.operatorframework.io/reconcileNow";
const KIND_PLURALS = {
  Entity: "entities",
  Team: "teams",
  Assignment: "assignments",
  Project: "projects",
  PlatformOpenshift: "platformopenshifts",
  CloudOSO: "cloudosos",
  CloudAWS: "cloudawss",
  CloudVirt: "cloudvirts",
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
  CloudInfrastructure: "cloudinfrastructures",
  HybridFabric: "hybridfabrics",
  CloudGateway: "cloudgateways",
  TransportLink: "transportlinks",
  HybridNetwork: "hybridnetworks",
  NetworkPlacement: "networkplacements",
  UIHealthChecker: "uihealthcheckers",
};

const PERSONA_TYPES = new Set([
  "assignmentAdmin", "auditor", "BobsTeam", "cloudOSOAdmin", "cloudOSOView", "cloudAWSAdmin", "cloudAWSView",
  "entityAdmin", "identityAdmin", "platformOpenshiftAdmin", "platformOpenshiftView",
  "teamAdmin", "teamView", "projectAdmin", "projectView",
]);

export function createK8sClient(apiServer, namespace) {
  const basePath = `/apis/hybridsovereign.redhat/v1alpha1/namespaces/${namespace}/entities`;

  async function fetchCRsFromCluster(server, token, ca) {
    const results = await Promise.allSettled(
      HS_KINDS.map(async (k) => {
        const resp = await k8sRequest(server, k.path, "GET", token, null, ca);
        if (resp.status >= 400) return [];
        return (resp.body?.items || []).map((item) => ({ ...item, kind: item.kind || k.kind }));
      }),
    );
    return results.flatMap((r) => (r.status === "fulfilled" ? r.value : []));
  }

  async function fetchRoutesFromCluster(server, token, clusterLabel, ca, { insecure = false } = {}) {
    try {
      const resp = await k8sRequest(server, "/apis/route.openshift.io/v1/routes", "GET", token, null, ca, null, { insecure });
      if (resp.status >= 400) return [];
      return (resp.body?.items || [])
        .filter((item) => {
          const ns = item.metadata?.namespace || "";
          if (EXCLUDED_NS.test(ns)) return false;
          const tls = item.spec?.tls;
          return tls?.termination != null && String(tls.termination).trim() !== "";
        })
        .map((item) => ({
          name: item.metadata?.name || "",
          namespace: item.metadata?.namespace || "",
          host: item.spec?.host || item.status?.ingress?.[0]?.host || "",
          tls: true,
          path: typeof item.spec?.path === "string" ? item.spec.path : "",
          cluster: clusterLabel,
        }))
        .filter((r) => r.host);
    } catch (err) {
      console.error(`Routes fetch error [${clusterLabel}]:`, err.message);
      return [];
    }
  }

  async function fetchDeploymentsFromCluster(server, token, clusterLabel, ca, namespaces, { insecure = false } = {}) {
    try {
      const operators = [];
      for (const { ns, category } of namespaces) {
        const resp = await k8sRequest(server, `/apis/apps/v1/namespaces/${ns}/deployments`, "GET", token, null, ca, null, { insecure });
        if (resp.status >= 400) continue;
        for (const dep of resp.body?.items || []) {
          const name = dep.metadata?.name || "";
          operators.push({
            name,
            namespace: ns,
            category,
            cluster: clusterLabel,
            ready: dep.status?.readyReplicas || 0,
            desired: dep.spec?.replicas || 1,
            available: dep.status?.availableReplicas || 0,
            updated: dep.status?.updatedReplicas || 0,
            image: dep.spec?.template?.spec?.containers?.[0]?.image || "",
            conditions: (dep.status?.conditions || []).map((c) => ({
              type: c.type,
              status: c.status,
              reason: c.reason || "",
              message: c.message || "",
              lastTransition: c.lastTransitionTime || "",
            })),
            creationTimestamp: dep.metadata?.creationTimestamp || "",
          });
        }
      }
      return operators;
    } catch (err) {
      console.error(`Operators fetch error [${clusterLabel}]:`, err.message);
      return [];
    }
  }

  return {
    async list(req, res) {
      const token = getUserAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      try {
        const resp = await k8sRequest(apiServer, basePath, "GET", token);
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to fetch entities from cluster" });
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("Entity list error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async get(req, res) {
      const { name } = req.params;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid entity name" });
      const token = getUserAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      try {
        const resp = await k8sRequest(apiServer, `${basePath}/${encodeURIComponent(name)}`, "GET", token);
        if (resp.status === 404) return res.status(404).json({ message: `Entity '${name}' not found` });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to fetch entity from cluster" });
        res.json(resp.body);
      } catch (err) {
        console.error("Entity get error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async create(req, res) {
      const { name, spec } = req.body;
      if (!name || !spec) return res.status(400).json({ message: "name and spec are required" });
      if (!NAME_PATTERN.test(name)) return res.status(400).json({ message: "Name must be lowercase alphanumeric with hyphens" });
      if (!spec.description || typeof spec.description !== "string" || spec.description.trim().length === 0) {
        return res.status(400).json({ message: "spec.description is required" });
      }
      if (!spec.billingID || !BILLING_PATTERN.test(spec.billingID)) {
        return res.status(400).json({ message: "spec.billingID is required and must match pattern [a-zA-Z0-9._-]{1,63}" });
      }
      if (spec.websiteLink !== undefined && typeof spec.websiteLink !== "string") {
        return res.status(400).json({ message: "spec.websiteLink must be a string" });
      }
      const token = getUserAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const entity = {
        apiVersion: "hybridsovereign.redhat/v1alpha1",
        kind: "Entity",
        metadata: { name, namespace },
        spec: {
          description: spec.description.trim(),
          billingID: spec.billingID,
          ...(spec.websiteLink ? { websiteLink: spec.websiteLink } : {}),
        },
      };
      try {
        const resp = await k8sRequest(apiServer, basePath, "POST", token, entity);
        if (resp.status === 409) return res.status(409).json({ message: `Entity '${name}' already exists` });
        if (resp.status >= 400) return res.status(502).json({ message: resp.body?.message || "Failed to create entity" });
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("Entity create error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async remove(req, res) {
      const { name } = req.params;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid entity name" });
      const token = getUserAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      try {
        const resp = await k8sRequest(apiServer, `${basePath}/${encodeURIComponent(name)}`, "DELETE", token);
        if (resp.status === 404) return res.status(404).json({ message: `Entity '${name}' not found` });
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to delete entity" });
        res.json({ message: `Entity '${name}' deleted` });
      } catch (err) {
        console.error("Entity delete error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async listRoutes(_req, res) {
      const saToken = getSAToken();
      if (!saToken) return res.status(500).json({ message: "Service account token not available" });

      try {
        const servicesRoutes = await fetchRoutesFromCluster(apiServer, saToken, "services", getCACert());

        let centralRoutes = [];
        const centralToken = getCentralToken();
        const centralApi = getCentralApiServer();
        if (centralToken && centralApi) {
          centralRoutes = await fetchRoutesFromCluster(
            centralApi, centralToken, "central", getCentralCACert(), { insecure: centralTlsInsecure() },
          );
        }

        const staticEntries = [
          { name: "OpenShift Console", namespace: "openshift-console", host: process.env.SERVICES_CONSOLE_HOST || `console-openshift-console.${process.env.SERVICES_APPS_DOMAIN || ""}`, tls: true, path: "", cluster: "services" },
          { name: "OpenShift API", namespace: "openshift-apiserver", host: process.env.OCP_SERVICES_HOST || (process.env.OCP_SERVICES_SERVER || "").replace(/^https?:\/\//, ""), tls: true, path: "", cluster: "services" },
          { name: "OpenShift Console", namespace: "openshift-console", host: process.env.CENTRAL_CONSOLE_HOST || `console-openshift-console.${process.env.CENTRAL_APPS_DOMAIN || ""}`, tls: true, path: "", cluster: "central" },
          { name: "OpenShift API", namespace: "openshift-apiserver", host: process.env.OCP_CENTRAL_HOST || (process.env.OCP_CENTRAL_SERVER || "").replace(/^https?:\/\//, ""), tls: true, path: "", cluster: "central" },
        ];

        const allRoutes = [...servicesRoutes, ...centralRoutes, ...staticEntries];
        res.json(allRoutes);
      } catch (err) {
        console.error("Routes list error:", err.message);
        res.status(502).json({ message: "Failed to fetch routes from cluster" });
      }
    },

    async listOperators(_req, res) {
      const saToken = getSAToken();
      if (!saToken) return res.status(500).json({ message: "Service account token not available" });

      const OPERATOR_NS = [
        { ns: "sovereign-cloud", category: "core" },
        { ns: "sovereign-cloud-plugins", category: "plugin" },
      ];

      try {
        const operators = await fetchDeploymentsFromCluster(
          apiServer, saToken, "services", getCACert(), OPERATOR_NS,
        );

        let centralOps = [];
        const centralToken = getCentralToken();
        const centralApi = getCentralApiServer();
        if (centralToken && centralApi) {
          centralOps = await fetchDeploymentsFromCluster(
            centralApi,
            centralToken,
            "central",
            getCentralCACert(),
            [{ ns: "openshift-gitops", category: "gitops" }],
            { insecure: centralTlsInsecure() },
          );
          centralOps = centralOps.filter((op) => op.name.includes("gitops") || op.name.includes("argocd"));
        }

        res.json([...operators, ...centralOps]);
      } catch (err) {
        console.error("List operators error:", err.message);
        res.status(502).json({ message: "Failed to fetch operator status" });
      }
    },

    async listRbacConfigs(_req, res) {
      const saToken = getSAToken();
      if (!saToken) return res.status(500).json({ message: "Service account token not available" });
      try {
        const resp = await k8sRequest(apiServer, "/apis/hybridsovereign.redhat/v1alpha1/rbacconfigs", "GET", saToken);
        const items = resp.body?.items || [];
        res.json(items.map((item) => ({
          name: item.metadata?.name || "",
          type: item.spec?.type || "unknown",
          description: item.spec?.description || "",
        })));
      } catch (err) {
        console.error("RbacConfig list error:", err.message);
        res.status(502).json({ message: "Failed to fetch RbacConfigs" });
      }
    },

    async listEntityRbacs(req, res) {
      const { name } = req.params;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid entity name" });
      const token = getUserAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const entityNs = `entity-${name}`;
      try {
        const resp = await k8sRequest(apiServer, `/apis/hybridsovereign.redhat/v1alpha1/namespaces/${entityNs}/rbacs`, "GET", token);
        if (resp.status === 404) return res.json([]);
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to fetch RBACs" });
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("Entity RBAC list error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async createEntityRbac(req, res) {
      const { name } = req.params;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid entity name" });
      const { rbacName, config, description } = req.body || {};
      if (!rbacName || !NAME_PATTERN.test(rbacName)) return res.status(400).json({ message: "Invalid RBAC name" });
      if (!config || typeof config !== "string") return res.status(400).json({ message: "config is required" });
      const token = getUserAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const entityNs = `entity-${name}`;
      const rbac = {
        apiVersion: "hybridsovereign.redhat/v1alpha1",
        kind: "Rbac",
        metadata: { name: rbacName, namespace: entityNs },
        spec: {
          config: config.trim(),
          ...(description ? { description: description.trim() } : {}),
        },
      };
      try {
        const resp = await k8sRequest(apiServer, `/apis/hybridsovereign.redhat/v1alpha1/namespaces/${entityNs}/rbacs`, "POST", token, rbac);
        if (resp.status === 409) return res.status(409).json({ message: `RBAC '${rbacName}' already exists` });
        if (resp.status >= 400) return res.status(resp.status < 500 ? resp.status : 502).json({ message: resp.body?.message || "Failed to create RBAC" });
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("Entity RBAC create error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async listEntityPersonas(req, res) {
      const { name } = req.params;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid entity name" });
      const token = getUserAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const entityNs = `entity-${name}`;
      try {
        const resp = await k8sRequest(apiServer, `/apis/hybridsovereign.redhat/v1alpha1/namespaces/${entityNs}/personas`, "GET", token);
        if (resp.status === 404) return res.json([]);
        if (resp.status >= 400) return res.status(502).json({ message: "Failed to fetch Personas" });
        res.json(resp.body?.items || []);
      } catch (err) {
        console.error("Entity Persona list error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async createEntityPersona(req, res) {
      const { name } = req.params;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid entity name" });
      const { personaName, rbac, type } = req.body || {};
      if (!personaName || !NAME_PATTERN.test(personaName)) return res.status(400).json({ message: "Invalid Persona name" });
      if (!rbac || typeof rbac !== "string" || !rbac.trim()) return res.status(400).json({ message: "rbac is required" });
      if (!type || typeof type !== "string" || !PERSONA_TYPES.has(type.trim())) {
        return res.status(400).json({ message: "type must be a valid Persona role type" });
      }
      const token = getUserAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const entityNs = `entity-${name}`;
      const persona = {
        apiVersion: "hybridsovereign.redhat/v1alpha1",
        kind: "Persona",
        metadata: { name: personaName, namespace: entityNs },
        spec: { rbac: rbac.trim(), type: type.trim() },
      };
      try {
        const resp = await k8sRequest(apiServer, `/apis/hybridsovereign.redhat/v1alpha1/namespaces/${entityNs}/personas`, "POST", token, persona);
        if (resp.status === 409) return res.status(409).json({ message: `Persona '${personaName}' already exists` });
        if (resp.status >= 400) return res.status(resp.status < 500 ? resp.status : 502).json({ message: resp.body?.message || "Failed to create Persona" });
        res.status(201).json(resp.body);
      } catch (err) {
        console.error("Entity Persona create error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async updateEntityRbac(req, res) {
      const { name, rbacName } = req.params;
      if (!name || !NAME_PATTERN.test(name)) return res.status(400).json({ message: "Invalid entity name" });
      if (!rbacName || !NAME_PATTERN.test(rbacName)) return res.status(400).json({ message: "Invalid RBAC name" });
      const { config, description } = req.body || {};
      if (!config || typeof config !== "string") return res.status(400).json({ message: "config is required" });
      const token = getUserAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const entityNs = `entity-${name}`;
      const patch = {
        spec: {
          config: config.trim(),
          ...(description !== undefined ? { description: (description || "").trim() } : {}),
        },
      };
      try {
        const resp = await k8sRequest(
          apiServer,
          `/apis/hybridsovereign.redhat/v1alpha1/namespaces/${entityNs}/rbacs/${encodeURIComponent(rbacName)}`,
          "PATCH", token, patch, undefined, "application/merge-patch+json",
        );
        if (resp.status === 404) return res.status(404).json({ message: `RBAC '${rbacName}' not found` });
        if (resp.status >= 400) return res.status(502).json({ message: resp.body?.message || "Failed to update RBAC" });
        res.json(resp.body);
      } catch (err) {
        console.error("Entity RBAC update error:", err.message);
        res.status(502).json({ message: "Failed to communicate with cluster API" });
      }
    },

    async patchAnnotation(req, res) {
      const token = getUserAccessToken(req);
      if (!token) return res.status(401).json({ message: "Unauthorized" });
      const {
        group = "hybridsovereign.redhat",
        version = "v1alpha1",
        kind,
        name,
        namespace: ns,
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
      if (!ns || !NS_PATTERN.test(ns)) {
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
        `/apis/${group}/${version}/namespaces/${encodeURIComponent(ns)}/${plural}/${encodeURIComponent(name)}`;
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
          undefined,
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

    async listAllCRs(_req, res) {
      const saToken = getSAToken();
      if (!saToken) return res.status(500).json({ message: "Service account token not available" });

      try {
        const servicesCRs = await fetchCRsFromCluster(apiServer, saToken, getCACert());

        let centralCRs = [];
        const centralToken = getCentralToken();
        const centralApi = getCentralApiServer();
        if (centralToken && centralApi) {
          centralCRs = await fetchCRsFromCluster(centralApi, centralToken, getCentralCACert());
        }

        const seen = new Set();
        const merged = [];
        for (const cr of [...servicesCRs, ...centralCRs]) {
          const key = `${cr.metadata?.uid || `${cr.metadata?.namespace}/${cr.metadata?.name}/${cr.kind}`}`;
          if (!seen.has(key)) {
            seen.add(key);
            merged.push(cr);
          }
        }
        res.json(merged);
      } catch (err) {
        console.error("List all CRs error:", err.message);
        res.status(502).json({ message: "Failed to fetch resources" });
      }
    },
  };
}
