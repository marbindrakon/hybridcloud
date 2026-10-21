/**
 * Transparent Kubernetes API proxy for the standalone dashboards (C6), with the
 * caller's token only.
 *
 *   app.use('/api/k8s', requireAuth, createApiK8sProxy(apiServer, { transformBody }))
 *
 * - Only /api/... and /apis/... paths are forwarded.
 * - Kubernetes `Warning` headers are forwarded to the browser.
 * - `transformBody(path, obj, req)` (optional) sees every JSON answer of a
 *   non-watch request before it is sent and returns the object to send (for
 *   example a tenant status allow-list). For a watch request it is called once
 *   with `obj === null` before the request is forwarded, and the stream is
 *   passed through unchanged. Throwing an error that carries `status` (and
 *   optionally `message`) refuses the request with that status.
 * - /can-i/<namespace>/<resource>/<verb> is a lightweight SelfSubjectAccessReview
 *   shim kept for one release (use /api/permissions instead).
 */
import https from "https";
import http from "http";
import { getCACert, userToken } from "./upstream.js";
import { sendStatus, newReference } from "./errors.js";

/** Patch media types the API server accepts; any other body is sent as application/json. */
const PATCH_TYPES = [
  "application/merge-patch+json",
  "application/json-patch+json",
  "application/strategic-merge-patch+json",
];

function isWatch(k8sPath) {
  const u = new URL(k8sPath, "http://x");
  return u.searchParams.get("watch") === "true" || u.searchParams.get("watch") === "1" || /\/watch\//.test(u.pathname);
}

function refuse(res, err) {
  const code = Number(err?.status) || 403;
  return sendStatus(res, code, err?.message || "Refused");
}

function forwardWarnings(incoming, res) {
  const w = incoming.headers.warning;
  if (w) res.setHeader("Warning", w);
}

function proxyRequest(apiServer, k8sPath, req, token, body, res, contentType, transformBody) {
  let url;
  try {
    url = new URL(k8sPath, apiServer);
  } catch {
    return sendStatus(res, 400, "Invalid path");
  }

  const ca = getCACert();
  const headers = {
    Authorization: `Bearer ${token}`,
    Accept: "application/json",
  };
  const payload = body === undefined ? undefined : typeof body === "string" ? body : JSON.stringify(body);
  if (payload !== undefined) {
    headers["Content-Type"] = contentType || "application/json";
    headers["Content-Length"] = Buffer.byteLength(payload);
  }

  const watch = isWatch(k8sPath);
  const transport = url.protocol === "http:" ? http : https;
  const upstream = transport.request(
    {
      hostname: url.hostname,
      port: url.port || (url.protocol === "http:" ? 80 : 443),
      path: url.pathname + url.search,
      method: req.method,
      headers,
      rejectUnauthorized: !!ca,
      ca,
      timeout: watch ? 0 : 30000,
    },
    (incoming) => {
      forwardWarnings(incoming, res);
      const ct = String(incoming.headers["content-type"] || "");
      if (!transformBody || watch || !ct.includes("json")) {
        res.status(incoming.statusCode || 502);
        if (ct) res.setHeader("Content-Type", ct);
        incoming.pipe(res);
        return;
      }
      let data = "";
      incoming.setEncoding("utf8");
      incoming.on("data", (c) => (data += c));
      incoming.on("end", () => {
        let obj;
        try {
          obj = data ? JSON.parse(data) : undefined;
        } catch {
          res.status(incoming.statusCode || 502);
          res.setHeader("Content-Type", ct);
          res.send(data);
          return;
        }
        try {
          const out = obj === undefined ? obj : transformBody(k8sPath, obj, req);
          res.status(incoming.statusCode || 502);
          if (out === undefined) res.end();
          else res.json(out);
        } catch (err) {
          refuse(res, err);
        }
      });
    },
  );
  upstream.on("timeout", () => {
    upstream.destroy();
    if (!res.headersSent) sendStatus(res, 504, "The Sovereign Cloud API did not answer in time");
  });
  upstream.on("error", (err) => {
    const reference = newReference();
    console.error(`[${reference}] api/k8s proxy error: ${err.message}`);
    if (!res.headersSent) {
      res.status(502).json({
        kind: "Status",
        apiVersion: "v1",
        status: "Failure",
        code: 502,
        reason: "UpstreamUnavailable",
        message: "The Sovereign Cloud API could not be reached",
        reference,
      });
    }
  });
  if (payload !== undefined) upstream.write(payload);
  upstream.end();
}

function canIShim(apiServer, token, namespace, resource, verb, res) {
  const namespaced = namespace && namespace !== "-" && namespace !== "*";
  const body = JSON.stringify({
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
  });
  const ca = getCACert();
  const url = new URL("/apis/authorization.k8s.io/v1/selfsubjectaccessreviews", apiServer);
  const transport = url.protocol === "http:" ? http : https;
  const upstream = transport.request(
    {
      hostname: url.hostname,
      port: url.port || (url.protocol === "http:" ? 80 : 443),
      path: url.pathname,
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/json",
        "Content-Type": "application/json",
        "Content-Length": Buffer.byteLength(body),
      },
      rejectUnauthorized: !!ca,
      ca,
      timeout: 10000,
    },
    (incoming) => {
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
    },
  );
  upstream.on("timeout", () => upstream.destroy(new Error("timeout")));
  upstream.on("error", () => {
    if (!res.headersSent) res.json({ allowed: false }); // fail closed for UI gating
  });
  upstream.write(body);
  upstream.end();
}

/**
 * @param {string} apiServer
 * @param {{ transformBody?: (path: string, obj: any, req: import('express').Request) => any }} [options]
 * @returns {import('express').RequestHandler}
 */
export function createApiK8sProxy(apiServer, options = {}) {
  const { transformBody } = options;
  return (req, res) => {
    const token = userToken(req);
    if (!token) {
      return sendStatus(res, 401, "Your session ended. Sign in again.");
    }

    const k8sPath = (req.url || "/").startsWith("/") ? req.url : `/${req.url}`;

    // Lightweight can-i: /api/k8s/can-i/:namespace/:resource/:verb
    // Use namespace "-" or "*" for cluster-scoped / any-namespace list checks.
    const canI = k8sPath.match(/^\/can-i\/([^/]+)\/([^/]+)\/([^/?]+)/);
    if (canI) {
      const [, namespace, resource, verb] = canI;
      return canIShim(apiServer, token, namespace, resource, verb, res);
    }

    if (!k8sPath.startsWith("/api/") && !k8sPath.startsWith("/apis/")) {
      return sendStatus(res, 400, "Only /api and /apis paths are allowed");
    }

    if (transformBody && isWatch(k8sPath)) {
      try {
        transformBody(k8sPath, null, req);
      } catch (err) {
        return refuse(res, err);
      }
    }

    const body =
      req.method !== "GET" && req.method !== "HEAD" && req.body && Object.keys(req.body).length
        ? req.body
        : undefined;
    const incomingType = String(req.headers["content-type"] || "").split(";")[0].trim().toLowerCase();
    const contentType = req.method === "PATCH" && PATCH_TYPES.includes(incomingType) ? incomingType : undefined;
    return proxyRequest(apiServer, k8sPath, req, token, body, res, contentType, transformBody);
  };
}
