/**
 * One error type for every request the shared hooks make (C4).
 *
 * The Kubernetes API (and the dashboard servers, which pass its Status body
 * through) answer a failure with a `Status` object: `{ message, reason, code,
 * details: { causes: [{ field, message, reason }] } }`. `K8sApiError` keeps those
 * parts so callers branch on `status` / `reason` instead of matching message
 * text, and forms map `causes[].field` to their inputs.
 *
 * The message keeps the historic `K8s API error <status>: <detail>` prefix for
 * one release so older callers that still match it keep working.
 */
import type { HybridSovereignKind } from '../types';

export interface K8sStatusCause {
  field?: string;
  message: string;
  reason?: string;
}

export interface K8sApiErrorContext {
  kind?: HybridSovereignKind | string;
  verb?: string;
  namespace?: string;
  name?: string;
}

export class K8sApiError extends Error {
  /** HTTP status; 0 when the request never got an answer (network, CORS, abort). */
  readonly status: number;
  /** Kubernetes Status reason (Forbidden, NotFound, Conflict, Invalid, AlreadyExists, ...). */
  readonly reason?: string;
  readonly causes?: K8sStatusCause[];
  /** Kubernetes `Warning` headers that came with the answer. */
  readonly warnings?: string[];
  /** Short server-side reference for 5xx answers from the dashboard servers. */
  readonly reference?: string;
  readonly kind?: string;
  readonly verb?: string;
  readonly namespace?: string;
  readonly objectName?: string;
  /** Message as the API sent it, without the compatibility prefix. */
  readonly detail: string;

  constructor(
    status: number,
    detail: string,
    opts: {
      reason?: string;
      causes?: K8sStatusCause[];
      warnings?: string[];
      reference?: string;
    } & K8sApiErrorContext = {},
  ) {
    super(status ? `K8s API error ${status}: ${detail}` : `Network error: ${detail}`);
    this.name = 'K8sApiError';
    this.status = status;
    this.detail = detail;
    this.reason = opts.reason;
    this.causes = opts.causes;
    this.warnings = opts.warnings;
    this.reference = opts.reference;
    this.kind = opts.kind;
    this.verb = opts.verb;
    this.namespace = opts.namespace;
    this.objectName = opts.name;
  }
}

export function isK8sApiError(err: unknown): err is K8sApiError {
  return err instanceof K8sApiError || (!!err && (err as { name?: string }).name === 'K8sApiError');
}

/**
 * HTTP status of any error the UI may hold. Errors thrown before this module
 * existed carry the status only in their message; that form is still read.
 */
export function statusOf(err: unknown): number | undefined {
  if (!err) return undefined;
  if (isK8sApiError(err)) return err.status;
  const msg = (err as { message?: unknown }).message;
  if (typeof msg === 'string') {
    const m = /K8s API error (\d{3})\b/.exec(msg);
    if (m) return Number(m[1]);
  }
  return undefined;
}

interface StatusBody {
  kind?: string;
  message?: string;
  error?: string;
  reason?: string;
  code?: number;
  reference?: string;
  details?: { causes?: { field?: string; message?: string; reason?: string }[] };
}

/** Parse a failed response's body (Kubernetes Status or a dashboard `{error}` body). */
export function parseStatusBody(
  status: number,
  statusText: string,
  bodyText: string,
  ctx: K8sApiErrorContext = {},
  warnings?: string[],
): K8sApiError {
  let body: StatusBody | undefined;
  try {
    body = bodyText ? (JSON.parse(bodyText) as StatusBody) : undefined;
  } catch {
    body = undefined;
  }
  const detail =
    (body && typeof body.message === 'string' && body.message) ||
    (body && typeof body.error === 'string' && body.error) ||
    statusText ||
    `HTTP ${status}`;
  const causes = body?.details?.causes
    ?.filter((c) => c && typeof c.message === 'string')
    .map((c) => ({ field: c.field, message: c.message as string, reason: c.reason }));
  return new K8sApiError(status, detail, {
    reason: typeof body?.reason === 'string' ? body.reason : undefined,
    causes: causes && causes.length ? causes : undefined,
    reference: typeof body?.reference === 'string' ? body.reference : undefined,
    warnings,
    ...ctx,
  });
}

