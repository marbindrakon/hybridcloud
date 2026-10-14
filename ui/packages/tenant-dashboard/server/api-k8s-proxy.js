/**
 * Transparent Kubernetes API proxy + lightweight can-i for standalone dashboards.
 * Mount with: app.use('/api/k8s', requireAuth, createApiK8sProxy(apiServer))
 */
import https from "https";
import fs from "fs";

const SA_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt";

function getCACert() {
  try {
    return fs.readFileSync(SA_CA_PATH);
  } catch {
    return undefined;
  }
}

function userToken(req) {
  const h = req.headers["x-forwarded-access-token"];
  if (typeof h === "string" && h.trim()) return h.trim();
  const auth = req.headers.authorization;
  if (typeof auth === "string" && auth.toLowerCase().startsWith("bearer ")) {
    return auth.slice(7).trim();
  }
  return null;
}

/** Patch media types the API server accepts; any other body is sent as application/json. */
const PATCH_TYPES = [
  "application/merge-patch+json",
  "application/json-patch+json",
  "application/strategic-merge-patch+json",
];

function proxyRequest(apiServer, k8sPath, method, token, body, res, contentType) {
  let url;
  try {
    url = new URL(k8sPath, apiServer);
  } catch {
    return res.status(400).json({ message: "Invalid path" });
  }

  const ca = getCACert();
  const headers = {
    Authorization: `Bearer ${token}`,
    Accept: "application/json",
  };
  if (body) headers["Content-Type"] = contentType || "application/json";

  const opts = {
    hostname: url.hostname,
    port: url.port || 443,
    path: url.pathname + url.search,
    method,
    headers,
    rejectUnauthorized: !!ca,
    ca,
    timeout: 30000,
  };

  const upstream = https.request(opts, (incoming) => {
    res.status(incoming.statusCode || 502);
    const ct = incoming.headers["content-type"];
    if (ct) res.setHeader("Content-Type", ct);
    incoming.pipe(res);
  });
  upstream.on("timeout", () => {
    upstream.destroy();
    if (!res.headersSent) res.status(504).json({ message: "Upstream timeout" });
  });
  upstream.on("error", (err) => {
    console.error("api/k8s proxy error:", err.message);
    if (!res.headersSent) res.status(502).json({ message: "Bad gateway" });
  });
  if (body) upstream.write(typeof body === "string" ? body : JSON.stringify(body));
  upstream.end();
}

/**
 * @param {string} apiServer
 * @returns {import('express').RequestHandler}
 */
export function createApiK8sProxy(apiServer) {
  return (req, res) => {
    const token = userToken(req);
    if (!token) {
      return res.status(401).json({ message: "Unauthorized" });
    }

    const k8sPath = (req.url || "/").startsWith("/") ? req.url : `/${req.url}`;

    // Lightweight can-i: /api/k8s/can-i/:namespace/:resource/:verb
    // Use namespace "-" or "*" for cluster-scoped / any-namespace list checks.
    const canI = k8sPath.match(/^\/can-i\/([^/]+)\/([^/]+)\/([^/?]+)/);
    if (canI) {
      const [, namespace, resource, verb] = canI;
      const namespaced = namespace && namespace !== "-" && namespace !== "*";
      const body = {
        apiVersion: "authorization.k8s.io/v1",
        kind: "SelfSubjectAccessReview",
        spec: {
          resourceAttributes: {
            ...(namespaced ? { namespace } : {}),
            verb,
            group: "hybridsovereign.redhat",
            resource,
          },
        },
      };
      const ca = getCACert();
      const url = new URL("/apis/authorization.k8s.io/v1/selfsubjectaccessreviews", apiServer);
      const opts = {
        hostname: url.hostname,
        port: url.port || 443,
        path: url.pathname,
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        rejectUnauthorized: !!ca,
        ca,
        timeout: 10000,
      };
      const upstream = https.request(opts, (incoming) => {
        let data = "";
        incoming.on("data", (c) => (data += c));
        incoming.on("end", () => {
          try {
            const parsed = JSON.parse(data);
            res.json({ allowed: !!parsed?.status?.allowed });
          } catch {
            res.json({ allowed: false });
          }
        });
      });
      upstream.on("error", () => res.json({ allowed: false })); // fail-closed for UI gating
      upstream.write(JSON.stringify(body));
      upstream.end();
      return;
    }

    if (!k8sPath.startsWith("/api/") && !k8sPath.startsWith("/apis/")) {
      return res.status(400).json({ message: "Only /api and /apis paths are allowed" });
    }

    const body =
      req.method !== "GET" && req.method !== "HEAD" && req.body && Object.keys(req.body).length
        ? req.body
        : undefined;
    const incomingType = String(req.headers["content-type"] || "").split(";")[0].trim().toLowerCase();
    const contentType = req.method === "PATCH" && PATCH_TYPES.includes(incomingType) ? incomingType : undefined;
    return proxyRequest(apiServer, k8sPath, req.method, token, body, res, contentType);
  };
}
