/**
 * Transparent Kubernetes API proxy for the standalone dashboards (C6), with the
 * caller's token only.
 *
 *   app.use('/api/k8s', requireAuth, createApiK8sProxy(apiServer, { transformBody }))
 *
 * - Only /api/... and /apis/... paths are forwarded, and only in their
 *   normalised form (normaliseK8sPath): %XX decoded, `//` collapsed, a trailing
 *   `/` dropped, the query re-encoded; an encoded slash or backslash and `.` /
 *   `..` segments are refused. The hook and the API server see the same path.
 * - A request is a watch when its first `watch` value is anything but `0` or
 *   `false` (any case), as the API server reads it, or when the path has the
 *   `watch` segment.
 * - Kubernetes `Warning` headers are forwarded to the browser.
 * - `transformBody(path, obj, req)` (optional) sees every JSON answer before it
 *   is sent and returns the object to send (for example a tenant status
 *   allow-list). For a watch request it is called once with `obj === null`
 *   before the request is forwarded, then once per watch event
 *   (`{type, object}`) of the stream. Throwing an error that carries `status`
 *   (and optionally `message`) refuses the request with that status. With a
 *   hook, an answer that is not valid JSON is refused with 502 (never sent
 *   unchecked), and a body (or one watch event) larger than `maxBodyBytes`
 *   (default 8 MiB) is refused with 502 or ends the stream.
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

export const DEFAULT_MAX_BODY_BYTES = 8 * 1024 * 1024;

/** True when the API server serves this request as a watch (metav1 bool conversion: first value, not 0/false). */
export function isWatchRequest(segments, params) {
  const values = params.getAll("watch");
  if (values.length) {
    const v = values[0].toLowerCase();
    if (v !== "0" && v !== "false") return true;
  }
  // /api/<version>/watch/... and /apis/<group>/<version>/watch/...
  const at = segments[0] === "api" ? 2 : 3;
  return segments[at] === "watch";
}

/**
 * The path the proxy forwards: every segment decoded once and re-encoded, empty
 * segments dropped (collapses // and drops a trailing /), the query re-encoded
 * from its parsed form. Returns { error } for paths it will not forward.
 *
 * @param {string} raw request URL below the mount point (path and query)
 * @returns {{ path: string, pathname: string, segments: string[], params: URLSearchParams, watch: boolean } | { error: string }}
 */
export function normaliseK8sPath(raw) {
  const text = String(raw || "/");
  if (text.includes("#")) return { error: "Invalid path" };
  const q = text.indexOf("?");
  const rawPath = q === -1 ? text : text.slice(0, q);
  const rawQuery = q === -1 ? "" : text.slice(q + 1);
  if (rawPath.includes("\\")) return { error: "Invalid path" };
  const segments = [];
  for (const part of rawPath.split("/")) {
    if (part === "") continue;
    let seg;
    try {
      seg = decodeURIComponent(part);
    } catch {
      return { error: "Invalid path encoding" };
    }
    if (seg.includes("/") || seg.includes("\\")) return { error: "Encoded slashes are not allowed in the path" };
    if (seg === "." || seg === "..") return { error: "Relative path segments are not allowed" };
    if (/[\u0000-\u001f\u007f]/.test(seg)) return { error: "Invalid path" };
    segments.push(seg);
  }
  const params = new URLSearchParams(rawQuery);
  const search = params.toString();
  const pathname = `/${segments.map(encodeURIComponent).join("/")}`;
  return {
    path: search ? `${pathname}?${search}` : pathname,
    pathname,
    segments,
    params,
    watch: isWatchRequest(segments, params),
  };
}

function refuse(res, err) {
  const code = Number(err?.status) || 403;
  return sendStatus(res, code, err?.message || "Refused");
}

function forwardWarnings(incoming, res) {
  const w = incoming.headers.warning;
  if (w) res.setHeader("Warning", w);
}

/** Refuse an answer the hook could not check (never sent unchecked). */
function refuseUnchecked(res, message) {
  const reference = newReference();
  console.error(`[${reference}] api/k8s proxy: ${message}`);
  if (!res.headersSent) {
    res.status(502).json({
      kind: "Status",
      apiVersion: "v1",
      status: "Failure",
      code: 502,
      reason: "UpstreamUnavailable",
      message: "The Sovereign Cloud API answer could not be checked",
      reference,
    });
  } else {
    res.end();
  }
}

