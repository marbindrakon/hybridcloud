/** Completion events of a polled list (pure, tested with node:test). */
import { isInFlight, phaseOf, summaryOf, type Phase, type PhaseSubject } from './phase';

export interface Seen {
  phase: Phase;
  name: string;
  kind: string;
}

export type Watched = PhaseSubject & { metadata?: { uid?: string; namespace?: string; name?: string } };

function keyOf(item: Watched): string {
  const m = item.metadata ?? {};
  return m.uid || `${m.namespace ?? ''}/${m.name ?? ''}`;
}

/**
 * The toasts a refreshed list causes: an object that left an in-flight phase
 * for Ready or Failed, and a Deleting object that is gone.
 */
export function completionEvents(
  before: Map<string, Seen>,
  items: Watched[],
  kind: string,
): { events: Array<{ type: 'ready' | 'failed' | 'deleted'; name: string; summary?: string }>; next: Map<string, Seen> } {
  const next = new Map<string, Seen>();
  const events: Array<{ type: 'ready' | 'failed' | 'deleted'; name: string; summary?: string }> = [];
  for (const item of items) {
    const key = keyOf(item);
    const phase = phaseOf(item);
    const name = item.metadata?.name ?? '';
    next.set(key, { phase, name, kind });
    const prev = before.get(key);
    if (!prev || !isInFlight(prev.phase) || isInFlight(phase)) continue;
    if (phase === 'Ready') events.push({ type: 'ready', name });
    else if (phase === 'Failed') events.push({ type: 'failed', name, summary: summaryOf(item) });
  }
  for (const [key, prev] of before) {
    if (!next.has(key) && prev.phase === 'Deleting') events.push({ type: 'deleted', name: prev.name });
  }
  return { events, next };
}
