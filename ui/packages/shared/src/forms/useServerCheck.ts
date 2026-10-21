/**
 * Server dry run of the exact object a form would create (Q8, UX-040, UX-090).
 * Whenever the client-side form is valid, the object is sent with dryRun=All
 * (debounced); Create is offered only when the latest check of the current
 * object passed. The API's warnings and refusals come back with their field
 * paths.
 */
import { useEffect, useMemo, useState } from 'react';
import { createDashboardResource, type WriteOptions } from '../hooks/k8s';
import type { HybridSovereignKind } from '../types';

export const SERVER_CHECK_DELAY_MS = 600;

export interface ServerCheckState {
  /** A check of the current object is running or waiting for the debounce. */
  checking: boolean;
  /** The current object passed the server's checks. */
  passed: boolean;
  /** The server refused the current object. */
  error: unknown;
  /** Warnings the server returned for the current object. */
  warnings: string[];
}

/**
 * The general form: `run` sends the object with the given write options; the
 * check re-runs whenever `key` changes (null = nothing to check).
 */
export function useServerCheckWith(
  key: string | null,
  run: (opts: WriteOptions) => Promise<unknown>,
): ServerCheckState {
  const [state, setState] = useState<{ key: string | null; passed: boolean; error: unknown; warnings: string[] }>({
    key: null,
    passed: false,
    error: null,
    warnings: [],
  });
  const [pending, setPending] = useState(false);

  useEffect(() => {
    if (!key) {
      setPending(false);
      return;
    }
    let cancelled = false;
    setPending(true);
    const id = setTimeout(() => {
      let warnings: string[] = [];
      run({ dryRun: true, onWarnings: (w) => (warnings = w) })
        .then(() => {
          if (!cancelled) setState({ key, passed: true, error: null, warnings });
        })
        .catch((err) => {
          if (!cancelled) setState({ key, passed: false, error: err, warnings: [] });
        })
        .finally(() => {
          if (!cancelled) setPending(false);
        });
    }, SERVER_CHECK_DELAY_MS);
    return () => {
      cancelled = true;
      clearTimeout(id);
    };
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps

  const current = state.key === key;
  return {
    checking: !!key && (pending || !current),
    passed: !!key && current && state.passed,
    error: current ? state.error : null,
    warnings: current ? state.warnings : [],
  };
}

/** Dry run of a create. */
export function useServerCheck(
  kind: HybridSovereignKind,
  body: { name: string; namespace: string; spec: unknown } | null,
  enabled = true,
): ServerCheckState {
  const key = useMemo(() => (body && enabled ? `${kind}:${JSON.stringify(body)}` : null), [kind, body, enabled]);
  return useServerCheckWith(key, (opts) =>
    body ? createDashboardResource(kind, body, body.namespace, opts) : Promise.resolve(undefined),
  );
}