/** Buffer a JSON answer (at most maxBody bytes), run the hook on it and send the result. */
function sendTransformed(incoming, upstream, res, k8sPath, req, transformBody, maxBody) {
  const chunks = [];
  let size = 0;
  let done = false;
  incoming.on("data", (c) => {
    if (done) return;
    size += c.length;
    if (size > maxBody) {
      done = true;
      incoming.destroy();
      upstream.destroy();
      refuseUnchecked(res, `answer larger than ${maxBody} bytes for ${k8sPath.split("?")[0]}`);
      return;
    }
    chunks.push(c);
  });
  incoming.on("error", () => {
    if (done) return;
    done = true;
    refuseUnchecked(res, "upstream answer broke off");
  });
  incoming.on("end", () => {
    if (done) return;
    done = true;
    const data = Buffer.concat(chunks).toString("utf8");
    let obj;
    try {
      obj = data ? JSON.parse(data) : undefined;
    } catch {
      refuseUnchecked(res, `unparsable JSON answer for ${k8sPath.split("?")[0]}`);
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
}

/**
 * A watch stream through the hook, one event (one JSON line) at a time. An event
 * that is not JSON, that the hook refuses, or that grows past maxBody ends the
 * stream (the client re-lists).
 */
function streamTransformed(incoming, upstream, res, k8sPath, req, transformBody, maxBody, ct) {
  let buf = "";
  let done = false;
  const stop = (why) => {
    if (done) return;
    done = true;
    console.error(`[${newReference()}] api/k8s watch ended: ${why}`);
    incoming.destroy();
    upstream.destroy();
    res.end();
  };
  const emit = (line) => {
    if (!line.trim()) return true;
    let event;
    try {
      event = JSON.parse(line);
    } catch {
      stop("unparsable watch event");
      return false;
    }
    let out;
    try {
      out = transformBody(k8sPath, event, req);
    } catch {
      stop("watch event refused");
      return false;
    }
    res.write(`${JSON.stringify(out)}\n`);
    return true;
  };
  res.status(incoming.statusCode || 502);
  if (ct) res.setHeader("Content-Type", ct);
  incoming.setEncoding("utf8");
  incoming.on("data", (c) => {
    if (done) return;
    buf += c;
    let i;
    while ((i = buf.indexOf("\n")) !== -1) {
      const line = buf.slice(0, i);
      buf = buf.slice(i + 1);
      if (!emit(line)) return;
    }
    if (Buffer.byteLength(buf) > maxBody) stop(`watch event larger than ${maxBody} bytes`);
  });
  incoming.on("error", () => stop("upstream stream broke off"));
  incoming.on("end", () => {
    if (done) return;
    if (buf && !emit(buf)) return;
    done = true;
    res.end();
  });
}

function proxyRequest(apiServer, k8sPath, watch, req, token, body, res, contentType, transformBody, maxBody) {
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
      if (!transformBody || !ct.includes("json")) {
        res.status(incoming.statusCode || 502);
        if (ct) res.setHeader("Content-Type", ct);
        incoming.pipe(res);
        return;
      }
      if (watch) {
        streamTransformed(incoming, upstream, res, k8sPath, req, transformBody, maxBody, ct);
        return;
      }
      sendTransformed(incoming, upstream, res, k8sPath, req, transformBody, maxBody);
    },
  );
  // The browser went away: stop reading the API answer (watches would stay open otherwise).
  res.on("close", () => {
    if (!res.writableFinished) upstream.destroy();
  });
  upstream.on("timeout", () => {
    upstream.destroy();
    if (!res.headersSent) sendStatus(res, 504, "The Sovereign Cloud API did not answer in time");
  });
  upstream.on("error", (err) => {
    if (res.destroyed || res.writableFinished) return;
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
    } else {
      res.end();
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
 * @param {{ transformBody?: (path: string, obj: any, req: import('express').Request) => any, maxBodyBytes?: number }} [options]
 * @returns {import('express').RequestHandler}
 */
export function createApiK8sProxy(apiServer, options = {}) {
  const { transformBody } = options;
  const maxBody = Number(options.maxBodyBytes) > 0 ? Number(options.maxBodyBytes) : DEFAULT_MAX_BODY_BYTES;
  return (req, res) => {
    const token = userToken(req);
    if (!token) {
      return sendStatus(res, 401, "Your session ended. Sign in again.");
    }

    const rawPath = (req.url || "/").startsWith("/") ? req.url : `/${req.url}`;

    // Lightweight can-i: /api/k8s/can-i/:namespace/:resource/:verb
    // Use namespace "-" or "*" for cluster-scoped / any-namespace list checks.
    const canI = rawPath.match(/^\/can-i\/([^/]+)\/([^/]+)\/([^/?]+)/);
    if (canI) {
      const [, namespace, resource, verb] = canI;
      return canIShim(apiServer, token, namespace, resource, verb, res);
    }

    const norm = normaliseK8sPath(rawPath);
    if ("error" in norm) return sendStatus(res, 400, norm.error);
    if (norm.segments[0] !== "api" && norm.segments[0] !== "apis") {
      return sendStatus(res, 400, "Only /api and /apis paths are allowed");
    }
    const k8sPath = norm.path;

    if (transformBody && norm.watch) {
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
    return proxyRequest(apiServer, k8sPath, norm.watch, req, token, body, res, contentType, transformBody, maxBody);
  };
}