/** Kubernetes `Warning` response headers ("299 - \"text\"") as plain texts. */
export function parseWarningHeaders(headers: Headers | undefined | null): string[] | undefined {
  const raw = headers?.get?.('warning');
  if (!raw) return undefined;
  const out: string[] = [];
  const re = /\d{3}\s+\S+\s+"((?:[^"\\]|\\.)*)"/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(raw))) out.push(m[1].replace(/\\"/g, '"'));
  return out.length ? out : [raw];
}

/** Build the error for a failed `fetch` Response. */
export async function errorFromResponse(
  response: Response,
  ctx: K8sApiErrorContext = {},
): Promise<K8sApiError> {
  let text = '';
  try {
    text = await response.text();
  } catch {
    text = '';
  }
  return parseStatusBody(response.status, response.statusText, text, ctx, parseWarningHeaders(response.headers));
}

/** Wrap a transport failure (fetch rejected without an answer) so it carries status 0; see errorFromFetchFailure. */
export function networkError(err: unknown, ctx: K8sApiErrorContext = {}): K8sApiError {
  if (isK8sApiError(err)) return err;
  const detail = err instanceof Error ? err.message : String(err);
  return new K8sApiError(0, detail, { reason: 'NetworkError', ...ctx });
}

/**
 * An HTTP answer that the fetch function threw instead of returning: the
 * OpenShift console's consoleFetch rejects every non-2xx answer with an
 * HttpError `{ code, message, json, response }`.
 */
interface ThrownHttpAnswer {
  code: number;
  message?: string;
  json?: unknown;
  response?: { headers?: Headers };
}

function isThrownHttpAnswer(err: unknown): err is ThrownHttpAnswer {
  const code = (err as { code?: unknown } | null)?.code;
  // DOMException codes (AbortError 20, TimeoutError 23) and Node's string codes are transport failures.
  return typeof code === 'number' && Number.isInteger(code) && code >= 100 && code <= 599;
}

/**
 * The error for a rejected fetch. An HTTP answer the fetch function threw
 * (consoleFetch HttpError) keeps its status and Status body (reason, causes);
 * only a request that never got an answer (network, abort, timeout) gets status 0.
 */
export function errorFromFetchFailure(err: unknown, ctx: K8sApiErrorContext = {}): K8sApiError {
  if (isK8sApiError(err)) return err;
  if (isThrownHttpAnswer(err)) {
    let bodyText = '';
    if (err.json !== undefined && err.json !== null) {
      try {
        bodyText = typeof err.json === 'string' ? err.json : JSON.stringify(err.json);
      } catch {
        bodyText = '';
      }
    }
    let warnings: string[] | undefined;
    try {
      warnings = parseWarningHeaders(err.response?.headers);
    } catch {
      warnings = undefined;
    }
    return parseStatusBody(err.code, typeof err.message === 'string' ? err.message : '', bodyText, ctx, warnings);
  }
  return networkError(err, ctx);
}

export type ErrorClass =
  | 'unauthenticated'
  | 'forbidden'
  | 'notFound'
  | 'conflict'
  | 'invalid'
  | 'unavailable'
  | 'server';

/** The §6 error classes the UI renders differently. */
export function classifyError(err: unknown): ErrorClass {
  const status = statusOf(err);
  if (status === undefined) {
    // A rejected fetch without our wrapper (TypeError: Failed to fetch).
    return err instanceof TypeError ? 'unavailable' : 'server';
  }
  if (status === 0) return 'unavailable';
  if (status === 401) return 'unauthenticated';
  if (status === 403) return 'forbidden';
  if (status === 404 || status === 410) return 'notFound';
  if (status === 409) {
    const reason = isK8sApiError(err) ? err.reason : undefined;
    return reason === 'AlreadyExists' ? 'invalid' : 'conflict';
  }
  if (status === 400 || status === 422) return 'invalid';
  if (status === 502 || status === 503 || status === 504) return 'unavailable';
  return 'server';
}

export function isForbiddenError(err: unknown): boolean {
  return classifyError(err) === 'forbidden';
}
