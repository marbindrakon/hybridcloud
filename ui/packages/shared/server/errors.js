/**
 * The one helper that turns an upstream failure into a dashboard answer (C5).
 *
 * 4xx answers of the API server are passed through with their Kubernetes
 * Status body (message, reason, details.causes), so the UI can tell
 * "forbidden" from "not found" from "invalid field" (UX-057, UX-040).
 * 5xx answers and transport errors become 502 with a short reference id; the
 * detail is logged server-side only.
 */
import crypto from "crypto";

const REASON_BY_STATUS = {
  400: "BadRequest",
  401: "Unauthorized",
  403: "Forbidden",
  404: "NotFound",
  405: "MethodNotAllowed",
  409: "Conflict",
  410: "Gone",
  413: "RequestEntityTooLarge",
  415: "UnsupportedMediaType",
  422: "Invalid",
  429: "TooManyRequests",
};

const MESSAGE_BY_STATUS = {
  400: "The request was not valid",
  401: "Your session ended. Sign in again.",
  403: "Forbidden",
  404: "Not found",
  409: "Conflict",
  422: "The object is not valid",
  429: "Too many requests",
};

export function newReference() {
  return `ref-${crypto.randomBytes(4).toString("hex")}`;
}

function status(code, reason, message, extra = {}) {
  return { kind: "Status", apiVersion: "v1", status: "Failure", code, reason, message, ...extra };
}

/**
 * @param {import('express').Response} res
 * @param {{status: number, body?: any, warnings?: string[]} | Error | unknown} upstream
 *   an answer of k8sRequest, or the error a transport failure threw
 * @param {{kind?: string, verb?: string, namespace?: string, name?: string, message?: string}} [ctx]
 */
export function sendUpstreamError(res, upstream, ctx = {}) {
  if (res.headersSent) return res;
  const answer = upstream && typeof upstream === "object" && typeof upstream.status === "number" ? upstream : null;
  if (answer && Array.isArray(answer.warnings) && answer.warnings.length) {
    res.setHeader("Warning", answer.warnings.map((w) => `299 - "${String(w).replace(/"/g, '\\"')}"`).join(", "));
  }
  if (answer && answer.status >= 400 && answer.status < 500) {
    const code = answer.status;
    const body = answer.body;
    if (body && typeof body === "object" && body.kind === "Status") {
      return res.status(code).json(body);
    }
    const message =
      (body && typeof body === "object" && typeof body.message === "string" && body.message) ||
      (typeof body === "string" && body.trim() && body.trim().slice(0, 500)) ||
      ctx.message ||
      MESSAGE_BY_STATUS[code] ||
      `Request failed (${code})`;
    return res.status(code).json(status(code, REASON_BY_STATUS[code] || "Failure", message));
  }
  const reference = newReference();
  const what = [ctx.verb, ctx.kind, ctx.namespace && `in ${ctx.namespace}`, ctx.name].filter(Boolean).join(" ");
  if (answer) {
    const detail = answer.body && typeof answer.body === "object" ? answer.body.message : answer.body;
    console.error(`[${reference}] upstream ${answer.status}${what ? ` (${what})` : ""}: ${String(detail ?? "").slice(0, 500)}`);
  } else {
    const msg = upstream instanceof Error ? upstream.message : String(upstream);
    console.error(`[${reference}] upstream transport error${what ? ` (${what})` : ""}: ${msg}`);
  }
  return res.status(502).json(
    status(502, answer ? "UpstreamError" : "UpstreamUnavailable", answer
      ? "The Sovereign Cloud API answered with an error"
      : "The Sovereign Cloud API could not be reached", { reference }),
  );
}

/** A 4xx answer the dashboard server itself decides (validation, missing token). */
export function sendStatus(res, code, message, extra = {}) {
  return res.status(code).json(status(code, REASON_BY_STATUS[code] || "Failure", message, extra));
}
