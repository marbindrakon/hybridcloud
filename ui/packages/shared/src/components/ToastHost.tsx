import React, { useEffect, useRef, useSyncExternalStore } from 'react';
import { Alert, AlertActionCloseButton, AlertGroup } from '@patternfly/react-core';

export type ToastVariant = 'success' | 'danger' | 'warning' | 'info' | 'custom';

export interface Toast {
  id: number;
  variant: ToastVariant;
  title: string;
  body?: string;
}

const TOAST_TIMEOUT_MS = 8000;
const MAX_TOASTS = 4;

let toasts: Toast[] = [];
let nextId = 1;
const listeners = new Set<() => void>();
function emit(): void {
  listeners.forEach((l) => l());
}

/** Show a toast in the page's toast area (no-op text when no host is mounted). */
export function pushToast(t: Omit<Toast, 'id'>): number {
  const id = nextId++;
  toasts = [...toasts, { ...t, id }].slice(-MAX_TOASTS);
  emit();
  return id;
}

export function dismissToast(id: number): void {
  toasts = toasts.filter((t) => t.id !== id);
  emit();
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

// Several components may mount a host (shells, detail pages, lists); only the first one renders.
const hosts: number[] = [];
let nextHost = 1;
const hostListeners = new Set<() => void>();
function hostSnapshot(): number {
  return hosts[0] ?? 0;
}

/**
 * The toast area (PatternFly AlertGroup, a live region). Each shell mounts it
 * once; ResourceDetail and ResourceListTable mount one too, and a singleton
 * guard makes only the first mounted host render, so toasts never appear twice.
 */
export function ToastHost(): React.ReactElement | null {
  const items = useSyncExternalStore(subscribe, () => toasts, () => toasts);
  const idRef = useRef(0);
  if (!idRef.current) idRef.current = nextHost++;
  useEffect(() => {
    hosts.push(idRef.current);
    hostListeners.forEach((l) => l());
    return () => {
      const i = hosts.indexOf(idRef.current);
      if (i >= 0) hosts.splice(i, 1);
      hostListeners.forEach((l) => l());
    };
  }, []);
  const first = useSyncExternalStore(
    (fn) => {
      hostListeners.add(fn);
      return () => hostListeners.delete(fn);
    },
    hostSnapshot,
    hostSnapshot,
  );
  if (first !== idRef.current) return null;
  return (
    <AlertGroup isToast isLiveRegion>
      {items.map((t) => (
        <Alert
          key={t.id}
          variant={t.variant}
          title={t.title}
          timeout={TOAST_TIMEOUT_MS}
          onTimeout={() => dismissToast(t.id)}
          actionClose={<AlertActionCloseButton onClose={() => dismissToast(t.id)} />}
        >
          {t.body}
        </Alert>
      ))}
    </AlertGroup>
  );
}
