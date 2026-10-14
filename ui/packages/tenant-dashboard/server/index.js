import express from "express";
import helmet from "helmet";
import rateLimit from "express-rate-limit";
import path from "path";
import { fileURLToPath } from "url";
import { createK8sHandlers } from "./k8s-proxy.js";
import { createApiK8sProxy } from "./api-k8s-proxy.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const app = express();
const PORT = process.env.PORT || 8080;

const OCP_API = process.env.OCP_SERVICES_SERVER || "https://kubernetes.default.svc";

app.set("trust proxy", 1);

app.use(
  helmet({
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
  }),
);

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
  max: 30,
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

app.use(express.json({ limit: "256kb" }));

function requireAuth(req, res, next) {
  const raw = req.headers["x-forwarded-access-token"];
  const token = typeof raw === "string" ? raw.trim() : "";
  if (!token) return res.status(401).json({ message: "Unauthorized" });
  req.user = {
    username: req.headers["x-forwarded-user"] || "unknown",
    email: req.headers["x-forwarded-email"] || "",
  };
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
    hasAccessToken: !!req.headers["x-forwarded-access-token"],
  });
});

const k8s = createK8sHandlers(OCP_API);

// Namespaces & configs
app.get("/api/entities", requireAuth, k8s.listEntities);
app.get("/api/namespaces", requireAuth, k8s.listNamespaces);
app.get("/api/permissions", requireAuth, k8s.getPermissions);
app.get("/api/rbacconfigs", requireAuth, k8s.listRbacConfigs);
app.get("/api/aapconfigs", requireAuth, k8s.listAAPConfigs);
app.get("/api/quayconfigs", requireAuth, k8s.listQuayConfigs);

// Rbac CRUD
app.get("/api/rbacs", requireAuth, k8s.listRbacs);
app.get("/api/rbacs/search", requireAuth, k8s.searchRbacs);
app.get("/api/rbacs/:name", requireAuth, k8s.get);
app.post("/api/rbacs", requireAuth, mutationLimiter, k8s.create);
app.put("/api/rbacs/:name", requireAuth, mutationLimiter, k8s.updateRbac);
app.delete("/api/rbacs/:name", requireAuth, mutationLimiter, k8s.remove);

// Vault CRUD
app.get("/api/vaults", requireAuth, k8s.listVaults);
app.get("/api/vaults/:name", requireAuth, k8s.getVault);
app.post("/api/vaults", requireAuth, mutationLimiter, k8s.createVault);
app.delete("/api/vaults/:name", requireAuth, mutationLimiter, k8s.deleteVault);

// VaultKV CRUD
app.get("/api/vaultkvs", requireAuth, k8s.listVaultKVs);
app.get("/api/vaultkvs/:name", requireAuth, k8s.getVaultKV);
app.post("/api/vaultkvs", requireAuth, mutationLimiter, k8s.createVaultKV);
app.put("/api/vaultkvs/:name", requireAuth, mutationLimiter, k8s.updateVaultKV);
app.delete("/api/vaultkvs/:name", requireAuth, mutationLimiter, k8s.deleteVaultKV);

// AAPOrg CRUD
app.get("/api/aaporgs", requireAuth, k8s.listAAPOrgs);
app.get("/api/aaporgs/:name", requireAuth, k8s.getAAPOrg);
app.post("/api/aaporgs", requireAuth, mutationLimiter, k8s.createAAPOrg);
app.put("/api/aaporgs/:name", requireAuth, mutationLimiter, k8s.updateAAPOrg);
app.delete("/api/aaporgs/:name", requireAuth, mutationLimiter, k8s.deleteAAPOrg);

// QuayOrg CRUD
app.get("/api/quayorgs", requireAuth, k8s.listQuayOrgs);
app.get("/api/quayorgs/:name", requireAuth, k8s.getQuayOrg);
app.post("/api/quayorgs", requireAuth, mutationLimiter, k8s.createQuayOrg);
app.put("/api/quayorgs/:name", requireAuth, mutationLimiter, k8s.updateQuayOrg);
app.delete("/api/quayorgs/:name", requireAuth, mutationLimiter, k8s.deleteQuayOrg);

// Team CRUD
app.get("/api/teams", requireAuth, k8s.listTeams);
app.get("/api/teams/:name", requireAuth, k8s.getTeam);
app.post("/api/teams", requireAuth, mutationLimiter, k8s.createTeam);
app.put("/api/teams/:name", requireAuth, mutationLimiter, k8s.updateTeam);
app.delete("/api/teams/:name", requireAuth, mutationLimiter, k8s.deleteTeam);

// Project CRUD
app.get("/api/projects", requireAuth, k8s.listProjects);
app.post("/api/projects", requireAuth, mutationLimiter, k8s.createProject);
app.delete("/api/projects/:name", requireAuth, mutationLimiter, k8s.deleteProject);

