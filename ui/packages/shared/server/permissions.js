/**
 * GET /api/permissions?namespace=<ns|->&resources=<plural,...>&verbs=<verb,...>   (C3)
 *
 * One bounded batch of SelfSubjectAccessReviews on the caller's own token, run
 * in parallel: at most 40 resources x 6 verbs, group hybridsovereign.redhat,
 * namespace "-" = cluster scope (All tenants). Answers
 *   { namespace, allowed: { [plural]: { [verb]: boolean } }, evaluatedAt }
 * and caches each answer for 30 s per token hash and query. A review that fails
 * counts as denied (the API stays authoritative; the gate only hides actions).
 */
import { k8sRequest, userToken } from "./upstream.js";
import { sendStatus } from "./errors.js";
import { tokenHash } from "./platformGate.js";

const GROUP = "hybridsovereign.redhat";
const VERBS = new Set(["get", "list", "watch", "create", "update", "patch", "delete", "deletecollection"]);
const PLURAL = /^[a-z][a-z0-9]{0,62}$/;
const NS = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;
const MAX_RESOURCES = 40;
const MAX_VERBS = 6;
const TTL_MS = 30000;
const CONCURRENCY = 24;

function list(v) {
  if (typeof v !== "string") return [];
  return [...new Set(v.split(",").map((x) => x.trim()).filter(Boolean))];
}

async function pool(items, n, fn) {
  const out = new Array(items.length);
  let i = 0;
  const workers = Array.from({ length: Math.min(n, items.length) }, async () => {
    while (i < items.length) {
      const idx = i++;
      out[idx] = await fn(items[idx]);
    }
  });
  await Promise.all(workers);
  return out;
}

/**
 * @param {{apiServer?: string, ttlMs?: number}} [opts]
 * @returns {import('express').RequestHandler}
 */
export function createPermissionsHandler(opts = {}) {
  const ttlMs = opts.ttlMs ?? TTL_MS;
  const cache = new Map();

  return async function permissions(req, res) {
    const token = userToken(req);
    if (!token) return sendStatus(res, 401, "Your session ended. Sign in again.");
    const namespace = typeof req.query.namespace === "string" ? req.query.namespace.trim() : "";
    const resources = list(req.query.resources);
    const verbs = list(req.query.verbs);
    if (!namespace || (namespace !== "-" && !NS.test(namespace))) {
      return sendStatus(res, 400, "namespace must be a namespace name or - for cluster scope");
    }
    if (!resources.length || resources.length > MAX_RESOURCES || !resources.every((r) => PLURAL.test(r))) {
      return sendStatus(res, 400, `resources must be 1 to ${MAX_RESOURCES} resource plurals`);
    }
    if (!verbs.length || verbs.length > MAX_VERBS || !verbs.every((v) => VERBS.has(v))) {
      return sendStatus(res, 400, `verbs must be 1 to ${MAX_VERBS} of ${[...VERBS].join(", ")}`);
    }

    const key = `${tokenHash(token)}|${namespace}|${[...resources].sort().join(",")}|${[...verbs].sort().join(",")}`;
    const now = Date.now();
    const hit = cache.get(key);
    if (hit && hit.until > now) return res.json(hit.body);

    const pairs = resources.flatMap((resource) => verbs.map((verb) => ({ resource, verb })));
    const results = await pool(pairs, CONCURRENCY, async ({ resource, verb }) => {
      try {
        const resp = await k8sRequest({
          apiServer: opts.apiServer,
          method: "POST",
          path: "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews",
          token,
          timeoutMs: 10000,
          body: {
            apiVersion: "authorization.k8s.io/v1",
            kind: "SelfSubjectAccessReview",
            spec: {
              resourceAttributes: {
                group: GROUP,
                resource,
                verb,
                ...(namespace !== "-" ? { namespace } : {}),
              },
            },
          },
        });
        return { resource, verb, allowed: resp.status < 300 && !!resp.body?.status?.allowed, status: resp.status };
      } catch {
        return { resource, verb, allowed: false, status: 0 };
      }
    });
    if (results.some((r) => r.status === 401)) return sendStatus(res, 401, "Your session ended. Sign in again.");

    const allowed = {};
    for (const r of results) {
      allowed[r.resource] = allowed[r.resource] || {};
      allowed[r.resource][r.verb] = r.allowed;
    }
    const body = { namespace, allowed, evaluatedAt: new Date().toISOString() };
    if (cache.size > 5000) cache.clear();
    if (results.every((r) => r.status && r.status < 500)) cache.set(key, { until: now + ttlMs, body });
    return res.json(body);
  };
}
