/**
 * Platform-only gate for the admin dashboard server (defense in depth behind
 * the oauth-proxy's --openshift-sar): every /api request is checked with a
 * SelfSubjectAccessReview on the caller's own token, per request (60 s cache
 * per token hash), not per session cookie.
 *
 * The permission is the same one the proxy checks; both come from the chart
 * value oauthProxy.openshiftSar, passed to the server as SHC_PLATFORM_SAR
 * (JSON: OpenShift SubjectAccessReview keys {namespace, resource,
 * resourceAPIGroup, verb, resourceName?}, translated here to the Kubernetes
 * ResourceAttributes keys group and name).
 */
import crypto from "crypto";
import { k8sRequest, userToken } from "./upstream.js";
import { sendStatus, sendUpstreamError } from "./errors.js";

export const DEFAULT_PLATFORM_SAR = {
  namespace: "sovereign-cloud",
  resource: "hybridfabrics",
  resourceAPIGroup: "hybridsovereign.redhat",
  verb: "list",
};

export function platformSarFromEnv(env = process.env) {
  const raw = env.SHC_PLATFORM_SAR;
  if (!raw) return DEFAULT_PLATFORM_SAR;
  try {
    const v = JSON.parse(raw);
    if (v && typeof v === "object" && typeof v.resource === "string" && typeof v.verb === "string") return v;
  } catch {
    /* fall through */
  }
  console.error("SHC_PLATFORM_SAR is not valid JSON with resource and verb; using the default");
  return DEFAULT_PLATFORM_SAR;
}

export function tokenHash(token) {
  return crypto.createHash("sha256").update(token).digest("hex");
}

/**
 * @param {{apiServer?: string, sar?: object, ttlMs?: number}} [opts]
 * @returns {import('express').RequestHandler}
 */
export function createRequirePlatform(opts = {}) {
  const sar = opts.sar || platformSarFromEnv();
  const ttlMs = opts.ttlMs ?? 60000;
  const cache = new Map();

  return async function requirePlatform(req, res, next) {
    const token = userToken(req);
    if (!token) return sendStatus(res, 401, "Your session ended. Sign in again.");
    const key = tokenHash(token);
    const now = Date.now();
    const hit = cache.get(key);
    if (hit && hit.until > now) {
      return hit.allowed ? next() : sendStatus(res, 403, "This dashboard is for platform administrators.");
    }
    try {
      const resp = await k8sRequest({
        apiServer: opts.apiServer,
        method: "POST",
        path: "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews",
        token,
        body: {
          apiVersion: "authorization.k8s.io/v1",
          kind: "SelfSubjectAccessReview",
          spec: {
            resourceAttributes: {
              ...(sar.namespace ? { namespace: sar.namespace } : {}),
              group: sar.resourceAPIGroup || "",
              resource: sar.resource,
              verb: sar.verb,
              ...(sar.resourceName || sar.name ? { name: sar.resourceName || sar.name } : {}),
            },
          },
        },
        timeoutMs: 10000,
      });
      if (resp.status === 401) return sendStatus(res, 401, "Your session ended. Sign in again.");
      if (resp.status >= 400) return sendUpstreamError(res, resp, { verb: "review", kind: "access" });
      const allowed = !!resp.body?.status?.allowed;
      if (cache.size > 5000) cache.clear();
      cache.set(key, { allowed, until: now + ttlMs });
      if (!allowed) return sendStatus(res, 403, "This dashboard is for platform administrators.");
      return next();
    } catch (err) {
      return sendUpstreamError(res, err, { verb: "review", kind: "access" });
    }
  };
}
