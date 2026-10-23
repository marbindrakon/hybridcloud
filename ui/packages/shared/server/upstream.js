/**
 * Requests to the Kubernetes API with the caller's token (C5).
 *
 * No dependencies besides Node itself: this directory is copied into the
 * dashboard images next to the app (/opt/app-root/shared/server).
 */
import https from "https";
import http from "http";
import fs from "fs";

const SA_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt";

export const DEFAULT_API_SERVER = process.env.OCP_SERVICES_SERVER || "https://kubernetes.default.svc";

let cachedCA;
let caLoaded = false;

/** The in-cluster CA bundle, or undefined outside a pod. */
export function getCACert() {
  if (caLoaded) return cachedCA;
  try {
    cachedCA = fs.readFileSync(SA_CA_PATH);
  } catch {
    cachedCA = undefined;
  }
  caLoaded = true;
  return cachedCA;
}

/**
 * The caller's token: the oauth-proxy's X-Forwarded-Access-Token, else a
 * bearer Authorization header (console plugin proxy, delegated by the
 * oauth-proxy's --openshift-delegate-urls).
 */
export function userToken(req) {
  const h = req.headers["x-forwarded-access-token"];
  if (typeof h === "string" && h.trim()) return h.trim();
  const auth = req.headers.authorization;
  if (typeof auth === "string" && auth.toLowerCase().startsWith("bearer ")) {
    const t = auth.slice(7).trim();
    return t || null;
  }
  return null;
}

/** Kubernetes `Warning` headers as plain texts. */
export function parseWarnings(header) {
  if (!header) return [];
  const raw = Array.isArray(header) ? header.join(", ") : String(header);
  const out = [];
  const re = /\d{3}\s+\S+\s+"((?:[^"\\]|\\.)*)"/g;
  let m;
  while ((m = re.exec(raw))) out.push(m[1].replace(/\\"/g, '"'));
  return out.length ? out : [raw];
}

/**
 * One request to the API server.
 *
 * @param {{method?: string, path: string, query?: Record<string, string|undefined>, body?: unknown,
 *          token: string, contentType?: string, apiServer?: string, timeoutMs?: number}} opts
 * @returns {Promise<{status: number, body: any, warnings: string[]}>} rejects only on transport errors
 */
export function k8sRequest(opts) {
  const { method = "GET", path, query, body, token, contentType, timeoutMs = 15000 } = opts;
  const apiServer = opts.apiServer || DEFAULT_API_SERVER;
  return new Promise((resolve, reject) => {
    let url;
    try {
      url = new URL(path, apiServer);
    } catch (err) {
      reject(err);
      return;
    }
    if (query) {
      for (const [k, v] of Object.entries(query)) {
        if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, String(v));
      }
    }
    const ca = getCACert();
    const headers = {
      Authorization: `Bearer ${token}`,
      Accept: "application/json",
    };
    const payload = body === undefined || body === null ? undefined : typeof body === "string" ? body : JSON.stringify(body);
    if (payload !== undefined) {
      headers["Content-Type"] = contentType || "application/json";
      headers["Content-Length"] = Buffer.byteLength(payload);
    }
    const transport = url.protocol === "http:" ? http : https;
    const req = transport.request(
      {
        hostname: url.hostname,
        port: url.port || (url.protocol === "http:" ? 80 : 443),
        path: url.pathname + url.search,
        method,
        headers,
        rejectUnauthorized: !!ca,
        ca,
        timeout: timeoutMs,
      },
      (res) => {
        let data = "";
        res.setEncoding("utf8");
        res.on("data", (c) => (data += c));
        res.on("end", () => {
          let parsed = data;
          try {
            parsed = data ? JSON.parse(data) : undefined;
          } catch {
            parsed = data;
          }
          resolve({ status: res.statusCode || 0, body: parsed, warnings: parseWarnings(res.headers.warning) });
        });
      },
    );
    req.on("timeout", () => {
      req.destroy(new Error("K8s API request timed out"));
    });
    req.on("error", reject);
    if (payload !== undefined) req.write(payload);
    req.end();
  });
}
