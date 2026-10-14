import express from "express";
import helmet from "helmet";
import rateLimit from "express-rate-limit";
import http from "http";
import https from "https";
import path from "path";
import { fileURLToPath } from "url";
import { createK8sClient } from "./k8s-proxy.js";
import { createApiK8sProxy } from "./api-k8s-proxy.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const app = express();
const PORT = process.env.PORT || 8080;

const OCP_API = process.env.OCP_SERVICES_SERVER || "https://kubernetes.default.svc";
const ENTITY_NAMESPACE = process.env.ENTITY_NAMESPACE || "sovereign-cloud";

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

const mutationLimiter = rateLimit({
  windowMs: 60 * 1000,
  max: 100,
  standardHeaders: true,
  legacyHeaders: false,
  message: { message: "Too many write requests, please try again later" },
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

function extractUser(req, _res, next) {
  const user = req.headers["x-forwarded-user"];
  const email = req.headers["x-forwarded-email"];
  const accessTokenRaw = req.headers["x-forwarded-access-token"];
  const accessToken = typeof accessTokenRaw === "string" && accessTokenRaw.trim().length
    ? accessTokenRaw.trim()
    : undefined;
  if (user || accessToken) {
    req.user = { username: user || "unknown", email: email || "", ...(accessToken ? { accessToken } : {}) };
  }
  next();
}

app.use(extractUser);

function requireAuth(req, res, next) {
  if (!req.user) {
    return res.status(401).json({ message: "Unauthorized" });
  }
  next();
}

app.get("/healthz", (_req, res) => {
  res.removeHeader("X-Frame-Options");
  res.json({ status: "ok" });
});

app.get("/api/user", requireAuth, (req, res) => {
  res.json({
    username: req.user.username,
    name: req.user.username,
    email: req.user.email,
  });
});

const k8s = createK8sClient(OCP_API, ENTITY_NAMESPACE);

app.get("/api/rbacconfigs", requireAuth, k8s.listRbacConfigs);

app.get("/api/entities", requireAuth, k8s.list);
app.post("/api/entities", requireAuth, mutationLimiter, k8s.create);
app.get("/api/entities/:name/rbacs", requireAuth, k8s.listEntityRbacs);
app.post("/api/entities/:name/rbacs", requireAuth, mutationLimiter, k8s.createEntityRbac);
app.get("/api/entities/:name/personas", requireAuth, k8s.listEntityPersonas);
app.post("/api/entities/:name/personas", requireAuth, mutationLimiter, k8s.createEntityPersona);
app.patch("/api/entities/:name/rbacs/:rbacName", requireAuth, mutationLimiter, k8s.updateEntityRbac);
app.get("/api/entities/:name", requireAuth, k8s.get);
app.delete("/api/entities/:name", requireAuth, mutationLimiter, k8s.remove);

app.get("/api/overview/crs", requireAuth, k8s.listAllCRs);
app.get("/api/operators", requireAuth, k8s.listOperators);

app.post("/api/k8s/patch-annotation", requireAuth, mutationLimiter, k8s.patchAnnotation);

// Transparent K8s API proxy used by PatternFly UI shared hooks (/api/k8s/apis/...)
app.use("/api/k8s", requireAuth, createApiK8sProxy(OCP_API));

app.get("/api/routes", requireAuth, k8s.listRoutes);

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

function httpsProbe(urlString, timeoutMs = 5000) {
  return urlProbe(urlString, timeoutMs, 200).then((r) => ({
    url: r.url,
    status: r.status,
    healthy: r.healthy,
  }));
}

const HEALTH_PATHS = ["/healthz", "/health", "/api/health"];

async function routeHealthCheck(baseUrl) {
  for (const hp of HEALTH_PATHS) {
    const result = await httpsProbe(`${baseUrl}${hp}`, 3000);
    if (result.healthy) return { ...result, url: baseUrl, checkedPath: hp };
  }
  const result = await httpsProbe(baseUrl, 5000);
  return { ...result, checkedPath: "/" };
}

app.get("/api/routes/healthcheck", requireAuth, async (req, res) => {
  const urlParam = req.query.url;
  if (!urlParam || typeof urlParam !== "string") {
    return res.status(400).json({ message: "url query parameter is required" });
  }
  const result = await routeHealthCheck(urlParam.trim());
  res.json(result);
});

app.post("/api/routes/healthcheck/batch", requireAuth, express.json(), async (req, res) => {
  const urls = req.body?.urls;
  if (!Array.isArray(urls) || urls.length === 0) {
    return res.status(400).json({ message: "urls array is required" });
  }
  const capped = urls.slice(0, 50);
  const results = await Promise.all(capped.map((u) => routeHealthCheck(String(u).trim())));
  res.json(results);
});

/** Probe UIHealthChecker targets from the dashboard pod (no browser CORS). */
app.post("/api/uihealth/probe", requireAuth, express.json(), async (req, res) => {
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
