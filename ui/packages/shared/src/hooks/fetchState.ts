/**
 * State rules shared by the list and object hooks (pure, no React), so they can
 * be unit-tested with fake timers.
 *
 * - A new URL (another kind, namespace, selector or name) never shows the
 *   previous URL's data or error while it loads (UX-113).
 * - The first load of a URL that fails shows the error (full-page states).
 * - A refresh of a URL that already has data and fails for a transient reason
 *   (no answer, 5xx, timeout) keeps the last good data and reports
 *   `refreshError`, so polling never blanks a page that already loaded.
 * - 401, 403, 404 and 410 always replace the data: access was withdrawn or the
 *   object is gone, and the page must say so.
 */
import { classifyError } from './errors';

export interface FetchState<T> {
  /** The URL the data and errors below belong to. */
  key: string;
  data: T | undefined;
  /** The load failed and there is no data to show. */
  error: Error | null;
  /** A refresh failed; `data` is the last good answer. */
  refreshError: Error | null;
  /** Epoch ms of the last good answer. */
  loadedAt?: number;
}

export function initialFetchState<T>(key: string): FetchState<T> {
  return { key, data: undefined, error: null, refreshError: null };
}

/** A transient failure that should not blank data already on screen. */
export function isTransientError(err: unknown): boolean {
  const cls = classifyError(err);
  return cls === 'unavailable' || cls === 'server';
}

export type FetchOutcome<T> = { ok: true; data: T; at?: number } | { ok: false; error: Error };

/** The next state after an answer for `key`. Answers for an older key are ignored. */
export function applyFetchOutcome<T>(prev: FetchState<T>, key: string, outcome: FetchOutcome<T>): FetchState<T> {
  const base = prev.key === key ? prev : initialFetchState<T>(key);
  if (outcome.ok) {
    return { key, data: outcome.data, error: null, refreshError: null, loadedAt: outcome.at ?? Date.now() };
  }
  if (base.data !== undefined && isTransientError(outcome.error)) {
    return { ...base, error: null, refreshError: outcome.error };
  }
  return { key, data: undefined, error: outcome.error, refreshError: null };
}

/** Faster polling while something is in flight. */
export const IN_FLIGHT_POLL_MS = 5000;
/** Polling while nothing is in flight (only when the page asked for in-flight polling). */
export const IDLE_POLL_MS = 30000;

/**
 * Milliseconds until the next refresh, or 0 for none: an explicit interval
 * wins; otherwise in-flight polling uses 5 s while something is in flight and
 * 30 s otherwise. Hidden pages do not poll.
 */
export function pollDelayMs(opts: {
  pollIntervalMs?: number;
  pollWhileInFlight?: boolean;
  inFlight: boolean;
  hidden?: boolean;
}): number {
  if (opts.hidden) return 0;
  if (opts.pollIntervalMs && opts.pollIntervalMs > 0) return opts.pollIntervalMs;
  if (!opts.pollWhileInFlight) return 0;
  return opts.inFlight ? IN_FLIGHT_POLL_MS : IDLE_POLL_MS;
}
