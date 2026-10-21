import express from "express";
import helmet from "helmet";
import rateLimit from "express-rate-limit";
import http from "http";
import https from "https";
import path from "path";
import { fileURLToPath } from "url";
import { createApiK8sProxy } from "../../shared/server/apiK8sProxy.js";
import { createRequirePlatform } from "../../shared/server/platformGate.js";
import { userToken } from "../../shared/server/upstream.js";
import { sendStatus } from "../../shared/server/errors.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const app = express();
const PORT = process.env.PORT || 8080;

const OCP_API = process.env.OCP_SERVICES_SERVER || "https://kubernetes.default.svc";

app.set("trust proxy", 1);

app.use(helmet({
  contentSecurityPolicy: {
    directives: {
      defaultSrc: ["'self'"],
      scriptSrc: ["'self'"],
      styleSrc: ["'self'", "'unsafe-inline'", "https://fonts.googleapis.com"],
      fontSrc: ["'self'", "https://fonts.gstatic.com"],
      imgSrc: ["'self'", "data:"],
      connectSrc: ["'self'"],
      frameAncestors: ["'none'"],
      baseUri: ["'self'"],
      formAction: ["'self'"],
      objectSrc: ["'none'"],
    },
  },
  crossOriginEmbedderPolicy: false,
  hsts: { maxAge: 31536000, includeSubDomains: true, preload: true },
  referrerPolicy: { policy: "strict-origin-when-cross-origin" },
}));

app.use((_req, res, next) => {
  res.setHeader("X-Permitted-Cross-Domain-Policies", "none");
  res.setHeader("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()");
  next();
});

const apiLimiter = rateLimit({
  windowMs: 60 * 1000,
  max: 2000,
  standardHeaders: true,
  legacyHeaders: false,
  message: { message: "Too many requests, please try again later" },
});


app.use("/api/", apiLimiter);

app.use("/api/", (_req, res, next) => {
  res.setHeader("Cache-Control", "no-store, no-cache, must-revalidate, proxy-revalidate");
  res.setHeader("Pragma", "no-cache");
  res.setHeader("Expires", "0");
  next();
});

// Kubernetes patch bodies (merge / JSON patch) are JSON too; the /api/k8s proxy forwards their type.
app.use(
  express.json({
    limit: "16kb",
    type: [
      "application/json",
      "application/merge-patch+json",
      "application/json-patch+json",
      "application/strategic-merge-patch+json",
    ],
  }),
);

/**
 * Every /api route runs on the caller's own token (X-Forwarded-Access-Token from
 * the oauth-proxy, or the bearer token of the console's plugin proxy) and only
 * for platform administrators: the oauth-proxy enforces --openshift-sar per
 * session, requirePlatform re-checks per request. Nothing here uses the pod's
 * service-account token.
 */
function requireAuth(req, res, next) {
  const token = userToken(req);
  if (!token) return sendStatus(res, 401, "Your session ended. Sign in again.");
  req.user = {
    username: req.headers["x-forwarded-user"] || "unknown",
    email: req.headers["x-forwarded-email"] || "",
  };
  next();
}

const requirePlatform = createRequirePlatform({ apiServer: OCP_API });

app.get("/healthz", (_req, res) => {
  res.removeHeader("X-Frame-Options");
  res.json({ status: "ok" });
});

app.use("/api/", requireAuth, requirePlatform);

app.get("/api/user", (req, res) => {
  res.json({
    username: req.user.username,
    name: req.user.username,
    email: req.user.email,
  });
});

// Transparent K8s API proxy used by PatternFly UI shared hooks (/api/k8s/apis/...)
app.use("/api/k8s", createApiK8sProxy(OCP_API));

function urlProbe(urlString, timeoutMs = 5000, expectedStatus = 200) {
  return new Promise((resolve) => {
    let parsed;
    try { parsed = new URL(urlString); } catch {
      return resolve({ url: urlString, status: 0, healthy: false, error: "invalid url" });
    }
    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
      return resolve({ url: urlString, status: 0, healthy: false, error: "unsupported protocol" });
    }
    const isHttps = parsed.protocol === "https:";
    const opts = {
      hostname: parsed.hostname,
      port: parsed.port || (isHttps ? 443 : 80),
      path: parsed.pathname + parsed.search,
      method: "GET",
      rejectUnauthorized: false,
      timeout: timeoutMs,
      headers: { "User-Agent": "sovereign-uihealth-probe/1.0", Accept: "*/*" },
    };
    const checkedAt = new Date().toISOString();
    const transport = isHttps ? https : http;
    const req = transport.request(opts, (incoming) => {
      const status = incoming.statusCode ?? 0;
      incoming.resume();
      const healthy =
        status === expectedStatus ||
        (expectedStatus === 200 && status >= 200 && status < 400);
      resolve({ url: urlString, status, healthy, expectedStatus, checkedAt });
    });
    req.on("timeout", () => {
      req.destroy();
      resolve({ url: urlString, status: 0, healthy: false, error: "timeout", expectedStatus, checkedAt });
    });
    req.on("error", (err) =>
      resolve({
        url: urlString,
        status: 0,
        healthy: false,
        error: err?.message || "unreachable",
        expectedStatus,
        checkedAt,
      }),
    );
    req.end();
  });
}

/** Probe UIHealthChecker targets from the dashboard pod (no browser CORS). */
app.post("/api/uihealth/probe", async (req, res) => {
  const targets = req.body?.targets;
  if (!Array.isArray(targets) || targets.length === 0) {
    return res.status(400).json({ message: "targets array is required" });
  }
  const capped = targets.slice(0, 100);
  const results = await Promise.all(
    capped.map(async (t) => {
      const name = typeof t?.name === "string" ? t.name : "";
      const url = typeof t?.url === "string" ? t.url.trim() : "";
      const timeoutSeconds = Math.min(120, Math.max(1, Number(t?.timeoutSeconds) || 10));
      const expectedStatus = Number(t?.expectedStatus) || 200;
      if (!name || !url) {
        return {
          name,
          url,
          status: 0,
          healthy: false,
          error: "missing name or url",
          checkedAt: new Date().toISOString(),
        };
      }
      const result = await urlProbe(url, timeoutSeconds * 1000, expectedStatus);
      return { name, ...result };
    }),
  );
  res.json({ results });
});

// Unknown API paths answer 404 JSON instead of falling through to the SPA.
app.all("/api/{*rest}", (_req, res) => sendStatus(res, 404, "Not found"));

const distPath = path.resolve(__dirname, "../dist");
app.use(express.static(distPath, {
  maxAge: "1h",
  setHeaders(res, filePath) {
    if (filePath.endsWith(".html")) {
      res.setHeader("Cache-Control", "no-cache, no-store, must-revalidate");
    }
  },
}));
app.get("/{*path}", (_req, res) => {
  res.setHeader("Cache-Control", "no-cache, no-store, must-revalidate");
  res.sendFile(path.join(distPath, "index.html"));
});

app.use((err, _req, res, _next) => {
  console.error("Unhandled error:", err.message);
  res.status(500).json({ message: "Internal server error" });
});

app.listen(PORT, "0.0.0.0", () => {
  console.log(`Sovereign Cloud Dashboard listening on :${PORT}`);
});
