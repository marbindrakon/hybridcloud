/**
 * Session end and installation settings for both dashboards.
 *
 *   POST /api/session/end  revokes the caller's own OpenShift access token
 *                          (useroauthaccesstokens/<sha256~ hash>, with that same
 *                          token; 404 tolerated) and answers the proxy sign-out
 *                          path. The oauth-proxy then clears its cookie and, when
 *                          --logout-url is set, sends the browser to the identity
 *                          provider's end-session page.
 *   GET  /api/ui-config    { helpBaseUrl, consoleUrl } from SHC_HELP_BASE_URL and
 *                          SHC_CONSOLE_URL (both empty by default: no installation
 *                          defaults in the image or chart).
 */
import crypto from "crypto";
import { k8sRequest, userToken } from "./upstream.js";
import { sendStatus } from "./errors.js";

/** Object name of an OpenShift access token: sha256~ + base64url(sha256(token body)). */
export function oauthTokenObjectName(token) {
  const body = token.startsWith("sha256~") ? token.slice("sha256~".length) : token;
  const digest = crypto.createHash("sha256").update(body).digest("base64url");
  return `sha256~${digest}`;
}

function cleanUrl(v) {
  if (typeof v !== "string") return "";
  const s = v.trim();
  return /^https:\/\/[^\s"'<>]+$/.test(s) ? s.replace(/\/+$/, "") : "";
}

export function uiConfigFromEnv(env = process.env) {
  return {
    helpBaseUrl: cleanUrl(env.SHC_HELP_BASE_URL),
    consoleUrl: cleanUrl(env.SHC_CONSOLE_URL),
  };
}

/**
 * @param {import('express').Express} app
 * @param {{apiServer?: string, requireAuth?: import('express').RequestHandler}} [opts]
 */
export function mountSessionRoutes(app, opts = {}) {
  const auth = opts.requireAuth || ((_req, _res, next) => next());
  const config = uiConfigFromEnv();

  app.get("/api/ui-config", auth, (_req, res) => res.json(config));

  app.post("/api/session/end", auth, async (req, res) => {
    const token = userToken(req);
    if (!token) return sendStatus(res, 401, "Your session ended. Sign in again.");
    if (token.startsWith("sha256~")) {
      try {
        const resp = await k8sRequest({
          apiServer: opts.apiServer,
          method: "DELETE",
          path: `/apis/oauth.openshift.io/v1/useroauthaccesstokens/${encodeURIComponent(oauthTokenObjectName(token))}`,
          token,
          timeoutMs: 10000,
        });
        if (resp.status >= 400 && resp.status !== 404 && resp.status !== 401) {
          console.error(`session end: token revocation answered ${resp.status}`);
        }
      } catch (err) {
        console.error(`session end: token revocation failed: ${err.message}`);
      }
    }
    return res.json({ redirect: "/oauth/sign_out" });
  });
}