// Assignment CRUD
app.get("/api/assignments", requireAuth, k8s.listAssignments);
app.get("/api/assignments/:name", requireAuth, k8s.getAssignment);
app.post("/api/assignments", requireAuth, mutationLimiter, k8s.createAssignment);
app.put("/api/assignments/:name", requireAuth, mutationLimiter, k8s.updateAssignment);
app.delete("/api/assignments/:name", requireAuth, mutationLimiter, k8s.deleteAssignment);

// PlatformOpenshift CRUD
app.get("/api/platformopenshifts", requireAuth, k8s.listPlatformOpenshifts);
app.get("/api/platformopenshifts/:name", requireAuth, k8s.getPlatformOpenshift);
app.post("/api/platformopenshifts", requireAuth, mutationLimiter, k8s.createPlatformOpenshift);
app.patch("/api/platformopenshifts/:name", requireAuth, mutationLimiter, k8s.patchPlatformOpenshift);
app.delete("/api/platformopenshifts/:name", requireAuth, mutationLimiter, k8s.deletePlatformOpenshift);

// CloudOSO CRUD
app.get("/api/cloudosos", requireAuth, k8s.listCloudOSOs);
app.get("/api/cloudosos/:name", requireAuth, k8s.getCloudOSO);
app.post("/api/cloudosos", requireAuth, mutationLimiter, k8s.createCloudOSO);
app.patch("/api/cloudosos/:name", requireAuth, mutationLimiter, k8s.patchCloudOSO);
app.delete("/api/cloudosos/:name", requireAuth, mutationLimiter, k8s.deleteCloudOSO);

// MTV catalog + OpenStackMigration CRUD
app.get("/api/mtv-catalog", requireAuth, k8s.getMtvCatalog);
app.get("/api/openstackmigrations", requireAuth, k8s.listOpenStackMigrations);
app.get("/api/openstackmigrations/:name", requireAuth, k8s.getOpenStackMigration);
app.post("/api/openstackmigrations", requireAuth, mutationLimiter, k8s.createOpenStackMigration);
app.delete("/api/openstackmigrations/:name", requireAuth, mutationLimiter, k8s.deleteOpenStackMigration);

// CloudAWS CRUD
app.get("/api/cloudawss", requireAuth, k8s.listCloudAWSs);
app.get("/api/cloudawss/:name", requireAuth, k8s.getCloudAWS);
app.post("/api/cloudawss", requireAuth, mutationLimiter, k8s.createCloudAWS);
app.patch("/api/cloudawss/:name", requireAuth, mutationLimiter, k8s.patchCloudAWS);
app.delete("/api/cloudawss/:name", requireAuth, mutationLimiter, k8s.deleteCloudAWS);

// CloudVirt CRUD
app.get("/api/cloudvirts", requireAuth, k8s.listCloudVirts);
app.get("/api/cloudvirts/:name", requireAuth, k8s.getCloudVirt);
app.post("/api/cloudvirts", requireAuth, mutationLimiter, k8s.createCloudVirt);
app.patch("/api/cloudvirts/:name", requireAuth, mutationLimiter, k8s.patchCloudVirt);
app.delete("/api/cloudvirts/:name", requireAuth, mutationLimiter, k8s.deleteCloudVirt);

// Project CRUD
app.get("/api/projects/:name", requireAuth, k8s.getProject);
app.patch("/api/projects/:name", requireAuth, mutationLimiter, k8s.patchProject);

// Persona CRUD
app.get("/api/personas", requireAuth, k8s.listPersonas);
app.get("/api/personas/:name", requireAuth, k8s.getPersona);
app.post("/api/personas", requireAuth, mutationLimiter, k8s.createPersona);
app.patch("/api/personas/:name/annotations", requireAuth, mutationLimiter, k8s.patchPersonaAnnotation);

// Generic CR annotation patch (reconcile trigger)
app.post("/api/k8s/patch-annotation", requireAuth, mutationLimiter, k8s.patchAnnotation);

// Transparent K8s API proxy used by PatternFly UI shared hooks (/api/k8s/apis/...)
app.use("/api/k8s", requireAuth, createApiK8sProxy(OCP_API));

const distPath = path.resolve(__dirname, "../dist");
app.use(
  express.static(distPath, {
    maxAge: "1h",
    setHeaders(res, filePath) {
      if (filePath.endsWith(".html")) {
        res.setHeader("Cache-Control", "no-cache, no-store, must-revalidate");
      }
    },
  }),
);
app.get("/{*path}", (_req, res) => {
  res.setHeader("Cache-Control", "no-cache, no-store, must-revalidate");
  res.sendFile(path.join(distPath, "index.html"));
});

app.use((err, _req, res, _next) => {
  console.error("Unhandled error:", err.message);
  res.status(500).json({ message: "Internal server error" });
});

app.listen(PORT, "0.0.0.0", () => {
  console.log(`Tenancy Dashboard listening on :${PORT}, K8s API: ${OCP_API}`);
});
