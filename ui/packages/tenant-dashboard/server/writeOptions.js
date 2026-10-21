/**
 * Write options of one incoming request, carried to every upstream call it makes.
 *
 * - Every POST, PUT and PATCH to the platform API group is sent with
 *   fieldValidation=Strict, so an unknown or misspelled field is refused with
 *   a 4xx and its field path instead of being dropped silently (UX-040).
 * - A request with ?dryRun=All (the forms' server dry run before Create and
 *   Save) sends dryRun=All upstream: nothing is persisted.
 * - Kubernetes `Warning` headers of the upstream answers are returned to the
 *   browser as `Warning` headers of the response.
 *
 * The curated handlers call the upstream through one positional helper; an
 * AsyncLocalStorage keeps these options per request without changing their
 * signatures.
 */
import { AsyncLocalStorage } from "node:async_hooks";

const store = new AsyncLocalStorage();

const PLATFORM_API = "/apis/hybridsovereign.redhat/";
const WRITE_METHODS = new Set(["POST", "PUT", "PATCH"]);

/** True when the incoming request asks for a server dry run. */
export function isDryRunRequest(req) {
  const v = req?.query?.dryRun;
  return v === "All" || v === "true";
}

function quote(text) {
  return `299 - "${String(text).replace(/[\r\n]+/g, " ").replace(/"/g, '\\"')}"`;
}

/** Express middleware: per-request options and Warning header passthrough. */
export function writeOptionsMiddleware(req, res, next) {
  const ctx = { dryRun: isDryRunRequest(req), warnings: [] };
  const sendJson = res.json.bind(res);
  res.json = (body) => {
    if (!res.headersSent) {
      if (ctx.warnings.length) res.setHeader("Warning", ctx.warnings.map(quote).join(", "));
      if (ctx.dryRun) res.setHeader("X-Dry-Run", "All");
    }
    return sendJson(body);
  };
  store.run(ctx, next);
}

/** Query parameters for one upstream call of the current request. */
export function upstreamWriteQuery(method, path) {
  const m = String(method || "GET").toUpperCase();
  if (!WRITE_METHODS.has(m) && m !== "DELETE") return undefined;
  const ctx = store.getStore();
  const query = {};
  if (WRITE_METHODS.has(m) && String(path || "").startsWith(PLATFORM_API)) query.fieldValidation = "Strict";
  if (ctx?.dryRun) query.dryRun = "All";
  return Object.keys(query).length ? query : undefined;
}

/** Remember the upstream warnings of the current request (deduplicated). */
export function recordUpstreamWarnings(warnings) {
  const ctx = store.getStore();
  if (!ctx || !Array.isArray(warnings)) return;
  for (const w of warnings) if (w && !ctx.warnings.includes(w)) ctx.warnings.push(w);
}

/** The current request asked for a dry run (handlers skip side effects then). */
export function currentIsDryRun() {
  return !!store.getStore()?.dryRun;
}
