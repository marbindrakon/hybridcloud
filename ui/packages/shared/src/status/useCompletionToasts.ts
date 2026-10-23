/**
 * Completion toasts for objects the user watches (Q10, UX-059): when a polled
 * object's phase leaves an in-flight phase, say so once ("… is ready",
 * "… failed: <summary>"); when a Deleting object disappears, "… deleted".
 * Only transitions seen on this page count: loading a list never toasts.
 */
import { useEffect, useRef } from 'react';
import { useTranslation } from '../i18n';
import { pushToast } from '../components/ToastHost';
import { completionEvents, type Seen, type Watched } from './completion';
import { kindTerm } from '../i18n/vocabulary';

export { completionEvents };

export function useCompletionToasts(
  items: Watched[] | null | undefined,
  kind: string,
  opts: { enabled?: boolean } = {},
): void {
  const { t } = useTranslation();
  const seen = useRef<Map<string, Seen> | null>(null);
  const enabled = opts.enabled ?? true;
  useEffect(() => {
    if (!enabled || !items) return;
    if (seen.current === null) {
      // First answer: remember, never toast.
      seen.current = completionEvents(new Map(), items, kind).next;
      return;
    }
    const { events, next } = completionEvents(seen.current, items, kind);
    seen.current = next;
    const label = kindTerm(t, kind);
    for (const e of events) {
      if (e.type === 'ready') pushToast({ variant: 'success', title: t('toasts.ready', { kind: label, name: e.name }) });
      else if (e.type === 'failed') pushToast({ variant: 'danger', title: t('toasts.failed', { kind: label, name: e.name }), body: e.summary });
      else pushToast({ variant: 'info', title: t('toasts.deleted', { kind: label, name: e.name }) });
    }
  }, [items, kind, enabled, t]);
}
